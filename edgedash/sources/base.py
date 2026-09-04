"""
Base contract for every EdgeDash job source.

Every source must satisfy the Source Protocol:
  - name: str          -- stable identifier used in logs, storage, and the registry
  - fetch(config)      -- return a list of normalised row dicts (see REQUIRED_KEYS)

Normalised row schema (steering rule 10)
----------------------------------------
Required keys, every row, every source:
    source          str   -- source name (same as Source.name)
    external_id     str   -- stable ID assigned by the source (slug, job ID, etc.)
    title           str   -- job title
    company         str   -- employer name
    location        str   -- location string as returned by the source
    url             str   -- canonical URL for this listing
    description     str   -- full job description text (may be HTML)
    posted_at       str | None  -- ISO-8601 date/datetime, or None if unknown
    raw             dict  -- the original payload from the source, unmodified

Missing values must be None, not "" and not "N/A".

Registry
--------
Decorate a Source class with @register to add it to SOURCES.  The Fetcher
discovers all registered sources by importing this module and inspecting SOURCES.

    @register
    class MySource:
        name = "my_source"
        def fetch(self, config: Config) -> list[dict]:
            ...
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from edgedash.config import Config


# The required keys every normalised row must contain (values may be None).
REQUIRED_KEYS: tuple[str, ...] = (
    "source",
    "external_id",
    "title",
    "company",
    "location",
    "url",
    "description",
    "posted_at",
    "raw",
)

# Global registry populated by @register.
SOURCES: dict[str, type["Source"]] = {}


def register(cls: type) -> type:
    """Class decorator: add cls to the SOURCES registry keyed by cls.name."""
    if not hasattr(cls, "name") or not isinstance(cls.name, str):
        raise TypeError(f"@register: {cls} must have a string class attribute 'name'")
    SOURCES[cls.name] = cls
    return cls


@runtime_checkable
class Source(Protocol):
    """
    Structural protocol every job source must satisfy.

    fetch() must never raise — catch source-level errors internally and return
    an empty list with a logged warning (or let the Fetcher handle the exception
    boundary via try/except per steering rule 12).
    """

    name: str

    def fetch(self, config: Config) -> list[dict]:
        ...
