"""
Tests for deterministic verification checks in edgedash/verification.py.
"""

from datetime import datetime, timezone, timedelta
import pytest

from edgedash.config import Config
from edgedash.verification import (
    check_score_spread,
    check_extraction_sanity,
    check_gap_sample_size,
    check_freshness,
    run_all_checks,
    CheckResult,
    Verdict,
)


@pytest.fixture
def sample_config() -> Config:
    return Config(
        target_role="Data Analyst",
        target_city="Bengaluru",
        keywords=["SQL", "Python"],
        my_skills=["Python", "SQL"],
        experience_years=3,
        db_path=":memory:",
        min_score_spread=10,
        min_score_stdev=5.0,
        max_empty_extraction_pct=20.0,
        max_skills_per_listing=20,
        min_gap_sample=3,
        max_data_age_days=3,
    )


# ---------------------------------------------------------------------------
# 1. check_score_spread
# ---------------------------------------------------------------------------

def test_score_spread_passing(sample_config: Config) -> None:
    scores = [25, 45, 60, 75, 90]  # spread: 65 (>=10), stdev: ~25.2 (>=5)
    res = check_score_spread(scores, sample_config)
    assert res.passed is True
    assert res.name == "check_score_spread"
    assert res.observed["spread"] == 65
    assert res.observed["stdev"] > 5.0
    assert "Passed" in res.message


def test_score_spread_failing(sample_config: Config) -> None:
    scores = [50, 51, 52, 51, 50]  # spread: 2 (<10), stdev: ~0.84 (<5)
    res = check_score_spread(scores, sample_config)
    assert res.passed is False
    assert res.observed["spread"] == 2
    assert "Failed" in res.message


def test_score_spread_fewer_than_five(sample_config: Config) -> None:
    scores = [70, 75, 80]  # fewer than 5 items
    res = check_score_spread(scores, sample_config)
    assert res.passed is True
    assert "Trivially passed" in res.message
    assert "fewer than 5" in res.message


# ---------------------------------------------------------------------------
# 2. check_extraction_sanity
# ---------------------------------------------------------------------------

def test_extraction_sanity_passing(sample_config: Config) -> None:
    facts_list = [
        {"required_skills": ["python", "sql"], "nice_to_have": ["docker"]},
        {"required_skills": ["tableau", "excel"], "nice_to_have": []},
        {"required_skills": ["python", "pandas", "numpy"], "nice_to_have": []},
        {"required_skills": ["sql", "power bi"], "nice_to_have": []},
        {"required_skills": [], "nice_to_have": ["r"]},  # 1/5 = 20% empty <= 20%
    ]
    res = check_extraction_sanity(facts_list, sample_config)
    assert res.passed is True
    assert res.name == "check_extraction_sanity"
    assert res.observed["empty_pct"] == 20.0
    assert res.observed["max_skills_found"] == 3
    assert "Passed" in res.message


def test_extraction_sanity_failing_too_many_empty(sample_config: Config) -> None:
    facts_list = [
        {"required_skills": ["python"]},
        {"required_skills": []},
        {"required_skills": []},  # 2/3 = 66.7% empty > 20%
    ]
    res = check_extraction_sanity(facts_list, sample_config)
    assert res.passed is False
    assert res.observed["empty_pct"] > 20.0
    assert "Failed" in res.message


def test_extraction_sanity_failing_excessive_skills(sample_config: Config) -> None:
    facts_list = [
        {"required_skills": [f"skill_{i}" for i in range(25)]},  # 25 > 20 limit
    ]
    res = check_extraction_sanity(facts_list, sample_config)
    assert res.passed is False
    assert res.observed["max_skills_found"] == 25
    assert "Failed" in res.message


# ---------------------------------------------------------------------------
# 3. check_gap_sample_size
# ---------------------------------------------------------------------------

def test_gap_sample_size_passing(sample_config: Config) -> None:
    gaps = [
        {"skill": "airflow", "listings_blocked": 5, "opportunity_cost": 3.5},
        {"skill": "spark", "listings_blocked": 2, "opportunity_cost": 1.2},
    ]
    res = check_gap_sample_size(gaps, sample_config)
    assert res.passed is True
    assert res.observed["top_skill"] == "airflow"
    assert res.observed["sample_size"] == 5
    assert "Passed" in res.message


def test_gap_sample_size_failing(sample_config: Config) -> None:
    gaps = [
        {"skill": "cobol", "listings_blocked": 1, "opportunity_cost": 0.8},  # 1 < 3
    ]
    res = check_gap_sample_size(gaps, sample_config)
    assert res.passed is False
    assert res.observed["top_skill"] == "cobol"
    assert res.observed["sample_size"] == 1
    assert "Failed" in res.message


# ---------------------------------------------------------------------------
# 4. check_freshness
# ---------------------------------------------------------------------------

def test_freshness_passing(sample_config: Config) -> None:
    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    latest_fetch_at = now - timedelta(days=1, hours=2)  # ~1.08 days old <= 3
    res = check_freshness(latest_fetch_at, sample_config, now=now)
    assert res.passed is True
    assert res.observed["age_days"] < 3
    assert "Passed" in res.message


def test_freshness_failing(sample_config: Config) -> None:
    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    latest_fetch_at = now - timedelta(days=5)  # 5 days old > 3
    res = check_freshness(latest_fetch_at, sample_config, now=now)
    assert res.passed is False
    assert res.observed["age_days"] == 5.0
    assert "Failed" in res.message


# ---------------------------------------------------------------------------
# 5. run_all_checks
# ---------------------------------------------------------------------------

def test_run_all_checks_all_pass(sample_config: Config) -> None:
    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    verdict = run_all_checks(
        scores=[30, 50, 70, 85, 90],
        facts_list=[{"required_skills": ["python", "sql"]}],
        gaps=[{"skill": "airflow", "listings_blocked": 4}],
        latest_fetch_at=now - timedelta(hours=6),
        config=sample_config,
        now=now,
    )
    assert verdict.passed is True
    assert len(verdict.failed_checks) == 0
    assert "All 4 verification check(s) passed" in verdict.summary


def test_run_all_checks_with_failures(sample_config: Config) -> None:
    now = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
    verdict = run_all_checks(
        scores=[50, 50, 50, 50, 50],  # Fails score spread
        facts_list=[{"required_skills": ["python"]}],
        gaps=[{"skill": "rare_tool", "listings_blocked": 1}],  # Fails gap sample
        latest_fetch_at=now - timedelta(days=1),
        config=sample_config,
        now=now,
    )
    assert verdict.passed is False
    assert len(verdict.failed_checks) == 2
    failed_names = [f.name for f in verdict.failed_checks]
    assert "check_score_spread" in failed_names
    assert "check_gap_sample_size" in failed_names
