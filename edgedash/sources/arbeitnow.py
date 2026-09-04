"""
ArbeitnowSource — free public job board, no API key required.

API docs: https://www.arbeitnow.com/api/job-board-api

Pagination: ?page=1, ?page=2, ...  (175 results per page)
Rate limit: 1 request per second (steering rule 14).
Hard cap: 5 pages maximum per fetch cycle.

Filtering strategy (steering rule):
  1. Keep jobs whose title, description, or tags contain at least one keyword.
  2. Of those, prefer jobs whose location matches config.target_city.
  3. If a strict city filter would leave fewer than 5 results, relax it and
     log that we did so.

Field mapping (steering rule 10):
  source      <- "arbeitnow"
  external_id <- slug   (stable, assigned by Arbeitnow)
  title       <- title
  company     <- company_name
  location    <- location
  url         <- url
  description <- description  (raw HTML from the API)
  posted_at   <- None  (API returns created_at as Unix timestamp; we store it
                        in raw but do not expose it as posted_at because the
                        field represents ingestion time, not posting date)
  raw         <- the full original job dict
"""

from __future__ import annotations

import time
import logging

from edgedash.config import Config
from edgedash.sources.base import register
from edgedash.sources.http import SourceError, get_json

logger = logging.getLogger(__name__)

_API_URL = "https://www.arbeitnow.com/api/job-board-api"
_MAX_PAGES = 5
_REQUEST_DELAY = 1.0   # seconds between pages (steering rule 14)


@register
class ArbeitnowSource:
    """Fetches jobs from the free Arbeitnow public API."""

    name: str = "arbeitnow"

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def fetch(self, config: Config) -> list[dict]:
        raw_jobs = self._fetch_pages(config.keywords)
        print(f"  [arbeitnow] raw results fetched: {len(raw_jobs)}")

        normalised = [self._normalise(job) for job in raw_jobs]

        filtered = self._filter(normalised, config)
        print(
            f"  [arbeitnow] after filtering: {len(filtered)} "
            f"(from {len(normalised)} keyword-matched listings)"
        )
        return filtered

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_pages(self, keywords: list[str]) -> list[dict]:
        """Fetch up to _MAX_PAGES pages, stopping early if no keyword match."""
        all_jobs: list[dict] = []

        for page in range(1, _MAX_PAGES + 1):
            if page > 1:
                time.sleep(_REQUEST_DELAY)

            try:
                payload = get_json(_API_URL, params={"page": page})
            except SourceError as exc:
                logger.warning("arbeitnow: page %d failed — %s", page, exc)
                break

            jobs: list[dict] = payload.get("data", [])
            if not jobs:
                break

            matching = [j for j in jobs if self._matches_keywords(j, keywords)]
            all_jobs.extend(matching)

            # Stop paging if this page had no keyword matches at all.
            if not matching:
                break

        return all_jobs

    def _matches_keywords(self, job: dict, keywords: list[str]) -> bool:
        """Return True if any keyword appears in title, description, or tags."""
        haystack = " ".join([
            job.get("title", ""),
            job.get("description", ""),
            " ".join(job.get("tags", [])),
        ]).lower()
        return any(kw.lower() in haystack for kw in keywords)

    def _normalise(self, job: dict) -> dict:
        """Map a raw Arbeitnow job dict onto our canonical schema."""
        return {
            "source":      self.name,
            "external_id": job.get("slug") or None,
            "title":       job.get("title") or None,
            "company":     job.get("company_name") or None,
            "location":    job.get("location") or None,
            "url":         job.get("url") or None,
            "description": job.get("description") or None,
            "posted_at":   None,    # created_at is ingestion time, not posting date
            "raw":         job,
        }

    def _filter(self, jobs: list[dict], config: Config) -> list[dict]:
        """
        Filter normalised rows for the target city.

        If city filtering leaves fewer than 5 results, relax it and log.
        """
        city = config.target_city.lower()

        city_matched = [
            j for j in jobs
            if j["location"] and city in j["location"].lower()
        ]

        if len(city_matched) >= 5:
            return city_matched

        # Relax the location filter — log clearly so the cycle summary shows it.
        logger.warning(
            "arbeitnow: only %d result(s) matched city '%s'. "
            "Relaxing location filter — returning all %d keyword-matched results.",
            len(city_matched),
            config.target_city,
            len(jobs),
        )
        print(
            f"  [arbeitnow] ⚠  City filter '{config.target_city}' matched only "
            f"{len(city_matched)} result(s). Returning all {len(jobs)} "
            f"keyword-matched listings instead."
        )
        return jobs
