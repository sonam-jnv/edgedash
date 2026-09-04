"""
Deterministic Gap Analyzer agent (Rules 22, 24, 25, 26, 27).

Calculates missing skill gaps across scored job listings and ranks them
by opportunity cost (sum of listing.score / 100). No LLM calls.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import uuid
from typing import Any

from edgedash import storage
from edgedash.agents.base import AgentResult
from edgedash.config import Config
from edgedash.skills import canonical


class GapAnalyzer:
    """
    Agent that analyzes skill gaps across scored listings and persists
    timestamped snapshots to storage.
    """

    name: str = "GapAnalyzer"

    def run(
        self,
        config: Config,
        db_path: str,
        stop_conditions: dict | None = None,
        goal: str | None = None,
    ) -> AgentResult:
        try:
            return self._run_internal(config, db_path, stop_conditions=stop_conditions)
        except Exception as exc:
            return AgentResult(
                agent=self.name,
                status="failed",
                records_touched=0,
                notes=f"Error analyzing skill gaps: {exc}",
            )

    def _run_internal(
        self,
        config: Config,
        db_path: str,
        stop_conditions: dict | None = None,
    ) -> AgentResult:
        storage.init_db(db_path)


        # 1. Read candidate's canonical skills from config
        user_skills_raw = getattr(config, "my_skills", []) or getattr(config, "skills", [])
        aliases = getattr(config, "skill_aliases", {}) or {}
        my_canonical_skills = {
            canonical(s, aliases) for s in user_skills_raw if canonical(s, aliases)
        }

        # 2. Read scored listings with extracted facts
        listings = storage.get_scored_listings_with_extractions(db_path)
        if not listings:
            return AgentResult(
                agent=self.name,
                status="ok",
                records_touched=0,
                notes="0 gaps found · 0 scored listings available",
            )

        # 3. Aggregate gaps per missing canonical skill
        # gap_listings: skill -> list of (listing_id, score)
        gap_listings: dict[str, list[tuple[str, int]]] = defaultdict(list)
        nice_counts: dict[str, int] = defaultdict(int)

        for item in listings:
            score = int(item.get("fit_score") or 0)
            lid = item["id"]

            # Required skills
            req_raw = item.get("required_skills") or []
            req_canonical = {canonical(s, aliases) for s in req_raw if canonical(s, aliases)}
            for sk in req_canonical:
                if sk not in my_canonical_skills:
                    gap_listings[sk].append((lid, score))

            # Nice to have skills (tracked separately, never mixed into required)
            nice_raw = item.get("nice_to_have") or []
            nice_canonical = {canonical(s, aliases) for s in nice_raw if canonical(s, aliases)}
            for sk in nice_canonical:
                if sk not in my_canonical_skills and sk not in req_canonical:
                    nice_counts[sk] += 1

        if not gap_listings:
            return AgentResult(
                agent=self.name,
                status="ok",
                records_touched=0,
                notes=f"0 gaps found · {len(listings)} listings analysed",
            )

        # 4. Compute metrics for each gap
        computed_gaps: list[dict[str, Any]] = []
        for skill, blocked in gap_listings.items():
            listings_blocked = len(blocked)
            # Opportunity cost: sum of (score / 100) per Rule 24
            opportunity_cost = sum(s / 100.0 for _, s in blocked)
            mean_score = sum(s for _, s in blocked) / listings_blocked
            top_score = max(s for _, s in blocked)

            # Example IDs: up to 5 highest scoring listings (Rule 26)
            sorted_blocked = sorted(blocked, key=lambda x: (x[1], x[0]), reverse=True)
            example_ids = [bid for bid, _ in sorted_blocked[:5]]

            # Sample size & low confidence flag (Rule 27)
            low_confidence = listings_blocked < 3

            computed_gaps.append({
                "skill": skill,
                "listings_blocked": listings_blocked,
                "opportunity_cost": opportunity_cost,
                "mean_score": mean_score,
                "top_score": top_score,
                "example_ids": example_ids,
                "also_nice_to_have": nice_counts.get(skill, 0),
                "low_confidence": low_confidence,
            })

        # 5. Rank by opportunity_cost descending, report top 10
        computed_gaps.sort(
            key=lambda g: (g["opportunity_cost"], g["listings_blocked"], g["mean_score"]),
            reverse=True,
        )
        top_10 = computed_gaps[:10]

        # 6. Save timestamped snapshot (Rule 25)
        run_id = f"run_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        computed_at = datetime.now(timezone.utc).isoformat()
        storage.save_skill_gap_snapshot(
            db_path,
            run_id=run_id,
            computed_at=computed_at,
            gaps=top_10,
        )

        top_gap = top_10[0]
        notes = (
            f"{len(top_10)} gaps · top: {top_gap['skill']} "
            f"({top_gap['listings_blocked']} listings, cost {top_gap['opportunity_cost']:.1f}) · "
            f"{len(listings)} listings analysed"
        )

        return AgentResult(
            agent=self.name,
            status="ok",
            records_touched=len(top_10),
            notes=notes,
        )
