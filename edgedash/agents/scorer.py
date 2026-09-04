"""
Scorer agent — extract facts via LLM, score deterministically (rules 16–21).

Each cycle scores up to config.llm_score_batch_size unscored listings.
Extraction failures skip that listing without aborting the batch.
"""

from __future__ import annotations

import time

from edgedash import storage
from edgedash.agents.base import AgentResult
from edgedash.agents.extractor import extract
from edgedash.config import Config
from edgedash.llm import LLMError
from edgedash.scoring import compute_fit_score


class Scorer:
    """Extract job requirements and assign deterministic fit scores."""

    name: str = "Scorer"

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
                notes=f"Scorer error: {exc}",
            )

    def _run_internal(
        self,
        config: Config,
        db_path: str,
        stop_conditions: dict | None = None,
    ) -> AgentResult:
        storage.init_db(db_path)
        batch_size = (
            stop_conditions.get("max_items") if stop_conditions else None
        ) or config.llm_score_batch_size
        max_seconds = stop_conditions.get("max_seconds") if stop_conditions else None
        widen = bool(stop_conditions.get("widen_distribution", False)) if stop_conditions else False
        rescore_recent = bool(stop_conditions.get("rescore_recent", False)) if stop_conditions else False

        pending = storage.get_unscored_listings(db_path, limit=batch_size)
        rescoring = False

        if not pending and rescore_recent:
            # Re-score already scored listings to widen distribution during retry
            scored_recs = storage.get_scored_listings_with_extractions(db_path)
            pending = [
                {
                    "id": r["id"],
                    "title": r["title"],
                    "description": "",
                    "_preloaded_extraction": {
                        "required_skills": r.get("required_skills", []),
                        "nice_to_have": r.get("nice_to_have", []),
                    },
                }
                for r in scored_recs[:batch_size]
            ]
            rescoring = True

        if not pending:
            return AgentResult(
                agent=self.name,
                status="ok",
                records_touched=0,
                notes="0 listings scored · nothing unscored",
            )

        scores: list[int] = []
        failures = 0
        start_time = time.monotonic()

        for listing in pending:
            if max_seconds and (time.monotonic() - start_time) >= max_seconds:
                break

            try:
                if "_preloaded_extraction" in listing:
                    extraction = listing["_preloaded_extraction"]
                else:
                    extraction = extract(listing, db_path)
            except (LLMError, ValueError) as exc:
                failures += 1
                print(f"  [!] Scorer: extraction failed for {listing.get('title', '?')}: {exc}")
                continue

            score, reason = compute_fit_score(
                title=listing.get("title") or "",
                extraction=extraction,
                my_skills=config.my_skills,
                experience_years=config.experience_years,
                target_role=config.target_role,
                keywords=config.keywords,
                skill_aliases=config.skill_aliases,
                widen_distribution=widen,
            )
            storage.save_listing_score(db_path, listing["id"], score, reason, overwrite=rescoring)
            scores.append(score)

        if not scores:
            return AgentResult(
                agent=self.name,
                status="ok" if failures == 0 else "failed",
                records_touched=0,
                notes=f"0 scored · {failures} extraction failure(s) · batch={len(pending)}",
            )

        notes = _distribution_notes(scores, failures, len(pending))
        if widen:
            notes = f"[widen_distribution=True] {notes}"
        return AgentResult(
            agent=self.name,
            status="ok",
            records_touched=len(scores),
            notes=notes,
        )


def _distribution_notes(scores: list[int], failures: int, batch_size: int) -> str:
    """Build cycle_log notes with score distribution (rule 20)."""
    count = len(scores)
    lo, hi = min(scores), max(scores)
    mean = sum(scores) / count
    spread = hi - lo
    parts = [
        f"{count} scored",
        f"min={lo}",
        f"max={hi}",
        f"mean={mean:.1f}",
        f"spread={spread}",
    ]
    if failures:
        parts.append(f"{failures} extraction failure(s)")
    if count >= 2 and spread <= 10:
        parts.append("SUSPECT: all scores within 10-point range")
    parts.append(f"batch cap={batch_size}")
    return " · ".join(parts)
