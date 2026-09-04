"""
Verifier agent — pure verification of cycle data and state (Rules 34, 35, 39).

Reads scores, extracted facts, gap snapshots, and timestamps from storage,
runs verification checks, and returns an AgentResult containing the Verdict.
Writes NO data other than returning its verdict result per Rule 34.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from edgedash import storage
from edgedash.agents.base import AgentResult
from edgedash.config import Config
from edgedash.verification import run_all_checks, Verdict


class Verifier:
    """
    Deterministic Verifier agent (Rules 34, 35).

    Reads scores, extractions, gap snapshots, and timestamps from storage,
    runs verification checks, and returns an AgentResult with the Verdict.
    Writes no data other than returning its verdict result.
    """

    name: str = "Verifier"

    def run(
        self,
        config: Config,
        db_path: str,
        stop_conditions: dict[str, Any] | None = None,
        goal: str | None = None,
    ) -> AgentResult:
        try:
            return self._run_internal(config, db_path, stop_conditions=stop_conditions)
        except Exception as exc:
            return AgentResult(
                agent=self.name,
                status="failed",
                records_touched=0,
                notes=f"Verifier error: {exc}",
            )

    def _run_internal(
        self,
        config: Config,
        db_path: str,
        stop_conditions: dict[str, Any] | None = None,
    ) -> AgentResult:
        storage.init_db(db_path)

        # 1. Fetch scores
        scored_records = storage.get_scored_listings_with_extractions(db_path)
        scores = [r["fit_score"] for r in scored_records if r.get("fit_score") is not None]

        # 2. Extracted facts
        facts_list = [
            {
                "required_skills": r.get("required_skills", []),
                "nice_to_have": r.get("nice_to_have", []),
            }
            for r in scored_records
        ]

        # 3. Gap snapshots
        gaps = storage.get_latest_skill_gaps(db_path)

        # 4. Latest fetch timestamp & now
        latest_fetch_at = storage.last_fetch_time(db_path)
        now = (stop_conditions or {}).get("now") or datetime.now(timezone.utc)

        # 5. Run pure verification
        verdict: Verdict = run_all_checks(
            scores=scores,
            facts_list=facts_list,
            gaps=gaps,
            latest_fetch_at=latest_fetch_at,
            config=config,
            now=now,
        )

        status = "ok" if verdict.passed else "failed"

        if verdict.passed:
            notes = f"VERDICT: pass — {verdict.summary}"
        else:
            # Build detailed failure notes e.g. "VERDICT: fail — check_score_spread observed {'spread': 6} (min 10)"
            fail_details = []
            for fc in verdict.failed_checks:
                fail_details.append(f"{fc.name} observed {fc.observed} (threshold {fc.threshold})")
            notes = f"VERDICT: fail — {'; '.join(fail_details)}"

        result = AgentResult(
            agent=self.name,
            status=status,
            records_touched=0,
            notes=notes,
        )
        # Attach verdict object for programmatic access
        setattr(result, "verdict", verdict)
        return result
