#!/usr/bin/env python3
"""
Digitail Advocate Map — Weekly Data Updater
Pulls HubSpot (address, PIMS, email) + Intercom CSAT quotes,
updates advocates.json, then pings Slack with a diff summary.

Run locally:   python update_advocates.py
Run via CI:    GitHub Actions (see weekly-update.yml)

Required env vars:
  HUBSPOT_TOKEN   — HubSpot Private App token (CRM objects: read)
  INTERCOM_TOKEN  — Intercom Access Token
  SLACK_WEBHOOK   — Slack Incoming Webhook URL (optional)
"""

import json, os, sys, time
from datetime import datetime, timedelta, timezone
import requests

HS_TOKEN   = os.environ.get("HUBSPOT_TOKEN", "")
IC_TOKEN   = os.environ.get("INTERCOM_TOKEN", "")
SLACK_URL  = os.environ.get("SLACK_WEBHOOK", "")
DATA_FILE  = "advocates.json"

# ── HubSpot ───────────────────────────────────────────────────────────────────

HS_PROPS = "name,address,address2,city,state,zip,country,contact_email,current_pims,phone"

def hs_get(company_id: str) -> dict | None:
    """Fetch a single HubSpot company by numeric ID."""
    if not HS_TOKEN or not company_id:
        return None
    r = requests.get(
        f"https://api.hubapi.com/crm/v3/objects/companies/{company_id}",
        params={"properties": HS_PROPS},
        headers={"Authorization": f"Bearer {HS_TOKEN}"},
        timeout=12,
    )
    if r.status_code == 200:
        return r.json().get("properties", {})
    print(f"  HS {company_id}: HTTP {r.status_code}")
    return None

def hs_search(name: str) -> dict | None:
    """Search HubSpot by company name (fallback for records without hsId)."""
    if not HS_TOKEN or not name:
        return None
    payload = {
        "filterGroups": [],
        "query": name,
        "properties": HS_PROPS.split(","),
        "limit": 1,
    }
    r = requests.post(
        "https://api.hubapi.com/crm/v3/objects/companies/search",
        json=payload,
        headers={"Authorization": f"Bearer {HS_TOKEN}", "Content-Type": "application/json"},
        timeout=12,
    )
    if r.status_code == 200:
        results = r.json().get("results", [])
        if results:
            return results[0].get("properties", {})
    return None

# ── Geocoding (Nominatim) ─────────────────────────────────────────────────────

def geocode(address: str) -> tuple[float | None, float | None]:
    """Convert an address string to (lat, lng). Rate-limited to 1 req/sec."""
    time.sleep(1.2)
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"format": "json", "q": address, "limit": 1},
            headers={"User-Agent": "DigitailAdvocateMap/1.0 (internal-sales-tool)"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            if data:
                return float(data[0]["lat"]), float(data[0]["lon"])
    except Exception as e:
        print(f"  Geocode failed for '{address}': {e}")
    return None, None

# ── Intercom CSAT ─────────────────────────────────────────────────────────────

def fetch_intercom_csat() -> list[dict]:
    """Pull positively-rated Intercom conversations from the past 365 days."""
    if not IC_TOKEN:
        return []
    cutoff = int((datetime.now(timezone.utc) - timedelta(days=365)).timestamp())
    headers = {
        "Authorization": f"Bearer {IC_TOKEN}",
        "Accept": "application/json",
        "Intercom-Version": "2.10",
    }
    results = []
    # Try the search endpoint first
    try:
        payload = {
            "query": {
                "operator": "AND",
                "value": [
                    {"field": "statistics.last_assignment_at", "operator": ">", "value": cutoff},
                    {"field": "rating", "operator": "=", "value": "amazing"},
                ],
            },
            "pagination": {"per_page": 150},
        }
        r = requests.post(
            "https://api.intercom.io/conversations/search",
            json=payload, headers=headers, timeout=15,
        )
        if r.status_code == 200:
            for c in r.json().get("conversations", []):
                remark = (c.get("conversation_rating") or {}).get("remark", "")
                author  = (c.get("source", {}).get("author") or {})
                company = author.get("name", "") or ""
                if remark:
                    results.append({"company": company, "remark": remark.strip()})
    except Exception as e:
        print(f"  Intercom CSAT fetch failed: {e}")
    print(f"  Intercom: {len(results)} CSAT records fetched")
    return results

# ── Name matching ─────────────────────────────────────────────────────────────

def name_match(a_name: str, csat_company: str) -> bool:
    """Loose match: check if either name is a substring of the other."""
    a, b = a_name.lower().strip(), csat_company.lower().strip()
    if not a or not b:
        return False
    return a in b or b in a or any(w in b for w in a.split() if len(w) > 4)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    # Load existing data
    with open(DATA_FILE, encoding="utf-8") as f:
        advocates: list[dict] = json.load(f)

    updated_names, geocoded_names = [], []
    original = json.dumps(advocates, sort_keys=True)  # for dirty-check

    print(f"Loaded {len(advocates)} advocates from {DATA_FILE}")
    print(f"HubSpot: {'✓ token present' if HS_TOKEN else '✗ no token'}")
    print(f"Intercom: {'✓ token present' if IC_TOKEN else '✗ no token'}")

    # ── 1. HubSpot refresh ────────────────────────────────────────────────────
    for a in advocates:
        hs_id  = a.get("hsId")
        name   = a.get("name", "")
        dirty  = False

        props = hs_get(str(hs_id)) if hs_id else hs_search(name)
        if not props:
            print(f"  Skipping (no HS data): {name}")
            continue

        # PIMS
        new_pims = (props.get("current_pims") or "").strip()
        if new_pims and new_pims != a.get("pims", ""):
            print(f"  PIMS change [{name}]: '{a.get('pims')}' → '{new_pims}'")
            a["pims"] = new_pims
            dirty = True

        # Contact email
        new_email = (props.get("contact_email") or "").strip()
        if new_email and "@" in new_email and new_email != a.get("email", ""):
            a["email"] = new_email
            dirty = True

        # Address — rebuild from parts
        parts = [
            (props.get("address") or "").strip(),
            (props.get("city") or "").strip(),
            (props.get("state") or "").strip(),
            (props.get("zip") or "").strip(),
        ]
        new_addr = ", ".join(p for p in parts if p)
        addr_changed = new_addr and new_addr != a.get("address", "")
        missing_coords = not a.get("lat") or not a.get("lng")

        if addr_changed:
            a["address"] = new_addr
            dirty = True

        if (addr_changed or missing_coords) and new_addr:
            print(f"  Geocoding: {name} → {new_addr}")
            lat, lng = geocode(new_addr)
            if lat:
                a["lat"], a["lng"] = round(lat, 5), round(lng, 5)
                a["approx"] = False
                geocoded_names.append(name)

        if dirty:
            updated_names.append(name)

    # ── 2. Intercom CSAT ──────────────────────────────────────────────────────
    csat_records = fetch_intercom_csat()
    for csat in csat_records:
        remark  = csat.get("remark", "")
        company = csat.get("company", "")
        if not remark or len(remark) < 10:
            continue
        for a in advocates:
            if name_match(a["name"], company):
                if remark[:300] != a.get("quote", ""):
                    print(f"  CSAT update [{a['name']}]: new quote")
                    a["quote"] = remark[:300]
                    if a["name"] not in updated_names:
                        updated_names.append(a["name"])
                break

    # ── 3. Write if changed ───────────────────────────────────────────────────
    if json.dumps(advocates, sort_keys=True) != original:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(advocates, f, indent=2, ensure_ascii=False)
        print(f"\n✅ advocates.json updated ({len(updated_names)} records changed)")
    else:
        print("\n✅ No changes detected — advocates.json unchanged")

    # ── 4. Slack notification ─────────────────────────────────────────────────
    if SLACK_URL:
        lines = ["*🐾 Digitail Advocate Map — Weekly Refresh*"]
        lines.append(f"Total advocates: *{len(advocates)}* | "
                     f"Pinned on map: *{sum(1 for a in advocates if a.get('lat'))}*")
        if updated_names:
            shown = updated_names[:6]
            tail  = f" + {len(updated_names)-6} more" if len(updated_names) > 6 else ""
            lines.append(f"📝 Updated: {', '.join(shown)}{tail}")
        if geocoded_names:
            lines.append(f"📍 Re-geocoded: {', '.join(geocoded_names[:4])}")
        if not updated_names:
            lines.append("No data changes this week — all good ✓")
        lines.append(f"_Run: {datetime.utcnow().strftime('%Y-%m-%d %H:%M')} UTC_")

        r = requests.post(SLACK_URL, json={"text": "\n".join(lines)}, timeout=10)
        print(f"Slack ping: HTTP {r.status_code}")

    return 0  # exit 1 = changes (triggers git commit step)

if __name__ == "__main__":
     main()
