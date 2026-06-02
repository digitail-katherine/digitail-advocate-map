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

import json, os, re, time, math
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
    "case_study":        "https://digitail.com/customer-stories/",
}

DIGITAIL_STORIES_URL = "https://digitail.com/customer-stories/"

# ── Manual referral overrides ─────────────────────────────────────────────────
# HubSpot company IDs that are KNOWN to be in the reference program regardless
# of what the API search returns. Add any company here if the property-based
# search keeps missing them. Find a company's ID in its HubSpot URL:
# app.hubspot.com/contacts/4912130/company/{ID}
KNOWN_REFERRAL_COMPANY_IDS = {
    "21507806557",   # Hefner Road Animal Hospital (Chris Martin)
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
# The script scans only the channels below — no others.
#
# POSITIVE qualifying channels (produce slack_mention strong signals):
#   • #general (exact name match — avoids matching #sales-general)
#   • Any channel containing "shout" (e.g. #company-shout-outs-and-celebrations)
SLACK_POSITIVE_KEYWORDS = ["shout"]        # substring match
SLACK_POSITIVE_EXACT    = {"general"}      # exact channel name (not substring)

# NEGATIVE detection channels (clinics mentioned here get excluded from the map):
#   • Any channel containing "churn"    → #customer-churn-requests
#   • Any channel containing "escalat"  → #customer-support-escalations
#   • Any channel containing "customer-success" → #customer-success
SLACK_NEGATIVE_PRIORITY = [
    "churn", "escalat", "customer-success",
]

# Lookback window in days for positive team mentions.
SLACK_LOOKBACK_DAYS = 90
# Negative exclusion window. Referral opt-ins and customer stories are blocked only by recent negatives.
SLACK_NEGATIVE_LOOKBACK_DAYS = 60

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
    "case_study",      # featured on digitail.com/customer-stories/
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
    "case_study":          "Featured in Digitail Customer Stories",
}

NEGATIVE_KW = [
    # Only words that unambiguously signal churn, departure, or do-not-contact.
    # Do NOT include words like "cancel", "frustrated", "disappointed", "rocky"
    # — these appear constantly in normal CS conversations and create false negatives.
    "churned", "churn confirmed", "lost to competitor", "switching away",
    "leaving digitail", "cancelled contract", "cancelling contract",
    "do not contact", "dnc ", "at risk of churning", "request to cancel",
    "wants to cancel", "going to cancel", "decided to leave",
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

# Words too generic to distinguish one vet clinic from another.
# Excluded when matching Slack mentions and customer story names to HubSpot companies
# so that "Animal Hospital" doesn't match every animal hospital in the database.
GENERIC_VET_WORDS = {
    "veterinary", "animal", "clinic", "hospital", "services", "service", "practice",
    "center", "centre", "mobile", "care", "health", "petcare", "companion", "pets", "pet",
    "vets", "vet", "medical", "wellness", "dvm", "doctor", "doctors",
    "road", "rd", "street", "st", "avenue", "ave", "boulevard", "blvd",
    "drive", "dr", "lane", "ln", "highway", "hwy", "parkway", "pkwy",
}

NAME_SYNONYMS = {
    "vet": "veterinary", "vets": "veterinary", "veterinarian": "veterinary",
    "veterinarians": "veterinary", "hospital": "clinic", "hospitals": "clinic",
    "clinics": "clinic", "ah": "clinic",
}

def identity_words(name: str) -> set:
    out = set()
    for w in re.split(r'\W+', (name or '').lower()):
        if not w or len(w) < 3:
            continue
        w = NAME_SYNONYMS.get(w, w)
        if w in GENERIC_VET_WORDS:
            continue
        out.add(w)
    return out

def strict_clinic_match(a: str, b: str) -> bool:
    aw, bw = identity_words(a), identity_words(b)
    if not aw or not bw:
        return False
    small, big = (aw, bw) if len(aw) <= len(bw) else (bw, aw)
    return small.issubset(big)

def infer_format(name: str) -> str:
    n = name.lower()
    if any(t in n for t in TELE_TERMS):   return "telemedicine"
    if any(t in n for t in MOBILE_TERMS): return "mobile"
    return "bnm"


def infer_clinic_type(name: str, props: dict = None) -> str:
    """Infer frontend colour/category from clinic name and available HubSpot text."""
    props = props or {}
    text = " ".join(str(x or "") for x in [
        name, props.get("description"), props.get("about_us"), props.get("industry"),
        props.get("type"), props.get("practice_type"), props.get("specialty"),
    ]).lower()
    if any(t in text for t in ["emergency", "urgent care", "specialty", "specialist", "24/7", "24 hour"]):
        return "emergency"
    if any(t in text for t in ["equine", "horse", "large animal", "bovine", "farm animal", "livestock"]):
        return "equine"
    if any(t in text for t in ["group", "corporate", "locations", "multi-location", "network"]):
        return "corporate"
    if any(t in text for t in ["small animal", "companion animal", "cat", "feline", "canine", "dog"]):
        return "smallAnimal"
    return "general"



def miles_between(lat1, lng1, lat2, lng2) -> float:
    try:
        r = 3958.8
        p1, p2 = math.radians(float(lat1)), math.radians(float(lat2))
        dp = math.radians(float(lat2) - float(lat1))
        dl = math.radians(float(lng2) - float(lng1))
        a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
        return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
    except Exception:
        return 0.0

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

STATE_PROVINCE_BOUNDS = {
    "TX": (25.0, 36.8, -106.7, -93.3), "OK": (33.4, 37.2, -103.2, -94.2),
    "CT": (40.8, 42.2, -73.9, -71.7), "MA": (41.1, 42.9, -73.6, -69.8),
    "ON": (41.5, 57.5, -95.5, -74.0), "KY": (36.4, 39.3, -89.6, -81.9),
    "VA": (36.4, 39.6, -83.8, -75.0), "NY": (40.4, 45.1, -79.8, -71.7),
    "CA": (32.0, 42.1, -124.6, -114.0), "FL": (24.3, 31.1, -87.8, -80.0),
}

def coord_matches_state(lat, lng, st: str) -> bool:
    st = (st or '').strip().upper()
    if not st or st not in STATE_PROVINCE_BOUNDS:
        return True
    lo_lat, hi_lat, lo_lng, hi_lng = STATE_PROVINCE_BOUNDS[st]
    try:
        return lo_lat <= float(lat) <= hi_lat and lo_lng <= float(lng) <= hi_lng
    except Exception:
        return False

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
HS_DEAL_PROPS = ["number_of_dvms", "dealstage", "hs_is_closed_won", "closedate", "competition", "other_pims_considering", "pipeline"]

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
def _infer_country_from_state_country(state: str = "", country: str = "") -> str:
    """Return normalized country. If HubSpot country is blank, infer from province/state."""
    c = (country or "").strip().lower()
    if c in {"canada", "ca", "can"}: return "Canada"
    if c in {"mexico", "mx", "mex"}: return "Mexico"
    if c in {"united states", "united states of america", "us", "usa", "u.s.", "u.s.a."}: return "United States"
    st = (state or "").strip().upper()
    ca_provinces = {"AB","BC","MB","NB","NL","NS","NT","NU","ON","PE","QC","SK","YT"}
    mx_states = {"AGU","BCN","BCS","CAM","CHP","CHH","COA","COL","DUR","GUA","GRO","HID","JAL","MEX","MIC","MOR","NAY","NLE","OAX","PUE","QUE","ROO","SLP","SIN","SON","TAB","TAM","TLA","VER","YUC","ZAC"}
    if st in ca_provinces: return "Canada"
    if st in mx_states: return "Mexico"
    return "United States"

def build_geocode_query(props: dict, contact: dict = None) -> tuple:
    """Build a geocode query and return (query, confidence).
    confidence='street' means a full address can safely replace stale coordinates.
    """
    raw     = (props.get("address") or "").strip()
    co_city = (props.get("city")    or "").strip().title()
    co_st   = (props.get("state")   or "").strip().upper()
    ct      = contact or {}
    ct_city = (ct.get("city")  or "").strip().title()
    ct_st   = (ct.get("state") or "").strip().upper()
    state   = co_st or ct_st
    city    = co_city or ct_city
    country = _infer_country_from_state_country(state, props.get("country") or (ct.get("country") if ct else ""))

    raw_ok = raw and len(raw) < 90 and "po box" not in raw.lower()
    if raw_ok and city and state:
        return f"{raw}, {city}, {state}, {country}", "street"
    if raw_ok and state:
        return f"{raw}, {state}, {country}", "street"
    if city and state:
        return f"{city}, {state}, {country}", "city"
    if state:
        return f"{state}, {country}", "state"
    return "", "none"

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

def geocode_nominatim(query: str, country: str = ""):
    if not query: return None, None
    time.sleep(1.2)
    try:
        params={"format":"json","q":query,"limit":1}
        cc={"Canada":"ca","United States":"us","Mexico":"mx"}.get(country)
        if cc: params["countrycodes"] = cc
        r = requests.get("https://nominatim.openstreetmap.org/search",
            params=params, headers={"User-Agent":"DigitailAdvocateMap/4.1"}, timeout=10)
        if r.status_code == 200:
            d = r.json()
            if d: return round(float(d[0]["lat"]),5), round(float(d[0]["lon"]),5)
    except Exception as e:
        print(f"  Nominatim failed '{query}': {e}")
    return None, None

def geocode(props: dict, contact: dict = None):
    query, confidence = build_geocode_query(props, contact)
    if not query: return None, None, confidence
    country = _infer_country_from_state_country((props.get("state") or ""), props.get("country") or "")
    state = (props.get("state") or (contact or {}).get("state") or "").strip().upper()

    def try_query(q, conf):
        lat, lng = None, None
        if GOOGLE_KEY:
            lat, lng = geocode_google(q)
        if not lat:
            lat, lng = geocode_nominatim(q, country)
        if lat:
            if in_na_bounds(lat, lng) and coord_matches_country(lat, lng, country) and coord_matches_state(lat, lng, state):
                return lat, lng, conf
            print(f"  Geocode rejected: {q} → {lat},{lng} (state={state or 'n/a'})")
        return None, None, conf

    lat, lng, conf = try_query(query, confidence)
    if lat:
        return lat, lng, conf

    # Street-level geocoding occasionally fails even with a valid address. Retry city/state
    # so a stale wrong pin gets replaced with a safe approximate pin instead of no pin.
    city = (props.get("city") or (contact or {}).get("city") or "").strip().title()
    if confidence == "street" and city and state:
        fallback_q = f"{city}, {state}, {country}"
        lat, lng, _ = try_query(fallback_q, "city")
        if lat:
            print(f"  Geocode used city fallback: {fallback_q} → {lat},{lng}")
            return lat, lng, "city"
    return None, None, confidence

# ── HubSpot referral network — contacts only ─────────────────────────────────
def hs_fetch_referral_network() -> tuple:
    """Return company IDs whose associated contact has reference_program_optin = TRUE/Yes.
    Resolves both contact→company and contact→deal→company.
    """
    if not HS_TOKEN:
        return {}, set()
    prop_name = "reference_program_optin"
    truthy_values = ["TRUE", "true", "True", "Yes", "yes", "YES", "1", "Opted In", "opted in"]
    contact_by_id = {}

    def read_contacts(value):
        out, after = [], None
        while True:
            payload = {
                "filterGroups": [{"filters": [{"propertyName": prop_name, "operator": "EQ", "value": value}]}],
                "properties": ["firstname", "lastname", "email", "phone", "company", "jobtitle", "city", "state", "zip", "country", prop_name],
                "limit": 100,
            }
            if after: payload["after"] = after
            r = requests.post("https://api.hubapi.com/crm/v3/objects/contacts/search", json=payload, headers=hs_h(), timeout=15)
            if r.status_code == 400:
                print(f"  ⚠ Referral property '{prop_name}' not found")
                return 400, out
            if r.status_code != 200:
                print(f"  Referral search {prop_name}={value}: HTTP {r.status_code}")
                return r.status_code, out
            data = r.json()
            out.extend(data.get("results", []))
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after: break
        return 200, out

    for value in truthy_values:
        code, rows = read_contacts(value)
        if code == 400:
            break
        for c in rows:
            contact_by_id[str(c["id"])] = c

    # Fallback for enum oddities: HAS_PROPERTY, then local truthy validation.
    if not contact_by_id:
        after = None
        while True:
            payload = {
                "filterGroups": [{"filters": [{"propertyName": prop_name, "operator": "HAS_PROPERTY"}]}],
                "properties": ["firstname", "lastname", "email", "phone", "company", "jobtitle", "city", "state", "zip", "country", prop_name],
                "limit": 100,
            }
            if after: payload["after"] = after
            r = requests.post("https://api.hubapi.com/crm/v3/objects/contacts/search", json=payload, headers=hs_h(), timeout=15)
            if r.status_code != 200:
                break
            data = r.json()
            for c in data.get("results", []):
                raw = str(c.get("properties", {}).get(prop_name, "") or "").strip().lower()
                if raw in {"true", "yes", "1", "on", "opted in", "opt-in", "agreed"}:
                    contact_by_id[str(c["id"])] = c
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after: break

    print(f"  Referral network: {len(contact_by_id)} opted-in contacts found (property: '{prop_name}')")
    contact_ids = list(contact_by_id.keys())
    company_ids = set()
    company_contact = {}

    # contact → company
    for i in range(0, len(contact_ids), 100):
        batch = contact_ids[i:i+100]
        ar = requests.post("https://api.hubapi.com/crm/v4/associations/contacts/companies/batch/read",
                           json={"inputs": [{"id": cid} for cid in batch]}, headers=hs_h(), timeout=15)
        if ar.status_code == 200:
            for item in ar.json().get("results", []):
                from_id = str(item.get("from", {}).get("id", ""))
                p = contact_by_id.get(from_id, {}).get("properties", {})
                cname = f"{p.get('firstname','')} {p.get('lastname','')}".strip() or p.get("email", from_id)
                for assoc in item.get("to", []):
                    cid = str(assoc.get("toObjectId", ""))
                    if cid:
                        company_ids.add(cid)
                        # Keep the actual opted-in contact attached to this company.
                        # This is used later for contact display + geocode fallback when the company lacks address data.
                        if cid not in company_contact:
                            company_contact[cid] = p
                        print(f"    Referral contact → company: {cname} → {cid}")
        time.sleep(0.1)

    # contact → deal → company fallback
    deal_ids = set()
    for i in range(0, len(contact_ids), 100):
        batch = contact_ids[i:i+100]
        ar = requests.post("https://api.hubapi.com/crm/v4/associations/contacts/deals/batch/read",
                           json={"inputs": [{"id": cid} for cid in batch]}, headers=hs_h(), timeout=15)
        if ar.status_code == 200:
            for item in ar.json().get("results", []):
                for assoc in item.get("to", []):
                    did = str(assoc.get("toObjectId", ""))
                    if did: deal_ids.add(did)
        time.sleep(0.1)
    for i in range(0, len(deal_ids), 100):
        batch = list(deal_ids)[i:i+100]
        ar = requests.post("https://api.hubapi.com/crm/v4/associations/deals/companies/batch/read",
                           json={"inputs": [{"id": did} for did in batch]}, headers=hs_h(), timeout=15)
        if ar.status_code == 200:
            for item in ar.json().get("results", []):
                for assoc in item.get("to", []):
                    cid = str(assoc.get("toObjectId", ""))
                    if cid:
                        company_ids.add(cid)
                        company_contact.setdefault(cid, {})
        time.sleep(0.1)

    before = len(company_ids)
    company_ids.update(KNOWN_REFERRAL_COMPANY_IDS)
    if len(company_ids) > before:
        print(f"  Referral network: +{len(company_ids)-before} from manual override")
    print(f"  Referral network: {len(company_ids)} companies resolved")
    for cid in sorted(company_ids):
        print(f"    Referral company ID: {cid}")
    return company_contact, company_ids

ONBOARDING_CS_COMPANY_IDS = set()
REFERRAL_CONTACT_BY_COMPANY = {}  # company_id -> opted-in contact properties for fallback contact/geocode

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
    Returns HubSpot company IDs to EXCLUDE from the map.
    Excludes any company whose MOST RECENT onboarding deal is NOT in the CS stage.
    This catches:
      - Pre-CS active deals (still being onboarded)
      - Churned deals (deal moved to churned stage and closed)
    Companies with NO onboarding deal at all are NOT excluded here —
    they may be older customers onboarded before the pipeline existed.
    """
    global ONBOARDING_CS_COMPANY_IDS
    ONBOARDING_CS_COMPANY_IDS = set()
    if not pipeline_id: return set()

    # Fetch ALL onboarding deals (active + closed), resolve to companies.
    # Eligibility rule: a company is allowed if it has ANY onboarding deal in CS.
    # This is intentional: HubSpot often has extra/duplicate onboarding deals that
    # may be newer than the real CS-stage implementation deal. Requiring the latest
    # deal to be CS incorrectly excludes legitimate advocate opt-ins/stories.
    company_stages = {}   # company_id → set(stage_id)

    after = None
    while True:
        payload = {
            "filterGroups": [{"filters": [
                {"propertyName": "pipeline", "operator": "EQ", "value": pipeline_id}
            ]}],
            "properties": ["dealname", "dealstage", "createdate", "closedate"],
            "limit": 200,
        }
        if after: payload["after"] = after
        r = requests.post("https://api.hubapi.com/crm/v3/objects/deals/search",
                          json=payload, headers=hs_h(), timeout=15)
        if r.status_code != 200: break
        data     = r.json()
        results  = data.get("results", [])
        deal_ids = [d["id"] for d in results]

        # Map deal_id → (date, stage)
        deal_info = {}
        for d in results:
            p    = d.get("properties", {})
            date = (p.get("closedate") or p.get("createdate") or "")
            deal_info[d["id"]] = (date, p.get("dealstage", ""))

        for i in range(0, len(deal_ids), 100):
            ar = requests.post(
                "https://api.hubapi.com/crm/v4/associations/deals/companies/batch/read",
                json={"inputs": [{"id": did} for did in deal_ids[i:i+100]]},
                headers=hs_h(), timeout=15)
            if ar.status_code == 200:
                for item in ar.json().get("results", []):
                    deal_id = str(item.get("from", {}).get("id", ""))
                    info    = deal_info.get(deal_id, ("", ""))
                    for assoc in item.get("to", []):
                        cid = str(assoc.get("toObjectId", ""))
                        if not cid: continue
                        company_stages.setdefault(cid, set()).add(info[1])
            time.sleep(0.1)

        after = data.get("paging", {}).get("next", {}).get("after")
        if not after: break

    # Exclude companies that have onboarding deals but NO CS-stage onboarding deal.
    # Companies with at least one CS deal are allowed, even if other duplicate/pre-CS
    # onboarding deals also exist.
    exclude_ids = set()
    cs_count    = 0
    for cid, stages in company_stages.items():
        if cs_stage_id and cs_stage_id in stages:
            cs_count += 1
            ONBOARDING_CS_COMPANY_IDS.add(cid)
        else:
            exclude_ids.add(cid)

    print(f"  Onboarding status: {cs_count} companies with at least one CS-stage deal (allowed), "
          f"{len(exclude_ids)} with no CS-stage onboarding deal (excluded)")
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

        deals = r.json().get("results", [])
        if not deals: return {}, {}

        deal_ids = [d["id"] for d in deals]
        sales_pipeline_id = hs_fetch_sales_pipeline_id()

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
            if sales_pipeline_id:
                all_results = [d for d in all_results if d.get("properties", {}).get("pipeline") == sales_pipeline_id]
            # Prefer Sales pipeline deals explicitly marked closed-won
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

                # Other PIMS considered — primary source is Sales deal property "other_pims_considering"; fallback to competition.
                comp = (p.get("other_pims_considering") or p.get("competition") or "").strip()
                if comp and not deal_props.get("pimsConsidered"):
                    deal_props["pimsConsidered"] = comp

                if deal_props.get("dvms") and deal_props.get("pimsConsidered"):
                    break  # have everything we need — stop iterating

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

def hs_get_customers(referral_ids: set = None) -> list:
    """
    Fetches all HubSpot companies that should be evaluated as potential advocates.
    Three sources are merged:
      1. Companies with lifecyclestage = customer (primary filter)
      2. Companies with a closed-won deal (catch-all for mis-staged records)
      3. Companies associated with a referral-program opted-in contact
         — guaranteed inclusion regardless of lifecycle stage, because a customer
           who explicitly opted in to be a reference must always be evaluated.
    """
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
        else:
            print(f"  HubSpot filter '{filt['propertyName']}': 0")

    # Add closed-won companies not already in the lifecycle filter result
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

    # Add referral-network companies not yet in the set.
    # These opted-in contacts MUST be evaluated regardless of lifecycle stage —
    # a wrong stage setting in HubSpot should never silently exclude a willing reference.
    if referral_ids:
        ref_missing = referral_ids - set(by_id.keys())
        if ref_missing:
            print(f"  HubSpot: adding {len(ref_missing)} referral-only companies "
                  f"(opted-in but lifecycle stage not 'customer')")
            for i in range(0, len(list(ref_missing)), 100):
                r = requests.post("https://api.hubapi.com/crm/v3/objects/companies/batch/read",
                    json={"inputs":[{"id":cid} for cid in list(ref_missing)[i:i+100]],
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
    neg_cutoff = int((datetime.now(timezone.utc) - timedelta(days=SLACK_NEGATIVE_LOOKBACK_DAYS)).timestamp())

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

    # Sort: negative channels first → positive channels
    def _ch_sort(ch):
        n = ch["name"].lower()
        if any(k in n for k in SLACK_NEGATIVE_PRIORITY): return 0
        if any(k in n for k in SLACK_POSITIVE_KEYWORDS) or n in SLACK_POSITIVE_EXACT: return 1
        return 2
    all_channels.sort(key=_ch_sort)

    neg_channels = [c["name"] for c in all_channels if _ch_sort(c) == 0]
    pos_channels = [c["name"] for c in all_channels if _ch_sort(c) == 1]
    print(f"  Slack: scanning {len(neg_channels)+len(pos_channels)} priority channels "
          f"({SLACK_LOOKBACK_DAYS}-day positive / {SLACK_NEGATIVE_LOOKBACK_DAYS}-day negative window)")
    print(f"  Negative channels: {neg_channels or ['none matched']}")
    print(f"  Positive channels: {pos_channels or ['none matched']}")

    # ── Scan each channel ─────────────────────────────────────────────────────
    # KEY RULES:
    # - POSITIVE signals → ONLY from #general and #shout-out channels
    # - NEGATIVE signals → ONLY from churn, escalation, customer-success channels
    # - All other channels are skipped entirely
    scanned    = 0
    is_neg_ch  = lambda name: any(k in name.lower() for k in SLACK_NEGATIVE_PRIORITY)
    is_pos_ch  = lambda name: (any(k in name.lower() for k in SLACK_POSITIVE_KEYWORDS)
                               or name.lower() in SLACK_POSITIVE_EXACT)

    for ch in all_channels:
        ch_name     = ch["name"]
        ch_id       = ch["id"]
        neg_channel = is_neg_ch(ch_name)
        pos_channel = is_pos_ch(ch_name)

        # Skip channels that can contribute neither positives nor negatives —
        # saves API calls and avoids picking up irrelevant mentions
        if not neg_channel and not pos_channel:
            continue

        try:
            messages = []
            cursor = None
            while True:
                params = {"channel":ch_id,"oldest":cutoff,"limit":200}
                if cursor:
                    params["cursor"] = cursor
                r = requests.get("https://slack.com/api/conversations.history",
                    params=params, headers=hdrs, timeout=15)
                data = r.json()
                if not data.get("ok"):
                    err = data.get("error","")
                    if err not in ("not_in_channel","channel_not_found"):
                        print(f"  Slack #{ch_name}: {err}")
                    break
                messages.extend(data.get("messages",[]))
                cursor = data.get("response_metadata",{}).get("next_cursor")
                if not cursor:
                    break
                time.sleep(0.2)
            for msg in messages:
                text = msg.get("text","")
                if not text or len(text) < 15: continue
                tl      = text.lower()
                scanned += 1
                ts       = msg.get("ts","")
                ts_nodot = ts.replace(".","")
                permalink = f"{workspace_url}/archives/{ch_id}/p{ts_nodot}" if ts_nodot else None

                # Use word-boundary matching so "great" inside "Great Plains Veterinary"
                # does NOT trigger is_pos (the clinic name itself is not sentiment)
                is_pos = any(re.search(r'\b' + re.escape(k) + r'\b', tl)
                             for k in POSITIVE_KW)
                is_neg = any(k in tl for k in NEGATIVE_KW)  # phrases — substring ok

                # Extract multi-word capitalised clinic name candidates.
                # Require at least one DISTINCTIVE word (non-generic vet term).
                for word in re.findall(r'\b[A-Z][a-zA-Z]{3,}(?:\s[A-Z][a-zA-Z]{3,})+\b', text):
                    wl = word.lower()
                    distinctive = {w for w in re.split(r'\W+', wl)
                                   if len(w) >= 5 and w not in GENERIC_VET_WORDS}
                    if len(word) > 10 and distinctive and wl not in {
                        "slack","digitail","tails","monday","friday",
                        "north america","good morning","great work","well done",
                    }:
                        if is_pos and not is_neg and pos_channel:
                            # Qualifying positive: shout-out/celebration channels ONLY.
                            # setdefault ensures the FIRST (likely most specific) message wins.
                            positive.setdefault(wl, {
                                "signal":    "slack_mention",
                                "quote":     text[:280],
                                "channel":   ch_name,
                                "permalink": permalink,
                            })
                        elif is_neg and not is_pos and neg_channel:
                            try:
                                msg_ts = int(float(ts or 0))
                            except Exception:
                                msg_ts = 0
                            if msg_ts >= neg_cutoff:
                                negative.add(wl)
            time.sleep(0.3)
        except Exception as e:
            print(f"  Slack #{ch_name} failed: {e}")

    print(f"  Slack: {scanned} messages scanned in "
          f"{sum(1 for c in all_channels if is_pos_ch(c['name']) or is_neg_ch(c['name']))} "
          f"priority channels → {len(positive)} positive, {len(negative)} negative")
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

# ── Digitail Customer Stories ─────────────────────────────────────────────────
STORY_VERBS = {
    "cut", "cuts", "saved", "saves", "save", "chose", "choose", "drove", "drive",
    "scaled", "scale", "built", "builds", "harnessed", "harnesses", "added", "adds",
    "launched", "launches", "prepared", "prepares", "sees", "reclaimed", "reclaims",
    "consolidated", "unified", "switched", "switches", "transformed", "transforms",
    "implemented", "implements", "boosted", "boosts", "grew", "grows", "reduced",
    "reduces", "increased", "increases", "achieved", "achieves", "used", "uses",
    "created", "creates", "streamlined", "streamlines", "went", "goes", "gained",
    "gains", "doubled", "doubles", "onboarded", "runs", "grew", "prepared",
}
STORY_STOPWORDS = {
    "how", "with", "from", "after", "using", "digitail", "pims", "software", "case",
    "study", "strategy", "proven", "steps", "minutes", "mins", "daily", "patients",
    "adoption", "parent", "app", "digital", "faster", "boost", "productivity", "week",
    "year", "paperless", "modern", "workflows", "inventory", "management", "time",
}


# Exact clinic names for known customer-story URL slug patterns.
# This is intentionally explicit because marketing headlines often start with
# metrics/person names ("5-10 min... Dr. Woodruff...") instead of the clinic name.
STORY_SLUG_OVERRIDES = [
    ("beeville-veterinary-hospital", "Beeville Veterinary Hospital"),
    ("simmons-veterinary-clinic", "Simmons Veterinary Clinic"),
    ("covina", "Covina Animal Hospital"),
    ("amici-cannis", "Amici Cannis"),
    ("hefner-road-animal-hospital", "Hefner Road Animal Hospital"),
    ("genesee", "Genesee Community College"),
    ("southern-trails", "Southern Trail Animal Clinic"),
    ("shoreview", "Shoreview Veterinary Hospital"),
    ("riverside", "Riverside Veterinary"),
    ("woodruff", "Woodruff Vet Services"),
    ("parker-ace", "Parker & Ace"),
    ("mill-brook", "Mill Brook Animal Clinic"),
    ("acharavet", "AcharaVet"),
    ("paumanok", "Paumanok Veterinary Hospital"),
    ("unam", "UNAM Vet School"),
    ("veterinary-united", "Veterinary United"),
    ("woofdoctor-on-wheels", "WoofDoctor on Wheels"),
    ("elevate", "Elevate Pet Wellness Center"),
    ("embrace-animal-hospital", "Embrace Animal Hospital"),
    ("my-home-vet", "My Home Vet"),
    ("vet-concierge", "Vet Concierge"),
    ("the-parks-animal-clinic", "The Parks Animal Clinic"),
    ("eco-vets", "Eco Vets"),
    ("goostrey-lane-vets", "Goostrey Lane Vets"),
    ("home-visit-pet-care", "Home Visit Pet Care"),
]

PERSON_PREFIX_RE = re.compile(r'^(?:dr|doctor|mr|mrs|ms|miss)\.?\s+', re.I)


def _clean_story_candidate(name: str) -> str:
    name = re.sub(r'\s+', ' ', (name or '')).strip(" -–—:|,.")
    name = re.sub(r"[’']s\b", "", name)
    name = re.sub(r"\b(Veterinary|United|Doctor|Vet|Vets|Clinic|Hospital|Service|Services)s\b", r"\1", name, flags=re.I)
    name = PERSON_PREFIX_RE.sub('', name).strip()

    # If the candidate includes card-role cruft, keep the text after the last role.
    # Example: "Liza Price Owner Riverside Veterinary" → "Riverside Veterinary".
    role_re = r'\b(?:Owner|Founder|Co-founder|Co founder|Director|Manager|Technician|Administrator|Program Director|Medical Director|Hospital Manager)\b'
    parts = re.split(role_re, name, flags=re.I)
    if len(parts) > 1 and parts[-1].strip():
        name = parts[-1].strip()
    name = re.sub(r'^.*\bHow\s+', '', name, flags=re.I).strip()

    # Remove credential crumbs.
    name = re.sub(r'\b(?:DVM|MBA|CVPM|PHR|MS|FVTE|LVT)\b', '', name, flags=re.I)
    name = re.sub(r'\s+', ' ', name).strip(" -–—:|,.")
    words = [w for w in re.split(r'\W+', name) if w]
    if len(words) < 1 or len(name) < 3:
        return ''
    if PERSON_PREFIX_RE.search(name):
        return ''
    clinicish = re.search(r'\b(?:vet|vets|veterinary|animal|clinic|hospital|road|community|college|school|united|home|mobile|care|park|parks|eco|embrace|elevate|ace|woofdoctor|unam|amici|cannis|shoreview|riverside|hefner|simmons|covina|beeville|paumanok|mill|brook)\b', name, re.I)
    if len(words) <= 2 and not clinicish:
        return ''
    if not distinctive_words(name):
        return ''
    return name


def _story_name_from_slug(slug: str) -> str:
    slug = re.sub(r'^[\d-]+', '', slug or '')
    slug = re.sub(r'^how-', '', slug)
    parts = [x for x in slug.split('-') if x]
    kept = []
    for part in parts:
        pl = part.lower()
        if pl in STORY_VERBS or pl in STORY_STOPWORDS:
            break
        if re.match(r'^\d', pl):
            break
        kept.append(part)
        if len(kept) >= 6:
            break
    return _clean_story_candidate(' '.join(kept).title())


def _extract_story_candidates(text: str, slug: str) -> list:
    """Return clean clinic-name candidates from a customer-story card/link."""
    text = re.sub(r'\s+', ' ', text or '').strip()
    candidates = []

    # Most reliable: exact clinic-ish phrase anywhere in the card text.
    clinic_patterns = [
        r"([A-Z][A-Za-z&'’\-. ]{2,80}?\b(?:Veterinary Hospital|Animal Hospital|Veterinary Clinic|Vet Clinic|Veterinary Service|Veterinary Services|Animal Clinic|Community College|Vet School|Mobile Vet|Lane Vets|Road Animal Hospital|Veterinary|Vets)\b)",
        r"\b(Amici Cannis|Hefner Road Animal Hospital|Simmons Veterinary Clinic|Simmons Veterinary|Covina Animal Hospital|Beeville Veterinary Hospital|Genesee Community College|Southern Trail|Shoreview Veterinary|Riverside Veterinary|Parker & Ace|Mill Brook|AcharaVet|Paumanok Veterinary Hospital|UNAM|Veterinary United|WoofDoctor on Wheels|WoofDoctor|Elevate|Embrace Animal Hospital|My Home Vet|Vet Concierge|The Parks Animal Clinic|Eco Vets|Goostrey Lane Vets|Home Visit Pet Care)\b",
    ]
    for pat in clinic_patterns:
        for m in re.finditer(pat, text):
            cand = _clean_story_candidate(m.group(1))
            if cand:
                candidates.append(cand)

    # Headline style: "How [Clinic] Drove..." / "[Clinic] cuts..."
    verb_alt = '|'.join(sorted(STORY_VERBS, key=len, reverse=True))
    for pat in [
        rf"\bHow\s+([A-Z][A-Za-z&'’\-. ]{{2,80}}?)\s+(?:{verb_alt})\b",
        rf"^([A-Z][A-Za-z&'’\-. ]{{2,80}}?)\s+(?:{verb_alt})\b",
    ]:
        m = re.search(pat, text)
        if m:
            cand = _clean_story_candidate(m.group(1))
            if cand:
                candidates.append(cand)

    slug_cand = _story_name_from_slug(slug)
    if slug_cand:
        candidates.append(slug_cand)

    # Deduplicate while preserving order. Prefer longer, more specific names first.
    dedup = []
    seen = set()
    for cand in sorted(candidates, key=lambda x: len(x), reverse=True):
        key = cand.lower()
        if key not in seen:
            seen.add(key)
            dedup.append(cand)
    return dedup


def scrape_customer_stories() -> list:
    """Scrape customer story URLs and extract exactly one clinic candidate per URL.
    Uses URL slug/headline only; never broad card text, to prevent story bleed.
    """
    if not BS4:
        print("  Customer stories: beautifulsoup4 not installed, skipping")
        return []

    def clean_story_name(raw: str) -> str:
        slug = (raw or '').strip().lower().strip('/')
        for needle, clinic in STORY_SLUG_OVERRIDES:
            if needle in slug:
                return clinic

        raw = re.sub(r'https?://\S+', ' ', raw or '')
        raw = raw.replace('-', ' ')
        raw = re.sub(r'\s+', ' ', raw).strip()
        raw = re.sub(r'^\d+\s+', '', raw)

        # If the slug has marketing copy before "how", keep the part after HOW.
        # Example: "2400 patients in year one how embrace animal hospital runs..."
        m = re.search(r'\bhow\s+(.+)$', raw, flags=re.I)
        if m:
            raw = m.group(1).strip()

        # Remove clinician-led prefixes only when followed by an actual clinic phrase later.
        raw = re.sub(r'^(dr\.?|doctor)\s+[a-z]+\s+[a-z]+\s+', '', raw, flags=re.I)
        stop = re.search(
            r'\b(saved|saves|save|cut|cuts|reduced|reduces|reclaim|reclaims|reclaimed|'
            r'switched|switches|switching|chose|chooses|built|builds|launched|launches|'
            r'scaled|scales|transformed|transforms|boosted|boosts|grew|grows|drove|drives|'
            r'prepared|uses|used|using|runs|run|delivers|with digitail|from avimark|'
            r'from cornerstone|per doctor|daily)\b',
            raw, flags=re.I)
        if stop:
            raw = raw[:stop.start()].strip()
        raw = re.sub(r'\b(and|with|from|to|by)$', '', raw, flags=re.I).strip()
        words = raw.split()
        if len(words) > 7:
            raw = ' '.join(words[:7])
        return raw.title().replace(' Ah', ' AH')

    try:
        r = requests.get(DIGITAIL_STORIES_URL, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/124 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }, timeout=15)
        if r.status_code != 200:
            print(f"  Customer stories: HTTP {r.status_code}")
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        stories, seen_urls = [], set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/customer-stories/" not in href:
                continue
            url = href if href.startswith("http") else f"https://digitail.com{href}"
            url = url.split("#")[0].rstrip("/") + "/"
            if url.rstrip("/") == DIGITAIL_STORIES_URL.rstrip("/") or url in seen_urls:
                continue
            seen_urls.add(url)
            slug = url.rstrip("/").split("/customer-stories/")[-1]
            # Use the slug as the source of truth. Anchor/card text can include adjacent
            # cards or clinician names, which previously attached Hefner stories to Embrace, etc.
            clinic_name = clean_story_name(slug)
            if not clinic_name or len(clinic_name) < 4:
                continue
            if clinic_name.lower() in {"customer stories", "stories"}:
                continue
            stories.append({
                "source": "customer_stories", "signal": "case_study",
                "reviewer": clinic_name, "clinic_candidates": [clinic_name],
                "text": f"Featured in Digitail customer story: {clinic_name}",
                "url": url,
            })
        dedup, seen = [], set()
        for st in stories:
            k = (st["url"], st["reviewer"].lower())
            if k not in seen:
                seen.add(k); dedup.append(st)
        print(f"  Customer stories: {len(dedup)} stories found on digitail.com")
        for st in dedup:
            print(f"    Story candidate: {st['reviewer']} → {st['url']}")
        return dedup
    except Exception as e:
        print(f"  Customer stories scrape failed: {e}")
        return []

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
    a, b = (a or "").lower().strip(), (b or "").lower().strip()
    if not a or not b:
        return False
    if a == b:
        return True
    return strict_clinic_match(a, b)

def distinctive_words(text: str, min_len: int = 4) -> set:
    return {w for w in re.split(r'\W+', (text or '').lower())
            if len(w) >= min_len and w not in GENERIC_VET_WORDS}

def matches_negative_name(name: str, negative_terms) -> bool:
    name_lc = (name or '').lower().strip()
    if not name_lc: return False
    name_words = distinctive_words(name_lc)
    for term, term_words in negative_terms:
        if not term:
            continue
        if name_lc == term or term in name_lc or name_lc in term:
            return True
        if name_words and term_words and len(name_words & term_words) >= 2:
            return True
    return False

def case_study_names_match(hs_name: str, story_name: str) -> bool:
    return strict_clinic_match(hs_name, story_name)

def build_review_matches(all_external: list, hs_customers: list) -> dict:
    matches, unmatched = {}, 0
    for ext in all_external:
        text     = (ext.get("text") or "").lower()
        reviewer = (ext.get("reviewer") or ext.get("author") or "").lower()
        pims_txt = ext.get("pims","")
        is_case_study = ext.get("signal") == "case_study"
        best_id, best_score = None, 0
        for customer in hs_customers:
            p     = customer.get("properties",{})
            hs_id = str(customer["id"])
            name  = (p.get("name") or "").lower()
            city  = (p.get("city") or "").lower()
            st    = (p.get("state") or "").lower()
            pims  = (p.get("current_pims") or "").lower()
            score = 0

            if is_case_study:
                # Case studies: match against all extracted clinic candidates, not
                # clinician names. This supports cards like "Dr. Martin... Hefner
                # Road Animal Hospital" and headlines like "How Simmons Veterinary...".
                candidates = ext.get("clinic_candidates") or ([reviewer] if reviewer else [])
                for cand in candidates:
                    if cand and case_study_names_match(name, cand.lower()):
                        score += 50
                        break
            else:
                if reviewer and names_match(name, reviewer): score += 10
                if name and name in text:                    score += 8
                if reviewer and len(reviewer) > 4 and reviewer in name: score += 8
                for pk, aliases in PIMS_MATCH.items():
                    if pk in pims and any(a in text + " " + pims_txt.lower() for a in aliases):
                        score += 6; break
                if city and len(city) > 3 and city in text: score += 4
                if st   and len(st)   > 1 and st   in text: score += 2

            if score > best_score: best_score = score; best_id = hs_id

        min_score = 10 if is_case_study else 8
        if best_id and best_score >= min_score:
            matches.setdefault(best_id,[]).append(ext)
            if is_case_study:
                matched_name = next((c.get("properties", {}).get("name", "") for c in hs_customers if str(c.get("id")) == str(best_id)), "")
                print(f"    Customer story matched HubSpot: {ext.get('reviewer')} → {matched_name} ({best_id})")
        else:
            unmatched += 1
            if is_case_study:
                print(f"    Customer story unmatched: {ext.get('reviewer')}")
    matched_count = sum(len(v) for v in matches.values())
    print(f"  Reviews matched: {matched_count}, unmatched: {unmatched}")
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
    all_reviews = (scrape_customer_stories() +
                   scrape_capterra() + scrape_g2() + scrape_software_advice()
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
    hs_customers = hs_get_customers(referral_ids=referral_ids)

    print("\n── Matching reviews to HubSpot companies ──────────────────")
    review_matches = build_review_matches(all_external, hs_customers)
    for ext in all_external:
        rkey = (ext.get("reviewer") or ext.get("contact") or "").lower().strip()
        if ext.get("signal") == "case_study":
            continue
        for mkey, hs_id in MANUAL_REVIEW_MATCHES.items():
            if mkey in rkey or rkey in mkey or names_match(rkey, mkey):
                if ext not in review_matches.get(hs_id,[]):
                    review_matches.setdefault(hs_id,[]).append(ext)

    # ── Process each HubSpot company ─────────────────────────────────────────
    new_advocates  = []
    added, updated = [], []
    excl_signal, excl_bad = 0, 0
    next_id = max((a.get("id",0) for a in existing), default=100) + 1

    negative_terms = [(b.lower().strip(), distinctive_words(b)) for b in all_negative]

    for customer in hs_customers:
        hs_id = str(customer["id"])
        props = customer.get("properties",{})
        name  = (props.get("name") or "").strip()
        if not name: continue

        if is_excluded_non_clinic(props): continue
        if not is_north_america(props):   continue

        name_lc = name.lower().strip()

        # ── Is this company in the referral program? ──────────────────────────
        # ONLY use exact HubSpot company ID match (referral_ids).
        # The name_map fallback was disabled because it matches by company TEXT FIELD
        # typed by the contact (e.g. a contact at a different clinic types "Brown
        # Veterinary Hospital"), which incorrectly flags unrelated companies as
        # referral members. Exact ID match has zero false positives.
        in_referral = hs_id in referral_ids

        # ── Negative checks — apply to everyone, including referral members ───
        # Referral opt-in means they agreed to be a reference — but a subsequent
        # negative signal (churn risk, bad CSAT, escalation) overrides that.
        if is_negative_props(props):
            excl_bad += 1
            if in_referral: print(f"  ✗ Negative (referral member): {name}")
            continue
        if matches_negative_name(name, negative_terms):
            excl_bad += 1
            print(f"  ✗ Negative{'(referral member) ' if in_referral else ''}: {name}")
            continue

        # Require a CS-stage deal in the onboarding pipeline. This applies to referral opt-ins, customer stories, reviews, and Slack signals.
        if hs_id not in ONBOARDING_CS_COMPANY_IDS:
            excl_bad += 1
            print(f"  ✗ Not in CS stage: {name}")
            continue
        if hs_id in onboarding_exclude:
            excl_bad += 1
            print(f"  ✗ Still onboarding: {name}")
            continue

        # ── Referral fast-path: opt-in + no negatives = include ───────────────
        # Collect all other signals too — they enrich the popup card — but the
        # gate is already satisfied by referral membership alone.
        strong_signals = []
        signal_urls    = {}

        if in_referral:
            strong_signals.append("hs_referral_network")
            signal_urls["hs_referral_network"] = (
                f"https://app.hubspot.com/contacts/{HS_ACCOUNT_ID}/company/{hs_id}"
            )

        # ── Additional signals (always collected; displayed on popup card) ─────
        for ic_key in ic_sigs:
            if names_match(name, ic_key):
                strong_signals.append("intercom_csat")
                if ic_sigs[ic_key].get("url"):
                    signal_urls["intercom_csat"] = ic_sigs[ic_key]["url"]
                break

        # Slack: require strong name alignment AND the linked message text must
        # contain the same distinctive clinic words. This prevents a message about
        # "Southshore Veterinary Service" from linking to "Thousand Hills Veterinary
        # Service" just because they share generic clinic terms.
        for s_key, s_val in slack_pos.items():
            s_words = distinctive_words(s_key, min_len=5)
            n_words = distinctive_words(name_lc, min_len=5)
            if not s_words or not n_words:
                continue

            quote_lc = (s_val.get("quote") or "").lower()
            overlap = s_words & n_words
            direct_match = name_lc in s_key or s_key in name_lc
            if not direct_match and len(overlap) < 2:
                continue

            # Link only when the actual Slack text contains the same distinctive
            # words used to match the HubSpot company.
            needed = n_words if direct_match else overlap
            if needed and not all(w in quote_lc for w in needed):
                continue

            strong_signals.append("slack_mention")
            if s_val.get("permalink"):
                signal_urls["slack_mention"] = s_val["permalink"]
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
            # Use per-story URL for case_study; static listing URL for review sites
            sig_url = ext.get("url") or SIGNAL_STATIC_URLS.get(ext["signal"])
            if sig_url and ext["signal"] not in signal_urls:
                signal_urls[ext["signal"]] = sig_url

        # External review fallback for non-case-study sources only.
        # Case studies are EXCLUDED here — they must match through build_review_matches()
        # using case_study_names_match() which filters generic vet words. Without this
        # exclusion, slug names like "Beeville Veterinary Hospital Cuts Cost Of Goods..."
        # match EVERY vet clinic via shared words "veterinary" and "hospital".
        for ext in all_external:
            if ext.get("signal") == "case_study":
                continue  # handled exclusively by build_review_matches + case_study_names_match
            rev = ext.get("reviewer","") or ext.get("author","") or ""
            # Apply GENERIC_VET_WORDS filter so "veterinary"/"animal"/"hospital" alone
            # don't produce a match — only distinctive clinic-specific words count
            rev_words  = {w for w in re.split(r'\W+', rev.lower())
                          if len(w) >= 5 and w not in GENERIC_VET_WORDS}
            name_words = {w for w in re.split(r'\W+', name_lc)
                          if len(w) >= 5 and w not in GENERIC_VET_WORDS}
            # Require at least 1 distinctive word in both AND they overlap
            is_solid_match = bool(rev_words) and bool(name_words) and len(rev_words & name_words) >= 1
            if rev and len(rev) > 10 and is_solid_match and ext not in review_matches.get(hs_id,[]):
                strong_signals.append(ext["signal"])
                if ext.get("text"): matched_quotes.append(ext["text"])
                sig_url = ext.get("url") or SIGNAL_STATIC_URLS.get(ext["signal"])
                if sig_url and ext["signal"] not in signal_urls:
                    signal_urls[ext["signal"]] = sig_url

        # Gate: referral alone is sufficient; non-referral needs at least one other signal
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
                   "dgtId":None,"dvms":None,"customerSince":None,"signalUrls":{},
                   "pimsConsidered":None}
            next_id += 1
        else:
            rec = dict(rec)
            rec["notes"] = None  # clear legacy "Location unknown" notes
            rec["quote"] = None  # clear stale/wrong story quote before re-attaching current signals

        old_lat, old_lng = rec.get("lat"), rec.get("lng")
        st_for_pin = (props.get("state") or "").strip().upper()
        if old_lat and old_lng and not coord_matches_state(old_lat, old_lng, st_for_pin):
            print(f"  Clearing stale coordinates: {name} ({old_lat},{old_lng}) not in {st_for_pin}")
            rec["lat"], rec["lng"] = None, None

        # ── Refresh from HubSpot + deal properties ────────────────────────────
        rec["hsId"]    = hs_id
        rec["name"]    = name
        rec["format"]  = infer_format(name)
        rec["ct"]      = infer_clinic_type(name, props)
        rec["metrics"] = None

        deal_contact, deal_props = best_contact_and_deal_props(hs_id)
        referral_contact = REFERRAL_CONTACT_BY_COMPANY.get(hs_id, {}) if in_referral else {}
        display_contact = deal_contact or referral_contact

        # Store DVM count from deal
        if deal_props.get("dvms"):
            rec["dvms"] = deal_props["dvms"]

        # Store PIMS considered (competition) from deal — used for "Other PIMS Considered" filter
        if deal_props.get("pimsConsidered"):
            rec["pimsConsidered"] = deal_props["pimsConsidered"]

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

        if display_contact:
            if display_contact.get("email"): rec["email"] = display_contact["email"]
            if display_contact.get("phone"): rec["phone"] = display_contact["phone"]
            fn = display_contact.get("firstname","")
            ln = display_contact.get("lastname","")
            jt = (display_contact.get("jobtitle") or "").lower()
            if fn or ln:
                full = f"{fn} {ln}".strip()
                rec["contact"] = f"Dr. {full}" if any(k in jt for k in ["dvm","veterinarian","doctor"]) else full
        else:
            for src, dest in [("contact_email","email"),("phone","phone")]:
                v = (props.get(src) or "").strip()
                if v: rec[dest] = v

        # Prefer company location, but use referral/deal contact location as fallback.
        city_val = (props.get("city") or (display_contact or {}).get("city") or "").strip()
        st_val   = (props.get("state") or (display_contact or {}).get("state") or "").strip()
        for src, dest in [("city","city"),("state","st"),("current_pims","pims")]:
            v = (props.get(src) or "").strip()
            if not v and src == "city": v = city_val
            if not v and src == "state": v = st_val
            if v: rec[dest] = v

        raw  = (props.get("address") or "").strip()
        city = city_val.title()
        st   = st_val.upper()
        zip_ = (props.get("zip") or (display_contact or {}).get("zip") or "").strip()
        raw_ok = raw and len(raw) < 80 and "po box" not in raw.lower()
        parts  = [raw, city, st, zip_] if raw_ok else [city, st, zip_]
        rec["address"] = ", ".join(p for p in parts if p)

        # Use referral opt-in contact as a geocode fallback for opt-in advocates whose
        # company record is sparse. This fixes opt-ins that appear in the list but have no pin.
        lat, lng, geo_confidence = geocode(props, display_contact or None)
        if lat:
            old_lat, old_lng = rec.get("lat"), rec.get("lng")
            if old_lat and old_lng and miles_between(old_lat, old_lng, lat, lng) > 75:
                if geo_confidence == "street":
                    print(f"  ⚠ Geocode corrected >75 mi for {name}; replacing stale coordinates using full street address")
                    rec["lat"], rec["lng"] = lat, lng
                    rec["approx"] = False
                else:
                    print(f"  ⚠ Geocode changed >75 mi for {name}; clearing stale coordinates because new query was low-confidence")
                    rec["lat"], rec["lng"] = None, None
                    rec["approx"] = True
            else:
                rec["lat"], rec["lng"] = lat, lng
                rec["approx"] = False
        elif raw_ok:
            rec["lat"], rec["lng"] = None, None
            rec["approx"] = True

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
            if matches_negative_name(old_name, negative_terms):
                excl_neg_preserved += 1
                print(f"  ✗ Negative (preserved): {old_name}")
                continue
            # Do not preserve stale automated signals from records no longer returned
            # by current HubSpot/source lookups. Only manual records survive.
            cleaned_signals = [s for s in old.get("signals",[]) if s == "manual"]
            old = dict(old)
            old["signals"] = cleaned_signals
            if cleaned_signals or old.get("src","") == "manual":
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
