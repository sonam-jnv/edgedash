"""
Load and validate the EdgeDash configuration from config.yaml at the repo root.

Usage:
    from edgedash.config import load_config
    cfg = load_config()          # reads <repo_root>/config.yaml
    cfg = load_config("my.yaml") # explicit path
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# PyYAML is the only practical choice for human-editable YAML in the stdlib-free
# world; pyyaml is a one-liner install and far less error-prone than rolling a
# parser. Alternative considered: tomllib (stdlib in 3.11) but TOML lists are
# less readable for keyword arrays and the user asked for YAML.
try:
    import yaml
except ModuleNotFoundError as exc:
    raise ModuleNotFoundError(
        "PyYAML is required: pip install pyyaml"
    ) from exc

# ---------------------------------------------------------------------------
# Defaults – every field has a fallback so a minimal config.yaml still works.
# ---------------------------------------------------------------------------
_DEFAULTS: dict[str, Any] = {
    "target_role": "Software Engineer",
    "target_city": "Remote",
    "keywords": [],
    "my_skills": [],
    "experience_years": 0,
    "db_path": "edgedash.db",
    "min_fit_score": 50,
    "sources": ["arbeitnow"],
    "use_mock_fetcher": False,
    "llm_provider": "gemini",
    "llm_model": "gemini-2.5-flash",
    "llm_score_batch_size": 25,
    "skill_aliases": {},
    "min_score_spread": 10,
    "min_score_stdev": 5.0,
    "max_empty_extraction_pct": 20.0,
    "max_skills_per_listing": 20,
    "min_gap_sample": 3,
    "max_data_age_days": 3,
}


@dataclass
class Config:
    target_role: str
    target_city: str
    keywords: list[str]
    my_skills: list[str]
    experience_years: int
    db_path: str
    min_fit_score: int = 50
    sources: list[str] = field(default_factory=lambda: ["arbeitnow"])
    use_mock_fetcher: bool = False
    llm_provider: str = "gemini"
    llm_model: str = "gemini-2.5-flash"
    llm_score_batch_size: int = 25
    skill_aliases: dict[str, str] = field(default_factory=dict)
    min_score_spread: int = 10
    min_score_stdev: float = 5.0
    max_empty_extraction_pct: float = 20.0
    max_skills_per_listing: int = 20
    min_gap_sample: int = 3
    max_data_age_days: int = 3

    # Convenience: resolved absolute path to the DB file.
    @property
    def db_abs_path(self) -> str:
        return str(Path(self.db_path).resolve())


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def _repo_root() -> Path:
    """Return the directory that contains this package (i.e. the repo root)."""
    return Path(__file__).parent.parent


def load_config(path: str | None = None) -> Config:
    """
    Read config.yaml and return a validated Config.

    Raises FileNotFoundError if the file does not exist.
    Raises ValueError if a field has the wrong type.
    """
    config_path = Path(path) if path else _repo_root() / "config.yaml"

    if not config_path.exists():
        raise FileNotFoundError(
            f"config.yaml not found at {config_path}. "
            "Copy config.yaml.example to config.yaml and fill in your profile."
        )

    with config_path.open("r", encoding="utf-8") as fh:
        raw: dict[str, Any] = yaml.safe_load(fh) or {}

    data = {**_DEFAULTS, **raw}

    # ---- type validation ------------------------------------------------
    _expect_str(data, "target_role")
    _expect_str(data, "target_city")
    _expect_str(data, "db_path")
    _expect_list_of_str(data, "keywords")
    _expect_list_of_str(data, "my_skills")
    _expect_list_of_str(data, "sources")
    _expect_int(data, "experience_years")
    _expect_int(data, "min_fit_score")
    _expect_int(data, "llm_score_batch_size")
    _expect_bool(data, "use_mock_fetcher")
    _expect_str(data, "llm_provider")
    _expect_str(data, "llm_model")

    return Config(
        target_role=data["target_role"],
        target_city=data["target_city"],
        keywords=data["keywords"],
        my_skills=data["my_skills"],
        experience_years=data["experience_years"],
        db_path=data["db_path"],
        min_fit_score=data["min_fit_score"],
        sources=data["sources"],
        use_mock_fetcher=data["use_mock_fetcher"],
        llm_provider=data["llm_provider"],
        llm_model=data["llm_model"],
        llm_score_batch_size=data["llm_score_batch_size"],
        skill_aliases=data.get("skill_aliases") or {},
        min_score_spread=int(data.get("min_score_spread", 10)),
        min_score_stdev=float(data.get("min_score_stdev", 5.0)),
        max_empty_extraction_pct=float(data.get("max_empty_extraction_pct", 20.0)),
        max_skills_per_listing=int(data.get("max_skills_per_listing", 20)),
        min_gap_sample=int(data.get("min_gap_sample", 3)),
        max_data_age_days=int(data.get("max_data_age_days", 3)),
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _expect_str(data: dict[str, Any], key: str) -> None:
    if not isinstance(data[key], str):
        raise ValueError(f"config.yaml: '{key}' must be a string, got {type(data[key]).__name__}")


def _expect_int(data: dict[str, Any], key: str) -> None:
    if not isinstance(data[key], int):
        raise ValueError(f"config.yaml: '{key}' must be an integer, got {type(data[key]).__name__}")


def _expect_list_of_str(data: dict[str, Any], key: str) -> None:
    val = data[key]
    if not isinstance(val, list) or not all(isinstance(item, str) for item in val):
        raise ValueError(f"config.yaml: '{key}' must be a list of strings")


def _expect_bool(data: dict[str, Any], key: str) -> None:
    if not isinstance(data[key], bool):
        raise ValueError(f"config.yaml: '{key}' must be a boolean (true/false), got {type(data[key]).__name__}")
