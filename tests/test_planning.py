"""
Unit tests for planning and state modules.
"""

from __future__ import annotations

from datetime import datetime, timezone
import pytest

from edgedash.config import Config
from edgedash.state import SystemState, read_state
from edgedash.planning import build_plan, Task, Plan
from edgedash import storage


@pytest.fixture
def mock_config(tmp_path) -> Config:
    db = str(tmp_path / "test.db")
    return Config(
        target_role="Data Analyst",
        target_city="Bengaluru",
        keywords=["Python", "SQL"],
        my_skills=["Python", "SQL"],
        experience_years=3,
        db_path=db,
    )


def test_plan_everything_stale(mock_config: Config) -> None:
    """Case 1: Everything stale -> All three agents run."""
    state = SystemState(
        last_fetch_at="2026-08-23T00:00:00+00:00",
        hours_since_fetch=12.5,
        unscored_count=15,
        gaps_computed_at="2026-08-23T01:00:00+00:00",
        gaps_stale=True,
        last_cycle_verdict="ok",
        last_cycle_at="2026-08-23T01:00:00+00:00",
    )

    plan = build_plan(state, mock_config)
    assert len(plan) == 3

    assert plan[0].agent_name == "Fetcher"
    assert not plan[0].skipped
    assert "hours_since_fetch=12.5 >= 6" in plan[0].reason

    assert plan[1].agent_name == "Scorer"
    assert not plan[1].skipped
    assert "unscored_count=15" in plan[1].reason

    assert plan[2].agent_name == "GapAnalyzer"
    assert not plan[2].skipped
    assert "gaps_stale=True" in plan[2].reason


def test_plan_nothing_to_do(mock_config: Config) -> None:
    """Case 2: Nothing to do -> All three agents skipped."""
    state = SystemState(
        last_fetch_at="2026-08-23T10:00:00+00:00",
        hours_since_fetch=2.0,
        unscored_count=0,
        gaps_computed_at="2026-08-23T11:00:00+00:00",
        gaps_stale=False,
        last_cycle_verdict="ok",
        last_cycle_at="2026-08-23T11:00:00+00:00",
    )

    plan = build_plan(state, mock_config)
    assert len(plan) == 3

    assert plan[0].agent_name == "Fetcher"
    assert plan[0].skipped
    assert "skipped: hours_since_fetch=2.0 < 6" in plan[0].reason

    assert plan[1].agent_name == "Scorer"
    assert plan[1].skipped
    assert plan[1].reason == "skipped: unscored_count=0"

    assert plan[2].agent_name == "GapAnalyzer"
    assert plan[2].skipped
    assert plan[2].reason == "skipped: gaps_stale=False (gaps up to date)"

    # Test Plan.render()
    rendered = plan.render()
    assert "[SKIP] Fetcher" in rendered
    assert "[SKIP] Scorer" in rendered
    assert "[SKIP] GapAnalyzer" in rendered
    assert "unscored_count=0" in rendered


def test_plan_only_unscored_listings(mock_config: Config) -> None:
    """Case 3: Only unscored listings -> Scorer runs, Fetcher & GapAnalyzer skipped."""
    state = SystemState(
        last_fetch_at="2026-08-23T10:00:00+00:00",
        hours_since_fetch=1.5,
        unscored_count=8,
        gaps_computed_at="2026-08-23T11:00:00+00:00",
        gaps_stale=False,
        last_cycle_verdict="ok",
        last_cycle_at="2026-08-23T11:00:00+00:00",
    )

    plan = build_plan(state, mock_config)
    assert len(plan) == 3

    assert plan[0].skipped
    assert not plan[1].skipped
    assert plan[1].agent_name == "Scorer"
    assert "unscored_count=8" in plan[1].reason
    assert plan[2].skipped


def test_plan_gaps_stale_nothing_unscored(mock_config: Config) -> None:
    """Case 4: Gaps stale but nothing unscored -> GapAnalyzer runs, Fetcher & Scorer skipped."""
    state = SystemState(
        last_fetch_at="2026-08-23T10:00:00+00:00",
        hours_since_fetch=3.0,
        unscored_count=0,
        gaps_computed_at="2026-08-23T09:00:00+00:00",
        gaps_stale=True,
        last_cycle_verdict="ok",
        last_cycle_at="2026-08-23T09:00:00+00:00",
    )

    plan = build_plan(state, mock_config)
    assert len(plan) == 3

    assert plan[0].skipped
    assert plan[1].skipped
    assert not plan[2].skipped
    assert plan[2].agent_name == "GapAnalyzer"
    assert "gaps_stale=True" in plan[2].reason


def test_read_state_empty_db(mock_config: Config) -> None:
    """Verify read_state on an empty database."""
    now = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)
    state = read_state(mock_config, now)

    assert state.last_fetch_at is None
    assert state.hours_since_fetch is None
    assert state.unscored_count == 0
    assert state.gaps_computed_at is None
    assert state.gaps_stale is False
    assert state.last_cycle_verdict is None
    assert state.last_cycle_at is None


def test_read_state_with_data(mock_config: Config) -> None:
    """Verify read_state with populated database."""
    db = mock_config.db_abs_path
    storage.init_db(db)

    # Insert listings
    storage.upsert_listings(db, [
        {
            "title": "Analyst",
            "company": "Corp",
            "location": "BLR",
            "url": "http://corp.com/job1",
            "source": "arbeitnow",
            "fetched_at": "2026-08-23T06:00:00+00:00",
        }
    ])

    now = datetime(2026, 8, 23, 12, 0, 0, tzinfo=timezone.utc)
    state = read_state(mock_config, now)

    assert state.last_fetch_at == "2026-08-23T06:00:00+00:00"
    assert state.hours_since_fetch == pytest.approx(6.0, rel=1e-2)
    assert state.unscored_count == 1
    assert state.gaps_computed_at is None
