"""
Orchestrator — state-driven execution, pure delegation, and cycle logging (Rules 28–33).
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any

from edgedash import storage
from edgedash.agents.base import Agent, AgentResult
from edgedash.agents.fetcher import Fetcher
from edgedash.agents.mock_fetcher import MockFetcher
from edgedash.agents.gap_analyzer import GapAnalyzer
from edgedash.agents.scorer import Scorer
from edgedash.agents.verifier import Verifier
from edgedash.config import Config
from edgedash.planning import build_plan, Plan
from edgedash.state import read_state
from edgedash.verification import Verdict


def _build_registry(config: Config) -> list[dict[str, Any]]:
    fetcher_agent = MockFetcher() if config.use_mock_fetcher else Fetcher()
    return [
        {"agent": fetcher_agent, "enabled": True, "reason": ""},
        {"agent": Scorer(), "enabled": True, "reason": ""},
        {"agent": GapAnalyzer(), "enabled": True, "reason": ""},
        {"agent": Verifier(), "enabled": True, "reason": ""},
    ]


def _identify_failing_agent(failed_checks: list[str]) -> str:
    """Map failed check names to responsible agent for retry."""
    if any("score_spread" in c or "extraction" in c for c in failed_checks):
        return "Scorer"
    if any("gap" in c for c in failed_checks):
        return "GapAnalyzer"
    if any("freshness" in c for c in failed_checks):
        return "Fetcher"
    return "Scorer"


def run_cycle(config: Config) -> None:
    db = config.db_abs_path
    storage.init_db(db)
    cycle_start = _utcnow()

    # 1. Read state and build plan
    state = read_state(config, datetime.now(timezone.utc))
    plan = build_plan(state, config)

    # 2. Print rendered plan before executing anything (Rule 31)
    print("\n" + "=" * 64 + "\n  EDGEDASH — CYCLE PLAN\n" + "=" * 64)
    print(plan.render() + "\n" + "-" * 64)

    # 3. Handle nothing_to_do state (Rule 28 & 33)
    if all(t.skipped for t in plan.tasks):
        storage.log_cycle(
            db, agent="Orchestrator", started_at=cycle_start, finished_at=_utcnow(),
            records_touched=0, status="nothing_to_do", notes="Plan: all agents skipped.",
        )
        _print_summary("nothing_to_do", [], plan, 0)
        return

    # 4. Resolve agents from registry and execute planned tasks
    registry = _build_registry(config)
    agents: dict[str, Agent] = {entry["agent"].name: entry["agent"] for entry in registry}
    executed_results: list[dict[str, Any]] = []
    has_failure, total_records = False, 0

    for task in plan.tasks:
        if task.skipped:
            continue

        agent = agents.get(task.agent_name)
        if not agent:
            has_failure = True
            msg = f"Agent '{task.agent_name}' not found in registry"
            print(f"  ✗  {msg}")
            executed_results.append({"agent": task.agent_name, "status": "failed", "records": 0, "duration": 0.0, "notes": msg})
            continue

        print(f"\n  ▶  Running {agent.name} …")
        t0, task_start = time.perf_counter(), _utcnow()

        try:
            result = agent.run(config, db, stop_conditions=task.stop_conditions, goal=task.goal)
        except Exception as exc:
            result = AgentResult(agent=task.agent_name, status="failed", records_touched=0, notes=f"Agent crashed: {exc}")

        duration, task_finish = round(time.perf_counter() - t0, 2), _utcnow()
        if not result.succeeded:
            has_failure = True

        total_records += result.records_touched
        storage.log_cycle(
            db, agent=result.agent, started_at=task_start, finished_at=task_finish,
            records_touched=result.records_touched, status=result.status, notes=result.notes or None,
        )
        executed_results.append({
            "agent": result.agent, "status": result.status, "records": result.records_touched,
            "duration": duration, "notes": result.notes,
        })
        _print_agent_result(result, duration)

    # 5. Verification and single-retry flow (Rule 36)
    verifier = agents.get("Verifier") or Verifier()
    print(f"\n  ▶  Running {verifier.name} …")
    t0, v_start = time.perf_counter(), _utcnow()
    v_res = verifier.run(config, db)
    v_dur, v_finish = round(time.perf_counter() - t0, 2), _utcnow()

    storage.log_cycle(
        db, agent=v_res.agent, started_at=v_start, finished_at=v_finish,
        records_touched=0, status=v_res.status, notes=v_res.notes or None,
    )
    executed_results.append({
        "agent": v_res.agent, "status": v_res.status, "records": 0,
        "duration": v_dur, "notes": v_res.notes,
    })
    _print_agent_result(v_res, v_dur)

    retries = 0
    verdict: Verdict | None = getattr(v_res, "verdict", None)
    failed_checks = [c.name for c in verdict.failed_checks] if verdict else ([] if v_res.succeeded else ["verification_failed"])

    if not v_res.succeeded:
        # Re-run ONLY the failing agent with adjusted context (max 1 retry for whole cycle)
        retries = 1
        target_agent_name = _identify_failing_agent(failed_checks)
        target_agent = agents.get(target_agent_name)

        if target_agent:
            print(f"\n  ↺  Verification failed ({', '.join(failed_checks)}). Retrying {target_agent_name} with adjusted context (retry 1/1) …")
            adjusted_stops: dict[str, Any] = {}
            if target_agent_name == "Scorer":
                adjusted_stops["widen_distribution"] = True
                adjusted_stops["rescore_recent"] = True

            t0, retry_start = time.perf_counter(), _utcnow()
            try:
                retry_res = target_agent.run(
                    config, db,
                    stop_conditions=adjusted_stops,
                    goal=f"Retry with adjusted context to resolve {', '.join(failed_checks)}",
                )
            except Exception as exc:
                retry_res = AgentResult(agent=target_agent_name, status="failed", records_touched=0, notes=f"Retry crashed: {exc}")

            retry_dur, retry_finish = round(time.perf_counter() - t0, 2), _utcnow()
            total_records += retry_res.records_touched
            storage.log_cycle(
                db, agent=f"{retry_res.agent} (retry)", started_at=retry_start, finished_at=retry_finish,
                records_touched=retry_res.records_touched, status=retry_res.status, notes=retry_res.notes or None,
            )
            executed_results.append({
                "agent": f"{retry_res.agent} (retry)", "status": retry_res.status, "records": retry_res.records_touched,
                "duration": retry_dur, "notes": retry_res.notes,
            })
            _print_agent_result(retry_res, retry_dur)

            # If Scorer was retried, re-sync GapAnalyzer if gaps need refresh
            if target_agent_name == "Scorer" and retry_res.succeeded and "GapAnalyzer" in agents:
                gap_agent = agents["GapAnalyzer"]
                t0, g_start = time.perf_counter(), _utcnow()
                g_res = gap_agent.run(config, db)
                g_dur, g_finish = round(time.perf_counter() - t0, 2), _utcnow()
                total_records += g_res.records_touched
                storage.log_cycle(
                    db, agent=f"{g_res.agent} (post-retry)", started_at=g_start, finished_at=g_finish,
                    records_touched=g_res.records_touched, status=g_res.status, notes=g_res.notes or None,
                )

            # Re-verify once more
            print(f"\n  ▶  Re-verifying …")
            t0, v2_start = time.perf_counter(), _utcnow()
            v2_res = verifier.run(config, db)
            v2_dur, v2_finish = round(time.perf_counter() - t0, 2), _utcnow()

            storage.log_cycle(
                db, agent=f"{v2_res.agent} (re-verify)", started_at=v2_start, finished_at=v2_finish,
                records_touched=0, status=v2_res.status, notes=v2_res.notes or None,
            )
            executed_results.append({
                "agent": f"{v2_res.agent} (re-verify)", "status": v2_res.status, "records": 0,
                "duration": v2_dur, "notes": v2_res.notes,
            })
            _print_agent_result(v2_res, v2_dur)

            v_res = v2_res
            verdict = getattr(v2_res, "verdict", None)
            failed_checks = [c.name for c in verdict.failed_checks] if verdict else ([] if v2_res.succeeded else ["verification_failed"])

    # 6. Determine final cycle outcome & log summary (Rules 32, 33, 36)
    if not v_res.succeeded:
        outcome = "degraded"
        verdict_str = "fail"
    elif has_failure:
        outcome = "partial"
        verdict_str = "pass"
    else:
        outcome = "complete"
        verdict_str = "pass"

    ran_desc = ", ".join(f"{r['agent']} ({r['duration']}s, {r['records']} rec, {r['status']})" for r in executed_results)
    skipped_desc = ", ".join(f"{t.agent_name} ({t.reason})" for t in plan.tasks if t.skipped)
    failed_checks_str = ", ".join(failed_checks) if failed_checks else "none"
    summary_notes = (
        f"Verdict: {verdict_str} | Failed checks: {failed_checks_str} | Retries: {retries} | "
        f"Outcome: {outcome} | Ran: {ran_desc or 'none'} | Skipped: {skipped_desc or 'none'}"
    )

    storage.log_cycle(
        db, agent="Orchestrator", started_at=cycle_start, finished_at=_utcnow(),
        records_touched=total_records, status=outcome, notes=summary_notes,
    )
    _print_summary(outcome, executed_results, plan, total_records)


def _print_agent_result(result: AgentResult, duration: float) -> None:
    icon = "✓" if result.succeeded else "✗"
    print(f"      {icon} {result.agent}: {result.status.upper()} ({duration}s) | records={result.records_touched}")
    if result.notes:
        print(f"        {result.notes}")


def _print_summary(outcome: str, executed: list[dict[str, Any]], plan: Plan, total_touched: int) -> None:
    print("\n" + "=" * 64 + f"\n  CYCLE SUMMARY — Outcome: {outcome.upper()}\n" + "=" * 64)
    print(f"  {'Total records touched:':<24} {total_touched}")
    if executed:
        print("  Executed agents:")
        for r in executed:
            print(f"    • {r['agent']:<14} {r['status'].upper():<8} {r['duration']:>5.2f}s  touched={r['records']}")
    skipped = [t for t in plan.tasks if t.skipped]
    if skipped:
        print("  Skipped agents:")
        for s in skipped:
            print(f"    • {s.agent_name:<14} {s.reason}")
    print("=" * 64 + "\n")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()

