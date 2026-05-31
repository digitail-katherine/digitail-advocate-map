#!/usr/bin/env python3
"""
Digitail Advocate Map — v4

Verification rules:
  - Must have ≥1 affirmative signal (NOT just tenure)
  - Must have email OR phone
  - Must be in North America (geocode validated)
  - Any recent negative signal = excluded
  - Non-clinic records = excluded
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
HS_TOKEN      = os.environ.get("HUBSPOT_TOKEN", "")
IC_TOKEN      = os.environ.get("INTERCOM_TOKEN", "")
SLACK_URL     = os.environ.get("SLACK_WEBHOOK", "")
REDDIT_ID     = os.environ.get("REDDIT_CLIENT_ID", "")
REDDIT_SECRET = os.environ.get("REDDIT_CLIENT_SECRET", "")
GOOGLE_KEY    = os.environ.get("GOOGLE_API_KEY", "")
GOOGLE_CSE_ID = os.environ.get("GOOGLE_CSE_ID", "")
GOOGLE_PLACE  = os.environ.get("GOOGLE_PLACE_ID", "")
FB_TOKEN      = os.environ.get("FACEBOOK_PAGE_TOKEN", "")
DATA_FILE     = "advocates.json"

# ── Signals ───────────────────────────────────────────────────────────────────
# hs_long_tenure intentionally excluded — not a happiness signal
SIGNAL_LABELS = {
    "hs_testimonial":    "Case Study / Article Published",
    "hs_dsp":            "DSP Payment Processing",
    "hs_positive_note":  "Team Verified Happy",
    "hs_long_tenure":    "Active Customer 12+ Months",
    "intercom_csat":     "5★ Support Rating",
    "capterra_positive": "Capterra Review",
    "g2_positive":       "G2 Review",
    "softwareadvice":    "Software Advice Review",
    "getapp":            "GetApp Review",
    "reddit_mention":    "Reddit Mention",
    "google_review":     "Google Review",
    "google_mention":    "Web Mention",
    "facebook_review":   "Facebook Review",
    "manual":            "Manually Verified",
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

# ── Practice format detection ─────────────────────────────────────────────────
MOBILE_TERMS = [
    "mobile", "house call", "housecall", "traveling", "on wheels", "doorstep",
    "home visit", "home vet", "at home", "at-home", "concierge", "on-site",
    "onsite", "road", "wagon", "roaming", "wandervet", "doggie motion",
    "paws on the move", "fetch the vet", "rideau river", "clinic nomad",
]
TELE_TERMS = ["tele", "virtual", "online", "remote"]

def infer_format(name: str) -> str:
    n = name.lower()
    if any(t in n for t in TELE_TERMS):
        return "telemedicine"
    if any(t in n for t in MOBILE_TERMS):
        return "mobile"
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
    state   = (props.get("state") or "").strip().upper()
    if country in NA_COUNTRIES:
        return True
    if not country and (state in US_STATES or state in CA_PROVINCES):
        return True
    return False

def in_na_bounds(lat: float, lng: float) -> bool:
    """Validate geocoded coordinate is actually in North America."""
    return 14.5 <= lat <= 72.0 and -170.0 <= lng <= -50.0

def is_negative(props: dict) -> bool:
    notes = (props.get("internal_comments") or "").lower()
    return any(k in notes for k in NEGATIVE_KW)

def is_excluded_non_clinic(props: dict) -> bool:
    name   = (props.get("name")   or "").lower()
    domain = (props.get("domain") or "").lower()
    return any(f in name for f in EXCLUDE_NAME_FRAGMENTS) or \
           any(d in domain for d in EXCLUDE_DOMAINS)

def has_contact_info(props: dict, rec: dict) -> bool:
    """Must have at least an email or phone number."""
    email = (props.get("contact_email") or rec.get("email") or "").strip()
    phone = (props.get("phone")         or rec.get("phone") or "").strip()
    return bool(email) or bool(phone)

def address_is_geocodable(addr: str) -> bool:
    """Need at least 2 meaningful parts to geocode reliably (avoid state-only strings)."""
    parts = [p.strip() for p in addr.split(",") if p.strip() and len(p.strip()) > 2]
    return len(parts) >= 2

# ── HubSpot ───────────────────────────────────────────────────────────────────
HS_PROPS = [
    "name", "address", "address2", "city", "state", "zip", "country",
    "contact_email", "phone", "current_pims", "domain", "createdate",
    "media_testimonials_dsp", "internal_comments",
    "hs_current_customer", "champion_contact",
]

def hs_h():
    return {"Authorization": f"Bearer {HS_TOKEN}", "Content-Type": "application/json"}

def hs_signals(props: dict) -> list:
    sigs  = []
    media = (props.get("media_testimonials_dsp") or "").lower()
    notes = (props.get("internal_comments") or "").lower()
    if any(k in media for k in ["article", "video", "testimonial"]):
        sigs.append("hs_testimonial")
    if any(k in media for k in ["dsp", "6 figure", "6-figure"]):
        sigs.append("hs_dsp")
    if any(k in notes for k in POSITIVE_KW) and not any(k in notes for k in NEGATIVE_KW):
        sigs.append("hs_positive_note")
    # Tenure — valid signal, displayed transparently so reps know the basis
    try:
        created = props.get("createdate", "")
        if created:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - dt).days > 365:
                sigs.append("hs_long_tenure")
    except Exception:
        pass
    return list(set(sigs))

def build_address(props: dict) -> str:
    return ", ".join(
        (props.get(k) or "").strip()
        for k in ["address", "city", "state", "zip"]
        if (props.get(k) or "").strip()
    )

def hs_address_with_fallback(hs_id: str, props: dict) -> str:
    addr = build_address(props)
    if addr and len(addr) > 8:
        return addr
    try:
        r = requests.get(
            f"https://api.hubapi.com/crm/v3/objects/companies/{hs_id}/associations/contacts",
            headers=hs_h(), timeout=10,
        )
        if r.status_code == 200:
            for c in r.json().get("results", [])[:3]:
                cr = requests.get(
                    f"https://api.hubapi.com/crm/v3/objects/contacts/{c['id']}",
                    params={"properties": "address,city,state,zip,country"},
                    headers=hs_h(), timeout=8,
                )
                if cr.status_code == 200:
                    caddr = build_address(cr.json().get("properties", {}))
                    if caddr and len(caddr) > 8:
                        return caddr
    except Exception:
        pass
    return addr

def hs_get_closedwon_company_ids() -> set:
    company_ids = set()
    for filter_obj in [
        {"propertyName": "hs_is_closed_won", "operator": "EQ", "value": "true"},
        {"propertyName": "dealstage",         "operator": "EQ", "value": "closedwon"},
    ]:
        after = None
        while True:
            payload = {"filterGroups": [{"filters": [filter_obj]}], "properties": ["dealname"], "limit": 200}
            if after:
                payload["after"] = after
            r = requests.post("https://api.hubapi.com/crm/v3/objects/deals/search",
                              json=payload, headers=hs_h(), timeout=15)
            if r.status_code != 200:
                break
            data     = r.json()
            deal_ids = [d["id"] for d in data.get("results", [])]
            if deal_ids:
                for i in range(0, len(deal_ids), 100):
                    chunk = deal_ids[i:i+100]
                    ar = requests.post(
                        "https://api.hubapi.com/crm/v4/associations/deals/companies/batch/read",
                        json={"inputs": [{"id": did} for did in chunk]},
                        headers=hs_h(), timeout=15,
                    )
                    if ar.status_code == 200:
                        for item in ar.json().get("results", []):
                            for assoc in item.get("to", []):
                                company_ids.add(str(assoc.get("toObjectId", "")))
                    time.sleep(0.1)
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
        if company_ids:
            break
    print(f"  HubSpot closed-won: {len(company_ids)} associated companies")
    return company_ids

def hs_get_customers() -> list:
    closed_won_ids = hs_get_closedwon_company_ids()
    results_by_id  = {}
    for filt in [
        {"propertyName": "lifecyclestage",     "operator": "EQ", "value": "customer"},
        {"propertyName": "hs_current_customer","operator": "EQ", "value": "true"},
    ]:
        after, batch = None, {}
        while True:
            payload = {"filterGroups": [{"filters": [filt]}], "properties": HS_PROPS, "limit": 100}
            if after:
                payload["after"] = after
            r = requests.post("https://api.hubapi.com/crm/v3/objects/companies/search",
                              json=payload, headers=hs_h(), timeout=15)
            if r.status_code != 200:
                print(f"  HubSpot filter error: {r.status_code}")
                break
            data = r.json()
            for c in data.get("results", []):
                batch[c["id"]] = c
            after = data.get("paging", {}).get("next", {}).get("after")
            if not after:
                break
        if batch:
            results_by_id.update(batch)
            print(f"  HubSpot filter '{filt['propertyName']}': {len(batch)} companies")
            break
        else:
            print(f"  HubSpot filter '{filt['propertyName']}': 0, trying next…")

    new_ids = closed_won_ids - set(results_by_id.keys())
    print(f"  HubSpot: {len(results_by_id)} customers + {len(new_ids)} closed-won-only")
    for i in range(0, len(list(new_ids)), 100):
        chunk = list(new_ids)[i:i+100]
        r = requests.post(
            "https://api.hubapi.com/crm/v3/objects/companies/batch/read",
            json={"inputs": [{"id": cid} for cid in chunk], "properties": HS_PROPS},
            headers=hs_h(), timeout=15,
        )
        if r.status_code == 200:
            for c in r.json().get("results", []):
                results_by_id[c["id"]] = c
        time.sleep(0.15)
    all_cos = list(results_by_id.values())
    print(f"  HubSpot total: {len(all_cos)} before filters")
    return all_cos

# ── Geocoding ─────────────────────────────────────────────────────────────────
def geocode(address: str, country: str = ""):
    if not address_is_geocodable(address):
        return None, None
    # Add country context to improve accuracy
    full_addr = address
    cl = (country or "").lower()
    if cl in {"united states", "us", "usa"} and "united states" not in address.lower():
        full_addr = f"{address}, United States"
    elif cl in {"canada", "ca"} and "canada" not in address.lower():
        full_addr = f"{address}, Canada"
    elif cl in {"mexico", "mx"} and "mexico" not in address.lower():
        full_addr = f"{address}, Mexico"

    time.sleep(1.2)
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"format": "json", "q": full_addr, "limit": 1},
            headers={"User-Agent": "DigitailAdvocateMap/4.0 (internal-sales-tool)"},
            timeout=10,
        )
        if r.status_code == 200:
            d = r.json()
            if d:
                lat, lng = round(float(d[0]["lat"]), 5), round(float(d[0]["lon"]), 5)
                if in_na_bounds(lat, lng):
                    return lat, lng
                else:
                    print(f"  Geocode rejected (outside NA bounds): {address} → {lat},{lng}")
    except Exception as e:
        print(f"  Geocode failed for '{address}': {e}")
    return None, None

# ── Intercom ──────────────────────────────────────────────────────────────────
def _ic_company_name(contact_id: str, headers: dict) -> str:
    try:
        cr = requests.get(f"https://api.intercom.io/contacts/{contact_id}", headers=headers, timeout=8)
        if cr.status_code == 200:
            cdata = cr.json()
            cos   = cdata.get("companies", {}).get("data", [])
            return cos[0].get("name", "") if cos else (cdata.get("name", "") or "")
    except Exception:
        pass
    return ""

def _ic_extract(convo: dict, headers: dict, results: dict):
    robj   = convo.get("conversation_rating") or {}
    remark = (robj.get("remark") or "").strip()
    contacts = convo.get("contacts", {}).get("contacts", [])
    name = _ic_company_name(contacts[0]["id"], headers) if contacts else ""
    if name:
        key = name.lower().strip()
        if key not in results:
            results[key] = {"signal": "intercom_csat",
                            "quote":  remark[:300] if remark else None, "company": name}

def fetch_intercom_csat() -> dict:
    if not IC_TOKEN:
        return {}
    headers = {"Authorization": f"Bearer {IC_TOKEN}", "Accept": "application/json", "Intercom-Version": "2.10"}
    results = {}
    for field, value in [("conversation_rating.rating", "amazing"), ("rating", "amazing")]:
        try:
            r = requests.post(
                "https://api.intercom.io/conversations/search",
                json={"query": {"operator": "AND", "value": [{"field": field, "operator": "=", "value": value}]},
                      "pagination": {"per_page": 150}},
                headers=headers, timeout=15,
            )
            if r.status_code == 200:
                convos = r.json().get("conversations", [])
                if convos:
                    for c in convos:
                        _ic_extract(c, headers, results)
                    print(f"  Intercom CSAT: {len(results)} records")
                    return results
        except Exception as e:
            print(f"  Intercom search failed: {e}")
    try:
        r = requests.get(
            "https://api.intercom.io/conversations",
            params={"per_page": 150, "order": "desc", "display_as": "plaintext"},
            headers=headers, timeout=15,
        )
        if r.status_code == 200:
            for c in r.json().get("conversations", []):
                robj = c.get("conversation_rating") or {}
                val  = str(robj.get("value", "") or robj.get("rating", ""))
                if val in ["5", "amazing", "great"]:
                    _ic_extract(c, headers, results)
    except Exception as e:
        print(f"  Intercom fallback failed: {e}")
    print(f"  Intercom (fallback): {len(results)} CSAT records")
    return results

def fetch_intercom_negative() -> set:
    if not IC_TOKEN:
        return set()
    headers = {"Authorization": f"Bearer {IC_TOKEN}", "Accept": "application/json", "Intercom-Version": "2.10"}
    bad = set()
    for field, value in [("conversation_rating.rating","terrible"),("conversation_rating.rating","bad"),
                         ("rating","terrible"),("rating","bad")]:
        try:
            r = requests.post(
                "https://api.intercom.io/conversations/search",
                json={"query": {"operator":"AND","value":[{"field":field,"operator":"=","value":value}]},
                      "pagination": {"per_page": 100}},
                headers=headers, timeout=15,
            )
            if r.status_code == 200:
                convos = r.json().get("conversations", [])
                if convos:
                    tmp = {}
                    for c in convos:
                        _ic_extract(c, headers, tmp)
                    bad.update(tmp.keys())
                    break
        except Exception:
            continue
    if bad:
        print(f"  Intercom negative: {len(bad)} companies flagged")
    return bad

# ── Review scrapers ───────────────────────────────────────────────────────────
def _scrape_reviews(url, signal_key, card_sels, rating_sels, body_sels, reviewer_sels, min_rating=4.0):
    if not BS4:
        return []
    results = []
    try:
        r = requests.get(url, headers={"User-Agent":"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36","Accept-Language":"en-US,en;q=0.9"}, timeout=15)
        if r.status_code != 200:
            print(f"  {signal_key}: HTTP {r.status_code}")
            return []
        soup  = BeautifulSoup(r.text, "html.parser")
        cards = []
        for sel in card_sels:
            cards = soup.select(sel)
            if cards:
                break
        for card in cards:
            try:
                rating = 0.0
                for sel in rating_sels:
                    el = card.select_one(sel)
                    if el:
                        nums = re.findall(r'(\d+\.?\d*)', el.get("aria-label","") or el.get_text())
                        if nums:
                            rating = float(nums[0])
                            break
                if rating and rating < min_rating:
                    continue
                text = ""
                for sel in body_sels:
                    el = card.select_one(sel)
                    if el:
                        text = el.get_text(" ", strip=True)[:400]
                        break
                if not text or len(text) < 20:
                    continue
                reviewer = ""
                for sel in reviewer_sels:
                    el = card.select_one(sel)
                    if el:
                        reviewer = el.get_text(strip=True)
                        break
                results.append({"source":signal_key,"reviewer":reviewer,"text":text,"rating":rating,"signal":signal_key})
            except Exception:
                continue
        print(f"  {signal_key}: {len(results)} reviews")
    except Exception as e:
        print(f"  {signal_key} failed: {e}")
    return results

def scrape_capterra():
    return _scrape_reviews("https://www.capterra.com/p/167764/Digitail/","capterra_positive",
        ["[data-testid='review-card']",".review-card","[class*='ReviewCard']","article[class*='review']"],
        ["[aria-label*='star']","[aria-label*='out of']","[class*='rating']"],
        ["p","[class*='body']","[class*='Body']","[class*='review-text']"],
        ["[class*='reviewer']","[class*='author']","[class*='Reviewer']"])

def scrape_g2():
    return _scrape_reviews("https://www.g2.com/products/digitail/reviews","g2_positive",
        ["[itemprop='review']","[class*='Paper__StyledPaper']","article"],
        ["[itemprop='ratingValue']","[class*='stars']","[aria-label*='star']"],
        ["[itemprop='reviewBody']","[class*='formatted-text']","p"],
        ["[itemprop='author']","[class*='reviewer']"])

def scrape_software_advice():
    return _scrape_reviews("https://www.softwareadvice.com/veterinary/digitail-profile/reviews/","softwareadvice",
        ["[class*='review-card']","[class*='ReviewCard']","article"],
        ["[class*='rating']","[aria-label*='star']"],
        ["[class*='review-body']","[class*='ReviewBody']","p"],
        ["[class*='reviewer']","[class*='author']"])

def scrape_getapp():
    return _scrape_reviews("https://www.getapp.com/veterinary-practice-management-software/a/digitail/reviews/","getapp",
        ["[class*='review']","article","[data-test*='review']"],
        ["[class*='rating']","[aria-label*='star']"],
        ["[class*='body']","p"],
        ["[class*='reviewer']","[class*='author']"])

def scrape_trustpilot():
    return _scrape_reviews("https://www.trustpilot.com/review/digitail.io","capterra_positive",
        ["[data-service-review-card-paper]","[class*='reviewCard']","article"],
        ["[data-service-review-rating]","[class*='starRating']"],
        ["[data-service-review-text-typography]","[class*='reviewContent']","p"],
        ["[class*='consumerName']","[class*='reviewer']"])

# ── Reddit ────────────────────────────────────────────────────────────────────
def fetch_reddit_mentions() -> list:
    if not REDDIT_ID or not REDDIT_SECRET:
        print("  Reddit: no credentials, skipping")
        return []
    results = []
    try:
        tok = requests.post("https://www.reddit.com/api/v1/access_token",
            auth=requests.auth.HTTPBasicAuth(REDDIT_ID, REDDIT_SECRET),
            data={"grant_type":"client_credentials"},
            headers={"User-Agent":"DigitailAdvocateMap/4.0"}, timeout=10).json().get("access_token","")
        if not tok:
            print("  Reddit: auth failed"); return []
        hdrs = {"Authorization":f"bearer {tok}","User-Agent":"DigitailAdvocateMap/4.0"}
        for query in ["Digitail veterinary software","Digitail PIMS","Digitail vet"]:
            r = requests.get("https://oauth.reddit.com/search",
                params={"q":query,"sort":"new","limit":50,"type":"link,comment"},
                headers=hdrs, timeout=15)
            if r.status_code == 200:
                for post in r.json().get("data",{}).get("children",[]):
                    d    = post.get("data",{})
                    text = d.get("selftext","") or d.get("body","") or d.get("title","")
                    tl   = text.lower()
                    if any(p in tl for p in POSITIVE_KW) and not any(n in tl for n in NEGATIVE_KW):
                        results.append({"source":"reddit","text":text[:400],"author":d.get("author",""),
                                        "url":f"https://reddit.com{d.get('permalink','')}","signal":"reddit_mention"})
            time.sleep(0.5)
        print(f"  Reddit: {len(results)} positive mentions")
    except Exception as e:
        print(f"  Reddit failed: {e}")
    return results

# ── Google ────────────────────────────────────────────────────────────────────
def fetch_google_reviews() -> list:
    if not GOOGLE_KEY or not GOOGLE_PLACE:
        print("  Google Reviews: no credentials, skipping"); return []
    results = []
    try:
        r = requests.get("https://maps.googleapis.com/maps/api/place/details/json",
            params={"place_id":GOOGLE_PLACE,"fields":"reviews,name","key":GOOGLE_KEY,"reviews_sort":"newest"}, timeout=10)
        if r.status_code == 200:
            for rev in r.json().get("result",{}).get("reviews",[]):
                if rev.get("rating",0) >= 4 and rev.get("text",""):
                    results.append({"source":"google","reviewer":rev.get("author_name",""),
                                    "text":rev["text"][:400],"rating":rev["rating"],"signal":"google_review"})
        print(f"  Google Reviews: {len(results)} reviews")
    except Exception as e:
        print(f"  Google Reviews failed: {e}")
    return results

def fetch_google_web_mentions() -> list:
    if not GOOGLE_KEY or not GOOGLE_CSE_ID:
        print("  Google CSE: no credentials, skipping"); return []
    results = []
    try:
        for query in ['"Digitail" veterinary review','"Digitail" PIMS "switched"','"Digitail" vet "recommend"']:
            r = requests.get("https://www.googleapis.com/customsearch/v1",
                params={"q":query,"key":GOOGLE_KEY,"cx":GOOGLE_CSE_ID,"num":10}, timeout=10)
            if r.status_code == 200:
                for item in r.json().get("items",[]):
                    snippet = item.get("snippet","")
                    sl = snippet.lower()
                    if any(p in sl for p in POSITIVE_KW) and not any(n in sl for n in NEGATIVE_KW):
                        results.append({"source":"google_web","text":snippet[:400],
                                        "url":item.get("link",""),"signal":"google_mention"})
            time.sleep(0.3)
        print(f"  Google CSE: {len(results)} web mentions")
    except Exception as e:
        print(f"  Google CSE failed: {e}")
    return results

def fetch_facebook_reviews() -> list:
    if not FB_TOKEN:
        print("  Facebook: no token, skipping"); return []
    results = []
    try:
        r = requests.get("https://graph.facebook.com/v18.0/me/ratings",
            params={"access_token":FB_TOKEN,"fields":"reviewer{name},rating,review_text,created_time","limit":50}, timeout=10)
        if r.status_code == 200:
            for rev in r.json().get("data",[]):
                if rev.get("rating",0) >= 4 and rev.get("review_text",""):
                    results.append({"source":"facebook","reviewer":rev.get("reviewer",{}).get("name",""),
                                    "text":rev["review_text"][:400],"rating":rev["rating"],"signal":"facebook_review"})
        print(f"  Facebook: {len(results)} reviews")
    except Exception as e:
        print(f"  Facebook failed: {e}")
    return results

# ── Name matching ─────────────────────────────────────────────────────────────
def names_match(a: str, b: str) -> bool:
    a, b = a.lower().strip(), b.lower().strip()
    if not a or not b:
        return False
    if a in b or b in a:
        return True
    wa = {w for w in re.split(r'\W+', a) if len(w) >= 4}
    wb = {w for w in re.split(r'\W+', b) if len(w) >= 4}
    return len(wa & wb) >= 2

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            existing: list = json.load(f)
    except FileNotFoundError:
        existing = []

    # Clean any bad coordinates from existing records (outside NA bounds)
    for rec in existing:
        if rec.get("lat") and not in_na_bounds(rec["lat"], rec.get("lng", 0)):
            rec["lat"], rec["lng"] = None, None

    by_hs_id = {str(a["hsId"]): a for a in existing if a.get("hsId")}
    by_name  = {a["name"].lower().strip(): a for a in existing}
    original = json.dumps(existing, sort_keys=True)
    print(f"Existing advocates: {len(existing)}\n")

    # ── Gather signals ─────────────────────────────────────────────────────────
    print("── Intercom ───────────────────────────────────────────────")
    ic_sigs     = fetch_intercom_csat()
    ic_negative = fetch_intercom_negative()

    print("\n── Review sites ───────────────────────────────────────────")
    all_reviews = (scrape_capterra() + scrape_g2() +
                   scrape_software_advice() + scrape_getapp() + scrape_trustpilot())

    print("\n── Reddit ─────────────────────────────────────────────────")
    reddit_mentions = fetch_reddit_mentions()

    print("\n── Google ─────────────────────────────────────────────────")
    google_reviews  = fetch_google_reviews()
    google_mentions = fetch_google_web_mentions()

    print("\n── Facebook ───────────────────────────────────────────────")
    fb_reviews = fetch_facebook_reviews()

    all_external = all_reviews + reddit_mentions + google_reviews + google_mentions + fb_reviews

    print("\n── HubSpot customers ──────────────────────────────────────")
    hs_customers = hs_get_customers()

    # ── Process each company ───────────────────────────────────────────────────
    new_advocates = []
    added, updated = [], []
    excluded_no_signal, excluded_no_contact, excluded_bad = 0, 0, 0
    next_id = max((a.get("id", 0) for a in existing), default=53) + 1

    for customer in hs_customers:
        hs_id = str(customer["id"])
        props = customer.get("properties", {})
        name  = (props.get("name") or "").strip()
        if not name:
            continue

        # Hard exclusions
        if is_excluded_non_clinic(props):
            continue
        if not is_north_america(props):
            continue
        if is_negative(props):
            excluded_bad += 1
            continue
        name_lc = name.lower().strip()
        if name_lc in ic_negative or any(names_match(name, bad) for bad in ic_negative):
            excluded_bad += 1
            print(f"  ✗ Bad CSAT: {name}")
            continue

        # Collect positive signals
        signals = hs_signals(props)
        for ic_key in ic_sigs:
            if names_match(name, ic_key):
                signals.append("intercom_csat")
                break
        matched_quotes = []
        for ext in all_external:
            reviewer = ext.get("reviewer","") or ext.get("author","") or ""
            if names_match(name, reviewer) or names_match(name, ext.get("text","")):
                signals.append(ext["signal"])
                if ext.get("text"):
                    matched_quotes.append(ext["text"])
        signals = [s for s in set(signals) if s]

        # Must have at least 1 signal
        if not signals:
            excluded_no_signal += 1
            continue
        if not rec:
            for k, v in by_name.items():
                if names_match(name, k):
                    rec = dict(v)
                    break

        is_new = rec is None
        if is_new:
            rec = {"id":next_id,"name":name,"ct":"general","src":"HubSpot",
                   "verify":False,"approx":False,"quote":None,"metrics":None,
                   "pm":None,"aiAdopter":None,"lat":None,"lng":None,
                   "features":None,"dgtId":None}
            next_id += 1
        else:
            rec = dict(rec)

        # Refresh from HubSpot
        rec["hsId"] = hs_id
        rec["name"] = name
        rec["format"] = infer_format(name)
        for src, dest in [("city","city"),("state","st"),("contact_email","email"),
                          ("phone","phone"),("current_pims","pims")]:
            v = (props.get(src) or "").strip()
            if v:
                rec[dest] = v

        # Require contact info — flag but don't exclude
        if not has_contact_info(props, rec):
            excluded_no_contact += 1
            # Still include — just won't have email/phone in popup

        # Address + geocode with NA bounds validation
        new_addr = hs_address_with_fallback(hs_id, props)
        country  = (props.get("country") or "").strip()
        addr_changed = new_addr and new_addr != rec.get("address","")
        if addr_changed:
            rec["address"] = new_addr
        if (addr_changed or not rec.get("lat")) and new_addr:
            lat, lng = geocode(new_addr, country)
            if lat:
                rec["lat"], rec["lng"] = lat, lng
                rec["approx"] = False
            else:
                # Invalid geocode — clear any bad existing coords
                if rec.get("lat") and not in_na_bounds(rec["lat"], rec.get("lng",0)):
                    rec["lat"], rec["lng"] = None, None

        # Also validate any existing coordinates
        if rec.get("lat") and not in_na_bounds(rec["lat"], rec.get("lng", 0)):
            print(f"  Clearing invalid coords for {name}: {rec['lat']},{rec['lng']}")
            rec["lat"], rec["lng"] = None, None

        if "manual" in rec.get("signals", []):
            signals.append("manual")
        rec["signals"]  = sorted(set(signals))
        rec["verified"] = True

        for ic_key, ic_val in ic_sigs.items():
            if names_match(name, ic_key) and ic_val.get("quote"):
                rec["quote"] = ic_val["quote"]
                break
        if not rec.get("quote") and matched_quotes:
            rec["quote"] = matched_quotes[0]

        if is_new:
            added.append(name)
            print(f"  + New: {name}")
        else:
            updated.append(name)
        new_advocates.append(rec)

    # Preserve non-HubSpot records
    hs_ids_in = {str(a.get("hsId","")) for a in new_advocates}
    names_in  = {a["name"].lower() for a in new_advocates}
    for old in existing:
        already = (str(old.get("hsId","")) in hs_ids_in or old["name"].lower() in names_in)
        if not already and (old.get("signals") or old.get("src","") in ("Capterra","Usage Report","Intercom CSAT")):
            # Still validate coords for preserved records
            if old.get("lat") and not in_na_bounds(old["lat"], old.get("lng",0)):
                old = dict(old)
                old["lat"], old["lng"] = None, None
            new_advocates.append(old)

    new_advocates.sort(key=lambda a: a.get("name",""))

    print(f"\nExcluded: {excluded_no_signal} (no signal), "
          f"{excluded_no_contact} (no contact info), {excluded_bad} (negative signal)")

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
        if added:
            lines.append(f"✅ *{len(added)} new:* " + ", ".join(added[:6]))
        if updated:
            lines.append(f"📝 Updated: " + ", ".join(updated[:6]) +
                         (f" +{len(updated)-6} more" if len(updated) > 6 else ""))
        lines.append(f"🚫 Excluded: {excluded_no_signal} no signal · {excluded_no_contact} no contact · {excluded_bad} negative")
        if not added and not updated:
            lines.append("No data changes this week ✓")
        lines.append(f"_Run: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC_")
        requests.post(SLACK_URL, json={"text": "\n".join(lines)}, timeout=10)

if __name__ == "__main__":
    main()
