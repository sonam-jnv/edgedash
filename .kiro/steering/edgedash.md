# EdgeDash — Project Steering Rules

These rules apply to every interaction in this project. Follow them without exception.
If a rule must be broken or bent, say so explicitly and wait for confirmation before proceeding.

---

## Project

**EdgeDash** is an autonomous AI career intelligence agent. It runs on a schedule,
fetches live job listings, scores them for fit against a user profile, surfaces skill gaps,
verifies its own output, and publishes results to a Streamlit dashboard.

---

## Architecture

```
Trigger (scheduled)
  └─> Orchestrator
        ├─> Fetcher        (sub-agent)
        ├─> Scorer         (sub-agent)
        └─> GapAnalyzer    (sub-agent)
              └─> Verifier
                    └─> Storage
                          └─> Dashboard (read-only)
```

**Invariants — do not deviate without explicit approval:**

- The Orchestrator reads state and delegates. It never fetches job listings or scores them directly.
- Each sub-agent has exactly one goal and one stop condition.
- The Dashboard is read-only. It never writes to Storage.

---

## Hard Rules

1. **Python 3.11+. Standard library first.**
   Add a third-party dependency only when it genuinely saves real work.
   State the dependency name, the reason, and the alternative considered before adding it.

2. **All storage access goes through a single storage module.**
   No other module may import `sqlite3` (or any DB driver) directly.
   The storage module exposes a thin interface so that swapping SQLite for hosted Postgres
   in week 4 is a one-file change.

3. **Nothing user-specific is hardcoded.**
   Role, city, keywords, skills profile, and any other user data live in `config`
   (a config file or environment-backed config object). Code receives these as parameters.

4. **No secrets in code.**
   All secrets (API keys, tokens, DSNs) are read from environment variables.
   They are loaded in exactly one place (e.g., `config.py` or a dedicated `env.py`).
   Never echo secret values in responses — reference them by key name only.

5. **Every agent run writes a cycle_log row.**
   Schema (minimum): `agent_name`, `run_at`, `records_touched`, `status` (pass/fail),
   `retry_reason` (nullable). The Verifier and Orchestrator also log their runs.

6. **Fail loudly. No silent swallowing of errors.**
   Bare `except: pass` and `except Exception: pass` are forbidden.
   Catch only what you can handle; re-raise or log-and-raise everything else.

7. **Type hints on every function signature.**
   Docstrings only where the intent is not obvious from the name and parameters alone.

8. **Keep files under ~150 lines.**
   Split a module before it approaches that limit, not after.

---

## Style

- Small, testable functions over large monolithic ones.
- Plain, readable Python over clever Python.
- When asked to build one module, build one module — do not scaffold the whole app.
- Prefer explicit over implicit (no magic, no metaprogramming unless unavoidable).
- Imports at the top of the file, standard library before third-party before local.

---

## Network & Sources

9. **Every external source lives behind a `Source` class with a uniform interface.**
   The Fetcher never contains source-specific parsing logic.
   Adding a new source must never require editing the Fetcher — only registering
   a new `Source` implementation.

10. **Every `Source` returns a list of normalised dicts with exactly these keys:**
    `source`, `external_id`, `title`, `company`, `location`, `url`,
    `description`, `posted_at`, `raw`.
    Missing values are `None`. Never empty string, never `"N/A"`.

11. **All network calls go through one shared helper.**
    That helper enforces: 10 s timeout (default), 2-attempt retry with
    exponential backoff, and a real `User-Agent` header.
    No bare `requests.get(...)` anywhere else in the codebase.

12. **A source failing must never kill the cycle.**
    Catch failures per-source, log to `cycle_log` with `status="failed"`,
    and continue to the next source.
    One dead job board must not stop the other sources from running.

13. **Secrets come from environment variables, loaded from a `.env` file.**
    The `.env` file is gitignored.
    Never a literal key in code; never a key in `config.yaml`.
    If a required key is absent, that source skips itself with a clear log
    line — it does not raise or crash the cycle.

14. **Respect the source.**
    Rate-limit to at most 1 request per second per source.
    Set a real `User-Agent` string.
    Honour any documented page or result-count limits.

---

## Intelligence & Scoring

15. **All LLM calls go through one module: `edgedash/llm.py`**, which exposes exactly one public function.
    The provider and model name come from `config`, never hardcoded.
    Rate-limit to stay inside a free tier: 1 request per second by default, 15 per minute maximum.
    No other file in the project may import an LLM SDK directly.

16. **Never ask a model for a score, ranking, or numeric rating.**
    The model extracts structured facts only.
    All scoring arithmetic is deterministic Python in exactly one function.
    The model never sees the scoring weights.

17. **Every model response is validated against an explicit schema before use.**
    A response that fails validation is retried once, then logged as a failure for that listing only —
    it must not crash the cycle or prevent remaining listings from being scored.
    Never call `json.loads` on raw model text without a validation and repair path.

18. **Scoring is idempotent.**
    Never re-score a listing that already has a score.
    Always select listings `WHERE fit_score IS NULL`.
    Cache extraction results keyed on a hash of the job description so the same text is
    never sent to the model twice.

19. **Every score carries a human-readable reason generated from the score components by our code** —
    never free text written by the model.

20. **Log the score distribution to `cycle_log` on every scoring run**: count, min, max, mean, and spread.
    A run where all scores fall within a 10-point range is a suspect run and must be logged as such.

21. **Cap listings scored per cycle at a configurable batch size** (default 25).
    This makes a cost or rate-limit blowup structurally impossible.

---

## Aggregate Analysis

22. **Aggregate analysis is deterministic SQL and Python.**
    No LLM call may produce, adjust, or rank an aggregate number.
    A model may only SUGGEST canonical groupings for a human to approve.

23. **Skill names are canonicalised through an explicit alias map in config.yaml that I own and can read.**
    Never auto-merge skill names by model judgement or string similarity alone.

24. **Gap ranking is weighted by the fit score of the listing the gap came from.**
    A gap in a listing I score 20 on is worth far less than a gap in a listing I score 85 on.
    Never rank gaps by raw frequency alone.

25. **Every gap report run writes a timestamped SNAPSHOT.**
    Never overwrite the previous report.
    Trend over time is a first-class output, not an afterthought.

26. **Every aggregate number must be traceable to the rows that produced it.**
    Any reported gap must be able to list the specific listing IDs it was computed from.
    No number appears in the dashboard that I cannot drill into.

27. **Report the sample size alongside every aggregate.**
    A gap computed from 3 listings and a gap computed from 90 listings must never be presented as equally reliable.

---

## Orchestration

28. **The Orchestrator reads system state and decides which agents to run.**
    It never runs a fixed sequence. Skipping an agent because there is no
    work for it is a SUCCESSFUL outcome, not a failure.

29. **Every delegation carries an explicit goal and an explicit stop condition (max items, max duration).**
    A sub-agent never decides its own limits — the Orchestrator sets them.

30. **The Orchestrator never does an agent's work.**
    It reads state, delegates, collects results, logs. No fetching, scoring, or analysis
    logic in the Orchestrator.

31. **The Orchestrator prints and logs its PLAN before executing it** —
    which agents will run, which are skipped, and the state value that
    caused each decision.

32. **One sub-agent failing does not stop the cycle.**
    Log the failure, continue with the remaining plan, and mark the cycle partial.

33. **Every cycle writes exactly one summary row:**
    what ran, what was skipped, why, duration per agent, and the outcome.

---

## Verification

34. **The Verifier judges output plausibility and NEVER repairs, rewrites, or adjusts data.**
    It returns a verdict and a reason. The Orchestrator decides what to do about a failure.

35. **Verification checks plausibility, never correctness.**
    There is no ground truth for a fit score. Checks assert properties of the output
    distribution and shape, not the accuracy of any single value.

36. **A failed verification triggers at most ONE retry of the failing agent with adjusted context.**
    After that the cycle is marked "degraded" and stops. Never retry in an unbounded loop.

37. **Every verdict is logged to cycle_log with the check that failed and the observed value that failed it** —
    never just "failed".

38. **Only cycles with a passing verdict may be read by the dashboard.**
    A failed cycle must never overwrite the last known-good data. Stale
    verified data always beats fresh unverified data.

39. **Verification thresholds live in config.yaml, not in code**, and every
    threshold has a comment saying what failure it is designed to catch.

---

## Natural Language Queries

40. **NEVER generate SQL from a model.** No text-to-SQL, ever, in any form.
    The model selects from a fixed registry of parameterised query
    functions that I wrote. It never composes a query.

41. **Every query tool is read-only, parameterised, and takes typed
    parameters that are validated and clamped to a safe range before
    execution.** A model-supplied parameter is untrusted input.

42. **The model appears exactly twice per question:** once to ROUTE (pick a
    tool and its parameters) and once to PHRASE (turn returned rows into
    prose). It never touches the database in either call.

43. **The phrasing call may use ONLY the numbers present in the rows it was
    given.** It must not estimate, extrapolate, add outside context, or
    infer a value that is not in the data. If the rows are empty it must
    say so plainly.

44. **Every answer displays the underlying rows alongside it.** No prose
    answer appears without the data that produced it.

45. **If no tool matches the question, say so and list what CAN be asked.**
    Never guess at the closest tool and never answer from general
    knowledge.

46. **Query tools read from the last passing cycle only**, per rule 38.
