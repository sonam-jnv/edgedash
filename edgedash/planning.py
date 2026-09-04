"""
Deterministic planning module (pure function of state and config).

Generates an ordered Plan of Tasks with goals, stop conditions, and reasons.
No I/O or LLM calls.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from edgedash.config import Config
from edgedash.state import SystemState


@dataclass
class Task:
    agent_name: str
    goal: str
    stop_conditions: dict[str, Any]
    reason: str
    skipped: bool = False

    def __post_init__(self) -> None:
        if not self.skipped and self.reason.startswith("skipped"):
            self.skipped = True


@dataclass
class Plan:
    tasks: list[Task] = field(default_factory=list)

    def __iter__(self):
        return iter(self.tasks)

    def __len__(self) -> int:
        return len(self.tasks)

    def __getitem__(self, index: int) -> Task:
        return self.tasks[index]

    def render(self) -> str:
        """Render compact printable plan showing goal, stop conditions, and reason."""
        lines = []
        for t in self.tasks:
            status = "[SKIP]" if t.skipped else "[RUN ]"
            stops = ", ".join(f"{k}={v}" for k, v in t.stop_conditions.items())
            lines.append(f"  {status} {t.agent_name:<12} | Goal: {t.goal} | Stops: {stops} | Reason: {t.reason}")
        return "\n".join(lines)


def build_plan(state: SystemState, config: Config) -> Plan:
    """Build an execution plan from state and config (pure function, no I/O)."""
    tasks: list[Task] = []

    # 1. Fetcher decision
    fetch_interval = getattr(config, "fetch_interval_hours", 6)
    fetch_stops = {
        "max_pages": getattr(config, "fetch_max_pages", getattr(config, "max_pages", 5)),
        "max_listings": getattr(config, "fetch_max_listings", getattr(config, "max_listings", 50)),
    }
    fetcher_name = "MockFetcher" if getattr(config, "use_mock_fetcher", False) else "Fetcher"
    fetch_goal = "Fetch new job listings from configured sources"

    if state.hours_since_fetch is None:
        fetch_reason, fetch_skip = "hours_since_fetch=None (no previous fetch recorded)", False
    elif state.hours_since_fetch >= fetch_interval:
        fetch_reason, fetch_skip = f"hours_since_fetch={state.hours_since_fetch:.1f} >= {fetch_interval}h", False
    else:
        fetch_reason, fetch_skip = f"skipped: hours_since_fetch={state.hours_since_fetch:.1f} < {fetch_interval}h", True
    tasks.append(Task(fetcher_name, fetch_goal, fetch_stops, fetch_reason, fetch_skip))

    # 2. Scorer decision
    score_batch = getattr(config, "score_batch_size", getattr(config, "llm_score_batch_size", 25))
    score_stops = {
        "max_items": score_batch,
        "max_seconds": getattr(config, "score_max_seconds", getattr(config, "max_seconds", 60)),
    }
    score_goal = "Extract facts and compute fit scores for unscored listings"

    if state.unscored_count > 0:
        score_reason, score_skip = f"unscored_count={state.unscored_count}", False
    else:
        score_reason, score_skip = "skipped: unscored_count=0", True
    tasks.append(Task("Scorer", score_goal, score_stops, score_reason, score_skip))

    # 3. GapAnalyzer decision
    analyse_stops = {"max_seconds": getattr(config, "analyse_max_seconds", getattr(config, "max_seconds", 30))}
    analyse_goal = "Compute skill gap snapshot across scored listings"

    if state.gaps_computed_at is None:
        gap_reason, gap_skip = "gaps_computed_at=None (no snapshots exist)", False
    elif state.gaps_stale:
        gap_reason, gap_skip = "gaps_stale=True (new scores since last snapshot)", False
    else:
        gap_reason, gap_skip = "skipped: gaps_stale=False (gaps up to date)", True
    tasks.append(Task("GapAnalyzer", analyse_goal, analyse_stops, gap_reason, gap_skip))

    return Plan(tasks=tasks)
