#!/usr/bin/env python3
"""
Digitail Advocate Map — Weekly Update Script

A clinic appears ONLY if it has at least one STRONG signal:
  - Opted into HubSpot referral network (fetched by exact list ID)
  - Left a public review (Capterra, G2, Google, etc.)
  - Gave a 5★ Intercom CSAT rating
  - Positive CS call in Fathom
  - Positive team mention in Slack
  - Manually verified by the team

Passive signals (tenure, DSP usage, generic notes) are shown
in popups as context but never qualify a clinic alone.
"""

import json, os, re, time
from datetime import datetime, timedelta, timezone
import requests

try:
    from bs4 import BeautifulSoup
    BS4 = True
except ImportError:
    BS4 = False

# ── Credentials ───────────────────────────────────────────────────────────────
HS_TOKEN        = os.environ.get("HUBSPOT_TOKEN", "")
IC_TOKEN        = os.environ.get("INTERCOM_TOKEN", "")
SLACK_URL       = os.environ.get("SLACK_WEBHOOK", "")
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
FATHOM_API_KEY  = os.environ.get("FATHOM_API_KEY", "")
FATHOM_CS_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("FATHOM_CS_EMAILS", "").split(",")
    if e.strip()
}
REDDIT_ID       = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_SECRET   = os.environ.get("REDDIT_CLIENT_SECRET", "")
GOOGLE_KEY      = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID   = os.environ.get("GOOGLE_CSE_ID", "")
GOOGLE_PLACE    = os.environ.get("GOOGLE_PLACE_ID", "")
FB_TOKEN        = os.environ.get("FACEBOOK_PAGE_TOKEN", "")
DATA_FILE        = "advocates.json"
HS_ACCOUNT_ID    = "4912130"   # HubSpot portal ID — used to build direct record links
# Note: referral network is fetched by contact property search, not list ID.
# The HubSpot list view at /contacts/4912130/objects/0-1/views/59320738/list
# is for human reference only — the property "reference_program_optin = true"
# is the authoritative source used by the script.

# Static public URLs for review platform signals — no per-record lookup needed
SIGNAL_STATIC_URLS = {
    "capterra_positive": "https://www.capterra.com/p/167764/Digitail/#reviews",
    "g2_positive":       "https://www.g2.com/products/digitail/reviews",
    "softwareadvice":    "https://www.softwareadvice.com/veterinary/digitail-profile/reviews/",
    "getapp":            "https://www.getapp.com/veterinary-practice-management-software/a/digitail/reviews/",
}

# Onboarding pipeline exclusion — clinics with an ACTIVE onboarding deal that
# has NOT yet reached the CS stage are excluded from the map (still being set up).
# The script auto-detects these by name; update if your HubSpot pipeline is named
# differently: HubSpot → Settings → Pipelines → Deals
ONBOARDING_PIPELINE_KEYWORD = "onboard"   # case-insensitive substring match on pipeline label
ONBOARDING_CS_STAGE_KEYWORD  = "cs"       # case-insensitive substring match on stage label

# Sales pipeline — customerSince date is sourced ONLY from closed-won deals here.
# CS, upsell, and expansion pipeline deals are intentionally excluded.
# Update if your pipeline label doesn't contain "sales".
SALES_PIPELINE_KEYWORD = "sales"

# ── Slack channel scanning config ─────────────────────────────────────────────
# The script scans ALL public channels the Slack bot is a member of.
# Channels are sorted so high-priority ones are scanned first.
#
# NEGATIVE priority — scanned first; finding a match here removes an advocate:
SLACK_NEGATIVE_PRIORITY = [
    "churn", "escalat", "support", "cancel", "at-risk", "risk",
]
# POSITIVE priority — scanned next; winning mentions land here:
SLACK_POSITIVE_PRIORITY = [
    "general", "shout", "win", "celebrat", "kudos", "success", "announcement",
]
# All other channels the bot is a member of are scanned last.
# Lookback window in days — applies to all channels:
SLACK_LOOKBACK_DAYS = 90

# ── Signal definitions ────────────────────────────────────────────────────────
STRONG_SIGNALS = {
    "hs_referral_network",
    "intercom_csat",
    "capterra_positive",
    "g2_positive",
    "softwareadvice",
    "getapp",
    "google_review",
    "facebook_review",
    "fathom_call",
    "slack_mention",
    "manual",
}

CONTEXT_ONLY_SIGNALS = {
    "hs_testimonial",
    "hs_dsp",
    "hs_long_tenure",
    "hs_positive_note",
    "google_mention",
    "reddit_mention",
}

SIGNAL_LABELS = {
    "hs_referral_network": "Part of Reference Program",
    "hs_testimonial":      "Case Study / Article on File",
    "hs_dsp":              "DSP Payment Processing",
    "hs_positive_note":    "Positive Team Note",
    "hs_long_tenure":      "Active Customer 12+ Months",
    "intercom_csat":       "5★ Intercom Support Rating",
    "capterra_positive":   "Capterra Review",
    "g2_positive":         "G2 Review",
    "softwareadvice":      "Software Advice Review",
    "getapp":              "GetApp Review",
    "google_review":       "Google Review",
    "google_mention":      "Web Mention",
    "facebook_review":     "Facebook Review",
    "slack_mention":       "Positive Slack Mention",
    "fathom_call":         "Positive Customer Call (Fathom)",
    "manual":              "Manually Verified",
}

NEGATIVE_KW = [
    "not happy", "unhappy", "at risk", "churn", "cancel", "triple check",
    "do not contact", "rocky", "leaving", "switching away", "terrible",
    "awful", "disappointed", "frustrated", "refund", "dispute",
]
POSITIVE_KW = [
    "great", "love", "loves", "happy", "excellent", "recommend", "advocate",
    "good experience", "amazing", "fantastic", "worth it", "switched", "best",
]

# ── Practice format ───────────────────────────────────────────────────────────
MOBILE_TERMS = [
    "mobile", "house call", "housecall", "traveling", "on wheels", "doorstep",
    "home visit", "home vet", "at home", "at-home", "on-site", "onsite",
    "wagon", "roaming", "wandervet", "paws on the move", "fetch the vet",
    "clinic nomad", "rolling",
]
TELE_TERMS = ["tele", "virtual", "online", "remote"]

def infer_format(name: str) -> str:
    n = name.lower()
    if any(t in n for t in TELE_TERMS):   return "telemedicine"
    if any(t in n for t in MOBILE_TERMS): return "mobile"
    return "bnm"

# ── Non-clinic exclusions ─────────────────────────────────────────────────────
EXCLUDE_NAME_FRAGMENTS = [
    "stripe", "care credit", "carecredit", "vetsource", "ellie diagnostics",
    "ma department of higher education", "hillsborough community college",
    "genesee community college", "marian university", "trocaire college",
    "university of arizona", "vermont state university", "trooper pet",
    "kindred", "animall", "kumba", "jdam", "pet nation", "semper k9",
    "national mill dog rescue", "animal welfare society",
    "the greyhound health initiative", "elephant aid",
]
EXCLUDE_DOMAINS = {"stripe.com", "carecredit.com", "vetsource.com"}

# ── Geography ─────────────────────────────────────────────────────────────────
NA_COUNTRIES = {"united states","us","usa","canada","ca","mexico","mx",""}
US_STATES    = set("AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD "
                   "MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC "
                   "SD TN TX UT VT VA WA WV WI WY DC".split())
CA_PROVINCES = set("AB BC MB NB NL NS NT NU ON PE QC SK YT".split())

def is_north_america(props: dict) -> bool:
    country = (props.get("country") or "").lower().strip()
    state   = (props.get("state")   or "").strip().upper()
    if country in NA_COUNTRIES: return True
    if not country and (state in US_STATES or state in CA_PROVINCES): return True
    return False

def in_na_bounds(lat: float, lng: float) -> bool:
    return 14.5 <= lat <= 72.0 and -170.0 <= lng <= -50.0

def coord_matches_country(lat: float, lng: float, country: str) -> bool:
    cl = (country or "").lower()
    if cl in {"canada","ca"}:  return lat > 41.5 and lng < -52.0
    if cl in {"mexico","mx"}:  return 14.5 <= lat <= 32.7 and -118.0 <= lng <= -86.0
    return True

def is_negative_props(props: dict) -> bool:
    return any(k in (props.get("internal_comments") or "").lower() for k in NEGATIVE_KW)

def is_excluded_non_clinic(props: dict) -> bool:
    name   = (props.get("name")   or "").lower()
    domain = (props.get("domain") or "").lower()
    return (any(f in name for f in EXCLUDE_NAME_FRAGMENTS) or
            any(d in domain for d in EXCLUDE_DOMAINS))

# ── HubSpot ───────────────────────────────────────────────────────────────────
HS_PROPS = [
    "name", "address", "city", "state", "zip", "country",
    "contact_email", "phone", "current_pims", "domain",
    "media_testimonials_dsp", "internal_comments",
]

# Deal properties fetched for each closed-won deal.
# If your "number of DVMs" property has a different internal name in HubSpot,
# update "number_of_dvms" below to match. Find it at:
# HubSpot → Settings → Properties → Deals → search "dvm"
HS_DEAL_PROPS = ["number_of_dvms", "dealstage", "hs_is_closed_won", "closedate"]

def hs_h():
    return {"Authorization": f"Bearer {HS_TOKEN}", "Content-Type": "application/json"}

def hs_context_signals(props: dict) -> list:
    """Context-only signals — displayed in popup but never qualify a clinic.
    NOTE: hs_long_tenure is no longer computed here. It is computed in the main
    loop from the actual closed-won deal close date, not the CRM createdate."""
    sigs  = []
    media = (props.get("media_testimonials_dsp") or "").lower()
    notes = (props.get("internal_comments")      or "").lower()
    if any(k in media for k in ["article","video","testimonial"]):
        sigs.append("hs_testimonial")
    if any(k in media for k in ["dsp","6 figure","6-figure"]):
        sigs.append("hs_dsp")
    if any(k in notes for k in POSITIVE_KW) and not any(k in notes for k in NEGATIVE_KW):
        sigs.append("hs_positive_note")
    return list(set(sigs))

# ── Geocoding ─────────────────────────────────────────────────────────────────
def build_geocode_query(props: dict, contact: dict = None) -> str:
    def norm(c):
        return {"us":"United States","usa":"United States","ca":"Canada",
                "canada":"Canada","mx":"Mexico","mexico":"Mexico"}.get(
                (c or "").lower().strip(), c or "United States")
    raw     = (props.get("address") or "").strip()
    co_city = (props.get("city")    or "").strip().title()
    co_st   = (props.get("state")   or "").strip().upper()
    co_ctry = norm(props.get("country",""))
    ct      = contact or {}
    ct_city = (ct.get("city")  or "").strip().title()
    ct_st   = (ct.get("state") or "").strip().upper()
    ct_ctry = norm(ct.get("country","")) if ct else co_ctry
    state   = ct_st or co_st
    country = ct_ctry or co_ctry
    city    = co_city or ct_city

    raw_ok = raw and len(raw) < 80 and "po box" not in raw.lower()
    if raw_ok and city and state:
        return f"{raw}, {city}, {state}, {country}"
    if raw_ok and state:
        return f"{raw}, {state}, {country}"
    if city and state: return f"{city}, {state}, {country}"
    if state:          return f"{state}, {country}"
    return ""

def geocode_google(query: str):
    if not GOOGLE_KEY or not query: return None, None
    try:
        r = requests.get("https://maps.googleapis.com/maps/api/geocode/json",
            params={"address":query,"key":GOOGLE_KEY}, timeout=10)
        if r.status_code == 200:
            res = r.json().get("results",[])
            if res:
                loc = res[0]["geometry"]["location"]
                return round(loc["lat"],5), round(loc["lng"],5)
    except Exception as e:
        print(f"  Google geocode failed '{query}': {e}")
    return None, None

def geocode_nominatim(query: str):
    if not query: return None, None
    time.sleep(1.2)
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
            params={"format":"json","q":query,"limit":1},
            headers={"User-Agent":"DigitailAdvocateMap/4.0"}, timeout=10)
        if r.status_code == 200:
            d = r.json()
            if d: return round(float(d[0]["lat"]),5), round(float(d[0]["lon"]),5)
    except Exception as e:
        print(f"  Nominatim failed '{query}': {e}")
    return None, None

def geocode(props: dict, contact: dict = None):
    query = build_geocode_query(props, contact)
    if not query: return None, None
    lat, lng = None, None
    if GOOGLE_KEY:
        lat, lng = geocode_google(query)
    if not lat:
        lat, lng = geocode_nominatim(query)
    if lat:
        country = (props.get("country") or "").strip()
        if in_na_bounds(lat, lng) and coord_matches_country(lat, lng, country):
            return lat, lng
        print(f"  Geocode rejected: {query} → {lat},{lng}")
    return None, None

# ── HubSpot referral network — search contacts by property ───────────────────
def hs_fetch_referral_network() -> tuple:
    """
    Fetches contacts where reference_program_optin = true using the standard
    contacts search API (no crm.lists.read scope required).

    Returns (name_map, company_ids_set):
      company_ids_set — exact HubSpot company object IDs resolved via associations;
                        matched first, zero name guessing
      name_map        — contact "company" text field fallback for contacts that
                        have no linked company record in HubSpot
    """
    if not HS_TOKEN: return {}, set()

    # Step 1: search contacts where Reference Program Opt-In = Yes
    all_contacts, after = [], None
    while True:
        payload = {
            "filterGroups": [{"filters": [{
                "propertyName": "reference_program_optin",
                "operator":     "EQ",
                "value":        "true",
            }]}],
            "properties": ["firstname","lastname","email","phone",
                           "company","jobtitle","city","state","zip","country"],
            "limit": 100,
        }
        if after: payload["after"] = after
        r = requests.post("https://api.hubapi.com/crm/v3/objects/contacts/search",
                          json=payload, headers=hs_h(), timeout=15)
        if r.status_code != 200:
            print(f"  Referral network search error: {r.status_code} — {r.text[:200]}")
            break
        data = r.json()
        all_contacts.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after: break

    print(f"  Referral network: {len(all_contacts)} opted-in contacts found")
    if not all_contacts: return {}, set()

    contact_ids = [c["id"] for c in all_contacts]
    company_ids = set()
    name_map    = {}

    # Build name_map directly from search results (no second API call needed)
    for contact in all_contacts:
        p  = contact.get("properties", {})
        co = (p.get("company") or "").strip()
        if co:
            name_map[co.lower()] = {"contact_props": p, "contact_id": contact["id"]}

    # Step 2: resolve each contact → associated company object ID (exact, no name guessing)
    for i in range(0, len(contact_ids), 100):
        batch = contact_ids[i:i+100]
        ar = requests.post(
            "https://api.hubapi.com/crm/v4/associations/contacts/companies/batch/read",
            json={"inputs": [{"id": cid} for cid in batch]},
            headers=hs_h(), timeout=15)
        if ar.status_code == 200:
            for item in ar.json().get("results", []):
                for assoc in item.get("to", []):
                    cid = str(assoc.get("toObjectId", ""))
                    if cid: company_ids.add(cid)
        time.sleep(0.1)

    print(f"  Referral network: {len(company_ids)} exact company ID matches "
          f"+ {len(name_map)} name fallbacks")
    return name_map, company_ids

# ── HubSpot Onboarding pipeline exclusion ────────────────────────────────────
def hs_fetch_onboarding_pipeline_config() -> tuple:
    """
    Auto-detects the Onboarding pipeline and its CS stage from HubSpot.
    Returns (pipeline_id, cs_stage_id) — both None if not found.
    Uses ONBOARDING_PIPELINE_KEYWORD and ONBOARDING_CS_STAGE_KEYWORD for matching.
    """
    if not HS_TOKEN: return None, None
    try:
        r = requests.get("https://api.hubapi.com/crm/v3/pipelines/deals",
                         headers=hs_h(), timeout=15)
        if r.status_code != 200:
            print(f"  Pipeline fetch error: {r.status_code}"); return None, None
        for pipeline in r.json().get("results", []):
            label = (pipeline.get("label") or "").lower()
            if ONBOARDING_PIPELINE_KEYWORD.lower() not in label:
                continue
            pipeline_id  = pipeline["id"]
            cs_stage_id  = None
            stage_labels = []
            for stage in pipeline.get("stages", []):
                slabel = (stage.get("label") or "").lower()
                stage_labels.append(stage.get("label",""))
                if ONBOARDING_CS_STAGE_KEYWORD.lower() in slabel:
                    cs_stage_id = stage["id"]
                    break
            print(f"  Onboarding pipeline: '{pipeline.get('label')}' (ID: {pipeline_id})")
            print(f"  Stages: {stage_labels}")
            if cs_stage_id:
                print(f"  CS stage matched: '{cs_stage_id}'")
            else:
                print(f"  ⚠ No CS stage matched — update ONBOARDING_CS_STAGE_KEYWORD")
            return pipeline_id, cs_stage_id
    except Exception as e:
        print(f"  Onboarding pipeline config failed: {e}")
    print("  ⚠ Onboarding pipeline not found — update ONBOARDING_PIPELINE_KEYWORD")
    return None, None

def hs_get_active_onboarding_company_ids(pipeline_id: str, cs_stage_id: str) -> set:
    """
    Returns HubSpot company IDs with an ACTIVE onboarding deal that has NOT
    yet reached the CS stage. These companies are still being set up and should
    not appear as references on the map.
    """
    if not pipeline_id: return set()
    exclude_ids = set()
    filters = [{"propertyName": "pipeline", "operator": "EQ", "value": pipeline_id}]
    # Exclude deals in the CS stage (those are ready)
    if cs_stage_id:
        filters.append({"propertyName": "dealstage", "operator": "NEQ", "value": cs_stage_id})
    # Only active deals (not closed)
    filters.append({"propertyName": "hs_is_closed", "operator": "EQ", "value": "false"})
    after = None
    while True:
        payload = {"filterGroups": [{"filters": filters}],
                   "properties": ["dealname","dealstage","pipeline"], "limit": 200}
        if after: payload["after"] = after
        r = requests.post("https://api.hubapi.com/crm/v3/objects/deals/search",
                          json=payload, headers=hs_h(), timeout=15)
        if r.status_code != 200: break
        data     = r.json()
        deal_ids = [d["id"] for d in data.get("results", [])]
        for i in range(0, len(deal_ids), 100):
            ar = requests.post(
                "https://api.hubapi.com/crm/v4/associations/deals/companies/batch/read",
                json={"inputs": [{"id": did} for did in deal_ids[i:i+100]]},
                headers=hs_h(), timeout=15)
            if ar.status_code == 200:
                for item in ar.json().get("results", []):
                    for assoc in item.get("to", []):
                        cid = str(assoc.get("toObjectId", ""))
                        if cid: exclude_ids.add(cid)
            time.sleep(0.1)
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after: break
    print(f"  Active onboarding (pre-CS): {len(exclude_ids)} companies excluded")
    return exclude_ids

# ── Sales pipeline — customer-since date source ───────────────────────────────
def hs_fetch_sales_pipeline_id() -> str:
    """
    Auto-detects the Sales pipeline ID from HubSpot by matching SALES_PIPELINE_KEYWORD
    against pipeline labels. Returns the pipeline ID string, or None if not found.
    """
    if not HS_TOKEN: return None
    try:
        r = requests.get("https://api.hubapi.com/crm/v3/pipelines/deals",
                         headers=hs_h(), timeout=15)
        if r.status_code != 200:
            print(f"  Sales pipeline fetch error: {r.status_code}"); return None
        for pipeline in r.json().get("results", []):
            label = (pipeline.get("label") or "").lower()
            if SALES_PIPELINE_KEYWORD.lower() in label:
                print(f"  Sales pipeline: '{pipeline.get('label')}' (ID: {pipeline['id']})")
                return pipeline["id"]
    except Exception as e:
        print(f"  Sales pipeline fetch failed: {e}")
    print(f"  ⚠ Sales pipeline not found — update SALES_PIPELINE_KEYWORD "
          f"(current value: '{SALES_PIPELINE_KEYWORD}')")
    return None

def hs_build_sales_closedate_map(sales_pipeline_id: str) -> dict:
    """
    Pre-fetches ALL closed-won deals in the Sales pipeline and maps each company
    to the EARLIEST sales close date (i.e. when they first became a customer).

    CS, upsell, and expansion deals are excluded by filtering on pipeline ID.
    Returns {company_hs_id: 'YYYY-MM-DD'}.
    """
    if not sales_pipeline_id: return {}
    company_closedate = {}
    after = None
    while True:
        payload = {
            "filterGroups": [{"filters": [
                {"propertyName": "pipeline",       "operator": "EQ", "value": sales_pipeline_id},
                {"propertyName": "hs_is_closed_won","operator": "EQ", "value": "true"},
            ]}],
            "properties": ["dealname", "closedate"],
            "limit": 200,
        }
        if after: payload["after"] = after
        r = requests.post("https://api.hubapi.com/crm/v3/objects/deals/search",
                          json=payload, headers=hs_h(), timeout=15)
        if r.status_code != 200:
            print(f"  Sales closedate map error: {r.status_code}"); break
        data    = r.json()
        results = data.get("results", [])

        # Build deal_id → closedate for this batch
        deal_closedate = {}
        for d in results:
            cd = (d.get("properties", {}).get("closedate") or "").strip()
            if cd:
                try:
                    dt = datetime.fromisoformat(cd.replace("Z", "+00:00"))
                    deal_closedate[d["id"]] = dt.strftime("%Y-%m-%d")
                except Exception:
                    pass

        # Resolve deals → company IDs
        deal_ids = list(deal_closedate.keys())
        for i in range(0, len(deal_ids), 100):
            ar = requests.post(
                "https://api.hubapi.com/crm/v4/associations/deals/companies/batch/read",
                json={"inputs": [{"id": did} for did in deal_ids[i:i+100]]},
                headers=hs_h(), timeout=15)
            if ar.status_code == 200:
                for item in ar.json().get("results", []):
                    deal_id   = str(item.get("from", {}).get("id", ""))
                    closedate = deal_closedate.get(deal_id)
                    if not closedate: continue
                    for assoc in item.get("to", []):
                        cid = str(assoc.get("toObjectId", ""))
                        if cid:
                            # Keep the EARLIEST date — first time they ever became a customer
                            if cid not in company_closedate or closedate < company_closedate[cid]:
                                company_closedate[cid] = closedate
            time.sleep(0.1)

        after = data.get("paging", {}).get("next", {}).get("after")
        if not after: break

    print(f"  Sales closedate map: {len(company_closedate)} companies with a sales close date")
    return company_closedate

# ── HubSpot deal helpers ──────────────────────────────────────────────────────
def hs_get_deal_contacts(deal_id: str) -> list:
    try:
        r = requests.get(
            f"https://api.hubapi.com/crm/v3/objects/deals/{deal_id}/associations/contacts",
            headers=hs_h(), timeout=10)
        if r.status_code != 200: return []
        ids = [c["id"] for c in r.json().get("results",[])]
        if not ids: return []
        cr = requests.post("https://api.hubapi.com/crm/v3/objects/contacts/batch/read",
            json={"inputs":[{"id":i} for i in ids[:5]],
                  "properties":["firstname","lastname","email","phone","jobtitle",
                                "city","state","zip","country"]},
            headers=hs_h(), timeout=10)
        if cr.status_code != 200: return []
        contacts = [c.get("properties",{}) for c in cr.json().get("results",[])]
        def rank(c):
            jt = (c.get("jobtitle") or "").lower()
            if any(k in jt for k in ["owner","dvm","veterinarian","doctor","ceo"]): return 0
            if any(k in jt for k in ["manager","director","admin","practice"]): return 1
            return 2
        return sorted(contacts, key=rank)
    except Exception as e:
        print(f"  Deal contact fetch failed {deal_id}: {e}")
        return []

def best_contact_and_deal_props(hs_id: str) -> tuple:
    """
    Returns (best_contact_dict, deal_props_dict) for the company.
    deal_props_dict may contain:
      'dvms' — number of DVMs from the most recent closed-won deal

    NOTE: closedate is intentionally NOT returned here. customerSince is sourced
    exclusively from hs_build_sales_closedate_map() (Sales pipeline only) to avoid
    CS expansion or upsell deal dates polluting the customer-since value.
    closedate is still in HS_DEAL_PROPS so candidates can be sorted by recency.
    """
    try:
        r = requests.get(
            f"https://api.hubapi.com/crm/v3/objects/companies/{hs_id}/associations/deals",
            headers=hs_h(), timeout=10)
        if r.status_code != 200: return {}, {}

        deals = r.json().get("results", [])[:5]
        if not deals: return {}, {}

        deal_ids = [d["id"] for d in deals]

        # Fetch deal properties — DVM count + close date
        # If "number_of_dvms" doesn't match your HubSpot property name, check:
        # HubSpot → Settings → Properties → Deals → search "dvm"
        deal_props = {}
        dr = requests.post(
            "https://api.hubapi.com/crm/v3/objects/deals/batch/read",
            json={"inputs":    [{"id": did} for did in deal_ids],
                  "properties": HS_DEAL_PROPS},
            headers=hs_h(), timeout=10)
        if dr.status_code == 200:
            all_results = dr.json().get("results", [])
            # Prefer deals explicitly marked closed-won
            won_deals = []
            for deal in all_results:
                p = deal.get("properties", {})
                if (p.get("hs_is_closed_won") == "true" or
                        p.get("dealstage") == "closedwon"):
                    won_deals.append(p)
            candidates = won_deals or [d.get("properties", {}) for d in all_results]

            # Sort candidates by closedate descending — most recent win first
            def _sort_key(p):
                cd = p.get("closedate") or ""
                return cd
            candidates.sort(key=_sort_key, reverse=True)

            for p in candidates:
                # DVM count only — closedate is now sourced from the Sales pipeline
                # pre-fetch map (hs_build_sales_closedate_map) to avoid CS/upsell dates
                dvms = (p.get("number_of_dvms") or "").strip()
                if dvms and dvms not in ("0", "0.0"):
                    try:
                        deal_props["dvms"] = str(int(float(dvms)))
                    except ValueError:
                        deal_props["dvms"] = dvms
                    break  # take from the most recent won deal and stop

        # Fetch best contact from the same set of deals
        best_contact = {}
        for deal_id in deal_ids:
            contacts = hs_get_deal_contacts(deal_id)
            if contacts:
                best_contact = contacts[0]
                break

        return best_contact, deal_props

    except Exception as e:
        print(f"  Deal props fetch failed {hs_id}: {e}")
        return {}, {}

def hs_get_closedwon_company_ids() -> set:
    ids = set()
    for filt in [{"propertyName":"hs_is_closed_won","operator":"EQ","value":"true"},
                 {"propertyName":"dealstage","operator":"EQ","value":"closedwon"}]:
        after = None
        while True:
            payload = {"filterGroups":[{"filters":[filt]}],
                       "properties":["dealname"],"limit":200}
            if after: payload["after"] = after
            r = requests.post("https://api.hubapi.com/crm/v3/objects/deals/search",
                              json=payload, headers=hs_h(), timeout=15)
            if r.status_code != 200: break
            data     = r.json()
            deal_ids = [d["id"] for d in data.get("results",[])]
            for i in range(0, len(deal_ids), 100):
                ar = requests.post(
                    "https://api.hubapi.com/crm/v4/associations/deals/companies/batch/read",
                    json={"inputs":[{"id":did} for did in deal_ids[i:i+100]]},
                    headers=hs_h(), timeout=15)
                if ar.status_code == 200:
                    for item in ar.json().get("results",[]):
                        for assoc in item.get("to",[]):
                            ids.add(str(assoc.get("toObjectId","")))
                time.sleep(0.1)
            after = data.get("paging",{}).get("next",{}).get("after")
            if not after: break
        if ids: break
    print(f"  HubSpot closed-won: {len(ids)} companies")
    return ids

def hs_get_customers() -> list:
    closed_won = hs_get_closedwon_company_ids()
    by_id      = {}
    for filt in [{"propertyName":"lifecyclestage","operator":"EQ","value":"customer"},
                 {"propertyName":"hs_current_customer","operator":"EQ","value":"true"}]:
        after, batch = None, {}
        while True:
            payload = {"filterGroups":[{"filters":[filt]}],
                       "properties":HS_PROPS,"limit":100}
            if after: payload["after"] = after
            r = requests.post("https://api.hubapi.com/crm/v3/objects/companies/search",
                              json=payload, headers=hs_h(), timeout=15)
            if r.status_code != 200: break
            data = r.json()
            for c in data.get("results",[]): batch[c["id"]] = c
            after = data.get("paging",{}).get("next",{}).get("after")
            if not after: break
        if batch:
            by_id.update(batch)
            print(f"  HubSpot filter '{filt['propertyName']}': {len(batch)} companies")
            break
        print(f"  HubSpot filter '{filt['propertyName']}': 0, trying next…")
    new_ids = closed_won - set(by_id.keys())
    print(f"  HubSpot: {len(by_id)} customers + {len(new_ids)} closed-won-only")
    for i in range(0, len(list(new_ids)), 100):
        r = requests.post("https://api.hubapi.com/crm/v3/objects/companies/batch/read",
            json={"inputs":[{"id":cid} for cid in list(new_ids)[i:i+100]],
                  "properties":HS_PROPS},
            headers=hs_h(), timeout=15)
        if r.status_code == 200:
            for c in r.json().get("results",[]): by_id[c["id"]] = c
        time.sleep(0.15)
    all_cos = list(by_id.values())
    print(f"  HubSpot total: {len(all_cos)} before filters")
    return all_cos

# ── Intercom ──────────────────────────────────────────────────────────────────
def _ic_name(contact_id: str, headers: dict) -> str:
    try:
        cr = requests.get(f"https://api.intercom.io/contacts/{contact_id}",
                          headers=headers, timeout=8)
        if cr.status_code == 200:
            d   = cr.json()
            cos = d.get("companies",{}).get("data",[])
            return cos[0].get("name","") if cos else (d.get("name","") or "")
    except Exception:
        pass
    return ""

def _ic_extract(convo: dict, headers: dict, results: dict):
    robj     = convo.get("conversation_rating") or {}
    remark   = (robj.get("remark") or "").strip()
    contacts = convo.get("contacts",{}).get("contacts",[])
    name     = _ic_name(contacts[0]["id"], headers) if contacts else ""
    if name:
        key     = name.lower().strip()
        conv_id = convo.get("id","")
        # Direct link to conversation in Intercom inbox
        conv_url = f"https://app.intercom.com/a/inbox/conversations/{conv_id}" if conv_id else None
        if key not in results:
            results[key] = {"signal":"intercom_csat",
                            "quote": remark[:300] if remark else None,
                            "url":   conv_url}

def fetch_intercom_csat() -> dict:
    if not IC_TOKEN: return {}
    hdrs = {"Authorization":f"Bearer {IC_TOKEN}","Accept":"application/json",
            "Intercom-Version":"2.10"}
    results = {}
    for field, val in [("conversation_rating.rating","amazing"),("rating","amazing")]:
        try:
            r = requests.post("https://api.intercom.io/conversations/search",
                json={"query":{"operator":"AND","value":[{"field":field,"operator":"=","value":val}]},
                      "pagination":{"per_page":150}},
                headers=hdrs, timeout=30)
            if r.status_code == 200:
                convos = r.json().get("conversations",[])
                if convos:
                    for c in convos: _ic_extract(c, hdrs, results)
                    print(f"  Intercom CSAT: {len(results)} records")
                    return results
        except Exception as e:
            print(f"  Intercom search failed: {e}")
    try:
        r = requests.get("https://api.intercom.io/conversations",
            params={"per_page":150,"order":"desc","display_as":"plaintext"},
            headers=hdrs, timeout=30)
        if r.status_code == 200:
            for c in r.json().get("conversations",[]):
                robj = c.get("conversation_rating") or {}
                if str(robj.get("value","") or robj.get("rating","")) in ["5","amazing","great"]:
                    _ic_extract(c, hdrs, results)
    except Exception as e:
        print(f"  Intercom fallback failed: {e}")
    print(f"  Intercom (fallback): {len(results)} CSAT records")
    return results

def fetch_intercom_negative() -> set:
    if not IC_TOKEN: return set()
    hdrs = {"Authorization":f"Bearer {IC_TOKEN}","Accept":"application/json",
            "Intercom-Version":"2.10"}
    bad = set()
    for field, val in [("conversation_rating.rating","terrible"),
                       ("conversation_rating.rating","bad"),
                       ("rating","terrible"),("rating","bad")]:
        try:
            r = requests.post("https://api.intercom.io/conversations/search",
                json={"query":{"operator":"AND","value":[{"field":field,"operator":"=","value":val}]},
                      "pagination":{"per_page":100}},
                headers=hdrs, timeout=30)
            if r.status_code == 200:
                convos = r.json().get("conversations",[])
                if convos:
                    tmp = {}
                    for c in convos: _ic_extract(c, hdrs, tmp)
                    bad.update(tmp.keys()); break
        except Exception:
            continue
    if bad: print(f"  Intercom negative: {len(bad)} companies flagged")
    return bad

# ── Slack channel scanning ────────────────────────────────────────────────────
def fetch_slack_signals() -> tuple:
    if not SLACK_BOT_TOKEN:
        print("  Slack: no token, skipping"); return {}, set()
    hdrs     = {"Authorization":f"Bearer {SLACK_BOT_TOKEN}"}
    positive, negative = {}, set()
    cutoff   = int((datetime.now(timezone.utc) - timedelta(days=SLACK_LOOKBACK_DAYS)).timestamp())

    # Get workspace URL once for constructing message permalinks
    workspace_url = "https://digitail.slack.com"
    try:
        tr = requests.get("https://slack.com/api/auth.test", headers=hdrs, timeout=10)
        if tr.ok:
            workspace_url = tr.json().get("url", workspace_url).rstrip("/")
    except Exception:
        pass

    # ── Discover ALL channels the bot is a member of ──────────────────────────
    all_channels, cursor = [], None
    while True:
        try:
            params = {"types":"public_channel","limit":200,
                      "exclude_archived":"true"}
            if cursor: params["cursor"] = cursor
            r = requests.get("https://slack.com/api/conversations.list",
                             params=params, headers=hdrs, timeout=15)
            data = r.json()
            if not data.get("ok"):
                print(f"  Slack channel list error: {data.get('error','')}"); break
            for ch in data.get("channels", []):
                if ch.get("is_member"):   # only channels the bot can read
                    all_channels.append({"name": ch["name"], "id": ch["id"]})
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor: break
        except Exception as e:
            print(f"  Slack channel list failed: {e}"); break

    # Sort: negative-priority first → positive-priority → everything else
    # This ensures churn/escalation channels are evaluated before we decide
    # whether a positive mention from another channel counts
    def _ch_sort(ch):
        n = ch["name"].lower()
        if any(k in n for k in SLACK_NEGATIVE_PRIORITY): return 0
        if any(k in n for k in SLACK_POSITIVE_PRIORITY): return 1
        return 2
    all_channels.sort(key=_ch_sort)

    neg_channels = [c["name"] for c in all_channels if _ch_sort(c) == 0]
    pos_channels = [c["name"] for c in all_channels if _ch_sort(c) == 1]
    print(f"  Slack: {len(all_channels)} accessible channels "
          f"({SLACK_LOOKBACK_DAYS}-day window)")
    print(f"  Negative-priority: {neg_channels or ['none matched']}")
    print(f"  Positive-priority: {pos_channels or ['none matched']}")

    # ── Scan each channel ─────────────────────────────────────────────────────
    scanned = 0
    for ch in all_channels:
        ch_name = ch["name"]
        ch_id   = ch["id"]
        try:
            r = requests.get("https://slack.com/api/conversations.history",
                params={"channel":ch_id,"oldest":cutoff,"limit":200},
                headers=hdrs, timeout=15)
            data = r.json()
            if not data.get("ok"):
                # "not_in_channel" is expected for some channels — skip silently
                err = data.get("error","")
                if err not in ("not_in_channel","channel_not_found"):
                    print(f"  Slack #{ch_name}: {err}")
                continue
            for msg in data.get("messages",[]):
                text = msg.get("text","")
                if not text or len(text) < 15: continue
                tl      = text.lower()
                is_pos  = any(k in tl for k in POSITIVE_KW)
                is_neg  = any(k in tl for k in NEGATIVE_KW)
                scanned += 1
                ts       = msg.get("ts","")
                ts_nodot = ts.replace(".","")
                permalink = f"{workspace_url}/archives/{ch_id}/p{ts_nodot}" if ts_nodot else None
                # Require 2+ capitalised words to avoid single-word false matches
                for word in re.findall(r'\b[A-Z][a-zA-Z]{3,}(?:\s[A-Z][a-zA-Z]{3,})+\b', text):
                    if len(word) > 10 and word.lower() not in {
                        "slack","digitail","tails","monday","friday",
                        "north america","good morning","great work","well done",
                    }:
                        if is_pos and not is_neg:
                            positive.setdefault(word.lower(), {
                                "signal":    "slack_mention",
                                "quote":     text[:280],
                                "channel":   ch_name,
                                "permalink": permalink,
                            })
                        elif is_neg and not is_pos:
                            negative.add(word.lower())
            time.sleep(0.3)  # respect Slack Tier 3 rate limit (50 req/min)
        except Exception as e:
            print(f"  Slack #{ch_name} failed: {e}")

    print(f"  Slack: {scanned} messages scanned → "
          f"{len(positive)} positive mentions, {len(negative)} negative terms")
    return positive, negative

# ── Fathom call intelligence ──────────────────────────────────────────────────
def fetch_fathom_signals() -> tuple:
    if not FATHOM_API_KEY:
        print("  Fathom: no API key, skipping"); return {}, set()
    hdrs   = {"Authorization":f"Bearer {FATHOM_API_KEY}","Content-Type":"application/json"}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    positive, negative = {}, set()
    try:
        r = requests.get("https://api.fathom.ai/v1/calls",
                         params={"limit":100,"after":cutoff}, headers=hdrs, timeout=15)
        if r.status_code != 200:
            print(f"  Fathom: HTTP {r.status_code}"); return {}, set()
        calls = r.json().get("data", r.json().get("calls",[]))
        print(f"  Fathom: {len(calls)} total calls")
        cs_filtered = 0
        for call in calls:
            owner       = call.get("owner") or call.get("host") or call.get("user") or {}
            owner_email = (owner.get("email","") if isinstance(owner,dict) else str(owner)).lower().strip()
            if FATHOM_CS_EMAILS:
                if owner_email not in FATHOM_CS_EMAILS:
                    cs_filtered += 1; continue
            else:
                attendees = call.get("attendees",[])
                internal  = [a.get("email","") for a in attendees
                             if "@digitail.io" in (a.get("email") or "")]
                if not internal and "@digitail.io" not in owner_email:
                    cs_filtered += 1; continue
            call_id = call.get("id","")
            title   = (call.get("title") or call.get("name") or "").strip()
            summary = (call.get("summary") or call.get("description") or "").strip()
            if not summary and call_id:
                try:
                    sr = requests.get(f"https://api.fathom.ai/v1/calls/{call_id}",
                                      headers=hdrs, timeout=10)
                    if sr.status_code == 200:
                        summary = (sr.json().get("summary") or "").strip()
                except Exception:
                    pass
            combined = f"{title} {summary}".lower()
            if not combined.strip(): continue
            is_pos = any(k in combined for k in POSITIVE_KW)
            is_neg = any(k in combined for k in NEGATIVE_KW)
            candidates = set()
            for pat in [r'(?:with|re:|follow.?up|check.?in)\s+([A-Z][^\-–|:]+)',
                        r'^([A-Z][a-zA-Z\s]{4,40})\s*[-–|:]']:
                m = re.search(pat, title)
                if m: candidates.add(m.group(1).strip().lower())
            for att in call.get("attendees",[]):
                email = (att.get("email") or "")
                if "@" in email and "@digitail.io" not in email:
                    domain = email.split("@")[1].split(".")[0]
                    if len(domain) > 4 and domain not in {"gmail","yahoo","hotmail","outlook"}:
                        candidates.add(domain.lower())
            for cand in candidates:
                if is_pos and not is_neg:
                    # Fathom call URL — try share_url first, fall back to constructed URL
                    call_url = (call.get("share_url") or call.get("url") or
                                (f"https://app.fathom.video/call/{call_id}" if call_id else None))
                    positive.setdefault(cand, {
                        "signal": "fathom_call",
                        "quote":  summary[:280] if summary else f"Positive CS call: {title}",
                        "url":    call_url,
                    })
                elif is_neg and not is_pos:
                    negative.add(cand)
        print(f"  Fathom: {len(calls)-cs_filtered} CS calls processed, "
              f"{len(positive)} positive, {len(negative)} negative")
    except Exception as e:
        print(f"  Fathom failed: {e}")
    return positive, negative

# ── Review scrapers ───────────────────────────────────────────────────────────
def _scrape(url, key, card_sels, rating_sels, body_sels, rev_sels, min_r=4.0):
    if not BS4: return []
    out = []
    try:
        r = requests.get(url, headers={
            "User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language":"en-US,en;q=0.9"}, timeout=15)
        if r.status_code != 200: print(f"  {key}: HTTP {r.status_code}"); return []
        soup  = BeautifulSoup(r.text,"html.parser")
        cards = next((soup.select(s) for s in card_sels if soup.select(s)),[])
        for card in cards:
            try:
                rating = 0.0
                for s in rating_sels:
                    el = card.select_one(s)
                    if el:
                        nums = re.findall(r'(\d+\.?\d*)', el.get("aria-label","") or el.get_text())
                        if nums: rating = float(nums[0]); break
                if rating and rating < min_r: continue
                text = next((card.select_one(s).get_text(" ",strip=True)[:400]
                             for s in body_sels if card.select_one(s)),"")
                if not text or len(text) < 20: continue
                reviewer = next((card.select_one(s).get_text(strip=True)
                                 for s in rev_sels if card.select_one(s)),"")
                out.append({"source":key,"reviewer":reviewer,"text":text,"rating":rating,"signal":key})
            except Exception:
                continue
        print(f"  {key}: {len(out)} reviews")
    except Exception as e:
        print(f"  {key} failed: {e}")
    return out

def scrape_capterra():
    return _scrape("https://www.capterra.com/p/167764/Digitail/","capterra_positive",
        ["[data-testid='review-card']",".review-card","[class*='ReviewCard']"],
        ["[aria-label*='star']","[aria-label*='out of']","[class*='rating']"],
        ["p","[class*='body']","[class*='review-text']"],
        ["[class*='reviewer']","[class*='author']"])

def scrape_g2():
    return _scrape("https://www.g2.com/products/digitail/reviews","g2_positive",
        ["[itemprop='review']","[class*='Paper__StyledPaper']","article"],
        ["[itemprop='ratingValue']","[class*='stars']","[aria-label*='star']"],
        ["[itemprop='reviewBody']","[class*='formatted-text']","p"],
        ["[itemprop='author']","[class*='reviewer']"])

def scrape_software_advice():
    return _scrape("https://www.softwareadvice.com/veterinary/digitail-profile/reviews/","softwareadvice",
        ["[class*='review-card']","[class*='ReviewCard']","article"],
        ["[class*='rating']","[aria-label*='star']"],
        ["[class*='review-body']","p"],["[class*='reviewer']","[class*='author']"])

def scrape_getapp():
    return _scrape("https://www.getapp.com/veterinary-practice-management-software/a/digitail/reviews/","getapp",
        ["[class*='review']","article"],["[class*='rating']","[aria-label*='star']"],
        ["[class*='body']","p"],["[class*='reviewer']","[class*='author']"])

def scrape_trustpilot():
    return _scrape("https://www.trustpilot.com/review/digitail.io","capterra_positive",
        ["[data-service-review-card-paper]","[class*='reviewCard']","article"],
        ["[data-service-review-rating]","[class*='starRating']"],
        ["[data-service-review-text-typography]","p"],
        ["[class*='consumerName']","[class*='reviewer']"])

# ── Google ────────────────────────────────────────────────────────────────────
def fetch_google_reviews() -> list:
    if not GOOGLE_KEY or not GOOGLE_PLACE:
        print("  Google Reviews: no credentials, skipping"); return []
    try:
        r = requests.get("https://maps.googleapis.com/maps/api/place/details/json",
            params={"place_id":GOOGLE_PLACE,"fields":"reviews","key":GOOGLE_KEY,
                    "reviews_sort":"newest"}, timeout=10)
        if r.status_code == 200:
            out = [{"source":"google","reviewer":rv.get("author_name",""),
                    "text":rv["text"][:400],"signal":"google_review"}
                   for rv in r.json().get("result",{}).get("reviews",[])
                   if rv.get("rating",0) >= 4 and rv.get("text","")]
            print(f"  Google Reviews: {len(out)} reviews"); return out
    except Exception as e:
        print(f"  Google Reviews failed: {e}")
    return []

def fetch_google_web_mentions() -> list:
    if not GOOGLE_KEY or not GOOGLE_CSE_ID:
        print("  Google CSE: no credentials, skipping"); return []
    out = []
    try:
        for q in ['"Digitail" veterinary review','"Digitail" PIMS switched',
                  '"Digitail" vet recommend']:
            r = requests.get("https://www.googleapis.com/customsearch/v1",
                params={"q":q,"key":GOOGLE_KEY,"cx":GOOGLE_CSE_ID,"num":10}, timeout=10)
            if r.status_code == 200:
                for item in r.json().get("items",[]):
                    s  = item.get("snippet","")
                    sl = s.lower()
                    if any(p in sl for p in POSITIVE_KW) and not any(n in sl for n in NEGATIVE_KW):
                        out.append({"source":"google_web","text":s[:400],"signal":"google_mention"})
            time.sleep(0.3)
        print(f"  Google CSE: {len(out)} mentions")
    except Exception as e:
        print(f"  Google CSE failed: {e}")
    return out

def fetch_facebook_reviews() -> list:
    if not FB_TOKEN: print("  Facebook: no token, skipping"); return []
    try:
        r = requests.get("https://graph.facebook.com/v18.0/me/ratings",
            params={"access_token":FB_TOKEN,
                    "fields":"reviewer{name},rating,review_text","limit":50}, timeout=10)
        if r.status_code == 200:
            out = [{"source":"facebook","reviewer":rv.get("reviewer",{}).get("name",""),
                    "text":rv["review_text"][:400],"signal":"facebook_review"}
                   for rv in r.json().get("data",[])
                   if rv.get("rating",0) >= 4 and rv.get("review_text","")]
            print(f"  Facebook: {len(out)} reviews"); return out
    except Exception as e:
        print(f"  Facebook failed: {e}")
    return []

# ── Review → HubSpot matching ─────────────────────────────────────────────────
PIMS_MATCH = {
    "avimark":["avimark"],"cornerstone":["cornerstone"],"impromed":["impromed"],
    "intravet":["intravet"],"ezyvet":["ezyvet","ezy vet"],"dvmax":["dvmax"],
    "pulse":["covetrus pulse","pulse"],"advantage":["advantage"],
    "neo":["neo vet","neovet"],"rhapsody":["rhapsody"],
    "hippomanager":["hippo","hipposoft"],
}

MANUAL_REVIEW_MATCHES = {
    "christopher m., ceo": "21507806557",  # Hefner Road Animal Hospital
    "heidi t.":            "18856205671",  # Covina Animal Hospital
    "heather w.":          "6250277934",   # Embrace Animal Hospital
    "donna r.":            "5649619997",   # Cimarron Canyon Mobile Vet
    "anne s.":             "13566081857",  # Cruisin' Vet, Happy Pet
    "emily p.":            "30735787553",  # Oceana Veterinary Clinic
    "tienne g.":           "4462711240",   # Hoffman Veterinary Clinic
}

def names_match(a: str, b: str) -> bool:
    a, b = a.lower().strip(), b.lower().strip()
    if not a or not b: return False
    if a in b or b in a: return True
    wa = {w for w in re.split(r'\W+', a) if len(w) >= 4}
    wb = {w for w in re.split(r'\W+', b) if len(w) >= 4}
    return len(wa & wb) >= 2

def build_review_matches(all_external: list, hs_customers: list) -> dict:
    matches, unmatched = {}, 0
    for ext in all_external:
        text     = (ext.get("text") or "").lower()
        reviewer = (ext.get("reviewer") or ext.get("author") or "").lower()
        pims_txt = ext.get("pims","")
        best_id, best_score = None, 0
        for customer in hs_customers:
            p     = customer.get("properties",{})
            hs_id = str(customer["id"])
            name  = (p.get("name") or "").lower()
            city  = (p.get("city") or "").lower()
            st    = (p.get("state") or "").lower()
            pims  = (p.get("current_pims") or "").lower()
            score = 0
            if reviewer and names_match(name, reviewer): score += 10
            if name and name in text:                    score += 8
            if reviewer and len(reviewer) > 4 and reviewer in name: score += 8
            for pk, aliases in PIMS_MATCH.items():
                if pk in pims and any(a in text + " " + pims_txt.lower() for a in aliases):
                    score += 6; break
            if city and len(city) > 3 and city in text: score += 4
            if st   and len(st)   > 1 and st   in text: score += 2
            if score > best_score: best_score = score; best_id = hs_id
        if best_id and best_score >= 8:
            matches.setdefault(best_id,[]).append(ext)
        else:
            unmatched += 1
    print(f"  Reviews matched: {sum(len(v) for v in matches.values())}, unmatched: {unmatched}")
    return matches

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            existing: list = json.load(f)
    except FileNotFoundError:
        existing = []

    by_hs_id = {str(a["hsId"]): a for a in existing if a.get("hsId")}
    by_name  = {a["name"].lower().strip(): a for a in existing}
    original = json.dumps(existing, sort_keys=True)
    print(f"Existing: {len(existing)} advocates\n")

    # ── Gather all signals ────────────────────────────────────────────────────
    print("── Intercom ───────────────────────────────────────────────")
    ic_sigs      = fetch_intercom_csat()
    ic_negative  = fetch_intercom_negative()

    print("\n── Slack ──────────────────────────────────────────────────")
    slack_pos, slack_neg = fetch_slack_signals()

    print("\n── Fathom ─────────────────────────────────────────────────")
    fathom_pos, fathom_neg = fetch_fathom_signals()

    all_negative = ic_negative | slack_neg | fathom_neg

    print("\n── Review sites ───────────────────────────────────────────")
    all_reviews = (scrape_capterra() + scrape_g2() + scrape_software_advice()
                   + scrape_getapp() + scrape_trustpilot())

    print("\n── Google ─────────────────────────────────────────────────")
    g_reviews  = fetch_google_reviews()
    g_mentions = fetch_google_web_mentions()

    print("\n── Facebook ───────────────────────────────────────────────")
    fb = fetch_facebook_reviews()

    all_external = all_reviews + g_reviews + g_mentions + fb

    print("\n── HubSpot referral network ───────────────────────────────")
    referral_net, referral_ids = hs_fetch_referral_network()

    print("\n── HubSpot Onboarding pipeline ────────────────────────────")
    ob_pipeline_id, ob_cs_stage_id = hs_fetch_onboarding_pipeline_config()
    onboarding_exclude = hs_get_active_onboarding_company_ids(ob_pipeline_id, ob_cs_stage_id)

    print("\n── HubSpot Sales pipeline (customer-since dates) ──────────")
    sales_pipeline_id  = hs_fetch_sales_pipeline_id()
    sales_closedate_map = hs_build_sales_closedate_map(sales_pipeline_id)

    print("\n── HubSpot customers ──────────────────────────────────────")
    hs_customers = hs_get_customers()

    print("\n── Matching reviews to HubSpot companies ──────────────────")
    review_matches = build_review_matches(all_external, hs_customers)
    for ext in all_external:
        rkey = (ext.get("reviewer") or ext.get("contact") or "").lower().strip()
        for mkey, hs_id in MANUAL_REVIEW_MATCHES.items():
            if mkey in rkey or rkey in mkey or names_match(rkey, mkey):
                if ext not in review_matches.get(hs_id,[]):
                    review_matches.setdefault(hs_id,[]).append(ext)

    # ── Process each HubSpot company ─────────────────────────────────────────
    new_advocates  = []
    added, updated = [], []
    excl_signal, excl_bad = 0, 0
    next_id = max((a.get("id",0) for a in existing), default=100) + 1

    for customer in hs_customers:
        hs_id = str(customer["id"])
        props = customer.get("properties",{})
        name  = (props.get("name") or "").strip()
        if not name: continue

        if is_excluded_non_clinic(props): continue
        if not is_north_america(props):   continue
        if is_negative_props(props):      excl_bad += 1; continue
        name_lc = name.lower().strip()
        if name_lc in all_negative or any(names_match(name,b) for b in all_negative):
            excl_bad += 1
            print(f"  ✗ Negative: {name}")
            continue

        # Exclude companies still in active onboarding (haven't reached CS stage yet)
        if hs_id in onboarding_exclude:
            excl_bad += 1
            print(f"  ✗ Still onboarding: {name}")
            continue

        # ── Collect strong signals + their source URLs ────────────────────────
        strong_signals = []
        signal_urls    = {}   # signal → direct link to the source that verified it

        # Referral network: company ID match first (exact), name fallback second
        if hs_id in referral_ids:
            strong_signals.append("hs_referral_network")
            signal_urls["hs_referral_network"] = (
                f"https://app.hubspot.com/contacts/{HS_ACCOUNT_ID}/company/{hs_id}"
            )
        elif any(names_match(name, rn_key) for rn_key in referral_net):
            strong_signals.append("hs_referral_network")
            signal_urls["hs_referral_network"] = (
                f"https://app.hubspot.com/contacts/{HS_ACCOUNT_ID}/company/{hs_id}"
            )

        for ic_key in ic_sigs:
            if names_match(name, ic_key):
                strong_signals.append("intercom_csat")
                if ic_sigs[ic_key].get("url"):
                    signal_urls["intercom_csat"] = ic_sigs[ic_key]["url"]
                break

        # Slack: require 2+ words of 5+ chars to overlap
        for s_key in slack_pos:
            s_words = {w for w in re.split(r'\W+', s_key) if len(w) >= 5}
            n_words = {w for w in re.split(r'\W+', name_lc) if len(w) >= 5}
            if len(s_words) >= 2 and len(s_words & n_words) >= 2:
                strong_signals.append("slack_mention")
                if slack_pos[s_key].get("permalink"):
                    signal_urls["slack_mention"] = slack_pos[s_key]["permalink"]
                break

        for f_key in fathom_pos:
            if names_match(name, f_key):
                strong_signals.append("fathom_call")
                if fathom_pos[f_key].get("url"):
                    signal_urls["fathom_call"] = fathom_pos[f_key]["url"]
                break

        matched_quotes = []
        for ext in review_matches.get(hs_id,[]):
            strong_signals.append(ext["signal"])
            if ext.get("text"): matched_quotes.append(ext["text"])
            # Static review site URLs — link to Digitail's listing page on that platform
            if ext["signal"] in SIGNAL_STATIC_URLS and ext["signal"] not in signal_urls:
                signal_urls[ext["signal"]] = SIGNAL_STATIC_URLS[ext["signal"]]

        # External review fallback: require substantial multi-word overlap
        for ext in all_external:
            rev = ext.get("reviewer","") or ext.get("author","") or ""
            rev_words  = {w for w in re.split(r'\W+', rev.lower()) if len(w) >= 5}
            name_words = {w for w in re.split(r'\W+', name_lc) if len(w) >= 5}
            is_solid_match = len(rev_words & name_words) >= 2
            if rev and len(rev) > 10 and is_solid_match and ext not in review_matches.get(hs_id,[]):
                strong_signals.append(ext["signal"])
                if ext.get("text"): matched_quotes.append(ext["text"])
                if ext["signal"] in SIGNAL_STATIC_URLS and ext["signal"] not in signal_urls:
                    signal_urls[ext["signal"]] = SIGNAL_STATIC_URLS[ext["signal"]]

        # Gate: must have at least one verified strong signal
        if not strong_signals:
            excl_signal += 1
            continue

        # ── Collect context signals (popup only) ──────────────────────────────
        context_signals = hs_context_signals(props)

        # ── Find or create record ─────────────────────────────────────────────
        rec = by_hs_id.get(hs_id)
        if not rec:
            for k, v in by_name.items():
                if names_match(name, k):
                    rec = dict(v); break
        is_new = rec is None
        if is_new:
            rec = {"id":next_id,"name":name,"ct":"general","src":"HubSpot",
                   "verify":False,"approx":False,"quote":None,"metrics":None,
                   "pm":None,"aiAdopter":None,"lat":None,"lng":None,
                   "dgtId":None,"dvms":None,"customerSince":None,"signalUrls":{}}
            next_id += 1
        else:
            rec = dict(rec)
            rec["notes"] = None  # clear legacy "Location unknown" notes

        # ── Refresh from HubSpot + deal properties ────────────────────────────
        rec["hsId"]    = hs_id
        rec["name"]    = name
        rec["format"]  = infer_format(name)
        rec["metrics"] = None

        deal_contact, deal_props = best_contact_and_deal_props(hs_id)

        # Store DVM count from deal
        if deal_props.get("dvms"):
            rec["dvms"] = deal_props["dvms"]

        # Customer since — Sales pipeline closed-won deals ONLY.
        # CS expansion, upsell, and any other pipeline dates are explicitly excluded.
        sales_closedate = sales_closedate_map.get(hs_id)
        if sales_closedate:
            rec["customerSince"] = sales_closedate
            try:
                since_dt = datetime.fromisoformat(sales_closedate + "T00:00:00+00:00")
                if (datetime.now(timezone.utc) - since_dt).days > 365:
                    context_signals.append("hs_long_tenure")
            except Exception:
                pass

        if deal_contact:
            if deal_contact.get("email"): rec["email"] = deal_contact["email"]
            if deal_contact.get("phone"): rec["phone"] = deal_contact["phone"]
            fn = deal_contact.get("firstname","")
            ln = deal_contact.get("lastname","")
            jt = (deal_contact.get("jobtitle") or "").lower()
            if fn or ln:
                full = f"{fn} {ln}".strip()
                rec["contact"] = f"Dr. {full}" if any(k in jt for k in ["dvm","veterinarian","doctor"]) else full
        else:
            for src, dest in [("contact_email","email"),("phone","phone")]:
                v = (props.get(src) or "").strip()
                if v: rec[dest] = v

        for src, dest in [("city","city"),("state","st"),("current_pims","pims")]:
            v = (props.get(src) or "").strip()
            if v: rec[dest] = v

        raw  = (props.get("address") or "").strip()
        city = (props.get("city")    or "").strip().title()
        st   = (props.get("state")   or "").strip().upper()
        zip_ = (props.get("zip")     or "").strip()
        raw_ok = raw and len(raw) < 80 and "po box" not in raw.lower()
        parts  = [raw, city, st, zip_] if raw_ok else [city, st, zip_]
        rec["address"] = ", ".join(p for p in parts if p)

        lat, lng = geocode(props, deal_contact or None)
        if lat: rec["lat"], rec["lng"] = lat, lng; rec["approx"] = False

        if "manual" in rec.get("signals",[]): strong_signals.append("manual")
        all_sigs        = sorted(set(strong_signals + context_signals))
        rec["signals"]  = all_sigs
        rec["verified"] = True
        rec["signalUrls"] = signal_urls  # signal → source URL for hyperlinks in popup

        for ic_key, ic_val in ic_sigs.items():
            if names_match(name, ic_key) and ic_val.get("quote"):
                rec["quote"] = ic_val["quote"]; break
        if not rec.get("quote") and matched_quotes:
            rec["quote"] = matched_quotes[0]

        if is_new:
            added.append(name); print(f"  + New: {name}")
        else:
            updated.append(name)
        new_advocates.append(rec)

    # ── Preserve non-HubSpot records (manual and public review sources only) ──
    # Every preserved record is evaluated against all current negative signals —
    # the same check the main HubSpot loop applies. An existing advocate with a
    # negative Slack mention, Intercom bad rating, or Fathom negative call is
    # removed here regardless of its original qualifying signal.
    hs_ids_in = {str(a.get("hsId","")) for a in new_advocates}
    names_in  = {a["name"].lower() for a in new_advocates}
    excl_neg_preserved = 0
    for old in existing:
        already = (str(old.get("hsId","")) in hs_ids_in or old["name"].lower() in names_in)
        if not already:
            old_name = (old.get("name") or "")
            # Full negative check — same logic as the HubSpot main loop
            if (old_name.lower() in all_negative or
                    any(names_match(old_name, b) for b in all_negative)):
                excl_neg_preserved += 1
                print(f"  ✗ Negative (preserved): {old_name}")
                continue
            # Remove referral signal — can only be verified via live HubSpot lookup
            cleaned_signals = [s for s in old.get("signals",[]) if s != "hs_referral_network"]
            old = dict(old)
            old["signals"] = cleaned_signals
            old_strong = [s for s in cleaned_signals if s in STRONG_SIGNALS]
            if old_strong or old.get("src","") in ("manual","Capterra","Intercom CSAT"):
                new_advocates.append(old)

    new_advocates.sort(key=lambda a: a.get("name",""))

    print(f"\nExcluded: {excl_signal} (no strong signal), "
          f"{excl_bad} (negative), {excl_neg_preserved} (negative — preserved records)")

    new_json = json.dumps(new_advocates, sort_keys=True)
    if new_json != original:
        with open(DATA_FILE,"w",encoding="utf-8") as f:
            json.dump(new_advocates, f, indent=2, ensure_ascii=False)
        print(f"\n✅ {len(new_advocates)} verified advocates saved "
              f"({len(added)} new, {len(updated)} refreshed)")
    else:
        print(f"\n✅ No changes — {len(new_advocates)} advocates current")

    if SLACK_URL:
        pinned = sum(1 for a in new_advocates if a.get("lat"))
        lines  = ["*🐾 Digitail Advocate Map — Weekly Refresh*",
                  f"Verified: *{len(new_advocates)}* | Pinned on map: *{pinned}*"]
        if added:   lines.append(f"✅ *{len(added)} new:* " + ", ".join(added[:6]))
        if updated: lines.append(f"📝 Updated: " + ", ".join(updated[:6]) +
                                  (f" +{len(updated)-6} more" if len(updated)>6 else ""))
        lines.append(f"🚫 Excluded: {excl_signal} no signal · {excl_bad} negative · {excl_neg_preserved} negative (preserved)")
        if not added and not updated: lines.append("No changes this week ✓")
        lines.append(f"_Run: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC_")
        requests.post(SLACK_URL, json={"text":"\n".join(lines)}, timeout=10)

if __name__ == "__main__":
    main()
