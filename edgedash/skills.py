"""
Deterministic skill canonicalisation and audit tools (Rules 22, 23).

Pure Python and SQL only — no LLM calls in this file.
"""

from __future__ import annotations

import argparse
import collections
import re
import sys
from typing import Any

from edgedash import storage
from edgedash.config import load_config

# Punctuation to strip from ends of skill strings (excluding + and # for C++, C#)
_STRIP_PUNCT = " \t\n\r'\"`.,;:!?*~-_/\\|()[]{}"
_PAREN_REGEX = re.compile(r"\(.*?\)")


def canonical(raw: str, aliases: dict[str, str] | None = None) -> str:
    """
    Canonicalise a skill string deterministically.

    1. Lowercase.
    2. Drop parenthetical qualifiers (e.g. 'kubernetes (eks)' -> 'kubernetes').
    3. Strip whitespace and surrounding punctuation.
    4. Collapse internal whitespace.
    5. Apply alias map lookup.
    """
    if not raw or not isinstance(raw, str):
        return ""

    s = raw.lower()
    s = _PAREN_REGEX.sub(" ", s)
    s = s.strip(_STRIP_PUNCT)
    s = " ".join(s.split())
    s = s.strip(_STRIP_PUNCT)

    if not s:
        return ""

    if aliases:
        return aliases.get(s, s)

    return s


def audit_skills(db_path: str, aliases: dict[str, str]) -> None:
    """
    Read all extracted required_skills from the database and print:
    - Top 40 most common raw skills with counts and canonical mappings
    - List of raw skills seen only once (candidate typos / junk)
    """
    raw_skills = storage.get_all_extracted_required_skills(db_path)

    if not raw_skills:
        print(f"No extracted skills found in database at '{db_path}'.")
        print("Run extraction / scoring cycles first to populate extraction_cache.")
        return

    counts = collections.Counter(raw_skills)
    total_occurrences = len(raw_skills)
    unique_raw = len(counts)

    print("=" * 80)
    print(f"SKILL AUDIT REPORT (Total extractions: {total_occurrences}, Unique raw: {unique_raw})")
    print("=" * 80)
    print()

    # 1. Top 40 most common raw skills
    top_40 = counts.most_common(40)
    print("TOP 40 MOST COMMON RAW SKILLS:")
    print("-" * 80)
    print(f"{'#':<4} {'COUNT':<7} {'RAW SKILL STRING':<35} -> {'CANONICAL FORM'}")
    print("-" * 80)
    for idx, (raw_skill, count) in enumerate(top_40, 1):
        canon = canonical(raw_skill, aliases)
        print(f"{idx:<4} {count:<7} {raw_skill:<35} -> {canon}")
    print("-" * 80)
    print()

    # 2. Raw skills seen only once
    singletons = sorted([skill for skill, count in counts.items() if count == 1])
    print("=" * 80)
    print(f"RAW SKILLS SEEN ONLY ONCE ({len(singletons)} items) — [Typos / Junk / Missing Aliases]:")
    print("=" * 80)
    if not singletons:
        print("  (None found)")
    else:
        for skill in singletons:
            canon = canonical(skill, aliases)
            if canon != skill.lower().strip():
                print(f"  • {skill!r} -> maps to: {canon!r}")
            else:
                print(f"  • {skill!r}")
    print("-" * 80)


def main() -> None:
    parser = argparse.ArgumentParser(description="Skill canonicalisation and audit tool.")
    parser.add_argument(
        "--audit",
        action="store_true",
        help="Audit extracted required_skills in the database (read-only).",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to config.yaml (defaults to repo root config.yaml).",
    )
    args = parser.parse_args()

    if args.audit:
        cfg = load_config(args.config)
        audit_skills(cfg.db_abs_path, cfg.skill_aliases)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
