#!/usr/bin/env python3
"""
Digitail Advocate Map — Weekly Update Script

Verification: a clinic appears ONLY if it has ≥1 STRONG signal.
Strong signals: referral network opt-in, external reviews, 5★ CSAT,
  DSP processing, published testimonial, Slack/Fathom positive mention.
Weak signals (tenure, positive notes): shown in popup but not sufficient alone.
Negative signals (bad CSAT, negative HubSpot notes): hard exclusion.
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
DATA_FILE       = "advocates.json"

SLACK_CHANNELS_TO_SCAN = [
    "sales-general", "cs-general", "corporate-athletes",
    "customer-success", "churn-risk",
]

# ── Signals ───────────────────────────────────────────────────────────────────
SIGNAL_LABELS = {
    "hs_referral_network": "In Digitail Referral Network",
    "hs_testimonial":      "Case Study / Article Published",
    "hs_dsp":              "DSP Payment Processing",
    "hs_positive_note":    "Team Verified Happy",
    "hs_long_tenure":      "Active Customer 12+ Months",
    "intercom_csat":       "5★ Support Rating",
    "capterra_positive":   "Capterra Review",
    "g2_positive":         "G2 Review",
    "softwareadvice":      "Software Advice Review",
    "getapp":              "GetApp Review",
    "reddit_mention":      "Reddit Mention",
    "google_review":       "Google Review",
    "google_mention":      "Web Mention",
    "facebook_review":     "Facebook Review",
    "slack_mention":       "Positive Slack Mention",
    "fathom_call":         "Positive Customer Call (Fathom)",
    "manual":              "Manually Verified",
}

# Signals strong enough to qualify a clinic for the map ON THEIR OWN.
# Must represent an explicit positive action by the customer or team.
# Passive signals (tenure, DSP usage, generic notes) do NOT qualify.
STRONG_SIGNALS = {
    "hs_referral_network",  # Opted in to referral program
    "hs_testimonial",       # Featured in published article, video, or case study
    "intercom_csat",        # Gave a 5★ support rating
    "capterra_positive",    # Wrote a public Capterra review
    "g2_positive",          # Wrote a public G2 review
    "softwareadvice",       # Wrote a public Software Advice review
    "getapp",               # Wrote a public GetApp review
    "google_review",        # Wrote a Google review
    "facebook_review",      # Wrote a Facebook review
    "fathom_call",          # CS team noted positive call (Fathom)
    "slack_mention",        # Team mentioned positively in Slack
    "manual",               # Manually added by Digitail team
}
# These appear in the popup as context but cannot qualify a clinic alone
CONTEXT_ONLY_SIGNALS = {
    "hs_dsp",           # Uses DSP — usage metric, not happiness
    "hs_long_tenure",   # Been a customer 12+ months — not happiness
    "hs_positive_note", # Positive note in HubSpot — too loose
    "google_mention",   # Web mention — too broad/unreliable
    "reddit_mention",   # Reddit mention — too unreliable
}

NEGATIVE_KW = [
    "not happy", "unhappy", "at risk", "churn", "cancel", "triple check",
    "do not contact", "rocky", "leaving", "switching away", "terrible", "awful",
    "disappointed", "frustrated", "refund", "dispute",
]
POSITIVE_KW = [
    "great", "love", "loves", "happy", "excellent", "recommend", "advocate",
    "good experience", "amazing", "fantastic", "worth it", "switched", "best",
]

# ── Practice format ───────────────────────────────────────────────────────────
MOBILE_TERMS = [
    "mobile", "house call", "housecall", "traveling", "on wheels", "doorstep",
    "home visit", "home vet", "at home", "at-home", "on-site", "onsite",
    "wagon", "roaming", "wandervet", "doggie motion", "paws on the move",
    "fetch the vet", "rideau river", "clinic nomad", "on the road",
    "road vet", "rolling",
]
TELE_TERMS = ["tele", "virtual", "online", "remote"]

def infer_format(name: str) -> str:
    n = name.lower()
    if any(t in n for t in TELE_TERMS):  return "telemedicine"
    if any(t in n for t in MOBILE_TERMS): return "mobile"
    return "bnm"

# ── Non-clinic exclusions ─────────────────────────────────────────────────────
EXCLUDE_NAME_FRAGMENTS = [
    "stripe", "care credit", "carecredit", "vetsource", "ellie diagnostics",
    "ma department of higher education", "hillsborough community college",
    "genesee community college", "marian university", "trocaire college",
    "university of arizona", "vermont state university", "trooper pet",
    "kindred", "animall", "kumba", "jdam", "hound app", "pet nation",
    "semper k9", "national mill dog rescue", "animal welfare society",
    "the greyhound health initiative", "elephant aid", "greyhound health",
]
EXCLUDE_DOMAINS = {"stripe.com", "carecredit.com", "vetsource.com"}

# ── Geography ─────────────────────────────────────────────────────────────────
NA_COUNTRIES = {"united states", "us", "usa", "canada", "ca", "mexico", "mx", ""}
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
    if cl in {"canada", "ca"}:   return lat > 41.5 and lng < -52.0
    if cl in {"mexico", "mx"}:   return 14.5 <= lat <= 32.7 and -118.0 <= lng <= -86.0
    return True

def is_negative_props(props: dict) -> bool:
    notes = (props.get("internal_comments") or "").lower()
    return any(k in notes for k in NEGATIVE_KW)

def is_excluded_non_clinic(props: dict) -> bool:
    name   = (props.get("name")   or "").lower()
    domain = (props.get("domain") or "").lower()
    return (any(f in name for f in EXCLUDE_NAME_FRAGMENTS) or
            any(d in domain for d in EXCLUDE_DOMAINS))

# ── HubSpot helpers ───────────────────────────────────────────────────────────
HS_PROPS = [
    "name", "address", "city", "state", "zip", "country",
    "contact_email", "phone", "current_pims", "domain", "createdate",
    "media_testimonials_dsp", "internal_comments",
]

def hs_h():
    return {"Authorization": f"Bearer {HS_TOKEN}", "Content-Type": "application/json"}

def hs_signals(props: dict) -> list:
    sigs  = []
    media = (props.get("media_testimonials_dsp") or "").lower()
    notes = (props.get("internal_comments")      or "").lower()
    if any(k in media for k in ["article", "video", "testimonial"]):
        sigs.append("hs_testimonial")
    if any(k in media for k in ["dsp", "6 figure", "6-figure"]):
        sigs.append("hs_dsp")
    if any(k in notes for k in POSITIVE_KW) and not any(k in notes for k in NEGATIVE_KW):
        sigs.append("hs_positive_note")
    try:
        created = props.get("createdate", "")
        if created:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - dt).days > 365:
                sigs.append("hs_long_tenure")
    except Exception:
        pass
    return list(set(sigs))

def build_address_display(props: dict) -> str:
    """Clean display address for popup — avoids garbled HubSpot address fields."""
    raw  = (props.get("address") or "").strip()
    city = (props.get("city")    or "").strip().title()
    st   = (props.get("state")   or "").strip().upper()
    zip_ = (props.get("zip")     or "").strip()
    raw_clean = (raw and len(raw) < 80
                 and "po box" not in raw.lower()
                 and "p.o. box" not in raw.lower())
    parts = [raw, city, st, zip_] if raw_clean else [city, st, zip_]
    return ", ".join(p for p in parts if p)

def build_geocode_query(props: dict, contact: dict = None) -> str:
    """ZIP-first geocode query. Contact location takes priority over company."""
    def norm(c):
        return {"us":"United States","usa":"United States","ca":"Canada",
                "canada":"Canada","mx":"Mexico","mexico":"Mexico"}.get(
                (c or "").lower().strip(), c or "United States")
    co_zip  = (props.get("zip")     or "").strip()
    co_city = (props.get("city")    or "").strip().title()
    co_st   = (props.get("state")   or "").strip().upper()
    co_ctry = norm(props.get("country", ""))
    ct      = contact or {}
    ct_zip  = (ct.get("zip")   or "").strip()
    ct_city = (ct.get("city")  or "").strip().title()
    ct_st   = (ct.get("state") or "").strip().upper()
    ct_ctry = norm(ct.get("country", "")) if ct else co_ctry
    state   = ct_st   or co_st
    country = ct_ctry or co_ctry
    if ct_zip  and state: return f"{ct_zip}, {state}, {country}"
    if co_zip  and state: return f"{co_zip}, {state}, {country}"
    if ct_city and state: return f"{ct_city}, {state}, {country}"
    if co_city and state: return f"{co_city}, {state}, {country}"
    if state:             return f"{state}, {country}"
    return ""

def geocode_google(query: str):
    if not GOOGLE_KEY or not query: return None, None
    try:
        r = requests.get("https://maps.googleapis.com/maps/api/geocode/json",
            params={"address": query, "key": GOOGLE_KEY}, timeout=10)
        if r.status_code == 200:
            res = r.json().get("results", [])
            if res:
                loc = res[0]["geometry"]["location"]
                return round(loc["lat"], 5), round(loc["lng"], 5)
    except Exception as e:
        print(f"  Google geocode failed '{query}': {e}")
    return None, None

def geocode_nominatim(query: str):
    if not query: return None, None
    time.sleep(1.2)
    try:
        r = requests.get("https://nominatim.openstreetmap.org/search",
            params={"format": "json", "q": query, "limit": 1},
            headers={"User-Agent": "DigitailAdvocateMap/4.0"}, timeout=10)
        if r.status_code == 200:
            d = r.json()
            if d: return round(float(d[0]["lat"]), 5), round(float(d[0]["lon"]), 5)
    except Exception as e:
        print(f"  Nominatim failed '{query}': {e}")
    return None, None

def geocode(props: dict, contact: dict = None):
    """Geocode using ZIP-first strategy. Validates NA bounds + country match."""
    query = build_geocode_query(props, contact)
    if not query: return None, None
    lat, lng = geocode_google(query) if GOOGLE_KEY else geocode_nominatim(query)
    if lat:
        country = (props.get("country") or "").strip()
        if in_na_bounds(lat, lng) and coord_matches_country(lat, lng, country):
            return lat, lng
        print(f"  Geocode rejected: {query} → {lat},{lng}")
    return None, None

# ── HubSpot data pulls ────────────────────────────────────────────────────────
def hs_fetch_referral_network() -> dict:
    """Pull contacts with reference_program_optin=true. Gold standard signal."""
    if not HS_TOKEN: return {}
    results, after = {}, None
    while True:
        payload = {
            "filterGroups": [{"filters": [{"propertyName": "reference_program_optin",
                                           "operator": "EQ", "value": "true"}]}],
            "properties": ["firstname","lastname","email","phone","company",
                           "jobtitle","city","state","zip","country"],
            "limit": 100,
        }
        if after: payload["after"] = after
        r = requests.post("https://api.hubapi.com/crm/v3/objects/contacts/search",
                          json=payload, headers=hs_h(), timeout=15)
        if r.status_code != 200:
            print(f"  Referral network error: {r.status_code}"); break
        data = r.json()
        for contact in data.get("results", []):
            p  = contact.get("properties", {})
            co = (p.get("company") or "").strip()
            if co and co.lower() not in results:
                results[co.lower()] = {"contact_props": p, "contact_id": contact["id"]}
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after: break
    print(f"  Referral network: {len(results)} opted-in companies")
    return results

def hs_get_deal_contacts(deal_id: str) -> list:
    """Get contacts associated with a deal, sorted by seniority."""
    try:
        r = requests.get(
            f"https://api.hubapi.com/crm/v3/objects/deals/{deal_id}/associations/contacts",
            headers=hs_h(), timeout=10)
        if r.status_code != 200: return []
        ids = [c["id"] for c in r.json().get("results", [])]
        if not ids: return []
        cr = requests.post("https://api.hubapi.com/crm/v3/objects/contacts/batch/read",
            json={"inputs": [{"id": i} for i in ids[:5]],
                  "properties": ["firstname","lastname","email","phone","jobtitle",
                                 "city","state","zip","country"]},
            headers=hs_h(), timeout=10)
        if cr.status_code != 200: return []
        contacts = [c.get("properties", {}) for c in cr.json().get("results", [])]
        def rank(c):
            jt = (c.get("jobtitle") or "").lower()
            if any(k in jt for k in ["owner","dvm","veterinarian","doctor","ceo"]): return 0
            if any(k in jt for k in ["manager","director","admin","practice"]):     return 1
            return 2
        return sorted(contacts, key=rank)
    except Exception as e:
        print(f"  Deal contact fetch failed {deal_id}: {e}")
        return []

def best_contact_from_deals(hs_id: str) -> dict:
    """Best contact from the company's associated deals."""
    try:
        r = requests.get(
            f"https://api.hubapi.com/crm/v3/objects/companies/{hs_id}/associations/deals",
            headers=hs_h(), timeout=10)
        if r.status_code != 200: return {}
        for deal in r.json().get("results", [])[:3]:
            contacts = hs_get_deal_contacts(deal["id"])
            if contacts: return contacts[0]
    except Exception:
        pass
    return {}

def hs_get_closedwon_company_ids() -> set:
    ids = set()
    for filt in [{"propertyName": "hs_is_closed_won", "operator": "EQ", "value": "true"},
                 {"propertyName": "dealstage",         "operator": "EQ", "value": "closedwon"}]:
        after = None
        while True:
            payload = {"filterGroups": [{"filters": [filt]}],
                       "properties": ["dealname"], "limit": 200}
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
                            ids.add(str(assoc.get("toObjectId", "")))
                time.sleep(0.1)
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after: break
        if ids: break
    print(f"  HubSpot closed-won: {len(ids)} companies")
    return ids

def hs_get_customers() -> list:
    closed_won = hs_get_closedwon_company_ids()
    by_id      = {}
    for filt in [{"propertyName": "lifecyclestage",      "operator": "EQ", "value": "customer"},
                 {"propertyName": "hs_current_customer", "operator": "EQ", "value": "true"}]:
        after, batch = None, {}
        while True:
            payload = {"filterGroups": [{"filters": [filt]}],
                       "properties": HS_PROPS, "limit": 100}
            if after: payload["after"] = after
            r = requests.post("https://api.hubapi.com/crm/v3/objects/companies/search",
                              json=payload, headers=hs_h(), timeout=15)
            if r.status_code != 200: break
            data = r.json()
            for c in data.get("results", []): batch[c["id"]] = c
            after = data.get("paging", {}).get("next", {}).get("after")
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
            json={"inputs": [{"id": cid} for cid in list(new_ids)[i:i+100]],
                  "properties": HS_PROPS},
            headers=hs_h(), timeout=15)
        if r.status_code == 200:
            for c in r.json().get("results", []): by_id[c["id"]] = c
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
            cos = d.get("companies", {}).get("data", [])
            return cos[0].get("name", "") if cos else (d.get("name", "") or "")
    except Exception:
        pass
    return ""

def _ic_extract(convo: dict, headers: dict, results: dict):
    robj     = convo.get("conversation_rating") or {}
    remark   = (robj.get("remark") or "").strip()
    contacts = convo.get("contacts", {}).get("contacts", [])
    name     = _ic_name(contacts[0]["id"], headers) if contacts else ""
    if name:
        key = name.lower().strip()
        if key not in results:
            results[key] = {"signal": "intercom_csat",
                            "quote":  remark[:300] if remark else None}

def fetch_intercom_csat() -> dict:
    if not IC_TOKEN: return {}
    hdrs = {"Authorization": f"Bearer {IC_TOKEN}", "Accept": "application/json",
            "Intercom-Version": "2.10"}
    results = {}
    for field, val in [("conversation_rating.rating","amazing"), ("rating","amazing")]:
        try:
            r = requests.post("https://api.intercom.io/conversations/search",
                json={"query":{"operator":"AND","value":[{"field":field,"operator":"=","value":val}]},
                      "pagination":{"per_page":150}},
                headers=hdrs, timeout=30)
            if r.status_code == 200:
                convos = r.json().get("conversations", [])
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
            for c in r.json().get("conversations", []):
                robj = c.get("conversation_rating") or {}
                if str(robj.get("value","") or robj.get("rating","")) in ["5","amazing","great"]:
                    _ic_extract(c, hdrs, results)
    except Exception as e:
        print(f"  Intercom fallback failed: {e}")
    print(f"  Intercom (fallback): {len(results)} CSAT records")
    return results

def fetch_intercom_negative() -> set:
    if not IC_TOKEN: return set()
    hdrs = {"Authorization": f"Bearer {IC_TOKEN}", "Accept": "application/json",
            "Intercom-Version": "2.10"}
    bad = set()
    for field, val in [("conversation_rating.rating","terrible"),
                       ("conversation_rating.rating","bad"),
                       ("rating","terrible"), ("rating","bad")]:
        try:
            r = requests.post("https://api.intercom.io/conversations/search",
                json={"query":{"operator":"AND","value":[{"field":field,"operator":"=","value":val}]},
                      "pagination":{"per_page":100}},
                headers=hdrs, timeout=30)
            if r.status_code == 200:
                convos = r.json().get("conversations", [])
                if convos:
                    tmp = {}
                    for c in convos: _ic_extract(c, hdrs, tmp)
                    bad.update(tmp.keys())
                    break
        except Exception:
            continue
    if bad: print(f"  Intercom negative: {len(bad)} companies flagged")
    return bad

# ── Slack channel scanning ────────────────────────────────────────────────────
def fetch_slack_signals() -> tuple:
    """Returns (positive_dict, negative_set) from scanning public channels."""
    if not SLACK_BOT_TOKEN:
        print("  Slack: no token, skipping")
        return {}, set()
    hdrs     = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    positive, negative = {}, set()
    cutoff   = int((datetime.now(timezone.utc) - timedelta(days=30)).timestamp())
    try:
        r        = requests.get("https://slack.com/api/conversations.list",
                    params={"types":"public_channel","limit":200,"exclude_archived":"true"},
                    headers=hdrs, timeout=10)
        channels = {c["name"]: c["id"] for c in r.json().get("channels", [])}
    except Exception as e:
        print(f"  Slack channel list failed: {e}"); return {}, set()
    scanned = 0
    for ch_name in SLACK_CHANNELS_TO_SCAN:
        ch_id = channels.get(ch_name)
        if not ch_id: continue
        try:
            r = requests.get("https://slack.com/api/conversations.history",
                params={"channel":ch_id,"oldest":cutoff,"limit":200},
                headers=hdrs, timeout=15)
            if not r.json().get("ok"): continue
            for msg in r.json().get("messages", []):
                text = msg.get("text", "")
                if not text or len(text) < 15: continue
                tl      = text.lower()
                is_pos  = any(k in tl for k in POSITIVE_KW)
                is_neg  = any(k in tl for k in NEGATIVE_KW)
                scanned += 1
                for word in re.findall(r'\b[A-Z][a-zA-Z]{3,}(?:\s[A-Z][a-zA-Z]{3,})*\b', text):
                    if len(word) > 5 and word.lower() not in {"slack","digitail","tails","monday","friday"}:
                        if is_pos and not is_neg:
                            positive.setdefault(word.lower(), {"signal":"slack_mention","quote":text[:280],"channel":ch_name})
                        elif is_neg and not is_pos:
                            negative.add(word.lower())
        except Exception as e:
            print(f"  Slack #{ch_name} failed: {e}")
    print(f"  Slack: {scanned} messages → {len(positive)} positive, {len(negative)} negative")
    return positive, negative

# ── Fathom call intelligence ──────────────────────────────────────────────────
def fetch_fathom_signals() -> tuple:
    """
    Pull recent Fathom call summaries from CS team members only.
    Filters by FATHOM_CS_EMAILS — calls owned by anyone not on that list are skipped.
    If FATHOM_CS_EMAILS is empty, falls back to any @digitail.io host.
    Returns (positive_dict, negative_set).
    """
    if not FATHOM_API_KEY:
        print("  Fathom: no API key, skipping")
        return {}, set()
    hdrs   = {"Authorization": f"Bearer {FATHOM_API_KEY}", "Content-Type": "application/json"}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d")
    positive, negative = {}, set()
    try:
        r = requests.get("https://api.fathom.ai/v1/calls",
                         params={"limit": 100, "after": cutoff},
                         headers=hdrs, timeout=15)
        if r.status_code != 200:
            print(f"  Fathom: HTTP {r.status_code}"); return {}, set()
        calls = r.json().get("data", r.json().get("calls", []))
        print(f"  Fathom: {len(calls)} total calls retrieved")

        cs_filtered = 0
        for call in calls:
            # ── CS team filter ────────────────────────────────────────────────
            owner_email = ""
            owner       = call.get("owner") or call.get("host") or call.get("user") or {}
            if isinstance(owner, dict):
                owner_email = (owner.get("email") or "").lower().strip()
            elif isinstance(owner, str):
                owner_email = owner.lower().strip()

            if FATHOM_CS_EMAILS:
                # Strict: skip if owner not in CS team list
                if owner_email not in FATHOM_CS_EMAILS:
                    cs_filtered += 1
                    continue
            else:
                # Fallback: skip calls with no @digitail.io host at all
                attendees   = call.get("attendees", [])
                internal    = [a.get("email","") for a in attendees
                               if "@digitail.io" in (a.get("email") or "")]
                if not internal and "@digitail.io" not in owner_email:
                    cs_filtered += 1
                    continue

            # ── Extract summary and classify ──────────────────────────────────
            call_id = call.get("id", "")
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

            # ── Extract company candidates ────────────────────────────────────
            candidates = set()
            for pat in [r'(?:with|re:|follow.?up|check.?in)\s+([A-Z][^\-–|:]+)',
                        r'^([A-Z][a-zA-Z\s]{4,40})\s*[-–|:]']:
                m = re.search(pat, title)
                if m: candidates.add(m.group(1).strip().lower())
            for att in call.get("attendees", []):
                email = (att.get("email") or "")
                if "@" in email and "@digitail.io" not in email:
                    domain = email.split("@")[1].split(".")[0]
                    if len(domain) > 4 and domain not in {"gmail","yahoo","hotmail","outlook"}:
                        candidates.add(domain.lower())

            for cand in candidates:
                if is_pos and not is_neg:
                    positive.setdefault(cand, {
                        "signal":     "fathom_call",
                        "quote":      summary[:280] if summary else f"Positive CS call: {title}",
                        "call_owner": owner_email,
                    })
                elif is_neg and not is_pos:
                    negative.add(cand)

        print(f"  Fathom: {len(calls) - cs_filtered} CS calls processed "
              f"({cs_filtered} skipped — not CS team), "
              f"{len(positive)} positive, {len(negative)} negative signals")
    except Exception as e:
        print(f"  Fathom failed: {e}")
    return positive, negative

# ── Review scrapers ───────────────────────────────────────────────────────────
def _scrape(url, key, card_sels, rating_sels, body_sels, rev_sels, min_r=4.0):
    if not BS4: return []
    out = []
    try:
        r = requests.get(url, headers={"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36","Accept-Language":"en-US,en;q=0.9"}, timeout=15)
        if r.status_code != 200: print(f"  {key}: HTTP {r.status_code}"); return []
        soup  = BeautifulSoup(r.text, "html.parser")
        cards = next((soup.select(s) for s in card_sels if soup.select(s)), [])
        for card in cards:
            try:
                rating = 0.0
                for s in rating_sels:
                    el = card.select_one(s)
                    if el:
                        nums = re.findall(r'(\d+\.?\d*)', el.get("aria-label","") or el.get_text())
                        if nums: rating = float(nums[0]); break
                if rating and rating < min_r: continue
                text = next((card.select_one(s).get_text(" ", strip=True)[:400]
                             for s in body_sels if card.select_one(s)), "")
                if not text or len(text) < 20: continue
                reviewer = next((card.select_one(s).get_text(strip=True)
                                 for s in rev_sels if card.select_one(s)), "")
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

# ── Reddit ────────────────────────────────────────────────────────────────────
def fetch_reddit_mentions() -> list:
    if not REDDIT_ID or not REDDIT_SECRET:
        print("  Reddit: no credentials, skipping"); return []
    results = []
    try:
        tok = requests.post("https://www.reddit.com/api/v1/access_token",
            auth=requests.auth.HTTPBasicAuth(REDDIT_ID, REDDIT_SECRET),
            data={"grant_type":"client_credentials"},
            headers={"User-Agent":"DigitailAdvocateMap/4.0"}, timeout=10).json().get("access_token","")
        if not tok: print("  Reddit: auth failed"); return []
        hdrs = {"Authorization":f"bearer {tok}","User-Agent":"DigitailAdvocateMap/4.0"}
        for q in ["Digitail veterinary software","Digitail PIMS","Digitail vet"]:
            r = requests.get("https://oauth.reddit.com/search",
                params={"q":q,"sort":"new","limit":50,"type":"link,comment"},
                headers=hdrs, timeout=15)
            if r.status_code == 200:
                for post in r.json().get("data",{}).get("children",[]):
                    d    = post.get("data",{})
                    text = d.get("selftext","") or d.get("body","") or d.get("title","")
                    tl   = text.lower()
                    if any(p in tl for p in POSITIVE_KW) and not any(n in tl for n in NEGATIVE_KW):
                        results.append({"source":"reddit","text":text[:400],
                                        "author":d.get("author",""),"signal":"reddit_mention"})
            time.sleep(0.5)
        print(f"  Reddit: {len(results)} positive mentions")
    except Exception as e:
        print(f"  Reddit failed: {e}")
    return results

# ── Google ────────────────────────────────────────────────────────────────────
def fetch_google_reviews() -> list:
    if not GOOGLE_KEY or not GOOGLE_PLACE:
        print("  Google Reviews: no credentials, skipping"); return []
    try:
        r = requests.get("https://maps.googleapis.com/maps/api/place/details/json",
            params={"place_id":GOOGLE_PLACE,"fields":"reviews","key":GOOGLE_KEY,"reviews_sort":"newest"}, timeout=10)
        if r.status_code == 200:
            out = [{"source":"google","reviewer":rv.get("author_name",""),
                    "text":rv["text"][:400],"rating":rv["rating"],"signal":"google_review"}
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
        for q in ['"Digitail" veterinary review','"Digitail" PIMS switched','"Digitail" vet recommend']:
            r = requests.get("https://www.googleapis.com/customsearch/v1",
                params={"q":q,"key":GOOGLE_KEY,"cx":GOOGLE_CSE_ID,"num":10}, timeout=10)
            if r.status_code == 200:
                for item in r.json().get("items",[]):
                    s = item.get("snippet","")
                    if any(p in s.lower() for p in POSITIVE_KW) and not any(n in s.lower() for n in NEGATIVE_KW):
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
            params={"access_token":FB_TOKEN,"fields":"reviewer{name},rating,review_text","limit":50}, timeout=10)
        if r.status_code == 200:
            out = [{"source":"facebook","reviewer":rv.get("reviewer",{}).get("name",""),
                    "text":rv["review_text"][:400],"signal":"facebook_review"}
                   for rv in r.json().get("data",[]) if rv.get("rating",0) >= 4 and rv.get("review_text","")]
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
    "hippomanager":["hippo","hipposoft"],"v-tech":["v-tech","vtech platinum"],
}

MANUAL_REVIEW_MATCHES = {
    "christopher m., ceo": "21507806557",  # Hefner Road Animal Hospital
    "heidi t.":            "18856205671",  # Covina Animal Hospital
    "heather w.":          "6250277934",   # Embrace Animal Hospital
    "donna r.":            "5649619997",   # Cimarron Canyon Mobile Vet
    "anne s.":             "13566081857",  # Cruisin' Vet, Happy Pet
    "emily p.":            "30735787553",  # Oceana Veterinary Clinic
    "tienne g.":           "4462711240",   # Hoffman Veterinary Clinic
    # "alicia b.":         "HUBSPOT_ID",
    # "kaitlynn s.":       "HUBSPOT_ID",
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
        pims_txt = ext.get("pims", "")
        best_id, best_score = None, 0
        for customer in hs_customers:
            p      = customer.get("properties", {})
            hs_id  = str(customer["id"])
            name   = (p.get("name")         or "").lower()
            city   = (p.get("city")         or "").lower()
            st     = (p.get("state")        or "").lower()
            pims   = (p.get("current_pims") or "").lower()
            score  = 0
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
            matches.setdefault(best_id, []).append(ext)
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

    # One-time: clear all coordinates for clean re-geocode
    for rec in existing:
        rec["lat"], rec["lng"] = None, None

    by_hs_id = {str(a["hsId"]): a for a in existing if a.get("hsId")}
    by_name  = {a["name"].lower().strip(): a for a in existing}
    original = json.dumps(existing, sort_keys=True)
    print(f"Existing: {len(existing)} advocates\n")

    # ── Signals from all sources ──────────────────────────────────────────────
    print("── Intercom ───────────────────────────────────────────────")
    ic_sigs      = fetch_intercom_csat()
    ic_negative  = fetch_intercom_negative()

    print("\n── Slack ──────────────────────────────────────────────────")
    slack_pos, slack_neg = fetch_slack_signals()

    print("\n── Fathom ─────────────────────────────────────────────────")
    fathom_pos, fathom_neg = fetch_fathom_signals()

    all_negative = ic_negative | slack_neg | fathom_neg

    print("\n── Review sites ───────────────────────────────────────────")
    all_reviews  = (scrape_capterra() + scrape_g2() + scrape_software_advice()
                    + scrape_getapp() + scrape_trustpilot())

    print("\n── Reddit ─────────────────────────────────────────────────")
    reddit = fetch_reddit_mentions()

    print("\n── Google ─────────────────────────────────────────────────")
    g_reviews  = fetch_google_reviews()
    g_mentions = fetch_google_web_mentions()

    print("\n── Facebook ───────────────────────────────────────────────")
    fb = fetch_facebook_reviews()

    all_external = all_reviews + reddit + g_reviews + g_mentions + fb

    print("\n── HubSpot referral network ───────────────────────────────")
    referral_net = hs_fetch_referral_network()

    print("\n── HubSpot customers ──────────────────────────────────────")
    hs_customers = hs_get_customers()

    print("\n── Matching reviews to HubSpot companies ──────────────────")
    review_matches = build_review_matches(all_external, hs_customers)
    for ext in all_external:
        rkey = (ext.get("reviewer") or ext.get("contact") or "").lower().strip()
        for mkey, hs_id in MANUAL_REVIEW_MATCHES.items():
            if mkey in rkey or rkey in mkey or names_match(rkey, mkey):
                if ext not in review_matches.get(hs_id, []):
                    review_matches.setdefault(hs_id, []).append(ext)
                    print(f"  Manual match: '{rkey}' → {hs_id}")

    # ── Process each HubSpot company ─────────────────────────────────────────
    new_advocates   = []
    added, updated  = [], []
    excl_signal, excl_bad = 0, 0
    next_id = max((a.get("id", 0) for a in existing), default=100) + 1

    for customer in hs_customers:
        hs_id = str(customer["id"])
        props = customer.get("properties", {})
        name  = (props.get("name") or "").strip()
        if not name: continue

        # Hard exclusions
        if is_excluded_non_clinic(props): continue
        if not is_north_america(props):   continue
        if is_negative_props(props):      excl_bad += 1; continue
        name_lc = name.lower().strip()
        if name_lc in all_negative or any(names_match(name, b) for b in all_negative):
            excl_bad += 1
            print(f"  ✗ Negative signal: {name}")
            continue

        # Collect signals
        signals = hs_signals(props)

        for rn_key, rn_val in referral_net.items():
            if names_match(name, rn_key):
                signals.append("hs_referral_network")
                break

        for ic_key in ic_sigs:
            if names_match(name, ic_key):
                signals.append("intercom_csat"); break

        for s_key in slack_pos:
            if names_match(name, s_key):
                signals.append("slack_mention"); break

        for f_key in fathom_pos:
            if names_match(name, f_key):
                signals.append("fathom_call"); break

        matched_quotes = []
        for ext in review_matches.get(hs_id, []):
            signals.append(ext["signal"])
            if ext.get("text"): matched_quotes.append(ext["text"])
        # Also direct name matching
        for ext in all_external:
            rev = ext.get("reviewer","") or ext.get("author","") or ""
            if names_match(name, rev) and ext not in review_matches.get(hs_id,[]):
                signals.append(ext["signal"])
                if ext.get("text"): matched_quotes.append(ext["text"])

        signals = list(set(signals))

        # Hard gate: must have at least one EXPLICIT happiness signal.
        # Tenure, DSP usage, and generic notes do NOT qualify alone.
        qualifying = [s for s in signals if s in STRONG_SIGNALS]
        if not qualifying:
            excl_signal += 1
            continue

        # Find or create record
        rec = by_hs_id.get(hs_id)
        if not rec:
            for k, v in by_name.items():
                if names_match(name, k):
                    rec = dict(v); break
        is_new = rec is None
        if is_new:
            rec = {"id": next_id, "name": name, "ct": "general", "src": "HubSpot",
                   "verify": False, "approx": False, "quote": None, "metrics": None,
                   "pm": None, "aiAdopter": None, "lat": None, "lng": None,
                   "features": None, "dgtId": None}
            next_id += 1
        else:
            rec = dict(rec)

        # Update from HubSpot + deal contact
        rec["hsId"]   = hs_id
        rec["name"]   = name
        rec["format"] = infer_format(name)

        deal_contact = best_contact_from_deals(hs_id)
        if deal_contact:
            if deal_contact.get("email"): rec["email"] = deal_contact["email"]
            if deal_contact.get("phone"): rec["phone"] = deal_contact["phone"]
            dc_fn = deal_contact.get("firstname","")
            dc_ln = deal_contact.get("lastname","")
            dc_jt = (deal_contact.get("jobtitle") or "").lower()
            if dc_fn or dc_ln:
                full = f"{dc_fn} {dc_ln}".strip()
                rec["contact"] = f"Dr. {full}" if any(k in dc_jt for k in ["dvm","veterinarian","doctor"]) else full
        else:
            for src, dest in [("contact_email","email"),("phone","phone")]:
                v = (props.get(src) or "").strip()
                if v: rec[dest] = v

        for src, dest in [("city","city"),("state","st"),("current_pims","pims")]:
            v = (props.get(src) or "").strip()
            if v: rec[dest] = v

        rec["address"] = build_address_display(props)

        # Geocode
        lat, lng = geocode(props, deal_contact or None)
        if lat:
            rec["lat"], rec["lng"] = lat, lng
            rec["approx"] = False

        if "manual" in rec.get("signals", []): signals.append("manual")
        rec["signals"]  = sorted(set(signals))
        rec["verified"] = True

        for ic_key, ic_val in ic_sigs.items():
            if names_match(name, ic_key) and ic_val.get("quote"):
                rec["quote"] = ic_val["quote"]; break
        if not rec.get("quote") and matched_quotes:
            rec["quote"] = matched_quotes[0]

        if is_new:
            added.append(name)
            print(f"  + New: {name}")
        else:
            updated.append(name)
        new_advocates.append(rec)

    # Preserve non-HubSpot records (Capterra, Usage Report, manual)
    hs_ids_in = {str(a.get("hsId","")) for a in new_advocates}
    names_in  = {a["name"].lower() for a in new_advocates}
    for old in existing:
        already = (str(old.get("hsId","")) in hs_ids_in or old["name"].lower() in names_in)
        if not already and (old.get("signals") or old.get("src","") in
                            ("Capterra","Usage Report","Intercom CSAT")):
            new_advocates.append(old)

    new_advocates.sort(key=lambda a: a.get("name",""))

    print(f"\nExcluded: {excl_signal} (no strong signal), {excl_bad} (negative signal)")

    new_json = json.dumps(new_advocates, sort_keys=True)
    if new_json != original:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(new_advocates, f, indent=2, ensure_ascii=False)
        print(f"\n✅ {len(new_advocates)} verified advocates saved "
              f"({len(added)} new, {len(updated)} refreshed)")
    else:
        print(f"\n✅ No changes — {len(new_advocates)} advocates current")

    if SLACK_URL:
        pinned = sum(1 for a in new_advocates if a.get("lat"))
        lines  = ["*🐾 Digitail Advocate Map — Weekly Refresh*",
                  f"Verified: *{len(new_advocates)}* | Pinned: *{pinned}*"]
        if added:   lines.append(f"✅ *{len(added)} new:* " + ", ".join(added[:6]))
        if updated: lines.append(f"📝 Updated: " + ", ".join(updated[:6]) + (f" +{len(updated)-6} more" if len(updated) > 6 else ""))
        lines.append(f"🚫 Excluded: {excl_signal} no signal · {excl_bad} negative")
        if not added and not updated: lines.append("No changes this week ✓")
        lines.append(f"_Run: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC_")
        requests.post(SLACK_URL, json={"text": "\n".join(lines)}, timeout=10)

if __name__ == "__main__":
    main()
