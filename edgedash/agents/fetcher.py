"""
Fetcher — the real network-facing fetch agent.

Reads the list of enabled sources from config.sources, instantiates each
from the SOURCES registry, calls fetch(config), and writes all results to
storage via upsert_listings.

Per steering rule 12: each source is wrapped in its own try/except.
A failing source is logged to cycle_log with status "failed" and the cycle
continues — one dead job board must not stop the others.

The listing id is computed by storage.make_listing_id (source + url hash)
so the dedup logic lives in exactly one place.

The notes field of the returned AgentResult summarises every source outcome
in a single readable string, e.g.:
    "arbeitnow: 47 rows (12 new) | apify: FAILED (connection timeout)"
"""

from __future__ import annotations

from datetime import datetime, timezone

# Import the SOURCES registry and trigger registration of all known sources
# by importing each source module.  Any source module decorated with @register
# will add itself to SOURCES on import.
from edgedash.sources.base import SOURCES
import edgedash.sources.arbeitnow  # noqa: F401 — registers ArbeitnowSource

from edgedash.agents.base import AgentResult
from edgedash.config import Config
from edgedash import storage


class Fetcher:
    """Fetches live job listings from all enabled sources."""

    name: str = "Fetcher"

    def run(
        self,
        config: Config,
        db_path: str,
        stop_conditions: dict | None = None,
        goal: str | None = None,
    ) -> AgentResult:
        source_names = config.sources
        summaries: list[str] = []
        total_new = 0

        for source_name in source_names:
            new_count, summary = self._run_one_source(
                source_name, config, db_path, stop_conditions=stop_conditions
            )
            summaries.append(summary)
            total_new += max(new_count, 0)

        notes = " | ".join(summaries) if summaries else "No sources configured."
        overall_status = "ok" if all("FAILED" not in s for s in summaries) else "failed"

        return AgentResult(
            agent=self.name,
            status=overall_status,
            records_touched=total_new,
            notes=notes,
        )


    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _run_one_source(
        self,
        source_name: str,
        config: Config,
        db_path: str,
        stop_conditions: dict | None = None,
    ) -> tuple[int, str]:
        """
        Fetch from one source and write results to storage.

        Returns (new_row_count, summary_string).
        new_row_count is -1 on failure.
        """
        started = _utcnow()

        if source_name not in SOURCES:
            msg = f"unknown source '{source_name}' — not in registry"
            print(f"  ⚠  Fetcher: {msg}")
            storage.log_cycle(
                db_path,
                agent=f"Fetcher/{source_name}",
                started_at=started,
                finished_at=_utcnow(),
                records_touched=0,
                status="failed",
                notes=msg,
            )
            return -1, f"{source_name}: FAILED ({msg})"

        source_cls = SOURCES[source_name]
        source = source_cls()

        try:
            raw_rows = source.fetch(config)
        except Exception as exc:
            short = _short_exc(exc)
            print(f"  ⚠  Fetcher: source '{source_name}' raised — {short}")
            storage.log_cycle(
                db_path,
                agent=f"Fetcher/{source_name}",
                started_at=started,
                finished_at=_utcnow(),
                records_touched=0,
                status="failed",
                notes=str(exc),
            )
            return -1, f"{source_name}: FAILED ({short})"

        # Map normalised source rows onto the storage schema.
        # storage.make_listing_id is reused — no second id implementation.
        storage_rows = _to_storage_rows(raw_rows)
        if stop_conditions and "max_listings" in stop_conditions:
            max_listings = stop_conditions["max_listings"]
            if isinstance(max_listings, int) and max_listings > 0:
                storage_rows = storage_rows[:max_listings]

        try:
            new_count = storage.upsert_listings(db_path, storage_rows)

        except Exception as exc:
            short = _short_exc(exc)
            print(f"  ⚠  Fetcher: upsert failed for '{source_name}' — {short}")
            storage.log_cycle(
                db_path,
                agent=f"Fetcher/{source_name}",
                started_at=started,
                finished_at=_utcnow(),
                records_touched=0,
                status="failed",
                notes=str(exc),
            )
            return -1, f"{source_name}: FAILED ({short})"

        total = len(storage_rows)
        storage.log_cycle(
            db_path,
            agent=f"Fetcher/{source_name}",
            started_at=started,
            finished_at=_utcnow(),
            records_touched=new_count,
            status="ok",
            notes=f"{total} rows presented, {new_count} new.",
        )
        return new_count, f"{source_name}: {total} rows ({new_count} new)"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _to_storage_rows(source_rows: list[dict]) -> list[dict]:
    """
    Convert normalised source rows to the dict shape upsert_listings expects.

    Source schema  → storage schema
    external_id    (ignored — storage.make_listing_id recomputes from source+url)
    source         → source
    title          → title          (None → "Unknown")
    company        → company        (None → "Unknown")
    location       → location       (None → "Unknown")
    url            → url
    description    → description
    posted_at      → posted_at
    raw            (not stored in the listings table)
    """
    rows = []
    for r in source_rows:
        url = r.get("url")
        if not url:
            continue  # can't dedup without a URL; skip silently
        rows.append({
            "source":      r["source"],
            "title":       r.get("title") or "Unknown",
            "company":     r.get("company") or "Unknown",
            "location":    r.get("location") or "Unknown",
            "url":         url,
            "description": r.get("description") or "",
            "posted_at":   r.get("posted_at"),
        })
    return rows


def _short_exc(exc: Exception) -> str:
    """Return a one-line summary of an exception."""
    return type(exc).__name__ + (f": {exc}" if str(exc) else "")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()
