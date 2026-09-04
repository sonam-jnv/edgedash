"""
Deterministic fit scoring (steering rules 16, 19).

All numeric scoring lives in compute_fit_score — the LLM never sees weights.
"""

from __future__ import annotations

from typing import Any

from edgedash.skills import canonical


def compute_fit_score(
    *,
    title: str,
    extraction: dict[str, Any],
    my_skills: list[str],
    experience_years: int,
    target_role: str,
    keywords: list[str],
    skill_aliases: dict[str, str],
    widen_distribution: bool = False,
) -> tuple[int, str]:
    """
    Return (fit_score 0–100, human-readable reason) from extracted facts.

    Weights (max points):
      required skills overlap  40
      nice-to-have overlap   10
      experience match         20
      seniority alignment    15
      role / keyword match     15

    When widen_distribution is True (Rule 36 strict retry mode), non-matches and
    missing requirements are penalized more sharply, expanding the score spread
    and increasing distribution variance.
    """
    my_canonical = {
        canonical(s, skill_aliases) for s in my_skills if canonical(s, skill_aliases)
    }

    req_raw = extraction.get("required_skills") or []
    nice_raw = extraction.get("nice_to_have") or []
    req_canonical = {canonical(s, skill_aliases) for s in req_raw if canonical(s, skill_aliases)}
    nice_canonical = {canonical(s, skill_aliases) for s in nice_raw if canonical(s, skill_aliases)}

    matched_req = req_canonical & my_canonical
    matched_nice = nice_canonical & my_canonical

    if req_canonical:
        if widen_distribution:
            # Quadratic scaling to separate strong matches from mediocre ones
            match_ratio = len(matched_req) / len(req_canonical)
            req_pts = int(round(40 * (match_ratio ** 1.8)))
            req_note = f"required {len(matched_req)}/{len(req_canonical)} (strict +{req_pts})"
        else:
            req_pts = int(round(40 * len(matched_req) / len(req_canonical)))
            req_note = f"required {len(matched_req)}/{len(req_canonical)} (+{req_pts})"
    else:
        req_pts = 5 if widen_distribution else 20
        req_note = f"required n/a (+{req_pts})"

    nice_mult = 3 if widen_distribution else 2
    nice_pts = min(10, len(matched_nice) * nice_mult)
    nice_note = f"nice-to-have {len(matched_nice)} (+{nice_pts})"

    years_required = extraction.get("years_required")
    if years_required is None:
        exp_pts = 5 if widen_distribution else 10
        exp_note = f"experience n/a (+{exp_pts})"
    elif experience_years >= years_required:
        exp_pts = 20
        exp_note = f"experience {experience_years}yr≥{years_required}yr (+20)"
    elif experience_years >= years_required - 1:
        exp_pts = 6 if widen_distribution else 12
        exp_note = f"experience {experience_years}yr≈{years_required}yr (+{exp_pts})"
    else:
        gap = years_required - experience_years
        exp_pts = max(0, 20 - gap * (12 if widen_distribution else 8))
        exp_note = f"experience {experience_years}yr<{years_required}yr (+{exp_pts})"

    seniority = str(extraction.get("seniority") or "unknown").lower()
    user_level = _level_from_years(experience_years)
    if seniority == "unknown":
        sen_pts = 4 if widen_distribution else 8
        sen_note = f"seniority unknown (+{sen_pts})"
    elif seniority == user_level:
        sen_pts = 15
        sen_note = f"seniority {seniority} (+15)"
    elif _levels_compatible(user_level, seniority):
        sen_pts = 5 if widen_distribution else 10
        sen_note = f"seniority {seniority}≈{user_level} (+{sen_pts})"
    else:
        sen_pts = 0 if widen_distribution else 3
        sen_note = f"seniority {seniority}≠{user_level} (+{sen_pts})"

    title_lower = (title or "").lower()
    role_pts = 0
    role_bits: list[str] = []
    if target_role and target_role.lower() in title_lower:
        role_pts += 10
        role_bits.append("title match")
    kw_hits = sum(1 for kw in keywords if kw.lower() in title_lower)
    if kw_hits:
        role_pts += min(5, kw_hits * (3 if widen_distribution else 2))
        role_bits.append(f"{kw_hits} keyword(s)")
    role_pts = min(15, role_pts)
    role_note = f"role {', '.join(role_bits) or 'none'} (+{role_pts})"

    total = max(0, min(100, req_pts + nice_pts + exp_pts + sen_pts + role_pts))
    reason = "; ".join([req_note, nice_note, exp_note, sen_note, role_note]) + f" → {total}"
    return total, reason



def _level_from_years(years: int) -> str:
    if years <= 2:
        return "junior"
    if years <= 5:
        return "mid"
    if years <= 8:
        return "senior"
    return "lead"


def _levels_compatible(user: str, job: str) -> bool:
    order = ["junior", "mid", "senior", "lead"]
    try:
        return abs(order.index(user) - order.index(job)) == 1
    except ValueError:
        return False
