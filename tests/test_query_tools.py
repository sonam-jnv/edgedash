"""
Unit tests for deterministic query tool registry and parameter clamping (Rules 40, 41, 46).
"""

from __future__ import annotations

import pytest
from edgedash import storage
from edgedash.query.tools import (
    TOOLS,
    companies_hiring,
    best_matches,
    top_gaps,
    gap_detail,
    trend,
    listing_count,
    skill_demand,
    _clamp_int,
)


@pytest.fixture
def test_db(tmp_path):
    """Set up a test database with verified passing cycle and sample listings/gaps."""
    db = str(tmp_path / "test_query.db")
    storage.init_db(db)

    # 1. Add passing cycle log
    storage.log_cycle(
        db,
        agent="Orchestrator",
        started_at="2026-08-29T10:00:00+00:00",
        finished_at="2026-08-29T10:05:00+00:00",
        records_touched=15,
        status="pass",
        notes="Verified pass",
    )

    # 2. Add listings
    listings = [
        {
            "source": "arbeitnow",
            "url": "https://example.com/job1",
            "title": "Senior Python Engineer",
            "company": "Acme Corp",
            "location": "Berlin",
            "description": "Looking for Python, AWS, Docker.",
            "posted_at": "2026-08-28T12:00:00+00:00",
        },
        {
            "source": "arbeitnow",
            "url": "https://example.com/job2",
            "title": "Data Engineer",
            "company": "DataTech",
            "location": "Remote",
            "description": "Python, SQL, Kubernetes.",
            "posted_at": "2026-08-27T12:00:00+00:00",
        },
    ]
    storage.upsert_listings(db, listings)
    l1_id = storage.make_listing_id("arbeitnow", "https://example.com/job1")
    l2_id = storage.make_listing_id("arbeitnow", "https://example.com/job2")
    storage.save_listing_score(db, l1_id, 85, "Strong Python match")
    storage.save_listing_score(db, l2_id, 70, "Good data skills")

    # 3. Add cached extractions
    h1 = storage.hashlib.sha256("Looking for Python, AWS, Docker.".encode("utf-8")).hexdigest()
    storage.save_cached_extraction(
        db,
        h1,
        {
            "required_skills": ["python", "aws", "docker"],
            "nice_to_have": ["kubernetes"],
            "seniority": "senior",
        },
    )

    # 4. Add skill gap snapshot
    storage.save_skill_gap_snapshot(
        db,
        run_id="run-1",
        computed_at="2026-08-29T10:05:00+00:00",
        gaps=[
            {
                "skill": "kubernetes",
                "listings_blocked": 3,
                "opportunity_cost": 42.5,
                "mean_score": 75.0,
                "top_score": 88,
                "example_ids": [l2_id],
            },
            {
                "skill": "aws",
                "listings_blocked": 2,
                "opportunity_cost": 30.0,
                "mean_score": 70.0,
                "top_score": 85,
                "example_ids": [l1_id],
            },
        ],
    )

    return db


def test_registry_contains_seven_tools():
    expected_tools = {
        "companies_hiring",
        "best_matches",
        "top_gaps",
        "gap_detail",
        "trend",
        "listing_count",
        "skill_demand",
    }
    assert expected_tools.issubset(set(TOOLS.keys()))
    for name in expected_tools:
        spec = TOOLS[name]
        assert "description" in spec and len(spec["description"]) > 10
        assert "parameters" in spec
        assert callable(spec["func"])


def test_clamp_int():
    assert _clamp_int(10, default=7, min_val=1, max_val=90) == 10
    assert _clamp_int(0, default=7, min_val=1, max_val=90) == 1
    assert _clamp_int(150, default=7, min_val=1, max_val=90) == 90
    assert _clamp_int("invalid", default=7, min_val=1, max_val=90) == 7
    assert _clamp_int(None, default=7, min_val=1, max_val=90) == 7


def test_companies_hiring_shape_and_clamping(test_db):
    res = companies_hiring(days=30, db_path=test_db)
    assert "summary" in res
    assert "rows" in res
    assert isinstance(res["rows"], list)

    # Clamping test
    res_clamped = companies_hiring(days=500, db_path=test_db)
    assert isinstance(res_clamped["rows"], list)


def test_best_matches_shape(test_db):
    res = best_matches(n=5, db_path=test_db)
    assert "summary" in res
    assert len(res["rows"]) >= 1
    assert res["rows"][0]["fit_score"] == 85
    assert "title" in res["rows"][0]
    assert "company" in res["rows"][0]


def test_top_gaps_shape(test_db):
    res = top_gaps(n=5, db_path=test_db)
    assert "summary" in res
    assert len(res["rows"]) == 2
    assert res["rows"][0]["skill"] == "kubernetes"
    assert res["rows"][0]["opportunity_cost"] == 42.5


def test_gap_detail_known_and_unknown_skill(test_db):
    # Known skill
    res = gap_detail(skill="kubernetes", db_path=test_db)
    assert len(res["rows"]) == 1

    # Unknown skill (should return empty without raising)
    res_unknown = gap_detail(skill="quantum_computing_unknown", db_path=test_db)
    assert res_unknown["rows"] == []
    assert "not present" in res_unknown["summary"]


def test_trend_shape(test_db):
    res = trend(weeks=3, db_path=test_db)
    assert "summary" in res
    assert isinstance(res["rows"], list)


def test_listing_count_shape(test_db):
    res = listing_count(db_path=test_db)
    assert "summary" in res
    assert len(res["rows"]) == 1
    assert res["rows"][0]["total_listings"] == 2
    assert res["rows"][0]["scored_listings"] == 2


def test_skill_demand_known_and_unknown(test_db):
    res = skill_demand(skill="python", db_path=test_db)
    assert len(res["rows"]) == 1
    assert res["rows"][0]["required_count"] == 1

    res_unknown = skill_demand(skill="brainfuck_lang", db_path=test_db)
    assert res_unknown["rows"] == []


def test_rule_46_no_passing_cycle(tmp_path):
    db = str(tmp_path / "empty.db")
    storage.init_db(db)
    res = best_matches(n=10, db_path=db)
    assert res["rows"] == []
    assert "No verified passing cycle" in res["summary"]
