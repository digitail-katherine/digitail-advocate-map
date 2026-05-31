#!/usr/bin/env python3
"""
Digitail Advocate Map — v2 Update Script

Architecture:
  - Source of truth: ALL active HubSpot customers
  - Each customer is checked for happiness signals across HubSpot, Intercom, Capterra
  - Only customers with ≥1 verified positive signal appear on the map
  - Rocky/negative customers are flagged and suppressed automatically

Required env vars:
  HUBSPOT_TOKEN   — HubSpot Private App token (CRM objects: read)
  INTERCOM_TOKEN  — Intercom Access Token
  SLACK_WEBHOOK   — Slack Incoming Webhook URL (optional)
"""

import json, os, re, sys, time
from datetime import datetime, timedelta, timezone
import requests

try:
    from bs4 import BeautifulSoup
    BS4 = True
except ImportError:
    BS4 = False
    print("  Warning: beautifulsoup4 not installed — Capterra scraping disabled")

HS_TOKEN  = os.environ.get("HUBSPOT_TOKEN", "")
IC_TOKEN  = os.environ.get("INTERCOM_TOKEN", "")
SLACK_URL = os.environ.get("SLACK_WEBHOOK", "")
DATA_FILE = "advocates.json"

# ── Signal definitions ────────────────────────────────────────────────────────

SIGNAL_LABELS = {
    "hs_testimonial":    "Article / testimonial published",
    "hs_dsp":            "Active DSP processing",
    "hs_long_tenure":    "Customer 12+ months",
    "hs_positive_note":  "Positive HubSpot note",
    "intercom_csat":     "5★ Intercom CSAT",
    "capterra_positive": "Capterra review (4–5★)",
    "manual":            "Manually verified",
}

NEGATIVE_KEYWORDS = [
    "not happy", "unhappy", "at risk", "churn", "cancel",
    "triple check", "do not contact", "rocky", "leaving",
]
POSITIVE_KEYWORDS = [
    "great", "loves", "happy", "excellent", "recommend",
    "advocate", "good experience", "amazing", "fantastic",
]

# ── HubSpot ───────────────────────────────────────────────────────────────────

HS_PROPS = [
    "name", "address", "address2", "city", "state", "zip", "country",
    "contact_email", "phone", "current_pims", "domain", "createdate",
    "media_testimonials_dsp", "internal_comments",
    "hs_current_customer", "champion_contact",
]

def hs_headers():
    return {"Authorization": f"Bearer {HS_TOKEN}", "Content-Type": "application/json"}

def hs_get_all_customers() -> list[dict]:
    """Pull all HubSpot companies where hs_current_customer = true, paginated."""
    url, results, after = "https://api.hubapi.com/crm/v3/objects/companies/search", [], None
    while True:
        payload = {
            "filterGroups": [{"filters": [{"propertyName": "hs_current_customer", "operator": "EQ", "value": "true"}]}],
            "properties": HS_PROPS,
            "limit": 100,
        }
        if after:
            payload["after"] = after
        r = requests.post(url, json=payload, headers=hs_headers(), timeout=15)
        if r.status_code != 200:
            print(f"  HubSpot error: {r.status_code} — {r.text[:200]}")
            break
        data = r.json()
        results.extend(data.get("results", []))
        after = data.get("paging", {}).get("next", {}).get("after")
        if not after:
            break
        time.sleep(0.15)
    print(f"  HubSpot: {len(results)} current customers fetched")
    return results

def hs_signals(props: dict) -> list[str]:
    """Return list of positive signal keys from HubSpot company properties."""
    signals = []
    media = (props.get("media_testimonials_dsp") or "").lower()
    notes = (props.get("internal_comments") or "").lower()

    # Testimonial/article/video
    if any(k in media for k in ["article", "video", "testimonial", "dsp", "6 figure", "6-figure"]):
        signals.append("hs_testimonial")
    # DSP
    if any(k in media for k in ["dsp", "6 figure", "6-figure"]):
        signals.append("hs_dsp")
    # Long tenure
    try:
        created = props.get("createdate", "")
        if created:
            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if (datetime.now(timezone.utc) - dt).days > 365:
                signals.append("hs_long_tenure")
    except Exception:
        pass
    # Positive notes (only if no negative keywords present)
    if any(k in notes for k in POSITIVE_KEYWORDS) and not any(k in notes for k in NEGATIVE_KEYWORDS):
        signals.append("hs_positive_note")

    return list(set(signals))

def is_negative(props: dict) -> bool:
    """True if HubSpot notes contain red-flag language."""
    notes = (props.get("internal_comments") or "").lower()
    return any(k in notes for k in NEGATIVE_KEYWORDS)

def build_address(props: dict) -> str:
    parts = [(props.get(k) or "").strip() for k in ["address", "city", "state", "zip"]]
    return ", ".join(p for p in parts if p)

# ── Geocoding ─────────────────────────────────────────────────────────────────

def geocode(address: str):
    time.sleep(1.2)  # Nominatim rate limit
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"format": "json", "q": address, "limit": 1},
            headers={"User-Agent": "DigitailAdvocateMap/2.0 (internal-sales-tool)"},
            timeout=10,
        )
        if r.status_code == 200:
            d = r.json()
            if d:
                return round(float(d[0]["lat"]), 5), round(float(d[0]["lon"]), 5)
    except Exception as e:
        print(f"  Geocode failed for '{address}': {e}")
    return None, None

# ── Intercom CSAT ─────────────────────────────────────────────────────────────

def fetch_intercom_csat() -> dict:
    """
    Returns dict keyed by lowercased company name →
    {"signal": "intercom_csat", "quote": "...", "company": "..."}
    Tries multiple filter field names to handle Intercom API version differences.
    """
    if not IC_TOKEN:
        return {}

    headers = {
        "Authorization": f"Bearer {IC_TOKEN}",
        "Accept": "application/json",
        "Intercom-Version": "2.10",
    }
    results = {}

    # Attempt 1: Search endpoint with different filter field names
    for field, value in [
        ("conversation_rating.rating", "amazing"),
        ("rating", "amazing"),
        ("conversation_rating.rating", "great"),
    ]:
        try:
            r = requests.post(
                "https://api.intercom.io/conversations/search",
                json={"query": {"operator": "AND", "value": [{"field": field, "operator": "=", "value": value}]}, "pagination": {"per_page": 150}},
                headers=headers, timeout=15,
            )
            if r.status_code == 200:
                convos = r.json().get("conversations", [])
                if convos:
                    for c in convos:
                        _extract_intercom_record(c, headers, results)
                    print(f"  Intercom CSAT: {len(results)} records via search ({field}={value})")
                    break
        except Exception as e:
            print(f"  Intercom search attempt failed: {e}")

    # Attempt 2: List endpoint fallback — scan for rated conversations
    if not results:
        try:
            r = requests.get(
                "https://api.intercom.io/conversations",
                params={"per_page": 150, "order": "desc", "display_as": "plaintext"},
                headers=headers, timeout=15,
            )
            if r.status_code == 200:
                for c in r.json().get("conversations", []):
                    robj = c.get("conversation_rating") or {}
                    val = str(robj.get("value", "") or robj.get("rating", ""))
                    if val in ["5", "amazing", "great"]:
                        _extract_intercom_record(c, headers, results)
                print(f"  Intercom CSAT: {len(results)} records via list endpoint")
        except Exception as e:
            print(f"  Intercom list fallback failed: {e}")

    return results

def _extract_intercom_record(convo: dict, headers: dict, results: dict):
    """Extract company name + quote from a single Intercom conversation."""
    robj   = convo.get("conversation_rating") or {}
    remark = (robj.get("remark") or "").strip()
    name   = ""

    # Try to get company name via contact lookup
    contacts = convo.get("contacts", {}).get("contacts", [])
    if contacts:
        cid = contacts[0].get("id")
        if cid:
            try:
                cr = requests.get(f"https://api.intercom.io/contacts/{cid}", headers=headers, timeout=8)
                if cr.status_code == 200:
                    cdata = cr.json()
                    cos = cdata.get("companies", {}).get("data", [])
                    if cos:
                        name = cos[0].get("name", "")
                    if not name:
                        name = cdata.get("name", "") or cdata.get("email", "")
            except Exception:
                pass

    if name:
        key = name.lower().strip()
        if key not in results:
            results[key] = {"signal": "intercom_csat", "quote": remark[:300] if remark else None, "company": name}

# ── Capterra scraping ─────────────────────────────────────────────────────────

def scrape_capterra() -> list[dict]:
    """
    Scrape Digitail's Capterra page for 4-5★ reviews.
    Returns list of {"reviewer": str, "rating": float, "text": str}.
    Fails gracefully — Capterra may block scrapers.
    """
    if not BS4:
        return []

    ua = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
    session = requests.Session()
    session.headers.update({"User-Agent": ua, "Accept-Language": "en-US,en;q=0.9"})
    results = []

    for url in [
        "https://www.capterra.com/p/167764/Digitail/",
        "https://www.capterra.com/reviews/167764/Digitail",
    ]:
        try:
            r = session.get(url, timeout=15)
            if r.status_code != 200:
                continue
            soup = BeautifulSoup(r.text, "html.parser")

            # Capterra's HTML structure changes — try multiple selectors
            cards = (soup.select("[data-testid='review-card']") or
                     soup.select(".review-card") or
                     soup.select("[class*='ReviewCard']") or
                     soup.select("article[class*='review']"))

            for card in cards:
                try:
                    # Rating — look for aria-label or filled stars
                    rating = 0
                    for el in card.select("[aria-label*='star'], [aria-label*='out of'], [class*='rating']"):
                        nums = re.findall(r'(\d+\.?\d*)\s*(?:out of|/|\s)', el.get("aria-label", ""))
                        if nums:
                            rating = float(nums[0])
                            break
                    if rating < 4:
                        continue

                    # Review body text
                    body = card.select_one("p, [class*='body'], [class*='Body'], [class*='review-text']")
                    text = body.get_text(" ", strip=True)[:400] if body else ""
                    if not text or len(text) < 20:
                        continue

                    # Reviewer name
                    reviewer_el = card.select_one("[class*='reviewer'], [class*='author'], [class*='Reviewer']")
                    reviewer = reviewer_el.get_text(strip=True) if reviewer_el else "Anonymous"

                    results.append({"reviewer": reviewer, "rating": rating, "text": text, "signal": "capterra_positive"})
                except Exception:
                    continue

            if results:
                print(f"  Capterra: {len(results)} positive reviews scraped")
                break
        except Exception as e:
            print(f"  Capterra scrape failed: {e}")

    if not results:
        print("  Capterra: 0 reviews (blocked or layout changed — using cached data)")
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
    # Load existing file to preserve manual fields and historical quotes
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            existing: list[dict] = json.load(f)
    except FileNotFoundError:
        existing = []

    by_hs_id = {str(a["hsId"]): a for a in existing if a.get("hsId")}
    by_name  = {a["name"].lower().strip(): a for a in existing}
    original_json = json.dumps(existing, sort_keys=True)

    print(f"Existing advocates: {len(existing)}\n")

    # ── Gather signals from all sources ───────────────────────────────────────
    print("── Intercom CSAT ──────────────────────────────────────────")
    ic_signals = fetch_intercom_csat()

    print("\n── Capterra ────────────────────────────────────────────────")
    ca_reviews = scrape_capterra()
    # Build a lookup: reviewer text → review for name-matching
    ca_lookup = {r["reviewer"].lower(): r for r in ca_reviews if r.get("reviewer")}

    print("\n── HubSpot customers ───────────────────────────────────────")
    hs_customers = hs_get_all_customers()

    # ── Process each HubSpot customer ─────────────────────────────────────────
    new_advocates: list[dict] = []
    added, updated_names = [], []
    next_id = max((a.get("id", 0) for a in existing), default=53) + 1

    for customer in hs_customers:
        hs_id = str(customer["id"])
        props = customer.get("properties", {})
        name  = (props.get("name") or "").strip()
        if not name:
            continue

        # Hard exclude — negative flags
        if is_negative(props):
            print(f"  ✗ Excluded (negative flag): {name}")
            continue

        # Collect signals
        signals = hs_signals(props)

        for ic_key in ic_signals:
            if names_match(name, ic_key):
                signals.append("intercom_csat")
                break

        for ca_key in ca_lookup:
            if names_match(name, ca_key):
                signals.append("capterra_positive")
                break

        # Must have ≥1 signal
        if not signals:
            continue

        # ── Merge with existing record ─────────────────────────────────────
        rec = None
        if hs_id in by_hs_id:
            rec = dict(by_hs_id[hs_id])
        else:
            for k, v in by_name.items():
                if names_match(name, k):
                    rec = dict(v)
                    break

        is_new = rec is None
        if is_new:
            rec = {
                "id": next_id, "name": name, "ct": "general",
                "src": "HubSpot", "verify": False, "approx": False,
                "quote": None, "metrics": None, "pm": None,
                "aiAdopter": None, "lat": None, "lng": None,
                "features": None, "dgtId": None,
            }
            next_id += 1
            added.append(name)
            print(f"  + New advocate: {name}")

        # Refresh from HubSpot
        rec["hsId"]  = hs_id
        rec["name"]  = name
        for src_key, dest_key in [("city","city"),("state","st"),("contact_email","email"),("phone","phone"),("current_pims","pims")]:
            val = (props.get(src_key) or "").strip()
            if val:
                rec[dest_key] = val

        # Address + geocode
        new_addr = build_address(props)
        addr_changed = new_addr and new_addr != rec.get("address", "")
        if addr_changed:
            rec["address"] = new_addr
        if (addr_changed or not rec.get("lat")) and new_addr:
            lat, lng = geocode(new_addr)
            if lat:
                rec["lat"], rec["lng"] = lat, lng
                rec["approx"] = False

        # Preserve manual signal; set verified signals
        if "manual" in rec.get("signals", []):
            signals.append("manual")
        rec["signals"]  = sorted(set(signals))
        rec["verified"] = True

        # Quote: prefer Intercom CSAT > Capterra > existing
        for ic_key, ic_val in ic_signals.items():
            if names_match(name, ic_key) and ic_val.get("quote"):
                rec["quote"] = ic_val["quote"]
                break
        if not rec.get("quote"):
            for ca_key, ca_val in ca_lookup.items():
                if names_match(name, ca_key) and ca_val.get("text"):
                    rec["quote"] = ca_val["text"]
                    break

        if not is_new:
            updated_names.append(name)
        new_advocates.append(rec)

    # ── Preserve non-HubSpot records (Capterra-only, Usage Report, manual) ────
    hs_ids_included = {str(a.get("hsId","")) for a in new_advocates}
    names_included  = {a["name"].lower() for a in new_advocates}
    for old in existing:
        already_in = (str(old.get("hsId","")) in hs_ids_included or
                      old["name"].lower() in names_included)
        if not already_in:
            # Keep if it had any signals or is from a non-HubSpot source
            if old.get("signals") or old.get("src","") in ("Capterra","Usage Report","Intercom CSAT"):
                new_advocates.append(old)

    new_advocates.sort(key=lambda a: a.get("name",""))

    # ── Save ──────────────────────────────────────────────────────────────────
    new_json = json.dumps(new_advocates, sort_keys=True)
    if new_json != original_json:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(new_advocates, f, indent=2, ensure_ascii=False)
        print(f"\n✅ advocates.json updated — {len(new_advocates)} verified advocates "
              f"({len(added)} new, {len(updated_names)} refreshed)")
    else:
        print(f"\n✅ No changes — {len(new_advocates)} advocates, all current")

    # ── Slack ─────────────────────────────────────────────────────────────────
    if SLACK_URL:
        pinned = sum(1 for a in new_advocates if a.get("lat"))
        lines  = [f"*🐾 Digitail Advocate Map — Weekly Refresh*",
                  f"Verified advocates: *{len(new_advocates)}* | Pinned on map: *{pinned}*"]
        if added:
            lines.append(f"✅ *{len(added)} new:* " + ", ".join(added[:6]))
        if updated_names:
            shown = updated_names[:6]
            lines.append(f"📝 *Updated:* " + ", ".join(shown) +
                         (f" +{len(updated_names)-6} more" if len(updated_names) > 6 else ""))
        if not added and not updated_names:
            lines.append("No changes this week ✓")
        lines.append(f"_Run: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC_")
        requests.post(SLACK_URL, json={"text": "\n".join(lines)}, timeout=10)

if __name__ == "__main__":
    main()
