"""
Unit tests for the state-driven Orchestrator.
"""

from __future__ import annotations

from unittest.mock import patch
import pytest

from edgedash.config import Config
from edgedash.orchestrator import run_cycle
from edgedash import storage
from edgedash.state import SystemState


@pytest.fixture
def test_config(tmp_path) -> Config:
    db = str(tmp_path / "test_orch.db")
    return Config(
        target_role="Data Analyst",
        target_city="Bengaluru",
        keywords=["Python", "SQL"],
        my_skills=["Python", "SQL"],
        experience_years=3,
        db_path=db,
        use_mock_fetcher=True,
    )


def test_orchestrator_nothing_to_do(test_config: Config) -> None:
    """Test run_cycle when all tasks are skipped (outcome: nothing_to_do)."""
    db = test_config.db_abs_path
    storage.init_db(db)

    fresh_state = SystemState(
        last_fetch_at="2026-08-23T10:00:00+00:00",
        hours_since_fetch=1.0,
        unscored_count=0,
        gaps_computed_at="2026-08-23T11:00:00+00:00",
        gaps_stale=False,
        last_cycle_verdict="ok",
        last_cycle_at="2026-08-23T11:00:00+00:00",
    )

    with patch("edgedash.orchestrator.read_state", return_value=fresh_state):
        run_cycle(test_config)

    # Check cycle_log for single Orchestrator row
    verdict, cycle_at = storage.last_cycle_info(db)
    assert verdict == "nothing_to_do"
    assert cycle_at is not None


def test_orchestrator_runs_planned_tasks(test_config: Config) -> None:
    """Test run_cycle when tasks need to run."""
    db = test_config.db_abs_path
    storage.init_db(db)

    # Initial run with empty DB -> will plan and run MockFetcher, GapAnalyzer, etc.
    run_cycle(test_config)

    verdict, _ = storage.last_cycle_info(db)
    assert verdict in ("complete", "partial")


def test_orchestrator_partial_on_agent_failure(test_config: Config) -> None:
    """Test run_cycle logs partial when an agent fails without crashing the cycle."""
    db = test_config.db_abs_path
    storage.init_db(db)

    with patch("edgedash.agents.mock_fetcher.MockFetcher.run", side_effect=RuntimeError("Simulated network crash")):
        run_cycle(test_config)

    verdict, _ = storage.last_cycle_info(db)
    assert verdict in ("partial", "degraded")


def test_orchestrator_retry_on_verification_failure(test_config: Config) -> None:
    """Test verification failure triggers single retry for failing agent."""
    db = test_config.db_abs_path
    storage.init_db(db)

    from edgedash.agents.base import AgentResult
    from edgedash.verification import Verdict, CheckResult

    fail_verdict = Verdict(
        passed=False,
        failed_checks=[
            CheckResult("check_score_spread", False, {"spread": 2}, {"min_score_spread": 10}, "Failed: spread low")
        ],
        summary="1 check failed: check_score_spread",
    )
    pass_verdict = Verdict(passed=True, failed_checks=[], summary="All checks passed")

    v_fail = AgentResult("Verifier", "failed", 0, "VERDICT: fail")
    setattr(v_fail, "verdict", fail_verdict)

    v_pass = AgentResult("Verifier", "ok", 0, "VERDICT: pass")
    setattr(v_pass, "verdict", pass_verdict)

    with patch("edgedash.agents.verifier.Verifier.run", side_effect=[v_fail, v_pass]) as mock_verifier:
        with patch("edgedash.agents.scorer.Scorer.run", return_value=AgentResult("Scorer", "ok", 5, "5 scored")) as mock_scorer:
            run_cycle(test_config)
            assert mock_verifier.call_count == 2
            assert mock_scorer.call_count >= 1

    # Cycle passed on retry -> complete
    verdict, _ = storage.last_cycle_info(db)
    assert verdict == "complete"

    passing_cycle = storage.get_latest_passing_cycle(db)
    assert passing_cycle is not None
    assert passing_cycle["status"] == "complete"
    assert "Retries: 1" in passing_cycle["notes"]


def test_orchestrator_degraded_when_retry_fails(test_config: Config) -> None:
    """Test cycle marked degraded when verification fails twice."""
    db = test_config.db_abs_path
    storage.init_db(db)

    from edgedash.agents.base import AgentResult
    from edgedash.verification import Verdict, CheckResult

    fail_verdict = Verdict(
        passed=False,
        failed_checks=[
            CheckResult("check_score_spread", False, {"spread": 2}, {"min_score_spread": 10}, "Failed: spread low")
        ],
        summary="1 check failed: check_score_spread",
    )

    v_fail = AgentResult("Verifier", "failed", 0, "VERDICT: fail")
    setattr(v_fail, "verdict", fail_verdict)

    with patch("edgedash.agents.verifier.Verifier.run", return_value=v_fail):
        run_cycle(test_config)

    verdict, _ = storage.last_cycle_info(db)
    assert verdict == "degraded"

    # get_latest_passing_cycle should return None since this cycle is degraded
    passing_cycle = storage.get_latest_passing_cycle(db)
    assert passing_cycle is None

