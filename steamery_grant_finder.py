#!/usr/bin/env python3
"""
STEAMery Grant Discovery Tool
==============================
Volunteer research tool for The STEAMery (Brown County, Indiana).
Searches ProPublica's Nonprofit Explorer API for private foundations that
are plausible funders, screens them against The STEAMery's hard disqualifiers,
attempts to pull grant history from IRS e-file XML, and writes:

  * results.json   -- full auditable log of every screened foundation
  * report.html    -- interactive browser report with confidence badges

Usage:
  python3 steamery_grant_finder.py           # full run (~15-30 min)
  python3 steamery_grant_finder.py --dry-run  # 1 page only, fast validation
  python3 steamery_grant_finder.py --skip-xml # skip XML parsing (faster)

Requirements: Python 3.8+, requests, lxml
  pip3 install requests lxml

No API key required. Hits ProPublica's live public API.
Data source: https://projects.propublica.org/nonprofits/api/

Author: STEAMery volunteer project -- Brown County, IN
"""

import argparse
import json
import os
import sys
import time
import datetime
from typing import Optional

import requests
from lxml import etree

# =============================================================================
# SECTION 0: CONFIGURATION
# All tuneable parameters are here. Safe to edit without touching the logic.
# =============================================================================

# ProPublica API base (no auth required)
API_BASE = "https://projects.propublica.org/nonprofits/api/v2"

# IRS e-file XML public S3 bucket (no auth)
IRS_XML_BASE = "https://s3.amazonaws.com/irs-form-990"

# -- Geography ----------------------------------------------------------------
# Tier 1 = Indiana (primary). Tier 2 = bordering states (secondary).
STATES_TIER1 = ["IN"]
STATES_TIER2 = ["OH", "IL", "KY", "MI"]

# -- Search depth -------------------------------------------------------------
# Pages to fetch per (state x NTEE x query) combination. 25 orgs/page.
# Raise MAX_PAGES_TIER2 to cast a wider net (slower).
MAX_PAGES_TIER1 = 999   # exhaust all Indiana pages
MAX_PAGES_TIER2 = 10    # cap bordering states at 250 orgs per combo

# NTEE major group codes: 2=Education, 7=Public/Societal Benefit
# (catches most small family/community foundations)
NTEE_GROUPS = [2, 7]

# Query terms -- ProPublica searches org name, alternate name, and city
SEARCH_QUERIES = [
    "foundation",
    "family foundation",
    "community foundation",
    "charitable trust",
    "trust",
]

# -- Asset thresholds ---------------------------------------------------------
MIN_ASSETS_DOLLARS        = 50_000        # below this = likely dormant, FAIL
FLAG_LARGE_ASSETS_DOLLARS = 500_000_000   # above this = FLAG (not auto-fail)
MAX_FILING_AGE_YEARS      = 5             # most recent filing older than this = FLAG

# -- Rate limiting ------------------------------------------------------------
REQUEST_DELAY_SECONDS = 1.0   # seconds between each API call

# -- Output -------------------------------------------------------------------
OUTPUT_JSON = "results.json"
OUTPUT_HTML = "report.html"

# -- STEAMery hard disqualifiers (used to generate manual-check notes) --------
# These are the constraints we KNOW about The STEAMery. We cannot determine
# from the API whether any specific funder enforces them -- but we surface
# the question for every passing candidate so Kirstie knows what to verify.
STEAMERY_PROFILE = {
    "org_name": "The STEAMery",
    "location": "Nashville, Brown County, Indiana (pop ~300 village; ~15,000 county; ~2M tourists/yr)",
    "status": "501(c)(3) since December 2024",
    "years_old_approx": 0,
    "site_control": False,
    "audited_financials": False,
    "paid_staff": False,
    "budget_note": "pre-revenue, unpaid founder, no paid staff",
    "mission": "STEAM education facility in a converted sock factory (capital + programming)",
    "hard_disqualifiers": [
        "Requires applicant org history >= 2 years -- The STEAMery incorporated Dec 2024 (<1 yr old)",
        "Requires municipal applicant -- The STEAMery is a 501(c)(3), NOT a municipality and cannot become one",
        "Requires site control (ownership or recorded long-term lease) -- The STEAMery has neither yet",
        "Requires audited financial statements -- none available",
        "Requires paid staff or minimum operating budget -- pre-revenue with no paid staff",
    ],
}

# Keywords indicating a past grant recipient resembles The STEAMery
SIMILARITY_KEYWORDS = [
    "steam", "stem", "science", "technology", "engineering", "art", "math",
    "education", "school", "youth", "children", "rural", "indiana",
    "brown county", "makerspace", "maker", "museum", "community", "capacity",
    "capital", "facility", "building", "renovation", "learning", "workshop",
]


# =============================================================================
# SECTION 1: FOUNDATION DISCOVERY
# =============================================================================

def api_get(url: str, params: dict = None, timeout: int = 15) -> Optional[dict]:
    """
    GET request to ProPublica with rate limiting and error handling.
    Returns parsed JSON dict, or None on any failure. Never raises.
    """
    time.sleep(REQUEST_DELAY_SECONDS)
    try:
        resp = requests.get(url, params=params, timeout=timeout, headers={
            "User-Agent": "STEAMery-GrantFinder/1.0 (volunteer nonprofit research)"
        })
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 429:
            print("    [rate-limited] Waiting 15s before retry...")
            time.sleep(15)
            return api_get(url, params, timeout)  # single retry
        else:
            print(f"    [HTTP {resp.status_code}] {url}")
            return None
    except requests.exceptions.Timeout:
        print(f"    [timeout] {url}")
        return None
    except Exception as exc:
        print(f"    [error] {type(exc).__name__}: {exc}")
        return None


def search_foundations(state: str, ntee: int, query: str,
                       max_pages: int, dry_run: bool) -> list:
    """
    Paginate through ProPublica search results for one (state, ntee, query) combo.
    Returns list of raw organization objects from the search endpoint.
    In dry_run mode only fetches 1 page.
    """
    results = []
    effective_max = 1 if dry_run else max_pages

    for page in range(effective_max):
        data = api_get(f"{API_BASE}/search.json", params={
            "q": query,
            "state[id]": state,
            "ntee[id]": ntee,
            "c_code[id]": 3,   # 501(c)(3) only
            "page": page,
        })
        if not data:
            break
        orgs = data.get("organizations", [])
        if not orgs:
            break
        results.extend(orgs)
        if page + 1 >= data.get("num_pages", 1):
            break   # reached last page

    return results


def collect_all_candidates(dry_run: bool = False) -> dict:
    """
    Run all search combinations. Deduplicate by EIN.
    Returns dict keyed by EIN; each value is the raw search org object
    plus a 'geo_tier' field (1 = Indiana, 2 = bordering state).
    """
    candidates = {}
    combos = [
        (1, s, max_p)
        for s, max_p in [(st, MAX_PAGES_TIER1) for st in STATES_TIER1]
    ] + [
        (2, s, MAX_PAGES_TIER2) for s in STATES_TIER2
    ]
    total = len(combos) * len(NTEE_GROUPS) * len(SEARCH_QUERIES)
    n = 0

    for tier, state, max_pages in combos:
        for ntee in NTEE_GROUPS:
            for query in SEARCH_QUERIES:
                n += 1
                label = "[DRY RUN] " if dry_run else ""
                print(f"  [{n}/{total}] {label}state={state} ntee={ntee} q='{query}'")
                orgs = search_foundations(state, ntee, query, max_pages, dry_run)
                new = 0
                for org in orgs:
                    ein = org.get("ein")
                    if ein and ein not in candidates:
                        org["geo_tier"] = tier
                        candidates[ein] = org
                        new += 1
                print(f"         -> {len(orgs)} results, {new} new (total unique: {len(candidates)})")

    return candidates


def fetch_org_detail(ein: int) -> Optional[dict]:
    """
    Fetch full organization record from ProPublica (filing history + financials).
    Returns None if unavailable.
    """
    return api_get(f"{API_BASE}/organizations/{ein}.json")


def has_990pf_filing(filings_with: list, filings_without: list) -> bool:
    """Returns True if org has at least one 990-PF filing (formtype == 2)."""
    for f in (filings_with or []):
        if f.get("formtype") == 2:
            return True
    for f in (filings_without or []):
        if f.get("formtype") == 2:
            return True
    return False


def most_recent_pf_with_data(filings_with: list) -> Optional[dict]:
    """Most recent 990-PF filing that has structured financial data."""
    pf = [f for f in (filings_with or []) if f.get("formtype") == 2]
    return max(pf, key=lambda f: f.get("tax_prd", 0)) if pf else None


def most_recent_pf_with_pdf(filings_with: list, filings_without: list) -> Optional[dict]:
    """Most recent 990-PF filing (any source) that has a PDF URL."""
    all_pf = [
        f for f in (filings_with or [])
        if f.get("formtype") == 2 and f.get("pdf_url")
    ] + [
        f for f in (filings_without or [])
        if f.get("formtype") == 2 and f.get("pdf_url")
    ]
    return max(all_pf, key=lambda f: f.get("tax_prd", 0)) if all_pf else None


# =============================================================================
# SECTION 2: ELIGIBILITY SCREENING
# =============================================================================

def screen_candidate(org_detail: dict, geo_tier: int) -> dict:
    """
    Apply all eligibility rules to one candidate foundation.

    Returns a dict with:
      verdict            -- "pass" | "fail" | "flag"
      rejection_reason   -- always set for fail; None for pass/flag
      flags              -- list of non-fatal concerns
      filing_summary     -- snapshot of most recent 990-PF data
      confidence         -- "high fit" | "worth a look" | "long shot"
      what_api_cannot_tell -- items Kirstie must verify manually
    """
    org = org_detail.get("organization", {})
    filings_with = org_detail.get("filings_with_data") or []
    filings_without = org_detail.get("filings_without_data") or []

    result = {
        "verdict": "pass",
        "rejection_reason": None,
        "flags": [],
        "filing_summary": {},
        "confidence": None,
        "what_api_cannot_tell": [],
    }

    # Rule 1: Must be a private foundation (990-PF filer)
    if not has_990pf_filing(filings_with, filings_without):
        result["verdict"] = "fail"
        result["rejection_reason"] = (
            "No 990-PF filings found -- not confirmed as a private foundation. "
            "May be a public charity, or may have never filed."
        )
        return result

    # Rule 2: Financial data checks (using most recent structured 990-PF)
    pf = most_recent_pf_with_data(filings_with)
    assets = None
    most_recent_year = None

    if pf:
        assets = pf.get("totassetsend")
        tax_prd = pf.get("tax_prd", 0)
        most_recent_year = int(str(tax_prd)[:4]) if tax_prd else None
        result["filing_summary"] = {
            "most_recent_year": most_recent_year,
            "assets_end_of_year": assets,
            "total_revenue": pf.get("totrevenue"),
        }

        # Fail: near-zero assets (likely dormant)
        if assets is not None and assets < MIN_ASSETS_DOLLARS:
            result["verdict"] = "fail"
            result["rejection_reason"] = (
                f"Assets too low (${assets:,}) -- likely dormant or inactive. "
                f"Minimum threshold: ${MIN_ASSETS_DOLLARS:,}."
            )
            return result

        # Flag: very large institutional funder
        if assets is not None and assets > FLAG_LARGE_ASSETS_DOLLARS:
            result["flags"].append(
                f"LARGE FUNDER: Assets ${assets:,.0f} exceed ${FLAG_LARGE_ASSETS_DOLLARS:,.0f}. "
                "These funders typically require formal LOI processes and multi-year org track records. "
                "Verify whether they accept applications from orgs < 1 year old."
            )

        # Flag: old most-recent filing (potentially dormant)
        current_year = datetime.datetime.now().year
        if most_recent_year and (current_year - most_recent_year) > MAX_FILING_AGE_YEARS:
            result["flags"].append(
                f"POTENTIALLY DORMANT: Most recent structured 990-PF is from {most_recent_year} "
                f"({current_year - most_recent_year} years ago). Verify foundation is still active."
            )
    else:
        # Has 990-PF but only PDFs -- cannot assess assets
        result["filing_summary"] = {
            "note": "All 990-PF filings are PDF-only; no structured financial data available via API."
        }
        result["flags"].append(
            "No structured financial data in API -- 990-PF filings are PDF-only. "
            "Cannot programmatically assess asset level. Manual review required."
        )

    # These are STEAMery-specific disqualifiers the API cannot verify for us.
    # Surfaced on every passing candidate so Kirstie knows exactly what to check.
    result["what_api_cannot_tell"] = [
        "Whether this foundation requires >= 2 years of org history "
        "(The STEAMery is < 1 year old -- incorporated Dec 2024)",

        "Whether this foundation requires audited financial statements "
        "(The STEAMery has none)",

        "Whether this foundation requires site control / property ownership or long-term lease "
        "(The STEAMery has neither yet)",

        "Whether this foundation requires paid staff or a minimum operating budget "
        "(The STEAMery is pre-revenue with no paid staff)",

        "Whether this foundation accepts unsolicited applications "
        "(many private foundations only fund at board discretion -- check their website)",

        "Whether this foundation funds capital campaigns for brand-new nonprofits "
        "(vs. operating support or established program grants)",
    ]

    # Compute confidence label
    result["confidence"] = _compute_confidence(
        geo_tier=geo_tier,
        ntee_code=org.get("ntee_code", ""),
        assets=assets,
        flags=result["flags"],
        most_recent_year=most_recent_year,
    )

    # Downgrade "pass" to "flag" if there are non-fatal flags
    if result["flags"] and result["verdict"] == "pass":
        result["verdict"] = "flag"

    return result


def _compute_confidence(geo_tier, ntee_code, assets, flags, most_recent_year) -> str:
    """
    Score a candidate and return "high fit" | "worth a look" | "long shot".

    Scoring:
      Geography:    IN=3pts, bordering=1pt
      NTEE cause:   B/T=3pts (education/philanthropy), S/W=2pts (community), other=1pt
      Asset range:  $250K-$50M=2pts, $50M-$500M=1pt, else=0pt
      Recency:      filing <=2yr old=1pt
      Large funder flag: -2pts
      Dormant flag:      -2pts
    """
    score = 3 if geo_tier == 1 else 1

    prefix = (ntee_code or "")[:1].upper()
    if prefix in ("B", "T"):
        score += 3
    elif prefix in ("S", "W", "R", "C"):
        score += 2
    else:
        score += 1

    if assets is not None:
        if 250_000 <= assets <= 50_000_000:
            score += 2
        elif 50_000_000 < assets <= FLAG_LARGE_ASSETS_DOLLARS:
            score += 1

    current_year = datetime.datetime.now().year
    if most_recent_year and (current_year - most_recent_year) <= 2:
        score += 1

    if any("LARGE FUNDER" in f for f in flags):
        score -= 2
    if any("DORMANT" in f for f in flags):
        score -= 2

    if score >= 7:
        return "high fit"
    elif score >= 4:
        return "worth a look"
    else:
        return "long shot"


# =============================================================================
# SECTION 3: GRANT HISTORY (IRS XML PARSING)
# =============================================================================

def fetch_irs_xml(object_id: str) -> Optional[bytes]:
    """
    Fetch a 990-PF XML from the IRS public S3 bucket.
    Returns raw bytes or None if unavailable.
    """
    url = f"{IRS_XML_BASE}/{object_id}_public.xml"
    time.sleep(REQUEST_DELAY_SECONDS)
    try:
        resp = requests.get(url, timeout=20, headers={
            "User-Agent": "STEAMery-GrantFinder/1.0 (volunteer nonprofit research)"
        })
        return resp.content if resp.status_code == 200 else None
    except Exception:
        return None


def parse_grants_from_xml(xml_bytes: bytes) -> list:
    """
    Parse a 990-PF XML filing for Part XV (grants paid schedule).
    Handles multiple IRS schema versions (namespace variations across years).

    Returns list of dicts: {recipient_name, recipient_city, recipient_state, purpose, amount}
    """
    grants = []
    try:
        root = etree.fromstring(xml_bytes)
        ns = _detect_ns(root)

        # Try multiple XPath locations used across different schema years
        for xpath in [
            f".//{ns}GrantOrContributionPaidDuringYear",
            f".//{ns}RecipientTable",
            f".//{ns}DistributionTable",
            f".//{ns}GrantsAndContributions",
            f".//{ns}GrantOrContriPaidDurYrGrp",
        ]:
            elems = root.findall(xpath)
            for elem in elems:
                g = _extract_grant(elem, ns)
                if g:
                    grants.append(g)
            if grants:
                break   # stop at first successful location

    except etree.XMLSyntaxError:
        pass   # malformed XML -- return empty list

    return grants


def _detect_ns(root) -> str:
    """Extract namespace prefix from root element tag."""
    tag = root.tag
    if tag.startswith("{"):
        return "{" + tag[1:tag.index("}")] + "}"
    return ""


def _find_text(elem, ns, *tags) -> Optional[str]:
    """Try multiple tag names, return first non-empty text found."""
    for tag in tags:
        node = elem.find(f".//{ns}{tag}")
        if node is not None and node.text and node.text.strip():
            return node.text.strip()
    return None


def _extract_grant(elem, ns) -> Optional[dict]:
    """Extract one grant record from an XML element."""
    name = _find_text(elem, ns,
        "RecipientNameAndAddress", "BusinessNameLine1Txt", "RecipientName",
        "GranteeName", "BusinessName", "NameLine1", "RecipientNameBusiness",
    )
    if not name:
        return None   # skip entries with no identifiable recipient

    city    = _find_text(elem, ns, "City", "CityNm", "RecipientCity")
    state   = _find_text(elem, ns, "State", "StateAbbreviationCd", "RecipientState")
    purpose = _find_text(elem, ns,
        "PurposeOfGrantOrContribution", "GrantPurpose", "Purpose",
        "DescriptionProgramSrvcAccomTxt",
    )
    amt_str = _find_text(elem, ns,
        "AmountOfCashGrant", "CashGrantAmt", "Amount",
        "TotalGrantOrContributionAmt", "FairMarketValue",
    )
    amount = None
    if amt_str:
        try:
            amount = int(amt_str.replace(",", "").replace(".", "").strip())
        except ValueError:
            pass

    return {
        "recipient_name":  name,
        "recipient_city":  city,
        "recipient_state": state,
        "purpose":         purpose,
        "amount":          amount,
    }


def _similarity_score(grant: dict) -> float:
    """0-1 score: how similar is this past grantee to The STEAMery?"""
    text = " ".join(filter(None, [
        grant.get("recipient_name", ""),
        grant.get("purpose", ""),
        grant.get("recipient_city", ""),
        grant.get("recipient_state", ""),
    ])).lower()
    hits = sum(1 for kw in SIMILARITY_KEYWORDS if kw in text)
    return min(hits / 3.0, 1.0)


def _similarity_reason(grant: dict) -> str:
    text = " ".join(filter(None, [
        grant.get("recipient_name", ""),
        grant.get("purpose", ""),
    ])).lower()
    matched = [kw for kw in SIMILARITY_KEYWORDS if kw in text]
    return f"Matched keywords: {', '.join(matched[:6])}" if matched else ""


def check_grant_history(ein: int, org_detail: dict) -> dict:
    """
    Attempt IRS XML grant history for one foundation. Falls back to PDF flag.

    Returns dict with:
      method         -- "xml_parsed" | "pdf_only" | "unavailable"
      grants_found   -- total grants in XML (0 if not parsed)
      similar_grants -- list of grants matching STEAMery keywords
      pdf_url        -- PDF URL if XML unavailable
      notes          -- always describes what was done and what was found
    """
    result = {
        "method": "unavailable",
        "grants_found": 0,
        "similar_grants": [],
        "pdf_url": None,
        "notes": "",
    }

    org = org_detail.get("organization", {})
    filings_with   = org_detail.get("filings_with_data")   or []
    filings_without = org_detail.get("filings_without_data") or []

    # Try IRS XML via latest_object_id
    latest_oid = org.get("latest_object_id")
    if latest_oid:
        print(f"      -> Fetching IRS XML (object_id={latest_oid})...")
        xml_bytes = fetch_irs_xml(latest_oid)
        if xml_bytes:
            grants = parse_grants_from_xml(xml_bytes)
            similar = []
            for g in grants:
                score = _similarity_score(g)
                if score > 0:
                    g["similarity_score"]  = round(score, 2)
                    g["similarity_reason"] = _similarity_reason(g)
                    similar.append(g)
            similar.sort(key=lambda g: g["similarity_score"], reverse=True)
            result.update({
                "method":         "xml_parsed",
                "grants_found":   len(grants),
                "similar_grants": similar[:10],
                "notes": (
                    f"IRS e-file XML parsed. Found {len(grants)} grant entries; "
                    f"{len(similar)} match STEAMery similarity keywords."
                    + (" (XML may be summary-only without grants schedule.)" if not grants else "")
                ),
            })
            return result
        else:
            result["notes"] = (
                f"IRS XML not available for object_id={latest_oid} "
                "(foundation likely files on paper, not electronically)."
            )

    # Fallback: flag PDF if available
    pf_pdf = most_recent_pf_with_pdf(filings_with, filings_without)
    if pf_pdf and pf_pdf.get("pdf_url"):
        result.update({
            "method":  "pdf_only",
            "pdf_url": pf_pdf["pdf_url"],
            "notes": (
                "No machine-readable XML available. PDF of most recent 990-PF linked above. "
                "Manually review Part XV (grants paid schedule) for recipient names. "
                "No OCR attempted to avoid unreliable data."
            ),
        })
        return result

    result["notes"] = (
        "No IRS XML and no PDF 990-PF found in ProPublica records. "
        "Foundation may file on paper without digitized filings, or may not have filed recently."
    )
    return result


# =============================================================================
# SECTION 4A: JSON OUTPUT
# =============================================================================

def build_record(ein: int, search_org: dict, org_detail: Optional[dict],
                 screening: Optional[dict], grant_history: Optional[dict]) -> dict:
    """Build one complete record for results.json. Never invents missing data."""
    org = (org_detail or {}).get("organization", {})
    return {
        "ein":                  ein,
        "strein":               org.get("strein") or search_org.get("strein"),
        "name":                 org.get("name")   or search_org.get("name"),
        "city":                 org.get("city")   or search_org.get("city"),
        "state":                org.get("state")  or search_org.get("state"),
        "ntee_code":            org.get("ntee_code") or search_org.get("ntee_code"),
        "geo_tier":             search_org.get("geo_tier"),
        "propublica_url":       f"https://projects.propublica.org/nonprofits/organizations/{ein}",
        "ruling_date":          org.get("ruling_date"),
        "asset_amount":         org.get("asset_amount"),
        "income_amount":        org.get("income_amount"),
        "verdict":              (screening or {}).get("verdict", "not_screened"),
        "rejection_reason":     (screening or {}).get("rejection_reason"),
        "flags":                (screening or {}).get("flags", []),
        "confidence":           (screening or {}).get("confidence"),
        "filing_summary":       (screening or {}).get("filing_summary", {}),
        "what_api_cannot_tell": (screening or {}).get("what_api_cannot_tell", []),
        "grant_history":        grant_history,
        "org_detail_available": org_detail is not None,
        "data_note": (
            "Detail fetch failed -- org may have been removed from ProPublica index."
            if org_detail is None else None
        ),
    }


def save_json(records: list, filepath: str):
    output = {
        "generated_at":  datetime.datetime.utcnow().isoformat() + "Z",
        "tool":          "STEAMery Grant Discovery Tool v1.0",
        "data_source":   "ProPublica Nonprofit Explorer API v2 (https://projects.propublica.org/nonprofits/api/)",
        "steamery_profile": STEAMERY_PROFILE,
        "total_records": len(records),
        "summary": {
            "pass":         sum(1 for r in records if r.get("verdict") == "pass"),
            "flag":         sum(1 for r in records if r.get("verdict") == "flag"),
            "fail":         sum(1 for r in records if r.get("verdict") == "fail"),
            "not_screened": sum(1 for r in records if r.get("verdict") == "not_screened"),
        },
        "records": records,
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)
    print(f"\n  [saved] {filepath}")


# =============================================================================
# SECTION 4B: HTML REPORT
# =============================================================================

def _fmt_assets(v: Optional[float]) -> str:
    if v is None:      return "Unknown"
    if v >= 1_000_000: return f"${v/1_000_000:.1f}M"
    if v >= 1_000:     return f"${v/1_000:.0f}K"
    return f"${v:,.0f}"


def _conf_class(c: Optional[str]) -> str:
    return {"high fit": "high-fit", "worth a look": "worth-a-look",
            "long shot": "long-shot"}.get(c or "", "long-shot")


def _why_fits(r: dict) -> str:
    """One-line 'why this might fit' from structured data only."""
    parts = []
    if r.get("geo_tier") == 1:
        parts.append("Indiana-based")
    elif r.get("state"):
        parts.append(f"{r['state']}-based")
    ntee = (r.get("ntee_code") or "")[:1].upper()
    if ntee == "B": parts.append("Education-focused NTEE")
    elif ntee == "T": parts.append("Philanthropy/Grantmaking NTEE")
    elif ntee == "S": parts.append("Community Benefit NTEE")
    assets = r.get("asset_amount")
    if assets and 1_000_000 <= assets <= 50_000_000:
        parts.append("right-sized for emerging nonprofits")
    return ("; ".join(parts) or "Listed as 501(c)(3) private foundation") + "."


def _render_top_cards(top: list) -> str:
    if not top:
        return '<p style="color:var(--muted)">No passing candidates found in this run.</p>'
    cards = []
    for r in top:
        name    = r.get("name") or "Unknown Foundation"
        loc     = ", ".join(filter(None, [r.get("city"), r.get("state")]))
        assets  = _fmt_assets(r.get("asset_amount"))
        conf    = r.get("confidence") or "long shot"
        cc      = _conf_class(conf)
        url     = r.get("propublica_url", "")
        why     = _why_fits(r)
        flags   = r.get("flags") or []
        gh      = r.get("grant_history") or {}
        similar = gh.get("similar_grants") or []

        # Evidence block
        evidence = ""
        if similar:
            g  = similar[0]
            gn = g.get("recipient_name", "Unknown")
            gp = (g.get("purpose") or "")[:120]
            ga = g.get("amount")
            amt_str = f" (${ga:,})" if ga else ""
            ellipsis = "..." if len(g.get("purpose") or "") > 120 else ""
            evidence = f"""
            <div class="evidence">
              <span class="ev-label">Past Grant Evidence</span>
              <span>Funded <strong>{gn}</strong>{amt_str}
              {f'&mdash; {gp}{ellipsis}' if gp else ''}
              </span>
            </div>"""
        elif gh.get("method") == "pdf_only":
            pdf = gh.get("pdf_url", "")
            evidence = f"""
            <div class="flag-note">
              <strong>Manual check needed:</strong> Grant list is in a scanned PDF.
              <a href="{pdf}" target="_blank">Open 990-PF PDF &rarr;</a>
            </div>"""

        flag_note = ""
        if flags:
            flag_note = f'<div class="flag-note"><strong>Note:</strong> {flags[0][:160]}</div>'

        cards.append(f"""
      <div class="card {cc}">
        <div class="card-top">
          <div class="card-name">{name}</div>
          <span class="badge {cc}">{conf}</span>
        </div>
        <div class="card-loc">&#128205; {loc}</div>
        <div class="card-assets">{assets}</div>
        <div class="card-why">{why}</div>
        {evidence}
        {flag_note}
        <a class="pp-link" href="{url}" target="_blank">View on ProPublica &rarr;</a>
      </div>""")
    return "\n".join(cards)


def _render_rows(records: list) -> str:
    rows = []
    for r in records:
        name    = r.get("name") or "Unknown"
        loc     = ", ".join(filter(None, [r.get("city"), r.get("state")]))
        assets  = _fmt_assets(r.get("asset_amount"))
        a_sort  = r.get("asset_amount") or 0
        ntee    = r.get("ntee_code") or "&mdash;"
        tier    = r.get("geo_tier", "")
        verdict = r.get("verdict") or "unknown"
        conf    = r.get("confidence") or ""
        cc      = _conf_class(conf)
        reason  = r.get("rejection_reason") or ""
        flags   = r.get("flags") or []
        if not reason and flags:
            reason = flags[0][:120]
        url     = r.get("propublica_url", "")
        gh      = r.get("grant_history") or {}
        similar = gh.get("similar_grants") or []
        method  = gh.get("method", "")
        if method == "xml_parsed" and similar:
            gh_cell = f'<span style="color:var(--green);font-weight:700;">&check; {len(similar)} similar</span>'
        elif method == "pdf_only":
            gh_cell = f'<a href="{gh.get("pdf_url","")}" target="_blank" class="pp-mini">PDF &rarr;</a>'
        elif method == "xml_parsed":
            gh_cell = '<span style="color:var(--muted);font-size:11px;">Parsed, no match</span>'
        else:
            gh_cell = '<span style="color:var(--muted);font-size:11px;">N/A</span>'

        rows.append(f"""
      <tr data-verdict="{verdict}" data-conf="{conf}">
        <td class="td-name">{name}</td>
        <td class="td-loc">{loc}</td>
        <td class="td-assets" data-sort="{a_sort}">{assets}</td>
        <td><code class="ntee-chip">{ntee}</code></td>
        <td><span class="tier {'t1' if tier==1 else ''}">T{tier}</span></td>
        <td><span class="badge {verdict}">{verdict.replace('_',' ')}</span></td>
        <td>{f'<span class="badge {cc}">{conf}</span>' if conf else '&mdash;'}</td>
        <td class="td-reason">{reason[:140] if reason else '&mdash;'}</td>
        <td>{gh_cell}</td>
        <td><a href="{url}" target="_blank" class="pp-mini">View &rarr;</a></td>
      </tr>""")
    return "\n".join(rows)


def save_html(records: list, filepath: str):
    ts   = datetime.datetime.utcnow().strftime("%B %d, %Y at %H:%M UTC")
    tot  = len(records)
    n_pass  = sum(1 for r in records if r.get("verdict") == "pass")
    n_flag  = sum(1 for r in records if r.get("verdict") == "flag")
    n_fail  = sum(1 for r in records if r.get("verdict") == "fail")

    passing = [r for r in records if r.get("verdict") in ("pass", "flag")]
    passing.sort(key=lambda r: (
        {"high fit": 0, "worth a look": 1, "long shot": 2}.get(r.get("confidence","long shot"), 3),
        -(r.get("asset_amount") or 0),
    ))
    top5 = _render_top_cards(passing[:5])
    rows = _render_rows(records)

    disq_items = "".join(f"<li>{d}</li>" for d in STEAMERY_PROFILE["hard_disqualifiers"])

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>STEAMery Grant Discovery Report</title>
<style>
*,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
:root{{
  --bg:#0d1117;--surf:#161b22;--surf2:#21262d;--border:#30363d;
  --text:#e6edf3;--muted:#8b949e;--accent:#58a6ff;
  --green:#3fb950;--yellow:#d29922;--red:#f85149;--purple:#bc8cff;
  --r:8px;--rl:12px;
}}
html{{scroll-behavior:smooth}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",system-ui,sans-serif;
  background:var(--bg);color:var(--text);line-height:1.6;font-size:14px}}
a{{color:var(--accent);text-decoration:none}}
a:hover{{text-decoration:underline}}

/* Header */
.hdr{{
  background:linear-gradient(135deg,#0d1117 0%,#1a1f35 50%,#0d1117 100%);
  border-bottom:1px solid var(--border);
  padding:52px 32px 44px;text-align:center;position:relative;overflow:hidden
}}
.hdr::before{{
  content:"";position:absolute;inset:0;
  background:radial-gradient(ellipse at 50% 0%,rgba(88,166,255,.09) 0%,transparent 68%);
  pointer-events:none
}}
.hdr-eyebrow{{
  font-size:12px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;
  color:var(--accent);margin-bottom:14px
}}
.hdr h1{{
  font-size:clamp(26px,5vw,46px);font-weight:800;line-height:1.12;margin-bottom:14px;
  background:linear-gradient(135deg,#e6edf3 0%,#58a6ff 45%,#bc8cff 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text
}}
.hdr-sub{{color:var(--muted);font-size:15px;max-width:580px;margin:0 auto 28px}}
.stats{{display:flex;gap:24px;justify-content:center;flex-wrap:wrap;margin-top:20px}}
.stat{{
  background:var(--surf);border:1px solid var(--border);border-radius:999px;
  padding:8px 22px;font-size:13px;font-weight:500;text-align:center
}}
.stat .n{{font-size:20px;font-weight:800;display:block}}
.stat.g .n{{color:var(--green)}}.stat.y .n{{color:var(--yellow)}}.stat.r .n{{color:var(--red)}}

/* Layout */
.wrap{{max-width:1440px;margin:0 auto;padding:0 24px}}
.sec{{padding:44px 0}}
.sec-title{{
  font-size:20px;font-weight:700;margin-bottom:22px;
  display:flex;align-items:center;gap:10px
}}
.sec-title .ico{{font-size:22px}}
.sec-title .sub{{color:var(--muted);font-size:13px;font-weight:400}}

/* Disqualifier box */
.disq{{
  background:var(--surf);border:1px solid rgba(248,81,73,.3);
  border-radius:var(--rl);padding:20px 24px;margin:20px 0
}}
.disq h3{{color:var(--red);font-size:14px;font-weight:700;margin-bottom:10px}}
.disq p{{color:var(--muted);font-size:13px;margin-bottom:10px}}
.disq ul{{padding-left:18px}}
.disq li{{color:var(--muted);font-size:13px;margin-bottom:6px;line-height:1.5}}

/* Cards */
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(310px,1fr));gap:16px}}
.card{{
  background:var(--surf);border:1px solid var(--border);border-radius:var(--rl);
  padding:20px;position:relative;overflow:hidden;
  transition:border-color .2s,box-shadow .2s
}}
.card:hover{{border-color:var(--accent);box-shadow:0 0 0 1px rgba(88,166,255,.2),0 8px 24px rgba(0,0,0,.35)}}
.card::before{{content:"";position:absolute;top:0;left:0;right:0;height:3px}}
.card.high-fit::before{{background:var(--green)}}
.card.worth-a-look::before{{background:var(--yellow)}}
.card.long-shot::before{{background:var(--muted)}}
.card-top{{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin-bottom:10px}}
.card-name{{font-size:15px;font-weight:700;line-height:1.3;flex:1}}
.card-loc{{color:var(--muted);font-size:12px;margin-bottom:8px}}
.card-assets{{font-size:24px;font-weight:800;color:var(--accent);margin-bottom:8px}}
.card-why{{
  font-size:12px;color:var(--muted);line-height:1.5;margin-bottom:12px;
  border-left:2px solid var(--border);padding-left:10px
}}
.evidence{{
  background:rgba(63,185,80,.07);border:1px solid rgba(63,185,80,.2);
  border-radius:var(--r);padding:10px 12px;font-size:12px;margin-bottom:12px;line-height:1.5
}}
.ev-label{{
  color:var(--green);font-weight:700;font-size:10px;text-transform:uppercase;
  letter-spacing:.08em;display:block;margin-bottom:4px
}}
.flag-note{{
  background:rgba(88,166,255,.07);border:1px solid rgba(88,166,255,.15);
  border-radius:var(--r);padding:8px 12px;font-size:11px;color:var(--muted);margin-bottom:12px
}}
.flag-note strong{{color:var(--accent)}}
.pp-link{{
  display:inline-flex;align-items:center;gap:5px;font-size:12px;font-weight:600;
  color:var(--accent);border:1px solid var(--border);border-radius:6px;padding:5px 12px;
  transition:background .15s,border-color .15s
}}
.pp-link:hover{{background:rgba(88,166,255,.1);border-color:var(--accent);text-decoration:none}}

/* Badge */
.badge{{
  display:inline-flex;align-items:center;padding:3px 10px;border-radius:999px;
  font-size:11px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;white-space:nowrap
}}
.badge.high-fit{{background:rgba(63,185,80,.15);color:#3fb950;border:1px solid rgba(63,185,80,.3)}}
.badge.worth-a-look{{background:rgba(227,179,65,.15);color:#e3b341;border:1px solid rgba(227,179,65,.3)}}
.badge.long-shot{{background:rgba(139,148,158,.15);color:#8b949e;border:1px solid rgba(139,148,158,.3)}}
.badge.pass{{background:rgba(63,185,80,.15);color:#3fb950;border:1px solid rgba(63,185,80,.3)}}
.badge.flag{{background:rgba(210,153,34,.15);color:#d29922;border:1px solid rgba(210,153,34,.3)}}
.badge.fail{{background:rgba(248,81,73,.12);color:#f85149;border:1px solid rgba(248,81,73,.25)}}
.badge.not_screened{{background:rgba(139,148,158,.1);color:#8b949e;border:1px solid rgba(139,148,158,.2)}}

/* Table */
.table-ctrl{{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px}}
.srch{{
  background:var(--surf);border:1px solid var(--border);border-radius:var(--r);
  color:var(--text);padding:8px 14px;font-size:13px;outline:none;width:260px;
  transition:border-color .15s
}}
.srch:focus{{border-color:var(--accent)}}
.fbtn{{
  background:var(--surf);border:1px solid var(--border);border-radius:var(--r);
  color:var(--muted);padding:7px 14px;font-size:12px;cursor:pointer;
  transition:all .15s;font-weight:500
}}
.fbtn.on{{background:rgba(88,166,255,.12);border-color:var(--accent);color:var(--accent)}}
.fbtn:hover{{border-color:var(--muted);color:var(--text)}}
.tbl-wrap{{overflow-x:auto;border-radius:var(--rl);border:1px solid var(--border)}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
thead{{background:var(--surf2)}}
thead th{{
  padding:11px 14px;text-align:left;font-weight:600;color:var(--muted);
  font-size:11px;text-transform:uppercase;letter-spacing:.06em;white-space:nowrap;
  cursor:pointer;user-select:none
}}
thead th:hover{{color:var(--text)}}
thead th.srt .arr{{color:var(--accent);opacity:1}}
.arr{{opacity:.35;margin-left:4px}}
tbody tr{{border-top:1px solid var(--border);transition:background .1s}}
tbody tr:hover{{background:var(--surf2)}}
tbody tr.hidden{{display:none}}
td{{padding:11px 14px;vertical-align:top}}
.td-name{{font-weight:600;max-width:190px;word-break:break-word}}
.td-loc{{color:var(--muted);font-size:12px}}
.td-assets{{font-weight:700;color:var(--accent);white-space:nowrap}}
.ntee-chip{{
  background:var(--surf2);border-radius:4px;padding:2px 7px;
  font-size:11px;color:var(--purple);font-family:monospace
}}
.tier{{
  background:var(--surf2);border:1px solid var(--border);border-radius:999px;
  padding:1px 8px;font-size:10px;color:var(--muted);font-weight:600
}}
.tier.t1{{color:var(--green);border-color:rgba(63,185,80,.3)}}
.td-reason{{color:var(--muted);font-size:12px;max-width:240px;line-height:1.4}}
.pp-mini{{
  color:var(--accent);border:1px solid var(--border);border-radius:5px;
  padding:2px 8px;font-size:11px;font-weight:600;white-space:nowrap;
  transition:background .15s
}}
.pp-mini:hover{{background:rgba(88,166,255,.1);text-decoration:none}}

/* Footer */
.ftr{{
  border-top:1px solid var(--border);padding:24px 32px;text-align:center;
  color:var(--muted);font-size:12px;line-height:1.9
}}
@media(max-width:768px){{
  .hdr{{padding:32px 16px}}
  .wrap{{padding:0 14px}}
  .grid{{grid-template-columns:1fr}}
  .srch{{width:100%}}
}}
</style>
</head>
<body>

<!-- HEADER -->
<div class="hdr">
  <div class="hdr-eyebrow">&#128302; STEAMery Grant Discovery Tool</div>
  <h1>Private Foundation<br>Discovery Report</h1>
  <p class="hdr-sub">
    Surfacing plausible funders for The STEAMery &mdash; a STEAM education
    facility in Nashville, Brown County, Indiana. Generated {ts}.
  </p>
  <div class="stats">
    <div class="stat"><span class="n">{tot}</span>Foundations examined</div>
    <div class="stat g"><span class="n">{n_pass + n_flag}</span>Pass screening</div>
    <div class="stat y"><span class="n">{n_flag}</span>Flagged (verify)</div>
    <div class="stat r"><span class="n">{n_fail}</span>Screened out</div>
  </div>
</div>

<div class="wrap">

<!-- HARD DISQUALIFIERS -->
<div class="sec">
  <div class="disq">
    <h3>&#9888; The STEAMery&rsquo;s Hard Disqualifiers &mdash; Always Verify Before Applying</h3>
    <p>The API cannot confirm whether any given foundation enforces these requirements.
    Verify directly against each foundation&rsquo;s website or 990-PF before pursuing an application.</p>
    <ul>{disq_items}</ul>
  </div>
</div>

<!-- TOP CANDIDATES -->
<div class="sec">
  <div class="sec-title">
    <span class="ico">&#127942;</span>
    Top Candidates
    <span class="sub">&nbsp;&mdash; highest-confidence, pass-screening foundations</span>
  </div>
  <div class="grid">
    {top5}
  </div>
</div>

<!-- FULL TABLE -->
<div class="sec">
  <div class="sec-title">
    <span class="ico">&#128203;</span>
    All Screened Foundations
    <span class="sub">&nbsp;&mdash; full auditable log ({tot} organizations)</span>
  </div>
  <div class="table-ctrl">
    <input class="srch" id="srch" placeholder="&#128269;  Search name, city, NTEE&hellip;" oninput="ft()">
    <button class="fbtn on"  onclick="sf('all',this)">All</button>
    <button class="fbtn"     onclick="sf('pass',this)">&#10003; Pass</button>
    <button class="fbtn"     onclick="sf('flag',this)">&#9873; Flag</button>
    <button class="fbtn"     onclick="sf('fail',this)">&#10007; Screened out</button>
    <button class="fbtn"     onclick="sf('high fit',this)">&#11088; High fit</button>
  </div>
  <div class="tbl-wrap">
    <table id="tbl">
      <thead><tr>
        <th onclick="st(0)">Foundation <span class="arr">&#8597;</span></th>
        <th onclick="st(1)">Location <span class="arr">&#8597;</span></th>
        <th onclick="st(2)">Assets <span class="arr">&#8597;</span></th>
        <th>NTEE</th><th>Tier</th><th>Verdict</th><th>Confidence</th>
        <th>Reason / Note</th><th>Grant History</th><th>Link</th>
      </tr></thead>
      <tbody id="tb">{rows}</tbody>
    </table>
  </div>
  <p style="color:var(--muted);font-size:11px;margin-top:8px">
    All financial data from most recent available IRS 990-PF filing via ProPublica.
    Verify all candidates at their ProPublica page before acting.
  </p>
</div>

</div>

<!-- FOOTER -->
<div class="ftr">
  Generated by the STEAMery Grant Discovery Tool (volunteer project) &bull;
  Data: ProPublica Nonprofit Explorer API &bull;
  <a href="https://projects.propublica.org/nonprofits/api/">API docs</a><br>
  <strong>This tool surfaces plausible candidates &mdash; it does not guarantee eligibility.
  Always verify requirements directly with each foundation before Kirstie applies.</strong>
</div>

<script>
var flt='all',srt={{c:-1,d:1}};
function ft(){{
  var q=document.getElementById('srch').value.toLowerCase();
  document.querySelectorAll('#tb tr').forEach(function(r){{
    var v=r.dataset.verdict||'',cf=r.dataset.conf||'',tx=r.textContent.toLowerCase();
    var mf=flt==='all'||v===flt||cf===flt;
    var ms=!q||tx.includes(q);
    r.classList.toggle('hidden',!(mf&&ms));
  }});
}}
function sf(f,b){{
  flt=f;
  document.querySelectorAll('.fbtn').forEach(function(x){{x.classList.remove('on')}});
  b.classList.add('on');ft();
}}
function st(ci){{
  var tb=document.getElementById('tb');
  var rows=Array.from(tb.querySelectorAll('tr'));
  var ths=document.querySelectorAll('thead th');
  if(srt.c===ci){{srt.d*=-1}}else{{srt.c=ci;srt.d=1}}
  ths.forEach(function(h,i){{
    h.classList.toggle('srt',i===ci);
    var a=h.querySelector('.arr');
    if(a)a.textContent=i===ci?(srt.d===1?'&#8593;':'&#8595;'):'&#8597;';
  }});
  rows.sort(function(a,b){{
    var at=a.cells[ci]&&(a.cells[ci].dataset.sort||a.cells[ci].textContent)||'';
    var bt=b.cells[ci]&&(b.cells[ci].dataset.sort||b.cells[ci].textContent)||'';
    var an=parseFloat(at.replace(/[^0-9.-]/g,''));
    var bn=parseFloat(bt.replace(/[^0-9.-]/g,''));
    if(!isNaN(an)&&!isNaN(bn))return(an-bn)*srt.d;
    return at.localeCompare(bt)*srt.d;
  }});
  rows.forEach(function(r){{tb.appendChild(r)}});
}}
</script>
</body>
</html>"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"  [saved] {filepath}")


# =============================================================================
# MAIN PIPELINE
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="STEAMery Grant Discovery Tool"
    )
    parser.add_argument("--dry-run", action="store_true",
        help="1 search page only, no XML fetch. Use to validate the pipeline quickly.")
    parser.add_argument("--skip-xml", action="store_true",
        help="Skip IRS XML grant history fetch (faster; skips Part 3).")
    parser.add_argument("--top-n", type=int, default=15,
        help="Number of top candidates to run grant history on (default: 15).")
    args = parser.parse_args()

    print()
    print("=" * 60)
    print("  STEAMery Grant Discovery Tool")
    print("  Brown County, Indiana | Volunteer research project")
    print("=" * 60)
    if args.dry_run:
        print("  [DRY RUN] 1 page per search, no XML. Fast validation mode.")
    print()

    # ── PART 1: Discovery ────────────────────────────────────────────────
    print("PART 1: Foundation Discovery")
    print("-" * 40)
    candidates = collect_all_candidates(dry_run=args.dry_run)
    print(f"\n  Collected {len(candidates)} unique organizations.\n")

    # ── PART 2: Screening ────────────────────────────────────────────────
    print("PART 2: Eligibility Screening")
    print("-" * 40)

    # In dry-run mode, cap the number of orgs we fetch detail for so the
    # test completes in ~30 seconds rather than 20+ minutes.
    DRY_RUN_MAX_ORGS = 25
    candidate_items = list(candidates.items())
    if args.dry_run:
        candidate_items = candidate_items[:DRY_RUN_MAX_ORGS]
        print(f"  [DRY RUN] Capping org detail fetches at {DRY_RUN_MAX_ORGS} orgs.\n")

    all_records = []
    n_pass = n_fail = n_flag = 0

    for i, (ein, search_org) in enumerate(candidate_items, 1):
        name = search_org.get("name") or str(ein)
        print(f"  [{i}/{len(candidate_items)}] {name}")

        org_detail = fetch_org_detail(ein)
        if not org_detail:
            rec = build_record(ein, search_org, None, None, None)
            rec["verdict"] = "fail"
            rec["rejection_reason"] = "Could not fetch organization detail from ProPublica API."
            all_records.append(rec)
            n_fail += 1
            print(f"    [fail] Could not fetch detail.")
            continue

        screening = screen_candidate(org_detail, search_org.get("geo_tier", 2))
        v = screening.get("verdict")
        if v == "pass":   n_pass += 1;  sym = "[pass]"
        elif v == "flag": n_flag += 1;  sym = "[flag]"
        else:             n_fail += 1;  sym = "[fail]"

        note = screening.get("rejection_reason") or (screening.get("flags") or [""])[0]
        print(f"    {sym} {(note or '')[:70]}")

        all_records.append(build_record(ein, search_org, org_detail, screening, None))

    print(f"\n  Screened {len(all_records)} foundations")
    print(f"  Pass: {n_pass}  Flag: {n_flag}  Fail: {n_fail}")

    # ── PART 3: Grant history ────────────────────────────────────────────
    if not args.dry_run and not args.skip_xml:
        print(f"\nPART 3: Grant History (top {args.top_n} candidates)")
        print("-" * 40)

        passing = [r for r in all_records if r.get("verdict") in ("pass", "flag")]
        passing.sort(key=lambda r: (
            {"high fit": 0, "worth a look": 1, "long shot": 2}.get(r.get("confidence","long shot"), 3),
            -(r.get("asset_amount") or 0),
        ))
        top_eins = {r["ein"] for r in passing[:args.top_n]}

        for r in all_records:
            if r["ein"] not in top_eins or not r.get("org_detail_available"):
                continue
            print(f"  Checking: {r.get('name')} (EIN {r['ein']})")
            org_detail = fetch_org_detail(r["ein"])
            if org_detail:
                gh = check_grant_history(r["ein"], org_detail)
                r["grant_history"] = gh
                sim = len(gh.get("similar_grants") or [])
                print(f"    -> {gh.get('method')}: {gh.get('notes','')[:80]}")
                if sim:
                    print(f"    [MATCH] {sim} similar past grant(s) found!")

    # ── PART 4: Output ───────────────────────────────────────────────────
    print("\nPART 4: Generating Output")
    print("-" * 40)
    save_json(all_records, OUTPUT_JSON)
    save_html(all_records, OUTPUT_HTML)

    print()
    print("=" * 60)
    print("  DONE")
    print(f"  {len(all_records)} foundations screened")
    print(f"  {n_pass + n_flag} passed  |  {n_fail} screened out (with logged reasons)")
    print()
    print(f"  Full data:    {os.path.abspath(OUTPUT_JSON)}")
    print(f"  HTML report:  {os.path.abspath(OUTPUT_HTML)}")
    print()
    print("  NOTE: This tool surfaces plausible candidates only.")
    print("  Always verify requirements directly with each foundation")
    print("  before Kirstie invests time in an application.")
    print("=" * 60)
    print()


if __name__ == "__main__":
    main()
