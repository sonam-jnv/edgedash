"""
EdgeDash Agent Activity Dashboard (Streamlit).

Read-only interface that inspects storage backend (SQLite / PostgreSQL).
- Never writes to the database (Rule 49).
- Never invokes agent cycles (Rule 49).
- Never leaks connection strings, secrets, or raw tracebacks to users (Rule 48 & Rule 50).
- Robust to hostile startup, unconfigured DB, or failing individual panels (Rule 50).
- Reads data panels from the last PASSING cycle per Rule 38.
- Displays all cycles (including failed/degraded) in the Agent Activity Log.
"""

from __future__ import annotations

from datetime import datetime, timezone
import logging
import os
import re
from typing import Any
import streamlit as st

from edgedash.config import load_config, Config
from edgedash import storage

# Configure server-side logger
logger = logging.getLogger("edgedash.app")

# ---------------------------------------------------------------------------
# Streamlit Page Setup
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="EdgeDash · Agent Activity Dashboard",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# Custom Styling for high-contrast, premium dark aesthetic
st.markdown(
    """
    <style>
    /* Metric card styles */
    .metric-container {
        background-color: #161b22;
        border: 1px solid #30363d;
        border-radius: 8px;
        padding: 16px 20px;
        margin-bottom: 12px;
    }
    .metric-label {
        font-size: 0.82rem;
        color: #8b949e;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-bottom: 4px;
    }
    .metric-value {
        font-size: 1.6rem;
        font-weight: 700;
        color: #f0f6fc;
    }
    /* Status Badges */
    .badge {
        display: inline-block;
        padding: 3px 10px;
        border-radius: 12px;
        font-size: 0.78rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.04em;
    }
    .badge-pass { background-color: rgba(46, 160, 67, 0.2); color: #3fb950; border: 1px solid rgba(46, 160, 67, 0.4); }
    .badge-fail { background-color: rgba(248, 81, 73, 0.2); color: #f85149; border: 1px solid rgba(248, 81, 73, 0.4); }
    .badge-degraded { background-color: rgba(248, 81, 73, 0.25); color: #ff7b72; border: 1px solid rgba(248, 81, 73, 0.6); }
    .badge-partial { background-color: rgba(210, 153, 34, 0.2); color: #d29922; border: 1px solid rgba(210, 153, 34, 0.4); }
    .badge-idle { background-color: rgba(88, 166, 255, 0.15); color: #58a6ff; border: 1px solid rgba(88, 166, 255, 0.3); }
    /* Table cards */
    .data-card {
        background-color: #0d1117;
        border: 1px solid #21262d;
        border-radius: 6px;
        padding: 12px 14px;
        margin-bottom: 8px;
    }
    .footer-text {
        font-size: 0.85rem;
        color: #8b949e;
        text-align: center;
        padding-top: 24px;
        padding-bottom: 12px;
    }
    .footer-text a {
        color: #58a6ff;
        text-decoration: none;
    }
    .footer-text a:hover {
        text-decoration: underline;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Cached Read Functions (TTL=5s to prevent database contention)
# ---------------------------------------------------------------------------
@st.cache_data(ttl=5)
def get_dashboard_data(db_path: str) -> dict[str, Any]:
    """Fetch all necessary read-only state from storage."""
    storage.init_db(db_path)
    recent_cycles = storage.get_recent_cycles(db_path, limit=30)
    last_passing = storage.get_latest_passing_cycle(db_path)
    total_listings = storage.count_total_listings(db_path)
    total_scored = storage.count_scored_listings(db_path)
    top_listings = storage.get_listings(db_path, limit=10, min_score=1)
    skill_gaps = storage.get_latest_skill_gaps(db_path)

    return {
        "recent_cycles": recent_cycles,
        "last_passing": last_passing,
        "total_listings": total_listings,
        "total_scored": total_scored,
        "top_listings": top_listings,
        "skill_gaps": skill_gaps,
    }


def parse_cycle_notes(notes: str | None) -> dict[str, str]:
    """Extract structured components from Orchestrator summary notes."""
    if not notes:
        return {}
    res: dict[str, str] = {}
    parts = [p.strip() for p in notes.split("|")]
    for p in parts:
        if ":" in p:
            k, v = p.split(":", 1)
            res[k.strip().lower()] = v.strip()
    return res


def format_duration(start_str: str, finish_str: str | None) -> str:
    """Calculate and format duration between ISO timestamps."""
    if not finish_str:
        return "in progress"
    try:
        t0 = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
        t1 = datetime.fromisoformat(finish_str.replace("Z", "+00:00"))
        secs = (t1 - t0).total_seconds()
        return f"{secs:.2f}s"
    except Exception:
        return "-"


def format_ts(ts_str: str | None) -> str:
    """Format ISO timestamp to readable UTC string."""
    if not ts_str:
        return "Never"
    try:
        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
    except Exception:
        return str(ts_str)


# ---------------------------------------------------------------------------
# Main App Layout
# ---------------------------------------------------------------------------
def main() -> None:
    # 1. Config loading with graceful fallback (Rule 50)
    try:
        config: Config = load_config()
    except Exception as exc:
        logger.error("Failed to load config: %s", exc, exc_info=True)
        st.error("⚠️ **Configuration Not Found / Invalid**: Please ensure `config.yaml` is present at repository root.")
        return

    # 2. Database loading with hostile startup resilience (Rule 50 & 48)
    db_path = config.db_abs_path
    try:
        data = get_dashboard_data(db_path)
    except Exception as exc:
        logger.error("Database connection / query failed: %s", exc, exc_info=True)
        is_pg = storage._is_postgres(db_path)
        if is_pg:
            st.warning(
                "🔌 **Database Not Configured / Unreachable**\n\n"
                "The dashboard is unable to reach the PostgreSQL database. "
                "Please verify your `DATABASE_URL` secret configuration and database network status."
            )
        else:
            st.warning(
                "🔌 **Database Not Configured**\n\n"
                "The database backend is currently unavailable or uninitialized."
            )
        return

    recent_cycles: list[dict[str, Any]] = data.get("recent_cycles", [])
    last_passing: dict[str, Any] | None = data.get("last_passing")
    total_listings: int = data.get("total_listings", 0)
    total_scored: int = data.get("total_scored", 0)
    top_listings: list[dict[str, Any]] = data.get("top_listings", [])
    skill_gaps: list[dict[str, Any]] = data.get("skill_gaps", [])

    st.title("⚡ EdgeDash · Agent Activity & State")

    # Empty tables / unrun state check (Rule 50)
    if not recent_cycles and total_listings == 0:
        st.info(
            "🕒 **No cycles yet — first run is scheduled for the next automated cycle interval.**\n\n"
            "Cycle history, scored job matches, and skill gap insights will automatically appear here "
            "as soon as the backend orchestrator completes its initial verified run."
        )

    # -----------------------------------------------------------------------
    # PANEL 1: Header Strip, Staleness Warning & Metrics (Wrapped)
    # -----------------------------------------------------------------------
    try:
        newest_cycle = recent_cycles[0] if recent_cycles else None
        newest_status = (newest_cycle["status"] if newest_cycle else "unknown").lower()
        last_passing_ts = last_passing["started_at"] if last_passing else None

        # Warning banner if newest cycle is not complete/verified
        if newest_cycle and newest_status in ("degraded", "partial", "fail"):
            st.warning(
                f"⚠️ **Attention**: The most recent cycle at `{format_ts(newest_cycle['started_at'])}` "
                f"ended with status **`{newest_status.upper()}`**. "
                f"Panels below are grounded in the last verified passing cycle from `{format_ts(last_passing_ts)}`."
            )
        elif not last_passing and recent_cycles:
            st.error(
                "⚠️ **Warning**: No passing cycle has been recorded yet. Data panels below may be incomplete."
            )

        # 4-Column Metric Strip
        m1, m2, m3, m4 = st.columns(4)
        with m1:
            st.markdown(
                f"""<div class="metric-container">
                    <div class="metric-label">Last Verified Cycle</div>
                    <div class="metric-value">{format_ts(last_passing_ts)}</div>
                </div>""",
                unsafe_allow_html=True,
            )
        with m2:
            st.markdown(
                f"""<div class="metric-container">
                    <div class="metric-label">Total Listings Ingested</div>
                    <div class="metric-value">{total_listings}</div>
                </div>""",
                unsafe_allow_html=True,
            )
        with m3:
            st.markdown(
                f"""<div class="metric-container">
                    <div class="metric-label">Total Listings Scored</div>
                    <div class="metric-value">{total_scored}</div>
                </div>""",
                unsafe_allow_html=True,
            )
        with m4:
            status_badge_class = "badge-pass" if newest_status in ("complete", "verified", "pass") else (
                "badge-idle" if newest_status == "nothing_to_do" else (
                    "badge-partial" if newest_status == "partial" else "badge-degraded"
                )
            )
            status_label = newest_status.upper() if newest_status and newest_status != "unknown" else "NO CYCLES"
            st.markdown(
                f"""<div class="metric-container">
                    <div class="metric-label">Latest Cycle Status</div>
                    <div class="metric-value"><span class="badge {status_badge_class}">{status_label}</span></div>
                </div>""",
                unsafe_allow_html=True,
            )
    except Exception as panel_err:
        logger.error("Header / metrics panel rendering error: %s", panel_err, exc_info=True)
        st.info("Header metrics are temporarily unavailable.")

    # -----------------------------------------------------------------------
    # PANEL 2: Natural Language Career Intelligence (Rules 40–46) (Wrapped)
    # -----------------------------------------------------------------------
    try:
        st.subheader("💬 Ask EdgeDash")
        st.caption("Ask natural language questions grounded in verified cycle data. Never generates raw SQL.")

        query_input = st.text_input(
            "Enter question:",
            placeholder="e.g. What are my best job matches? / What skills should I learn? / Which companies are hiring?",
            key="nl_query_input",
        )
        if query_input:
            with st.spinner("Routing query and analyzing verified data..."):
                try:
                    from edgedash.query.ask import ask as ask_query, Answer
                    answer_result: Answer = ask_query(query_input, db_path=db_path)
                    st.markdown(f"**Answer:** {answer_result.text}")
                    if answer_result.tool_used:
                        st.caption(f"🔧 Tool Selected: `{answer_result.tool_used}` | Parameters: `{answer_result.params}`")
                    if answer_result.rows:
                        with st.expander("📊 Underlying Data Rows (Rule 44)", expanded=True):
                            st.dataframe(answer_result.rows, use_container_width=True, hide_index=True)
                except Exception as query_err:
                    logger.error("Query processing failed for '%s': %s", query_input, query_err, exc_info=True)
                    st.error("Unable to process query at this time. Please try a different question.")
    except Exception as panel_err:
        logger.error("Ask panel error: %s", panel_err, exc_info=True)
        st.info("Career Intelligence query panel is temporarily unavailable.")

    st.markdown("---")

    # -----------------------------------------------------------------------
    # PANEL 3: AGENT ACTIVITY LOG (Most Recent 30 Cycles) (Wrapped)
    # -----------------------------------------------------------------------
    try:
        st.subheader("📋 Agent Activity Log (Recent 30 Cycles)")
        st.caption("Inspectable chronological cycle logs across all agents, retries, and verification outcomes.")

        if not recent_cycles:
            st.info("No cycles recorded yet in `cycle_log`. The scheduler will record cycles upon execution.")
        else:
            log_rows = []
            for c in recent_cycles:
                parsed = parse_cycle_notes(c.get("notes"))
                verdict = parsed.get("verdict", c["status"])
                failed_checks = parsed.get("failed checks", "-")
                retries = parsed.get("retries", "0")
                ran = parsed.get("ran", "-")
                skipped = parsed.get("skipped", "-")
                duration = format_duration(c["started_at"], c.get("finished_at"))
                records = c.get("records_touched", 0)

                log_rows.append({
                    "Timestamp (UTC)": format_ts(c["started_at"]),
                    "Status": c["status"].upper(),
                    "Verdict": verdict.upper(),
                    "Duration": duration,
                    "Records": records,
                    "Failed Checks": failed_checks,
                    "Retries": retries,
                    "Agents Ran": ran,
                    "Agents Skipped": skipped,
                })

            st.dataframe(
                log_rows,
                use_container_width=True,
                column_config={
                    "Status": st.column_config.TextColumn("Cycle Status", width="small"),
                    "Verdict": st.column_config.TextColumn("Verdict", width="small"),
                    "Failed Checks": st.column_config.TextColumn("Failed Checks", width="medium"),
                    "Agents Ran": st.column_config.TextColumn("Agents Ran", width="large"),
                    "Agents Skipped": st.column_config.TextColumn("Skipped", width="medium"),
                },
                hide_index=True,
            )

            with st.expander("🔍 View Raw Notes for Recent Cycles"):
                for c in recent_cycles[:10]:
                    st.markdown(f"**Cycle `{c['id']}`** · `{format_ts(c['started_at'])}` · Status: `{c['status']}`")
                    st.code(c.get("notes") or "No notes recorded.")
    except Exception as panel_err:
        logger.error("Activity log panel error: %s", panel_err, exc_info=True)
        st.info("Agent activity log is temporarily unavailable.")

    st.markdown("---")

    # -----------------------------------------------------------------------
    # PANEL 4 & 5: Two Compact Panels (Top 10 Listings & Top 10 Skill Gaps)
    # -----------------------------------------------------------------------
    col1, col2 = st.columns(2)

    # Panel 4: Top 10 Scored Listings (Rule 38) (Wrapped)
    with col1:
        try:
            st.subheader("🎯 Top 10 Scored Listings")
            st.caption("Highest fit listings from verified cycle data.")

            if not top_listings:
                st.info("No scored listings available above min fit score.")
            else:
                for item in top_listings[:10]:
                    score = item.get("fit_score", 0)
                    title = item.get("title", "Untitled")
                    company = item.get("company", "Unknown")
                    reason = item.get("fit_reason", "No reason provided")

                    score_color = "#3fb950" if score >= 75 else ("#d29922" if score >= 50 else "#f85149")
                    st.markdown(
                        f"""<div class="data-card">
                            <div style="display: flex; justify-content: space-between; align-items: center;">
                                <span style="font-weight: 600; font-size: 1.05rem; color: #f0f6fc;">{title}</span>
                                <span style="font-size: 1.15rem; font-weight: 700; color: {score_color};">{score}/100</span>
                            </div>
                            <div style="color: #8b949e; font-size: 0.85rem; margin-bottom: 6px;">🏢 {company}</div>
                            <div style="color: #c9d1d9; font-size: 0.82rem; font-family: monospace;">{reason}</div>
                        </div>""",
                        unsafe_allow_html=True,
                    )
        except Exception as panel_err:
            logger.error("Top listings panel error: %s", panel_err, exc_info=True)
            st.info("Top scored listings are temporarily unavailable.")

    # Panel 5: Current Top 10 Skill Gaps (Rule 38) (Wrapped)
    with col2:
        try:
            st.subheader("📊 Top 10 Skill Gaps")
            st.caption("Highest opportunity-cost skill gaps from verified cycle snapshot.")

            if not skill_gaps:
                st.info("No skill gaps computed yet.")
            else:
                gap_data = []
                for g in skill_gaps[:10]:
                    gap_data.append({
                        "Skill": g.get("skill", "-"),
                        "Listings Blocked": g.get("listings_blocked", 0),
                        "Opportunity Cost": f"{float(g.get('opportunity_cost', 0.0)):.1f}",
                        "Top Score": g.get("top_score", 0),
                        "Mean Score": f"{float(g.get('mean_score', 0.0)):.1f}",
                    })

                st.dataframe(
                    gap_data,
                    use_container_width=True,
                    hide_index=True,
                )
        except Exception as panel_err:
            logger.error("Skill gaps panel error: %s", panel_err, exc_info=True)
            st.info("Skill gaps panel is temporarily unavailable.")

    # -----------------------------------------------------------------------
    # PANEL 6: Footer (Rule 50 & Requirement 5) (Wrapped)
    # -----------------------------------------------------------------------
    try:
        last_success_ts = format_ts(last_passing["started_at"]) if last_passing else "None recorded"
        repo_url = os.environ.get("GITHUB_REPO_URL", "https://github.com/sonam-jnv/edgedash")
        st.markdown("---")
        st.markdown(
            f"""<div class="footer-text">
                ⚡ <strong>EdgeDash</strong> · Last successful cycle: <code>{last_success_ts}</code> · 
                <a href="{repo_url}" target="_blank" rel="noopener noreferrer">GitHub Repository</a>
            </div>""",
            unsafe_allow_html=True,
        )
    except Exception as footer_err:
        logger.error("Footer rendering error: %s", footer_err, exc_info=True)



if __name__ == "__main__":
    main()
