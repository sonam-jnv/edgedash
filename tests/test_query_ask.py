"""
Unit tests for the two-call query pipeline (Rules 42–45).
"""

from __future__ import annotations

from unittest.mock import patch
import pytest

from edgedash import storage
from edgedash.query.ask import ask, Answer


@pytest.fixture
def populated_db(tmp_path):
    db = str(tmp_path / "test_ask.db")
    storage.init_db(db)
    storage.log_cycle(
        db,
        agent="Orchestrator",
        started_at="2026-08-29T10:00:00+00:00",
        finished_at="2026-08-29T10:05:00+00:00",
        records_touched=10,
        status="pass",
        notes="Verified pass",
    )
    storage.upsert_listings(
        db,
        [
            {
                "source": "arbeitnow",
                "url": "https://example.com/j1",
                "title": "Lead Python Engineer",
                "company": "TechGlobal",
                "location": "Berlin",
                "description": "Python, GCP",
                "posted_at": "2026-08-28T12:00:00+00:00",
            }
        ],
    )
    lid = storage.make_listing_id("arbeitnow", "https://example.com/j1")
    storage.save_listing_score(db, lid, 92, "Great match")
    return db


def test_ask_successful_two_call_pipeline(populated_db):
    route_mock = {
        "tool": "best_matches",
        "params": {"n": 5},
        "confidence": "high",
    }
    phrase_mock = {
        "answer": "You have 1 top match: Lead Python Engineer at TechGlobal with a fit score of 92."
    }

    with patch("edgedash.llm.complete_json", side_effect=[route_mock, phrase_mock]):
        answer = ask("What are my best job matches?", db_path=populated_db)

    assert isinstance(answer, Answer)
    assert answer.tool_used == "best_matches"
    assert answer.params == {"n": 5}
    assert len(answer.rows) == 1
    assert "TechGlobal" in answer.text

    # Verify query_log record
    with storage._connect(populated_db) as conn:
        row = conn.execute("SELECT * FROM query_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["question"] == "What are my best job matches?"
    assert row["tool_chosen"] == "best_matches"
    assert row["answerable"] == 1


def test_ask_unanswerable_question_rule_45(populated_db):
    route_mock = {
        "tool": None,
        "params": {},
        "confidence": "low",
    }

    # Should only call complete_json once for routing, never for phrasing
    with patch("edgedash.llm.complete_json", return_value=route_mock) as mock_llm:
        answer = ask("What is the weather in Paris?", db_path=populated_db)
        assert mock_llm.call_count == 1

    assert answer.tool_used is None
    assert answer.rows == []
    assert "I cannot answer that question" in answer.text
    assert "companies_hiring" in answer.text

    # Verify query_log
    with storage._connect(populated_db) as conn:
        row = conn.execute("SELECT * FROM query_log ORDER BY id DESC LIMIT 1").fetchone()
    assert row["tool_chosen"] is None
    assert row["answerable"] == 0
