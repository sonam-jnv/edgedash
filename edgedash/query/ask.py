"""
Two-call natural language query pipeline (Rules 40–46).

ROUTE (pick tool) -> EXECUTE (deterministic Python/SQL) -> PHRASE (turn rows into prose).
No LLM ever composes or touches SQL.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any

from edgedash import llm, storage
from edgedash.config import load_config
from edgedash.query.tools import TOOLS


@dataclass(frozen=True)
class Answer:
    """Structure returned by ask()."""
    text: str
    rows: list[dict[str, Any]] = field(default_factory=list)
    tool_used: str | None = None
    params: dict[str, Any] = field(default_factory=dict)


ROUTER_SCHEMA = {
    "type": "object",
    "properties": {
        "tool": {"type": ["string", "null"]},
        "params": {"type": "object"},
        "confidence": {"type": "string", "enum": ["high", "low"]},
    },
    "required": ["tool", "params", "confidence"],
}

PHRASE_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
    },
    "required": ["answer"],
}


def _build_unanswerable_text() -> str:
    """Format fixed fallback message listing available queries (Rule 45)."""
    lines = [
        "I cannot answer that question from the available dataset. "
        "Here are the questions you can ask:"
    ]
    for name, t in TOOLS.items():
        lines.append(f"• **{name}**: {t['description']}")
    return "\n".join(lines)


def _build_routing_prompt(question: str) -> str:
    tools_formatted = []
    for name, t in TOOLS.items():
        tools_formatted.append(
            f"Tool: {name}\n"
            f"Description: {t['description']}\n"
            f"Parameters: {json.dumps(t['parameters'], indent=2)}"
        )
    tools_block = "\n\n".join(tools_formatted)

    return f"""You are a strict query router. Your only job is to match a user question to an exact available query tool from the registry below and extract its parameters.

AVAILABLE TOOLS:
{tools_block}

USER QUESTION:
"{question}"

INSTRUCTIONS:
1. Review the user question and the available tools.
2. If an available tool directly and accurately answers the question, choose that tool and extract its parameters into "params".
3. CRITICAL (Rule 45): If NO tool in the registry directly matches the question, you MUST set "tool": null and "params": {{}}.
4. NEVER pick a tool that is merely 'close', approximate, or partially related.
5. NEVER invent tools, generate SQL, or answer from general knowledge.
6. Rate your routing confidence as "high" or "low".

Respond with a JSON object matching this schema:
{{
  "tool": "<tool_name>" | null,
  "params": {{ ... }},
  "confidence": "high" | "low"
}}"""


def _build_phrasing_prompt(question: str, summary: str, rows: list[dict[str, Any]]) -> str:
    rows_json = json.dumps(rows, indent=2)
    return f"""You are phrasing the answer to a user's question based strictly on data returned from our verified query engine.

USER QUESTION:
"{question}"

DATA SUMMARY:
"{summary}"

DATA ROWS:
{rows_json}

INSTRUCTIONS (Rules 42, 43):
1. Write a clear, concise prose answer (2–3 sentences) answering the user's question.
2. Incorporate the context from the data summary (e.g. "{summary}").
3. CRITICAL: Use ONLY facts and numbers directly present in the data rows above.
4. DO NOT extrapolate, estimate, speculate, or introduce outside knowledge.
5. If the data rows are empty, state clearly that the verified database contains no matching records for this query.

Respond with a JSON object matching this schema:
{{
  "answer": "Your 2-3 sentence answer here."
}}"""


def ask(question: str, *, db_path: str | None = None) -> Answer:
    """
    Execute natural language query against verified database data (Rules 40–46).
    """
    start_time = time.perf_counter()
    path = db_path or load_config().db_abs_path
    storage.init_db(path)

    # 1. ROUTE (First model call)
    route_prompt = _build_routing_prompt(question)
    route_resp = llm.complete_json(route_prompt, ROUTER_SCHEMA)

    tool_name = route_resp.get("tool")
    raw_params = route_resp.get("params") or {}

    # Handle unanswerable query (Rule 45 - fixed message, no phrasing call)
    if not tool_name:
        duration_ms = (time.perf_counter() - start_time) * 1000.0
        storage.log_query(
            path,
            question=question,
            tool_chosen=None,
            params=raw_params,
            answerable=False,
            duration_ms=duration_ms,
        )
        return Answer(
            text=_build_unanswerable_text(),
            rows=[],
            tool_used=None,
            params={},
        )

    # Validate tool is in registry (hard error if invalid)
    if tool_name not in TOOLS:
        raise ValueError(f"Router returned unknown tool '{tool_name}' not in TOOLS registry.")

    # 2. EXECUTE (Deterministic query function)
    tool_entry = TOOLS[tool_name]
    tool_fn = tool_entry["func"]

    # Call tool passing db_path
    tool_result = tool_fn(**raw_params, db_path=path)
    rows = tool_result.get("rows", [])
    summary = tool_result.get("summary", "")

    # 3. PHRASE (Second model call)
    phrase_prompt = _build_phrasing_prompt(question, summary, rows)
    phrase_resp = llm.complete_json(phrase_prompt, PHRASE_SCHEMA)
    answer_text = phrase_resp.get("answer", summary)

    duration_ms = (time.perf_counter() - start_time) * 1000.0
    storage.log_query(
        path,
        question=question,
        tool_chosen=tool_name,
        params=raw_params,
        answerable=True,
        duration_ms=duration_ms,
    )

    return Answer(
        text=answer_text,
        rows=rows,
        tool_used=tool_name,
        params=raw_params,
    )
