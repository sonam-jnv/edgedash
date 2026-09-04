"""
Single door to any language model (steering rule 15).

Public API
----------
complete_json(prompt, schema, *, max_retries=1) -> dict
    Send a prompt, request JSON, validate against schema, return the dict.
    Retries once on parse/validation failure with the error appended.
    Raises LLMError if all attempts fail — callers are responsible for
    catching it per steering rule 17.

Rate limiting (rule 15)
-----------------------
- Minimum 1 second between calls.
- Rolling cap: 15 calls per 60 seconds.
- On HTTP 429 / quota errors: exponential backoff, 3 attempts, then raise.

Providers
---------
Configured via config.llm_provider. Adding a new provider means adding
one entry to _PROVIDERS — complete_json never needs editing.

  "gemini"  — google-generativeai SDK, key from GEMINI_API_KEY env var.
  "ollama"  — local HTTP server (http://localhost:11434), no key required.

CLI self-check
--------------
  python -m edgedash.llm --check
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import warnings
from collections import deque
from pathlib import Path
from typing import Any, Callable

# ---------------------------------------------------------------------------
# Custom exception
# ---------------------------------------------------------------------------

class LLMError(Exception):
    """Raised when a model call fails after all retries."""


# ---------------------------------------------------------------------------
# Rate limiter (shared across all calls in the process)
# ---------------------------------------------------------------------------

class _RateLimiter:
    """
    Enforces two constraints:
      - At least `min_interval` seconds between consecutive calls.
      - At most `max_per_window` calls in any rolling `window_seconds` window.
    Sleeps as needed; never raises.
    """

    def __init__(
        self,
        min_interval: float = 1.0,
        max_per_window: int = 15,
        window_seconds: float = 60.0,
    ) -> None:
        self._min_interval = min_interval
        self._max_per_window = max_per_window
        self._window = window_seconds
        self._last_call: float = 0.0
        self._call_times: deque[float] = deque()

    def acquire(self) -> None:
        now = time.monotonic()

        # 1. Enforce minimum interval between calls.
        elapsed = now - self._last_call
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
            now = time.monotonic()

        # 2. Enforce rolling window cap.
        cutoff = now - self._window
        while self._call_times and self._call_times[0] < cutoff:
            self._call_times.popleft()

        if len(self._call_times) >= self._max_per_window:
            sleep_for = self._call_times[0] - cutoff
            if sleep_for > 0:
                time.sleep(sleep_for)
            now = time.monotonic()
            cutoff = now - self._window
            while self._call_times and self._call_times[0] < cutoff:
                self._call_times.popleft()

        self._last_call = time.monotonic()
        self._call_times.append(self._last_call)


_rate_limiter = _RateLimiter(min_interval=1.0, max_per_window=15, window_seconds=60.0)


# ---------------------------------------------------------------------------
# Environment loader
# ---------------------------------------------------------------------------

def _load_dotenv() -> None:
    """Load .env from candidate paths into os.environ if not already set."""
    candidates = [
        Path(__file__).parent.parent / ".env",
        Path(__file__).parent / ".env",
        Path.cwd() / ".env",
    ]
    for env_path in candidates:
        if env_path.exists():
            with env_path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip().strip('"').strip("'")
                    if key and key not in os.environ:
                        os.environ[key] = value


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_json(text: str) -> dict:
    """
    Strip markdown fences and leading/trailing prose, then parse JSON.
    Raises ValueError with a descriptive message on failure.
    """
    # Try to find a fenced block first.
    fence_match = _FENCE_RE.search(text)
    candidate = fence_match.group(1) if fence_match else text

    # Find the first '{' and last '}' to strip prose around a bare JSON object.
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1:
        raise ValueError(f"No JSON object found in model response. Raw text: {candidate[:300]!r}")

    json_str = candidate[start : end + 1]
    try:
        parsed = json.loads(json_str)
        if not isinstance(parsed, dict):
            raise ValueError(f"Expected JSON object (dict), got {type(parsed).__name__}.")
        return parsed
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON parse error: {exc}. Extracted: {json_str[:300]!r}") from exc


_JSON_TYPE_MAP: dict[str, type | tuple[type, ...]] = {
    "string": str,
    "str": str,
    "integer": int,
    "int": int,
    "number": (int, float),
    "float": float,
    "boolean": bool,
    "bool": bool,
    "object": dict,
    "dict": dict,
    "array": list,
    "list": list,
    "null": type(None),
    "none": type(None),
}


def _resolve_expected_type(raw_type: Any) -> type | tuple[type, ...]:
    if isinstance(raw_type, str):
        return _JSON_TYPE_MAP.get(raw_type.lower(), object)
    if isinstance(raw_type, (list, tuple)):
        resolved_types: list[type] = []
        for item in raw_type:
            res = _resolve_expected_type(item)
            if isinstance(res, tuple):
                resolved_types.extend(res)
            else:
                resolved_types.append(res)
        return tuple(resolved_types)
    if isinstance(raw_type, type):
        return raw_type
    return object


def _validate(data: dict, schema: dict) -> None:
    """
    Minimal schema validation: check required keys and their expected types.

    Supports both Python types (str, list, int) and JSON schema types
    ("string", "object", "null", ["string", "null"]).

    Raises ValueError describing the first violation found.
    """
    required = schema.get("required", [])
    properties = schema.get("properties", {})

    for key in required:
        if key not in data:
            raise ValueError(f"Missing required key '{key}' in model response.")

    for key, rules in properties.items():
        if key not in data:
            continue
        raw_type = rules.get("type")
        val = data[key]
        if raw_type is not None:
            expected_type = _resolve_expected_type(raw_type)
            if expected_type is int and isinstance(val, bool):
                raise ValueError(f"Key '{key}': expected int, got bool.")
            if not isinstance(val, expected_type):
                if isinstance(expected_type, tuple):
                    type_names = " | ".join(
                        t.__name__ if hasattr(t, "__name__") else str(t)
                        for t in expected_type
                    )
                else:
                    type_names = (
                        expected_type.__name__
                        if hasattr(expected_type, "__name__")
                        else str(expected_type)
                    )
                raise ValueError(
                    f"Key '{key}': expected {type_names}, "
                    f"got {type(val).__name__}."
                )
        enum_values = rules.get("enum")
        if enum_values is not None and val not in enum_values:
            raise ValueError(f"Key '{key}': value {val!r} not in enum {enum_values}.")



# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------
# Each provider is a callable: (model: str, prompt: str) -> str (raw response text)

def _call_gemini(model: str, prompt: str) -> str:
    """Call Google Gemini via the google-generativeai SDK."""
    _load_dotenv()
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", category=FutureWarning)
            import google.generativeai as genai  # type: ignore[import]
    except ModuleNotFoundError as exc:
        raise LLMError(
            "google-generativeai is required for the gemini provider: "
            "pip install google-generativeai"
        ) from exc

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise LLMError(
            "GEMINI_API_KEY environment variable is not set. "
            "Add it to your .env file (see .env.example)."
        )

    genai.configure(api_key=api_key)
    client = genai.GenerativeModel(model)

    try:
        response = client.generate_content(prompt)
    except Exception as exc:
        msg = str(exc)
        msg_lower = msg.lower()
        if (
            "429" in msg
            or "quota" in msg_lower
            or "resource_exhausted" in msg_lower
            or "rate limit" in msg_lower
            or "rate-limit" in msg_lower
        ):
            raise LLMError(f"429: Gemini quota/rate-limit error — {msg}") from exc
        raise LLMError(f"Gemini API error: {msg}") from exc

    return response.text


def _call_ollama(model: str, prompt: str) -> str:
    """Call a local Ollama server via its HTTP API (no key required)."""
    import urllib.request
    import urllib.error

    url = "http://localhost:11434/api/generate"
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False}).encode()

    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = json.loads(resp.read().decode())
            return body.get("response", "")
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            raise LLMError(f"429: Ollama rate-limit error — {exc}") from exc
        raise LLMError(f"Ollama HTTP error {exc.code}: {exc}") from exc
    except OSError as exc:
        raise LLMError(
            f"Cannot reach Ollama at {url}. Is 'ollama serve' running? Error: {exc}"
        ) from exc


# Map provider name -> callable
_PROVIDERS: dict[str, Callable[[str, str], str]] = {
    "gemini": _call_gemini,
    "ollama": _call_ollama,
}


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------

def complete_json(
    prompt: str,
    schema: dict,
    *,
    max_retries: int = 1,
) -> dict:
    """
    Send `prompt` to the configured LLM, parse the response as JSON, and
    validate it against `schema`.

    On parse/validation failure the call is retried `max_retries` times
    (default 1) with the validation error appended to the prompt.

    On HTTP 429 / quota errors the call is retried up to 3 times with
    exponential backoff regardless of `max_retries`.

    Raises LLMError if all attempts fail. Never returns partial or
    unvalidated data.
    """
    from edgedash.config import load_config  # local import to avoid circular dep

    cfg = load_config()

    provider_name = cfg.llm_provider
    model = cfg.llm_model

    if provider_name not in _PROVIDERS:
        raise LLMError(
            f"Unknown llm_provider '{provider_name}'. "
            f"Supported: {sorted(_PROVIDERS)}. "
            "Add a new entry to _PROVIDERS in edgedash/llm.py."
        )

    call_fn = _PROVIDERS[provider_name]

    # Build the base prompt — instruct for JSON-only output up front.
    json_instruction = (
        "\n\nRespond with a single JSON object only. "
        "No markdown code fences, no prose before or after the JSON."
    )
    current_prompt = prompt + json_instruction

    last_error: Exception | None = None

    for attempt in range(max_retries + 1):
        if attempt > 0:
            # Append the previous error so the model knows what to fix.
            current_prompt = (
                prompt
                + json_instruction
                + f"\n\nYour previous response failed validation: {last_error}. "
                "Reply with a valid JSON object only — no markdown, no prose."
            )

        raw_text = _call_with_quota_backoff(call_fn, model, current_prompt)

        try:
            data = _extract_json(raw_text)
            _validate(data, schema)
            return data
        except ValueError as exc:
            last_error = exc
            # Loop to retry with error appended.

    raise LLMError(
        f"Model response failed validation after {max_retries + 1} attempt(s). "
        f"Last error: {last_error}"
    )


# ---------------------------------------------------------------------------
# Quota backoff wrapper (3 attempts, independent of max_retries)
# ---------------------------------------------------------------------------

def _call_with_quota_backoff(
    call_fn: Callable[[str, str], str],
    model: str,
    prompt: str,
    *,
    quota_attempts: int = 3,
) -> str:
    """
    Call call_fn(model, prompt) with exponential backoff on 429/quota errors.
    Raises LLMError after `quota_attempts` failures.
    """
    for quota_attempt in range(quota_attempts):
        _rate_limiter.acquire()
        try:
            return call_fn(model, prompt)
        except LLMError as exc:
            msg = str(exc)
            if "429" in msg or "quota" in msg.lower():
                if quota_attempt < quota_attempts - 1:
                    backoff = 2 ** quota_attempt * 5  # 5s, 10s, 20s
                    print(
                        f"  [!] LLM quota/rate-limit on attempt "
                        f"{quota_attempt + 1}/{quota_attempts}. "
                        f"Sleeping {backoff}s...",
                        file=sys.stderr,
                    )
                    time.sleep(backoff)
                    continue
            raise  # non-quota errors, or final quota attempt

    raise LLMError("Quota backoff exhausted.")


# ---------------------------------------------------------------------------
# CLI self-check: python -m edgedash.llm --check
# ---------------------------------------------------------------------------

def _cli_check() -> None:
    """Send one trivial prompt and report success or failure."""
    from edgedash.config import load_config

    _load_dotenv()
    cfg = load_config()

    print(f"Provider : {cfg.llm_provider}")
    print(f"Model    : {cfg.llm_model}")
    print("Sending  : trivial schema-extraction prompt...")

    test_prompt = (
        'Extract the following fields from this sentence: '
        '"The product name is Acme and the version is 3.14." '
        'Return JSON with keys "product" (string) and "version" (string).'
    )
    test_schema = {
        "required": ["product", "version"],
        "properties": {
            "product": {"type": str},
            "version": {"type": str},
        },
    }

    try:
        result = complete_json(test_prompt, test_schema)
        print(f"Result   : {result}")
        print("Status   : [OK] Successful")
    except LLMError as exc:
        print(f"Status   : [FAILED] - {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    if "--check" in sys.argv:
        _cli_check()
    else:
        print("Usage: python -m edgedash.llm --check", file=sys.stderr)
        sys.exit(1)
