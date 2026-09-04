"""
Extract structured factual requirements from job listings (Rule 15, 16, 17, 18).

The extractor is the ONLY component in scoring that interfaces with an LLM.
It extracts structured facts (skills, seniority, years, remote) without any
knowledge of the candidate profile or scoring weights.
"""

from __future__ import annotations

import hashlib
from typing import Any

from edgedash import llm, storage
from edgedash.config import load_config

# ---------------------------------------------------------------------------
# Extraction Schema (Rule 16: No score fields allowed)
# ---------------------------------------------------------------------------

VALID_SENIORITY = {"junior", "mid", "senior", "lead", "unknown"}

EXTRACTION_SCHEMA: dict[str, Any] = {
    "required": [
        "required_skills",
        "nice_to_have",
        "seniority",
        "years_required",
        "remote_ok",
    ],
    "properties": {
        "required_skills": {"type": list},
        "nice_to_have": {"type": list},
        "seniority": {"type": str},
        "years_required": {"type": (int, type(None))},
        "remote_ok": {"type": (bool, type(None))},
    },
}

EXTRACTION_PROMPT_TEMPLATE = """You are an objective document reader extracting structured facts from a job listing.
Extract ONLY factual information explicitly stated in the job description below.

STRICT EXTRACTION INSTRUCTIONS:
- Extract ONLY what is explicitly stated in the text.
- Do NOT infer, do not guess, and do not extrapolate.
- If a detail is not explicitly stated in the text, use null (or an empty list [] for skill lists).
- Extract EXACTLY these 5 fields as a single JSON object:
  1. "required_skills": list of strings (technical skills, tools, programming languages, or platforms explicitly required). Empty list [] if none stated.
  2. "nice_to_have": list of strings (skills or tools stated as preferred, optional, bonus, or nice-to-have). Empty list [] if none stated.
  3. "seniority": exactly one of "junior", "mid", "senior", "lead", or "unknown". If not clearly stated, use "unknown".
  4. "years_required": integer (minimum years of experience explicitly required), or null if not explicitly stated. Never guess.
  5. "remote_ok": boolean (true if remote work is explicitly permitted, false if on-site only is explicitly required), or null if not stated.

JOB TITLE: {title}
COMPANY: {company}
LOCATION: {location}

JOB DESCRIPTION:
{description}"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def compute_description_hash(text: str) -> str:
    """Return a stable SHA-256 hash of the normalized description text."""
    normalized = (text or "").strip().encode("utf-8")
    return hashlib.sha256(normalized).hexdigest()


def _normalize_skills(skills: list[Any]) -> list[str]:
    """Convert skills to unique lowercase stripped strings while preserving order."""
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in skills:
        if isinstance(item, str):
            s = item.strip().lower()
            if s and s not in seen:
                seen.add(s)
                cleaned.append(s)
    return cleaned


def _normalize_extraction(data: dict[str, Any]) -> dict[str, Any]:
    """Ensure skills are lowercase and seniority is a valid enum value."""
    req_skills = _normalize_skills(data.get("required_skills", []))
    nice_skills = _normalize_skills(data.get("nice_to_have", []))

    seniority = str(data.get("seniority", "unknown")).strip().lower()
    if seniority not in VALID_SENIORITY:
        seniority = "unknown"

    years = data.get("years_required")
    if years is not None:
        try:
            years = int(years)
        except (ValueError, TypeError):
            years = None

    remote = data.get("remote_ok")
    if not isinstance(remote, bool):
        remote = None

    return {
        "required_skills": req_skills,
        "nice_to_have": nice_skills,
        "seniority": seniority,
        "years_required": years,
        "remote_ok": remote,
    }


# ---------------------------------------------------------------------------
# Public extraction function
# ---------------------------------------------------------------------------

def extract(listing: dict[str, Any], db_path: str | None = None) -> dict[str, Any]:
    """
    Extract structured facts from a listing description with caching.

    1. Computes description hash.
    2. Checks extraction cache in DB (storage module) first.
    3. On miss, calls llm.complete_json, normalizes skill names to lowercase,
       saves to cache via storage module, and returns the result.
    """
    if db_path is None:
        db_path = load_config().db_abs_path

    # Ensure database tables exist (safe and idempotent)
    storage.init_db(db_path)

    desc = listing.get("description") or ""
    desc_hash = compute_description_hash(desc)

    # 1. Check cache first
    cached = storage.get_cached_extraction(db_path, desc_hash)
    if cached is not None:
        return cached

    # 2. On miss, call LLM
    prompt = EXTRACTION_PROMPT_TEMPLATE.format(
        title=listing.get("title", "Unknown"),
        company=listing.get("company", "Unknown"),
        location=listing.get("location", "Unknown"),
        description=desc,
    )

    raw_result = llm.complete_json(prompt, EXTRACTION_SCHEMA)
    normalized = _normalize_extraction(raw_result)

    # 3. Store in cache and return
    storage.save_cached_extraction(db_path, desc_hash, normalized)
    return normalized
