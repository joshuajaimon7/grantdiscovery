# STEAMery Grant Discovery Tool

A volunteer research tool for **The STEAMery** — a STEAM education nonprofit in Nashville, Brown County, Indiana (converting an old sock factory into a hands-on learning facility).

**The problem it solves:** There are thousands of private foundations out there, but The STEAMery is disqualified from most of them. Finding that out late is expensive. This tool pre-screens foundations against The STEAMery's specific hard disqualifiers before anyone invests time in an application.

---

## What it does

1. **Discovers** private foundations (990-PF filers) in Indiana + bordering states via ProPublica's free Nonprofit Explorer API — no API key required.
2. **Screens** every candidate against The STEAMery's hard disqualifiers (new org, no site control, no audited financials, no paid staff).
3. **Checks grant history** by parsing IRS e-file XML for the top candidates — surfaces actual evidence like "Funded [Rural Youth STEM Org] for $15,000 in 2022."
4. **Outputs** two files:
   - `results.json` — full auditable log of every foundation screened (with logged rejection reasons)
   - `report.html` — beautiful interactive browser report with confidence badges, sortable table, and one-click ProPublica links

---

## Quick start

```bash
# Install dependencies (one time)
pip3 install requests lxml

# Run a quick test (1 page per search, ~2 minutes)
python3 steamery_grant_finder.py --dry-run

# Full run (~15–30 minutes)
python3 steamery_grant_finder.py

# Full run, skip XML parsing (faster, no grant history)
python3 steamery_grant_finder.py --skip-xml
```

Then open `report.html` in any browser.

---

## Output files

| File | What it is |
|------|------------|
| `results.json` | Complete data for every foundation examined. Every rejected org has a `rejection_reason`. Auditable. |
| `report.html` | Human-readable interactive report. Open in browser. Sortable table, filter buttons, confidence badges. |

---

## Confidence labels

| Label | Meaning |
|-------|---------|
| **High fit** | Indiana-based, education/community NTEE, healthy asset range, active recent filing |
| **Worth a look** | Adjacent state OR less relevant NTEE OR larger funder — worth verifying manually |
| **Long shot** | Multiple concerns, dormancy flag, or very large institutional funder |

---

## The STEAMery's hard disqualifiers

These are **always** flagged as "verify manually" on every passing candidate, because the API cannot tell us whether a specific foundation enforces them:

- Org history requirement ≥ 2 years → STEAMery incorporated Dec 2024 (< 1 yr old)
- Municipal applicant required → STEAMery is a 501(c)(3), not a municipality
- Site control required → STEAMery does not yet own or lease the building
- Audited financials required → none available
- Minimum budget / paid staff required → pre-revenue, no paid staff

---

## Configuration

All tuneable settings are at the top of `steamery_grant_finder.py` under **SECTION 0**:

| Setting | Default | Effect |
|---------|---------|--------|
| `MAX_PAGES_TIER1` | `999` (all) | How many search result pages to fetch for Indiana |
| `MAX_PAGES_TIER2` | `10` | Pages per search for bordering states |
| `MIN_ASSETS_DOLLARS` | `$50,000` | Below this = likely dormant, screened out |
| `FLAG_LARGE_ASSETS_DOLLARS` | `$500,000,000` | Above this = flagged (not auto-excluded) |
| `REQUEST_DELAY_SECONDS` | `1.0` | Seconds between API calls (be polite) |
| `--top-n` flag | `15` | How many top candidates get XML grant history checked |

---

## Data sources

- **ProPublica Nonprofit Explorer API** — https://projects.propublica.org/nonprofits/api/ (free, no auth)
- **IRS e-file XML** — https://s3.amazonaws.com/irs-form-990/ (public, no auth)
- All data is from IRS filings. Financial figures come from each foundation's most recent 990-PF.

---

## Version roadmap

| Version | What's in it |
|---------|-------------|
| **v1 (now)** | Discovery + screening + XML grant history + HTML report |
| **v2** | Streamlit web UI — Kirstie can run searches herself in a browser with a "Run" button and live progress |
| **v3** | Scheduled weekly re-run + email digest when new matching foundations appear in ProPublica data |

---

## Important caveat

**This tool surfaces plausible candidates — it does not guarantee eligibility.** Many private foundation requirements are not published in the API (org age, site control, audit requirements, etc.). Always verify requirements directly with each foundation before Kirstie invests time in writing an application.

Every record in `results.json` includes a `what_api_cannot_tell` list that spells out exactly what needs manual verification for that candidate.
