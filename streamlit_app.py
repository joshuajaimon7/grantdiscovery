#!/usr/bin/env python3
"""
STEAMery Grant Discovery Tool — Streamlit Web App
==================================================
Real-time web interface. Kirstie opens a URL, clicks Run,
sees foundations discovered + screened live in her browser.

Deploy free: share.streamlit.io → connect GitHub repo → done.
"""

import streamlit as st
import datetime
import json
import os
import sys

# Import all core logic from the existing script — zero duplication
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import steamery_grant_finder as gf

# ─────────────────────────────────────────────────────────────────────────────
# DISK PERSISTENCE  — survives WebSocket reconnects on Streamlit Cloud
# ─────────────────────────────────────────────────────────────────────────────
_SESSION_CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".streamlit_session.json")

def _save_session(records, ts, n_pass, n_flag, n_fail):
    """Write results to disk so they survive a WebSocket reconnect."""
    try:
        with open(_SESSION_CACHE, "w") as f:
            json.dump({"records": records, "ts": ts,
                       "n_pass": n_pass, "n_flag": n_flag, "n_fail": n_fail}, f)
    except Exception:
        pass

def _load_session():
    """Load results from disk if session_state is empty (after reconnect)."""
    try:
        if os.path.exists(_SESSION_CACHE):
            with open(_SESSION_CACHE) as f:
                return json.load(f)
    except Exception:
        pass
    return None

# ─────────────────────────────────────────────────────────────────────────────
# PAGE CONFIG
# ─────────────────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="STEAMery Grant Finder",
    page_icon="🔭",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        "Get help": "https://github.com/joshuajaimon7/grantdiscovery",
        "About": "STEAMery Grant Discovery Tool — volunteer project, Brown County Indiana",
    },
)

# ─────────────────────────────────────────────────────────────────────────────
# CUSTOM CSS  (dark theme matching the HTML report aesthetic)
# ─────────────────────────────────────────────────────────────────────────────
st.markdown("""
<style>
/* Candidate cards */
.cand-card {
    background:#161b22; border:1px solid #30363d; border-radius:12px;
    padding:18px; margin-bottom:14px; position:relative; overflow:hidden;
}
.cand-card::before {
    content:""; position:absolute; top:0; left:0; right:0; height:3px;
}
.cand-card.high-fit::before   { background:#3fb950; }
.cand-card.worth-a-look::before { background:#d29922; }
.cand-card.long-shot::before  { background:#8b949e; }

/* Confidence badges */
.badge { display:inline-block; padding:3px 10px; border-radius:999px;
         font-size:11px; font-weight:700; text-transform:uppercase; letter-spacing:.05em; }
.badge-hf  { background:rgba(63,185,80,.15);  color:#3fb950; border:1px solid rgba(63,185,80,.3); }
.badge-wal { background:rgba(210,153,34,.15); color:#d29922; border:1px solid rgba(210,153,34,.3); }
.badge-ls  { background:rgba(139,148,158,.15);color:#8b949e; border:1px solid rgba(139,148,158,.3); }

/* Past grant evidence block */
.ev-block {
    background:rgba(63,185,80,.07); border:1px solid rgba(63,185,80,.2);
    border-radius:8px; padding:10px 12px; font-size:12px; margin-top:10px; line-height:1.5;
}
.ev-label { color:#3fb950; font-size:10px; font-weight:700;
            text-transform:uppercase; letter-spacing:.08em; display:block; margin-bottom:4px; }

/* Main gradient title */
.main-title {
    font-size:2.5rem; font-weight:800; line-height:1.15; margin-bottom:6px;
    background:linear-gradient(135deg,#e6edf3 0%,#58a6ff 45%,#bc8cff 100%);
    -webkit-background-clip:text; -webkit-text-fill-color:transparent; background-clip:text;
}

/* Disqualifier / warning box */
.disq-box {
    background:#161b22; border:1px solid rgba(248,81,73,.3);
    border-radius:10px; padding:14px 18px; font-size:13px;
}
</style>
""", unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────────────────────
# SIDEBAR — SETTINGS
# ─────────────────────────────────────────────────────────────────────────────
with st.sidebar:
    st.markdown("### 🔭 STEAMery Grant Finder")
    st.markdown("Volunteer research tool for **Kirstie Tiernan** — The STEAMery, Nashville IN")
    st.divider()

    st.subheader("⚙️ Search Settings")

    depth = st.radio(
        "Search depth",
        options=["Quick (~8 min)", "Standard (~20 min) ★ Recommended", "Full (~45 min)"],
        index=1,
        help=(
            "Quick: 3 pages/state, ~1,500 foundations — good for testing.\n"
            "Standard: 10 pages/state, ~4,000 foundations — best balance.\n"
            "Full: exhaustive Indiana + 20 pages bordering — most complete."
        ),
    )

    include_xml = st.checkbox(
        "Check grant history (IRS XML)",
        value=True,
        help="Parses IRS e-file XML for top candidates — finds past grants similar to STEAMery. Adds ~5-10 min.",
    )

    top_n = st.slider(
        "Grant history checks (top N candidates)", 5, 30, 15,
        help="How many top-ranked passing foundations to check IRS XML for grant evidence.",
    )

    st.divider()

    st.subheader("🔍 Thresholds")
    min_assets = st.number_input(
        "Min assets ($)", value=50_000, step=10_000,
        help="Foundations with assets below this threshold are considered potentially dormant.",
    )

    st.divider()

    with st.expander("⚠️ STEAMery Hard Disqualifiers"):
        st.caption("Always verify these manually before Kirstie applies:")
        for d in gf.STEAMERY_PROFILE["hard_disqualifiers"]:
            st.markdown(f"• {d}")

    st.caption("Data: ProPublica Nonprofit Explorer API · IRS e-file XML (free, no auth)")


# ─────────────────────────────────────────────────────────────────────────────
# HEADER
# ─────────────────────────────────────────────────────────────────────────────
st.markdown('<div class="main-title">STEAMery Grant Discovery</div>', unsafe_allow_html=True)
st.markdown(
    "**Surfacing private foundation funders for The STEAMery** — Nashville, Brown County, Indiana.  \n"
    "Click **Run Grant Search** to discover, screen, and analyze foundations in real time."
)
st.caption("Volunteer research tool. Results are a starting point — always verify eligibility before applying.")
st.divider()


# ─────────────────────────────────────────────────────────────────────────────
# RUN BUTTON
# ─────────────────────────────────────────────────────────────────────────────
col_btn, col_hint = st.columns([1, 3])
with col_btn:
    run_clicked = st.button("🔍 Run Grant Search", type="primary", use_container_width=True)
with col_hint:
    if "Quick" in depth:
        st.info("**Quick mode**: 3 pages/state · ~8 min · good for testing")
    elif "Standard" in depth:
        st.success("**Standard mode ★**: 10 pages/state · ~20 min · recommended balance")
    else:
        st.warning("**Full mode**: exhaustive Indiana · ~45 min · most complete")


# ─────────────────────────────────────────────────────────────────────────────
# SESSION STATE  (results persist after run completes)
# ─────────────────────────────────────────────────────────────────────────────
if "results" not in st.session_state:
    st.session_state.results = None
if "run_ts" not in st.session_state:
    st.session_state.run_ts = None
if "n_pass" not in st.session_state:
    st.session_state.n_pass = 0
if "n_flag" not in st.session_state:
    st.session_state.n_flag = 0
if "n_fail" not in st.session_state:
    st.session_state.n_fail = 0

# Restore from disk if session was lost due to WebSocket reconnect
if st.session_state.results is None:
    cached = _load_session()
    if cached:
        st.session_state.results = cached["records"]
        st.session_state.run_ts  = cached["ts"]
        st.session_state.n_pass  = cached["n_pass"]
        st.session_state.n_flag  = cached["n_flag"]
        st.session_state.n_fail  = cached["n_fail"]

# Show banner at the top if results already exist from a previous run
if st.session_state.results is not None:
    n_ready = st.session_state.n_pass + st.session_state.n_flag
    st.success(
        f"📊 Results from last run are ready below — **{n_ready} foundations passed**. "
        "Scroll down to see them ↓"
    )


# ─────────────────────────────────────────────────────────────────────────────
# MAIN RUN LOGIC
# ─────────────────────────────────────────────────────────────────────────────
if run_clicked:
    # Override MIN_ASSETS from sidebar
    gf.MIN_ASSETS_DOLLARS = min_assets

    # Set max pages based on depth
    if "Quick" in depth:
        t1_pages, t2_pages = 3, 3
    elif "Standard" in depth:
        t1_pages, t2_pages = 10, 10
    else:
        t1_pages, t2_pages = 999, 20

    all_records = []
    n_pass = n_fail = n_flag = 0

    # ── UI placeholders ─────────────────────────────────────────────────
    phase_label   = st.empty()
    metrics_row   = st.empty()
    bar_search    = st.empty()
    bar_screen    = st.empty()
    action_txt    = st.empty()

    # Build combo list
    combos = (
        [(1, s, t1_pages) for s in gf.STATES_TIER1] +
        [(2, s, t2_pages) for s in gf.STATES_TIER2]
    )
    total_combos = len(combos) * len(gf.NTEE_GROUPS) * len(gf.SEARCH_QUERIES)
    combo_done   = 0

    # ── PHASE 1: Discovery ──────────────────────────────────────────────
    phase_label.markdown("### 🔍 Phase 1 of 4 — Foundation Discovery")
    candidates = {}

    for tier, state, max_pages in combos:
        for ntee in gf.NTEE_GROUPS:
            for query in gf.SEARCH_QUERIES:
                combo_done += 1
                pct = combo_done / total_combos
                bar_search.progress(pct, text=f"Searching foundations: {combo_done}/{total_combos} combinations")
                action_txt.caption(f"📡 {state} / NTEE {ntee} / \"{query}\"")

                orgs = gf.search_foundations(state, ntee, query, max_pages, False)
                for org in orgs:
                    ein = org.get("ein")
                    if ein and ein not in candidates:
                        org["geo_tier"] = tier
                        candidates[ein] = org

                with metrics_row.container():
                    c1, c2, c3, c4 = st.columns(4)
                    c1.metric("🏛️ Orgs Found",       f"{len(candidates):,}")
                    c2.metric("🔬 Screened",          "—")
                    c3.metric("✅ Passed",             "—")
                    c4.metric("📡 Combos Done",        f"{combo_done}/{total_combos}")

    # ── PHASE 2: Screening ──────────────────────────────────────────────
    phase_label.markdown("### 🔬 Phase 2 of 4 — Eligibility Screening")
    bar_search.progress(1.0, text="Search complete ✓")
    candidate_items = list(candidates.items())
    total_screen    = len(candidate_items)

    for i, (ein, search_org) in enumerate(candidate_items, 1):
        name = search_org.get("name") or str(ein)

        # ── PRE-SCREEN (no API call) ─────────────────────────────────────
        # The ProPublica search result already includes foundation_code from
        # the IRS BMF. If the org is a public charity (codes 10-21), we can
        # fail it immediately without making an expensive fetch_org_detail
        # call. This eliminates ~96% of orgs and cuts runtime from 47 min → 2 min.
        fc = search_org.get("foundation_code") or search_org.get("foundation_cd")
        if fc:
            try:
                if int(fc) in gf.PUBLIC_CHARITY_CODES:
                    rec = gf.build_record(ein, search_org, None, None, None)
                    rec["verdict"] = "fail"
                    rec["rejection_reason"] = (
                        f"IRS foundation_code={fc} = public charity "
                        "(pre-screened from search result — no API call needed)."
                    )
                    all_records.append(rec)
                    n_fail += 1
                    # Only update UI every 50 orgs during fast pre-screen pass
                    # to avoid Streamlit render overhead dominating the loop
                    if i % 50 == 0 or i == total_screen:
                        pct = i / total_screen
                        bar_screen.progress(pct, text=f"Screening: {i:,}/{total_screen:,} (⚡ fast pre-screen)")
                        action_txt.caption(f"⚡ Pre-screening by foundation type: {i:,}/{total_screen:,}")
                        with metrics_row.container():
                            c1, c2, c3, c4 = st.columns(4)
                            c1.metric("🏛️ Orgs Found",  f"{len(candidates):,}")
                            c2.metric("🔬 Screened",     f"{i:,}")
                            c3.metric("✅ Passed",        f"{n_pass + n_flag}")
                            c4.metric("✗ Screened Out",  f"{n_fail:,}")
                    continue
            except (ValueError, TypeError):
                pass  # foundation_code not parseable — fall through to full check

        # ── FULL SCREEN (requires API call) ─────────────────────────────
        # Only reaches here for orgs that might be real private foundations
        pct = i / total_screen
        bar_screen.progress(pct, text=f"Screening: {i:,}/{total_screen:,} foundations")
        action_txt.caption(f"🔬 [{i}/{total_screen}] {name}")

        with metrics_row.container():
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("🏛️ Orgs Found",  f"{len(candidates):,}")
            c2.metric("🔬 Screened",     f"{i:,}")
            c3.metric("✅ Passed",        f"{n_pass + n_flag}")
            c4.metric("✗ Screened Out",  f"{n_fail:,}")

        org_detail = gf.fetch_org_detail(ein)
        if not org_detail:
            rec = gf.build_record(ein, search_org, None, None, None)
            rec["verdict"] = "fail"
            rec["rejection_reason"] = "Could not fetch org detail from ProPublica API."
            all_records.append(rec)
            n_fail += 1
            continue

        screening = gf.screen_candidate(org_detail, search_org.get("geo_tier", 2))
        v = screening.get("verdict")
        if v == "pass":   n_pass += 1
        elif v == "flag": n_flag += 1
        else:             n_fail += 1

        all_records.append(gf.build_record(ein, search_org, org_detail, screening, None))


    # ── PHASE 3: Grant history ──────────────────────────────────────────
    if include_xml:
        phase_label.markdown(f"### 📄 Phase 3 of 4 — Grant History (top {top_n})")
        bar_screen.progress(1.0, text="Screening complete ✓")

        passing_pre = sorted(
            [r for r in all_records if r.get("verdict") in ("pass", "flag")],
            key=lambda r: (
                {"high fit": 0, "worth a look": 1, "long shot": 2}.get(r.get("confidence", "long shot"), 3),
                -(r.get("asset_amount") or 0),
            ),
        )
        top_eins = {r["ein"] for r in passing_pre[:top_n]}
        xml_done = 0

        for r in all_records:
            if r["ein"] not in top_eins or not r.get("org_detail_available"):
                continue
            xml_done += 1
            pct = xml_done / len(top_eins) if top_eins else 1
            bar_search.progress(pct, text=f"Checking IRS XML: {xml_done}/{len(top_eins)}")
            action_txt.caption(f"📄 Fetching grant history: {r.get('name')}")

            with metrics_row.container():
                c1, c2, c3, c4 = st.columns(4)
                c1.metric("🏛️ Orgs Found",   f"{len(candidates):,}")
                c2.metric("🔬 Screened",      f"{total_screen:,}")
                c3.metric("✅ Passed",         f"{n_pass + n_flag}")
                c4.metric("📄 XML Checked",   f"{xml_done}/{len(top_eins)}")

            org_detail = gf.fetch_org_detail(r["ein"])
            if org_detail:
                gh  = gf.check_grant_history(r["ein"], org_detail)
                r["grant_history"] = gh
                est = gf.compute_typical_grant(r)
                if est and r.get("filing_summary") is not None:
                    r["filing_summary"]["typical_grant_estimate"] = est

    # ── PHASE 4: Output files ───────────────────────────────────────────
    phase_label.markdown("### 💾 Phase 4 of 4 — Writing output files")
    action_txt.caption("Generating report, CSV, email templates...")

    passing_sorted = sorted(
        [r for r in all_records if r.get("verdict") in ("pass", "flag")],
        key=lambda r: (
            {"high fit": 0, "worth a look": 1, "long shot": 2}.get(r.get("confidence", "long shot"), 3),
            -(r.get("asset_amount") or 0),
        ),
    )
    for r in passing_sorted[:10]:
        r["email_template"] = gf.generate_email_template(r)

    gf.save_json(all_records, gf.OUTPUT_JSON)
    gf.save_csv(all_records, gf.OUTPUT_CSV)
    gf.save_html(all_records, gf.OUTPUT_HTML)

    run_ts = datetime.datetime.utcnow().strftime("%B %d, %Y at %H:%M UTC")

    # Persist to session state
    st.session_state.results = all_records
    st.session_state.run_ts  = run_ts
    st.session_state.n_pass  = n_pass
    st.session_state.n_flag  = n_flag
    st.session_state.n_fail  = n_fail

    # Also save to disk — survives WebSocket reconnects on Streamlit Cloud
    _save_session(all_records, run_ts, n_pass, n_flag, n_fail)

    # Clean up progress UI
    phase_label.empty(); metrics_row.empty()
    bar_search.empty();  bar_screen.empty(); action_txt.empty()

    # Show prominent completion message — results render inline below
    # NOTE: do NOT call st.rerun() here. After a 20-min run the WebSocket
    # is often degraded; rerun causes a restart that loses session state.
    # Instead, let the results section below render in the same pass.
    st.balloons()
    st.success(
        f"✅ Complete! Screened **{len(all_records):,}** foundations — "
        f"**{n_pass + n_flag}** passed · {n_fail:,} screened out.  \n"
        "**The full results are right below — scroll down ↓**"
    )


# ─────────────────────────────────────────────────────────────────────────────
# RESULTS DISPLAY  (renders inline after run, or on refresh if cache exists)
# ─────────────────────────────────────────────────────────────────────────────
# Bug-safe check: use `is not None` not truthiness — an empty list [] is falsy
# but still means we ran and got results (just 0 passing). Always show the section.
if st.session_state.results is not None:
    records = st.session_state.results
    ts      = st.session_state.run_ts
    n_pass  = st.session_state.n_pass
    n_flag  = st.session_state.n_flag
    n_fail  = st.session_state.n_fail
    n_hf    = sum(1 for r in records if r.get("confidence") == "high fit")

    # Anchor so browser can jump here
    st.markdown('<a name="results"></a>', unsafe_allow_html=True)
    st.markdown("## 📊 Results")
    st.caption(f"📅 Last run: {ts}")

    # ── Summary metrics ──────────────────────────────────────────────────
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Total Examined",  f"{len(records):,}")
    m2.metric("⭐ High Fit",     n_hf)
    m3.metric("✅ Passed",       n_pass + n_flag)
    m4.metric("⚑ Flagged",      n_flag)
    m5.metric("✗ Screened Out",  n_fail)

    st.divider()

    passing = sorted(
        [r for r in records if r.get("verdict") in ("pass", "flag")],
        key=lambda r: (
            {"high fit": 0, "worth a look": 1, "long shot": 2}.get(r.get("confidence", "long shot"), 3),
            -(r.get("asset_amount") or 0),
        ),
    )

    # ── Tabs ────────────────────────────────────────────────────────────
    tab1, tab2, tab3, tab4 = st.tabs(
        ["🏆 Top Candidates", "📋 Full Table", "✉️ Email Drafts", "💾 Downloads"]
    )

    # ── Tab 1: Top candidates ────────────────────────────────────────────
    with tab1:
        if not passing:
            st.warning(
                "**No foundations passed all screening rules in this run.**\n\n"
                "This can happen because:\n"
                "- The search was capped at fewer pages (try **Full** depth in the sidebar)\n"
                "- All Indiana private foundations in this batch were operating foundations "
                "(zero grants paid) or public charities — both are correctly filtered out\n"
                "- The screening rules are strict by design to avoid wasting Kirstie's time\n\n"
                "**What to try:**\n"
                "1. Switch to **Full (~45 min)** depth in the sidebar and run again\n"
                "2. Check the **📋 Full Table** tab to see all screened foundations and why each failed\n"
                "3. Lower the Min Assets threshold in the sidebar (currently $50,000)"
            )

        else:
            st.caption(f"{len(passing)} foundations passed screening — showing top 10")
            for r in passing[:10]:
                name    = r.get("name") or "Unknown"
                loc     = ", ".join(filter(None, [r.get("city"), r.get("state")]))
                assets  = gf._fmt_assets(r.get("asset_amount"))
                conf    = r.get("confidence") or "long shot"
                url     = r.get("propublica_url", "")
                gh      = r.get("grant_history") or {}
                similar = gh.get("similar_grants") or []
                typical = (r.get("filing_summary") or {}).get("typical_grant_estimate")
                flags   = r.get("flags") or []

                badge_cls = {"high fit": "badge-hf", "worth a look": "badge-wal"}.get(conf, "badge-ls")
                card_cls  = conf.replace(" ", "-")

                ev_html = ""
                if similar:
                    g  = similar[0]
                    gn = g.get("recipient_name", "")
                    gp = (g.get("purpose") or "")[:90]
                    ga = g.get("amount")
                    amt_str = f" (${ga:,})" if ga else ""
                    ev_html = f"""
                    <div class="ev-block">
                      <span class="ev-label">Past Grant Evidence</span>
                      Funded <strong>{gn}</strong>{amt_str}
                      {f"&mdash; {gp}" if gp else ""}
                    </div>"""

                typical_html = f'<div style="font-size:12px;color:#8b949e;margin-bottom:4px">Typical grant ≈ <strong>${typical:,}</strong></div>' if typical else ""
                flag_html    = f'<div style="font-size:11px;color:#d29922;margin-bottom:6px">⚑ {flags[0][:100]}</div>' if flags else ""

                st.markdown(f"""
<div class="cand-card {card_cls}">
  <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px">
    <div style="font-size:16px;font-weight:700">{name}</div>
    <span class="badge {badge_cls}">{conf}</span>
  </div>
  <div style="color:#8b949e;font-size:12px;margin-bottom:6px">📍 {loc}</div>
  <div style="font-size:24px;font-weight:800;color:#58a6ff;margin-bottom:6px">{assets}</div>
  {typical_html}{flag_html}{ev_html}
  <div style="margin-top:12px">
    <a href="{url}" target="_blank"
       style="color:#58a6ff;border:1px solid #30363d;border-radius:6px;
              padding:5px 12px;font-size:12px;font-weight:600;text-decoration:none">
      View on ProPublica →
    </a>
  </div>
</div>""", unsafe_allow_html=True)

    # ── Tab 2: Full sortable table ───────────────────────────────────────
    with tab2:
        import pandas as pd

        f_col1, f_col2 = st.columns([1, 3])
        with f_col1:
            vf = st.selectbox("Filter by verdict", ["All", "pass", "flag", "fail"])
        with f_col2:
            sq = st.text_input("Search name / city / state", placeholder="e.g. Smith, Indianapolis, IN")

        rows = []
        for r in records:
            fs = r.get("filing_summary") or {}
            gh = r.get("grant_history") or {}
            rows.append({
                "Name":              r.get("name"),
                "State":             r.get("state"),
                "City":              r.get("city"),
                "NTEE":              r.get("ntee_code"),
                "Tier":              r.get("geo_tier"),
                "Verdict":           r.get("verdict"),
                "Confidence":        r.get("confidence"),
                "Assets ($)":        r.get("asset_amount"),
                "Grants Paid ($)":   fs.get("grants_paid_total"),
                "Typical Grant ($)": fs.get("typical_grant_estimate"),
                "Filing Year":       fs.get("most_recent_year"),
                "Similar Grants":    len(gh.get("similar_grants") or []),
                "ProPublica URL":    r.get("propublica_url"),
            })

        df = pd.DataFrame(rows)
        if vf != "All":
            df = df[df["Verdict"] == vf]
        if sq:
            mask = df.apply(lambda row: sq.lower() in str(row).lower(), axis=1)
            df = df[mask]

        st.caption(f"Showing {len(df):,} of {len(records):,} foundations")
        st.dataframe(
            df,
            use_container_width=True,
            height=520,
            column_config={
                "ProPublica URL":    st.column_config.LinkColumn("ProPublica"),
                "Assets ($)":        st.column_config.NumberColumn(format="$%d"),
                "Grants Paid ($)":   st.column_config.NumberColumn(format="$%d"),
                "Typical Grant ($)": st.column_config.NumberColumn(format="$%d"),
            },
        )

    # ── Tab 3: Email drafts ──────────────────────────────────────────────
    with tab3:
        st.warning(
            "⚠️ **Always verify** eligibility and check for unsolicited application policies "
            "before sending. Customize all [bracketed placeholders] before use.",
            icon="⚠️",
        )

        templates = [r for r in passing[:10] if r.get("email_template")]
        if not templates:
            st.info(
                "Email templates are generated for the top 10 passing foundations.  \n"
                "Re-run with **Check grant history (IRS XML)** enabled to get personalised evidence."
            )
        else:
            for r in templates:
                name   = r.get("name") or "Unknown"
                conf   = r.get("confidence") or ""
                assets = gf._fmt_assets(r.get("asset_amount"))
                sim    = len((r.get("grant_history") or {}).get("similar_grants") or [])
                ev_tag = f" · {sim} past grant match{'es' if sim!=1 else ''}" if sim else ""
                with st.expander(f"✉️  {name}  |  {conf}  |  {assets}{ev_tag}"):
                    edited = st.text_area(
                        "Edit before sending:",
                        value=r.get("email_template", ""),
                        height=380,
                        key=f"email_{r['ein']}",
                    )
                    st.caption("💡 Find the correct contact name · confirm they accept unsolicited inquiries · verify eligibility requirements")

    # ── Tab 4: Downloads ─────────────────────────────────────────────────
    with tab4:
        st.markdown("### Download Files")
        st.markdown(
            "All three files are generated from this run. The **HTML report** is the "
            "easiest to share — Kirstie just opens it in any browser."
        )

        dl1, dl2, dl3 = st.columns(3)

        if os.path.exists(gf.OUTPUT_HTML):
            with open(gf.OUTPUT_HTML, "rb") as f:
                dl1.download_button(
                    "📄 report.html",
                    data=f.read(),
                    file_name="steamery_grant_report.html",
                    mime="text/html",
                    use_container_width=True,
                    help="Self-contained — share via email, opens offline in any browser",
                )
        else:
            dl1.info("Run the search first")

        if os.path.exists(gf.OUTPUT_CSV):
            with open(gf.OUTPUT_CSV, "rb") as f:
                dl2.download_button(
                    "📊 results.csv",
                    data=f.read(),
                    file_name="steamery_grants.csv",
                    mime="text/csv",
                    use_container_width=True,
                    help="17 columns — open in Google Sheets or Excel",
                )

        if os.path.exists(gf.OUTPUT_JSON):
            with open(gf.OUTPUT_JSON, "rb") as f:
                dl3.download_button(
                    "🗂️ results.json",
                    data=f.read(),
                    file_name="steamery_grants.json",
                    mime="application/json",
                    use_container_width=True,
                    help="Full auditable log with all rejection reasons",
                )

        st.divider()
        with st.expander("📬 How to share the HTML report with Kirstie"):
            st.markdown("""
1. Click **📄 report.html** above to download it
2. Email the file to Kirstie — or upload to Dropbox/Google Drive and send a link
3. She double-clicks the file → opens in Safari or Chrome
4. **No Python, no internet, no installation** — it works entirely offline
5. She sees the full interactive report: filter buttons, sortable table, draft emails, everything

**Alternative**: once you've run the full search and committed `report.html` to GitHub,
enable GitHub Pages (repo Settings → Pages → main branch → Save) and she can just
visit: `https://joshuajaimon7.github.io/grantdiscovery/`
            """)
