"""
CLI dashboard for displaying Skill Gaps snapshot and trend reports.

Usage:
    python -m edgedash.gaps          # latest snapshot
    python -m edgedash.gaps --trend  # trend over historical snapshots
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from typing import Any

from edgedash import storage
from edgedash.config import load_config

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def display_latest_gaps(db_path: str) -> None:
    """Format and print the most recent skill gaps snapshot to stdout."""
    storage.init_db(db_path)
    gaps = storage.get_latest_skill_gaps(db_path)
    if not gaps:
        _print_no_snapshots()
        return

    computed_at = gaps[0].get("computed_at", "unknown")
    print()
    print("=" * 88)
    print(f"  EDGEDASH — TOP SKILL GAPS REPORT  (Snapshot: {computed_at[:19]} UTC)")
    print("=" * 88)
    print(
        f"  {'#':<3} {'SKILL':<20} {'BLOCKED':<9} {'OPP. COST':<11} "
        f"{'MEAN FIT':<10} {'TOP FIT':<9} {'IMPACT BAR (by Opp Cost)'}"
    )
    print("  " + "-" * 84)

    max_cost = max((g["opportunity_cost"] for g in gaps), default=1.0) or 1.0
    bar_width = 18
    for rank, g in enumerate(gaps, 1):
        skill, blocked, cost = g["skill"], g["listings_blocked"], g["opportunity_cost"]
        mean_score, top_score = g["mean_score"], g.get("top_score", 0)
        fill_len = max(1, min(bar_width, int(round((cost / max_cost) * bar_width))))
        bar = "█" * fill_len + "░" * (bar_width - fill_len)
        conf = " [!] Low (N<3)" if g.get("low_confidence") else ""
        print(
            f"  {rank:<3} {skill:<20} {blocked:<9} {cost:>8.2f}    "
            f"{mean_score:>6.1f}%   {top_score:>5}%   {bar}{conf}"
        )

    print("  " + "-" * 84)
    print("  [!] Low (N<3): Computed from fewer than 3 listings (low confidence sample).")
    print("=" * 88)
    print()


def display_gap_trends(db_path: str) -> None:
    """Format and print historical trends across skill gaps snapshots."""
    storage.init_db(db_path)
    rows = storage.get_all_skill_gap_snapshots(db_path)
    if not rows:
        _print_no_snapshots()
        return

    # Group rows by run_id
    runs_order: list[dict[str, Any]] = []
    runs_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        rid = r["run_id"]
        if rid not in runs_map:
            runs_order.append({"run_id": rid, "computed_at": r["computed_at"]})
        runs_map[rid].append(r)

    if len(runs_order) < 2:
        single_date = runs_order[0]["computed_at"][:19]
        print()
        print("=" * 88)
        print("  EDGEDASH — SKILL GAP TREND REPORT")
        print("=" * 88)
        print(f"  Only 1 snapshot recorded so far ({single_date} UTC).")
        print("  Trend reporting requires at least 2 snapshots from separate runs.")
        print("  No trend can be extrapolated from a single data point.")
        print("  1 more day of scheduled runs needed to show a trend.")
        print("=" * 88)
        print()
        return

    earliest_meta, latest_meta = runs_order[0], runs_order[-1]
    e_date, l_date = earliest_meta["computed_at"][:19], latest_meta["computed_at"][:19]

    earliest_rows = sorted(runs_map[earliest_meta["run_id"]], key=lambda x: x["opportunity_cost"], reverse=True)
    latest_rows = sorted(runs_map[latest_meta["run_id"]], key=lambda x: x["opportunity_cost"], reverse=True)

    earliest_top10 = earliest_rows[:10]
    latest_top10 = latest_rows[:10]

    earliest_map = {g["skill"]: g["opportunity_cost"] for g in earliest_rows}
    earliest_top10_map = {g["skill"]: (idx, g["opportunity_cost"]) for idx, g in enumerate(earliest_top10, 1)}

    print()
    print("=" * 88)
    print("  EDGEDASH — SKILL GAP TREND REPORT")
    print(f"  Window: {e_date} UTC  →  {l_date} UTC  ({len(runs_order)} total snapshots)")
    print("=" * 88)
    print(
        f"  {'#':<3} {'SKILL':<20} {'EARLIEST':<10} {'LATEST':<10} "
        f"{'ABS CHG':<11} {'PCT CHG':<10} {'STATUS'}"
    )
    print("  " + "-" * 84)

    for rank, g in enumerate(latest_top10, 1):
        skill = g["skill"]
        l_cost = g["opportunity_cost"]
        if skill in earliest_map:
            e_cost = earliest_map[skill]
            abs_chg = l_cost - e_cost
            pct_str = f"{((abs_chg / e_cost) * 100):+6.1f}%" if e_cost > 0 else "  0.0%"
            status = "▲ Rising" if abs_chg > 0.05 else ("▼ Falling" if abs_chg < -0.05 else "• Steady")
            print(f"  {rank:<3} {skill:<20} {e_cost:>8.2f}   {l_cost:>8.2f}   {abs_chg:>+8.2f}    {pct_str:>8}   {status}")
        else:
            print(f"  {rank:<3} {skill:<20} {'-':>8}   {l_cost:>8.2f}   {'+' + f'{l_cost:.2f}':>8}    {'NEW':>8}   ★ NEW IN TOP 10")

    # Check for dropped skills
    latest_top10_skills = {g["skill"] for g in latest_top10}
    dropped = [
        (skill, rank, cost)
        for skill, (rank, cost) in earliest_top10_map.items()
        if skill not in latest_top10_skills
    ]

    if dropped:
        print("  " + "-" * 84)
        print("  DROPPED OUT OF TOP 10 (since earliest snapshot):")
        for skill, old_rank, old_cost in dropped:
            cur_cost = next((g["opportunity_cost"] for g in latest_rows if g["skill"] == skill), None)
            cur_str = f"now {cur_cost:.2f}" if cur_cost is not None else "no longer in gaps"
            print(f"  • {skill:<18} (was #{old_rank}, cost {old_cost:.2f} → {cur_str})")

    print("=" * 88)
    print()


def _print_no_snapshots() -> None:
    print()
    print("=" * 80)
    print("  EDGEDASH — SKILL GAP ANALYSIS")
    print("=" * 80)
    print("  No skill gap snapshots found in the database.")
    print("  Run a cycle (python run_cycle.py) with scored listings to generate gaps.")
    print("=" * 80)
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="View EdgeDash skill gaps snapshot or trends.")
    parser.add_argument(
        "--trend",
        action="store_true",
        help="Display historical trends across all recorded snapshots.",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config.yaml (defaults to repo root config.yaml).",
    )
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.trend:
        display_gap_trends(cfg.db_abs_path)
    else:
        display_latest_gaps(cfg.db_abs_path)


if __name__ == "__main__":
    main()
