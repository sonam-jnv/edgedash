"""
Shared HTTP helper for all EdgeDash sources.

This is the ONLY place in the project that performs an HTTP request.
No source module may call requests.get() directly (steering rule 11).

Public API
----------
get_json(url, params=None, headers=None, timeout=10) -> dict | list
    Make a GET request, retry up to 2 times with exponential backoff,
    and return the decoded JSON body.  Raises SourceError on all failures.
"""

from __future__ import annotations

import time
from typing import Any

# Dependency: requests
# Alternative considered: urllib (stdlib) — viable but error-prone for JSON,
# retries, and header management.  requests saves real work here.
try:
    import requests
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "requests is required: pip install requests==2.32.3"
    ) from exc

# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class SourceError(Exception):
    """Raised when an HTTP request fails after all retry attempts."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_AGENT = "EdgeDash/0.1 (career intelligence agent; contact your-email@example.com)"
_MAX_RETRIES = 2          # total attempts = 1 + _MAX_RETRIES
_BACKOFF_BASE = 1.0       # seconds; doubled on each retry (1s, 2s, ...)


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------

def get_json(
    url: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 10,
) -> dict | list:
    """
    GET *url* and return the decoded JSON body.

    Retries up to _MAX_RETRIES times with exponential backoff.
    Merges the caller's headers with a default User-Agent.

    Raises SourceError if all attempts fail.
    """
    merged_headers = {"User-Agent": _USER_AGENT}
    if headers:
        merged_headers.update(headers)

    last_exc: Exception | None = None

    for attempt in range(1 + _MAX_RETRIES):
        if attempt > 0:
            sleep_seconds = _BACKOFF_BASE * (2 ** (attempt - 1))
            time.sleep(sleep_seconds)

        try:
            response = requests.get(
                url,
                params=params,
                headers=merged_headers,
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json()

        except requests.exceptions.RequestException as exc:
            last_exc = exc

    raise SourceError(
        f"GET {url} failed after {1 + _MAX_RETRIES} attempts. "
        f"Last error: {last_exc}"
    ) from last_exc
