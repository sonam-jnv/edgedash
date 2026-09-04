"""
Deterministic Natural Language Query Tools Registry (Rules 40–46).

Pure Python and deterministic SQL via storage.py only — no LLM calls in this file.
"""

from __future__ import annotations

from typing import Any, Callable

from edgedash import storage
from edgedash.config import load_config
from edgedash.skills import canonical

# ---------------------------------------------------------------------------
# Tool Registry & Decorator
# ---------------------------------------------------------------------------

TOOLS: dict[str, dict[str, Any]] = {}


def tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """
    Register a query tool in the TOOLS dictionary with its JSON-schema spec.
    """
    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        TOOLS[name] = {
            "name": name,
            "description": description.strip(),
            "parameters": parameters,
            "func": func,
        }
        return func
    return decorator


def _clamp_int(val: Any, default: int, min_val: int, max_val: int) -> int:
    """Validate and clamp integer parameter safely against untrusted input (Rule 41)."""
    try:
        if val is None:
            return default
        num = int(val)
        return max(min_val, min(max_val, num))
    except (ValueError, TypeError):
        return default


def _get_db_path() -> str:
    """Retrieve database path from project config."""
    return load_config().db_abs_path


def _get_aliases() -> dict[str, str]:
    """Retrieve skill alias map from project config."""
    try:
        return load_config().skill_aliases
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Registered Query Tools
# ---------------------------------------------------------------------------

@tool(
    name="companies_hiring",
    description=(
        "Returns companies that have posted job listings in the last N days along with their listing counts. "
        "Use this tool when the user asks which companies are hiring, who has open roles, or asks for recent hiring activity."
    ),
    parameters={
        "type": "object",
        "properties": {
            "days": {
                "type": "integer",
                "description": "Number of days in the past to look back (clamped between 1 and 90, default 7).",
                "default": 7,
                "minimum": 1,
                "maximum": 90,
            }
        },
        "required": [],
    },
)
def companies_hiring(days: int = 7, *, db_path: str | None = None) -> dict[str, Any]:
    """Companies with listings posted in the last N days, with counts."""
    clamped_days = _clamp_int(days, default=7, min_val=1, max_val=90)
    path = db_path or _get_db_path()

    if not storage.get_latest_passing_cycle(path):
        return {"summary": "No verified passing cycle found in storage.", "rows": []}

    rows, total_listings = storage.get_companies_hiring(path, days=clamped_days)
    summary = f"{len(rows)} companies with {total_listings} listings posted in the last {clamped_days} days."
    return {"summary": summary, "rows": rows}


@tool(
    name="best_matches",
    description=(
        "Returns the highest-scoring job listings with fit score, job title, company name, and score reason. "
        "Use this tool when the user asks for top matches, best jobs, high fit listings, or recommended roles."
    ),
    parameters={
        "type": "object",
        "properties": {
            "n": {
                "type": "integer",
                "description": "Number of listings to return (clamped between 1 and 25, default 10).",
                "default": 10,
                "minimum": 1,
                "maximum": 25,
            }
        },
        "required": [],
    },
)
def best_matches(n: int = 10, *, db_path: str | None = None) -> dict[str, Any]:
    """Highest-scoring listings with score, title, company, reason."""
    clamped_n = _clamp_int(n, default=10, min_val=1, max_val=25)
    path = db_path or _get_db_path()

    if not storage.get_latest_passing_cycle(path):
        return {"summary": "No verified passing cycle found in storage.", "rows": []}

    rows = storage.get_best_matches(path, n=clamped_n)
    summary = f"Top {len(rows)} highest-scoring job matches."
    return {"summary": summary, "rows": rows}


@tool(
    name="top_gaps",
    description=(
        "Returns top skill gaps ranked by opportunity cost along with number of listings blocked and fit score metrics. "
        "Use this tool when the user asks about skill gaps, missing skills, what skills are holding them back, or highest impact skills to learn."
    ),
    parameters={
        "type": "object",
        "properties": {
            "n": {
                "type": "integer",
                "description": "Number of skill gaps to return (clamped between 1 and 25, default 5).",
                "default": 5,
                "minimum": 1,
                "maximum": 25,
            }
        },
        "required": [],
    },
)
def top_gaps(n: int = 5, *, db_path: str | None = None) -> dict[str, Any]:
    """Top skill gaps by opportunity cost, with listings_blocked."""
    clamped_n = _clamp_int(n, default=5, min_val=1, max_val=25)
    path = db_path or _get_db_path()

    if not storage.get_latest_passing_cycle(path):
        return {"summary": "No verified passing cycle found in storage.", "rows": []}

    rows = storage.get_top_gaps(path, n=clamped_n)
    summary = f"Top {len(rows)} skill gaps by opportunity cost from the latest snapshot."
    return {"summary": summary, "rows": rows}


@tool(
    name="gap_detail",
    description=(
        "Returns the listings blocked by one named skill gap. "
        "Use this tool when the user asks which specific jobs require a named skill or asks to drill down into a skill."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "The exact skill name to inspect.",
            }
        },
        "required": ["skill"],
    },
)
def gap_detail(skill: str, *, db_path: str | None = None) -> dict[str, Any]:
    """The listings blocked by one named skill — Rule 26 drill-down."""
    path = db_path or _get_db_path()
    if not storage.get_latest_passing_cycle(path):
        return {"summary": "No verified passing cycle found in storage.", "rows": []}

    canon_skill = canonical(skill, _get_aliases()) if skill else ""
    known_skills = storage.get_all_known_skills(path)
    if not canon_skill or (canon_skill.lower() not in known_skills and skill.lower() not in known_skills):
        return {"summary": f"Skill '{skill}' is not present in the current dataset.", "rows": []}

    rows = storage.get_gap_detail(path, skill=canon_skill or skill)
    summary = f"{len(rows)} listings blocked by skill gap: '{canon_skill or skill}'."
    return {"summary": summary, "rows": rows}


@tool(
    name="trend",
    description=(
        "Returns skill gap opportunity cost change over N weeks from historical snapshots. "
        "Use this tool when the user asks about skill gap trends, how gaps have changed, or skill trajectory over time."
    ),
    parameters={
        "type": "object",
        "properties": {
            "weeks": {
                "type": "integer",
                "description": "Number of weeks of history to inspect (clamped between 1 and 12, default 3).",
                "default": 3,
                "minimum": 1,
                "maximum": 12,
            }
        },
        "required": [],
    },
)
def trend(weeks: int = 3, *, db_path: str | None = None) -> dict[str, Any]:
    """Gap opportunity_cost change over N weeks from the snapshots."""
    clamped_weeks = _clamp_int(weeks, default=3, min_val=1, max_val=12)
    path = db_path or _get_db_path()

    if not storage.get_latest_passing_cycle(path):
        return {"summary": "No verified passing cycle found in storage.", "rows": []}

    rows = storage.get_gap_trend(path, weeks=clamped_weeks)
    summary = f"Skill gap trend data across {len(rows)} skills over the last {clamped_weeks} weeks."
    return {"summary": summary, "rows": rows}


@tool(
    name="listing_count",
    description=(
        "Returns dataset totals: total listings, scored listings, unscored listings, and newest listing date. "
        "Use this tool when the user asks how many jobs or listings exist, dataset statistics, or sync status."
    ),
    parameters={"type": "object", "properties": {}, "required": []},
)
def listing_count(*, db_path: str | None = None) -> dict[str, Any]:
    """Totals: listings, scored, unscored, newest listing date."""
    path = db_path or _get_db_path()
    if not storage.get_latest_passing_cycle(path):
        return {"summary": "No verified passing cycle found in storage.", "rows": []}

    data = storage.get_listing_counts(path)
    summary = (
        f"Dataset contains {data['total_listings']} total listings "
        f"({data['scored_listings']} scored, {data['unscored_listings']} unscored, "
        f"newest: {data['newest_listing_date'] or 'N/A'})."
    )
    return {"summary": summary, "rows": [data]}


@tool(
    name="skill_demand",
    description=(
        "Returns how often one skill appears in required skills versus nice-to-have skills across listings. "
        "Use this tool when the user asks about the demand or frequency of a specific skill."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "The exact skill name to inspect.",
            }
        },
        "required": ["skill"],
    },
)
def skill_demand(skill: str, *, db_path: str | None = None) -> dict[str, Any]:
    """How often one skill appears in required vs nice_to_have."""
    path = db_path or _get_db_path()
    if not storage.get_latest_passing_cycle(path):
        return {"summary": "No verified passing cycle found in storage.", "rows": []}

    canon_skill = canonical(skill, _get_aliases()) if skill else ""
    known_skills = storage.get_all_known_skills(path)
    if not canon_skill or (canon_skill.lower() not in known_skills and skill.lower() not in known_skills):
        return {"summary": f"Skill '{skill}' was not found in any extracted listings.", "rows": []}

    data = storage.get_skill_demand(path, skill=canon_skill or skill)
    summary = (
        f"Skill '{canon_skill or skill}' appears in {data['required_count']} required "
        f"and {data['nice_to_have_count']} nice-to-have listings."
    )
    return {"summary": summary, "rows": [data]}
