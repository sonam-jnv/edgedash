"""
Base contract for every EdgeDash agent.

All agents implement the `Agent` Protocol: expose a `name` string and a
`run(config, db_path)` method that returns an `AgentResult`.

Using `typing.Protocol` (structural subtyping) means agents don't need to
inherit from a base class — they just need the right shape. This keeps each
agent module self-contained and easy to test in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class AgentResult:
    """Value returned by every agent after a single run."""

    agent: str
    status: str          # "ok" | "failed"
    records_touched: int
    notes: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status == "ok"


@runtime_checkable
class Agent(Protocol):
    """
    Structural protocol every EdgeDash agent must satisfy.

    `name`  -- stable identifier used in logs and the registry.
    `run`   -- execute one unit of work; never raises (catch internally and
               return status="failed" with a descriptive notes string).
    """

    name: str

    def run(
        self,
        config: object,
        db_path: str,
        stop_conditions: dict[str, Any] | None = None,
        goal: str | None = None,
    ) -> AgentResult:
        ...

