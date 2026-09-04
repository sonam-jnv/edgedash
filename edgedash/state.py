"""
State inspection module (deterministic timestamp and count arithmetic).

Reads current system state from storage through cheap queries (max timestamps, counts).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from edgedash import storage
from edgedash.config import Config


@dataclass(frozen=True)
class SystemState:
    last_fetch_at: str | None
    hours_since_fetch: float | None
    unscored_count: int
    gaps_computed_at: str | None
    gaps_stale: bool
    last_cycle_verdict: str | None
    last_cycle_at: str | None


def _parse_iso(ts: str) -> datetime:
    """Parse ISO 8601 string to timezone-aware UTC datetime."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def read_state(config: Config, now: datetime) -> SystemState:
    """
    Read system state from the database at a specific timestamp 'now'.

    'now' is passed as a parameter for deterministic testing.
    """
    db_path = config.db_abs_path
    storage.init_db(db_path)

    # 1. Fetch timestamp & hours calculation
    last_fetch_at = storage.last_fetch_time(db_path)
    hours_since_fetch: float | None = None
    if last_fetch_at:
        try:
            fetch_dt = _parse_iso(last_fetch_at)
            ref_now = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
            delta_seconds = (ref_now - fetch_dt).total_seconds()
            hours_since_fetch = max(0.0, delta_seconds / 3600.0)
        except Exception:
            hours_since_fetch = None

    # 2. Unscored count
    unscored_count = storage.count_unscored(db_path)

    # 3. Gap snapshots & staleness
    gaps_computed_at = storage.last_gap_computed_at(db_path)
    last_score_at = storage.last_score_time(db_path)

    gaps_stale = False
    if gaps_computed_at and last_score_at:
        try:
            gap_dt = _parse_iso(gaps_computed_at)
            score_dt = _parse_iso(last_score_at)
            gaps_stale = score_dt > gap_dt
        except Exception:
            gaps_stale = last_score_at > gaps_computed_at

    # 4. Last cycle info
    verdict, cycle_at = storage.last_cycle_info(db_path)

    return SystemState(
        last_fetch_at=last_fetch_at,
        hours_since_fetch=hours_since_fetch,
        unscored_count=unscored_count,
        gaps_computed_at=gaps_computed_at,
        gaps_stale=gaps_stale,
        last_cycle_verdict=verdict,
        last_cycle_at=cycle_at,
    )
