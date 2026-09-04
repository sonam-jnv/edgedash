"""
Deterministic Verification checks for EdgeDash outputs (Rules 34, 35, 39).

Each check is a pure function taking the data it needs and returning
a CheckResult(name, passed, observed, threshold, message).
Pure functions only: no LLM, no database access, no network, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import statistics
from typing import Any


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    observed: Any
    threshold: Any
    message: str


@dataclass(frozen=True)
class Verdict:
    passed: bool
    failed_checks: list[CheckResult]
    summary: str


def _cfg(config: Any, key: str, default: Any) -> Any:
    """Read a threshold from a Config object, dict, or fallback to default."""
    if config is None:
        return default
    if isinstance(config, dict):
        return config.get(key, default)
    return getattr(config, key, default)


def check_score_spread(scores: list[int | float], config: Any = None) -> CheckResult:
    """
    Check that the fit score distribution has sufficient variance.
    FAILS if max - min < min_score_spread (default 10) or stdev < min_score_stdev (default 5.0).
    Catches score inflation and compression failure modes.
    Passes trivially if fewer than 5 scores.
    """
    min_spread = _cfg(config, "min_score_spread", 10)
    min_stdev = _cfg(config, "min_score_stdev", 5.0)
    thresholds = {"min_score_spread": min_spread, "min_score_stdev": min_stdev}

    count = len(scores)
    if count < 5:
        return CheckResult(
            name="check_score_spread",
            passed=True,
            observed={"count": count},
            threshold=thresholds,
            message=f"Trivially passed: only {count} score(s) provided (fewer than 5 required for distribution check).",
        )

    spread = max(scores) - min(scores)
    stdev = statistics.stdev(scores)
    observed = {"spread": spread, "stdev": round(stdev, 2), "count": count}

    if spread < min_spread or stdev < min_stdev:
        return CheckResult(
            name="check_score_spread",
            passed=False,
            observed=observed,
            threshold=thresholds,
            message=(
                f"Failed: score distribution lacks variance (spread={spread} < {min_spread} "
                f"or stdev={stdev:.2f} < {min_stdev})."
            ),
        )

    return CheckResult(
        name="check_score_spread",
        passed=True,
        observed=observed,
        threshold=thresholds,
        message=f"Passed: score spread={spread} (>= {min_spread}) and stdev={stdev:.2f} (>= {min_stdev}).",
    )


def check_extraction_sanity(
    facts_list: list[dict[str, Any]], config: Any = None
) -> CheckResult:
    """
    Check that extracted job facts are well-formed.
    FAILS if > max_empty_extraction_pct (default 20%) listings have empty required_skills,
    or if any listing has > max_skills_per_listing (default 20).
    Catches broken extractors and sentence-dump hallucinations.
    """
    max_empty_pct = float(_cfg(config, "max_empty_extraction_pct", 20.0))
    max_skills = int(_cfg(config, "max_skills_per_listing", 20))
    thresholds = {
        "max_empty_extraction_pct": max_empty_pct,
        "max_skills_per_listing": max_skills,
    }

    if not facts_list:
        return CheckResult(
            name="check_extraction_sanity",
            passed=True,
            observed={"total": 0, "empty_pct": 0.0, "max_skills_found": 0},
            threshold=thresholds,
            message="Trivially passed: 0 extractions to validate.",
        )

    total = len(facts_list)
    empty_count = sum(1 for f in facts_list if not f.get("required_skills"))
    empty_pct = (empty_count / total) * 100.0
    max_skills_found = max(
        (len(f.get("required_skills") or []) for f in facts_list), default=0
    )

    observed = {
        "total": total,
        "empty_pct": round(empty_pct, 1),
        "max_skills_found": max_skills_found,
    }

    if empty_pct > max_empty_pct or max_skills_found > max_skills:
        return CheckResult(
            name="check_extraction_sanity",
            passed=False,
            observed=observed,
            threshold=thresholds,
            message=(
                f"Failed: {empty_pct:.1f}% empty extractions (> {max_empty_pct}%) "
                f"or max skills {max_skills_found} (> {max_skills})."
            ),
        )

    return CheckResult(
        name="check_extraction_sanity",
        passed=True,
        observed=observed,
        threshold=thresholds,
        message=(
            f"Passed: {empty_pct:.1f}% empty extractions (<= {max_empty_pct}%) "
            f"and max skills {max_skills_found} (<= {max_skills})."
        ),
    )


def check_gap_sample_size(
    gaps: list[dict[str, Any]], config: Any = None
) -> CheckResult:
    """
    Check that skill gaps are grounded in sufficient sample evidence.
    FAILS if the top-ranked gap was computed from < min_gap_sample (default 3) listings.
    Catches ranking a rumour.
    """
    min_sample = int(_cfg(config, "min_gap_sample", 3))
    thresholds = {"min_gap_sample": min_sample}

    if not gaps:
        return CheckResult(
            name="check_gap_sample_size",
            passed=True,
            observed={"gap_count": 0, "top_sample": 0},
            threshold=thresholds,
            message="Trivially passed: no skill gaps to check.",
        )

    top_gap = gaps[0]
    sample_size = top_gap.get("listings_blocked", top_gap.get("sample_size", 0))
    skill = top_gap.get("skill", "unknown")
    observed = {"top_skill": skill, "sample_size": sample_size}

    if sample_size < min_sample:
        return CheckResult(
            name="check_gap_sample_size",
            passed=False,
            observed=observed,
            threshold=thresholds,
            message=f"Failed: top gap '{skill}' backed by only {sample_size} listing(s) (< {min_sample}).",
        )

    return CheckResult(
        name="check_gap_sample_size",
        passed=True,
        observed=observed,
        threshold=thresholds,
        message=f"Passed: top gap '{skill}' backed by {sample_size} listing(s) (>= {min_sample}).",
    )


def check_freshness(
    latest_fetch_at: datetime | str | None,
    config: Any = None,
    now: datetime | None = None,
) -> CheckResult:
    """
    Check that the newest listing data is fresh.
    FAILS if newest listing is older than max_data_age_days (default 3).
    `now` is a parameter, never internal datetime.now().
    """
    max_days = int(_cfg(config, "max_data_age_days", 3))
    thresholds = {"max_data_age_days": max_days}

    if latest_fetch_at is None:
        return CheckResult(
            name="check_freshness",
            passed=False,
            observed={"latest_fetch_at": None},
            threshold=thresholds,
            message="Failed: latest_fetch_at timestamp is missing/None.",
        )

    if now is None:
        raise ValueError("Parameter 'now' is required for check_freshness to ensure testability.")

    if isinstance(latest_fetch_at, str):
        parsed_dt = datetime.fromisoformat(latest_fetch_at.replace("Z", "+00:00"))
    else:
        parsed_dt = latest_fetch_at

    # Normalize timezone awareness between now and parsed_dt
    if now.tzinfo is not None and parsed_dt.tzinfo is None:
        parsed_dt = parsed_dt.replace(tzinfo=timezone.utc)
    elif now.tzinfo is None and parsed_dt.tzinfo is not None:
        now = now.replace(tzinfo=timezone.utc)

    age_days = (now - parsed_dt).total_seconds() / 86400.0
    observed = {"age_days": round(age_days, 2), "latest_fetch_at": str(latest_fetch_at)}

    if age_days > max_days:
        return CheckResult(
            name="check_freshness",
            passed=False,
            observed=observed,
            threshold=thresholds,
            message=f"Failed: newest listing data is {age_days:.1f} days old (> {max_days} days).",
        )

    return CheckResult(
        name="check_freshness",
        passed=True,
        observed=observed,
        threshold=thresholds,
        message=f"Passed: newest listing data is {age_days:.1f} days old (<= {max_days} days).",
    )


def run_all_checks(
    *,
    scores: list[int | float] | None = None,
    facts_list: list[dict[str, Any]] | None = None,
    gaps: list[dict[str, Any]] | None = None,
    latest_fetch_at: datetime | str | None = None,
    config: Any = None,
    now: datetime | None = None,
) -> Verdict:
    """
    Run every verification check, collecting results and returning a Verdict.
    Passes only if ALL individual checks pass.
    """
    results: list[CheckResult] = []

    if scores is not None:
        results.append(check_score_spread(scores, config))

    if facts_list is not None:
        results.append(check_extraction_sanity(facts_list, config))

    if gaps is not None:
        results.append(check_gap_sample_size(gaps, config))

    if latest_fetch_at is not None or now is not None:
        results.append(check_freshness(latest_fetch_at, config, now))

    failed = [r for r in results if not r.passed]
    passed = len(failed) == 0

    if passed:
        summary = f"All {len(results)} verification check(s) passed."
    else:
        summary = f"{len(failed)} of {len(results)} check(s) failed: {', '.join(f.name for f in failed)}."

    return Verdict(passed=passed, failed_checks=failed, summary=summary)
