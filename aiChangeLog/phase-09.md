# Phase 09 — SQLite Job Persistence + LLM Task Classifier + Playwright Install

## Goals
1. Persist the in-memory `_jobs` dict to SQLite so job history survives API restarts.
2. Replace keyword-based `_detect_task_type()` with an LLM zero-shot classifier (keyword fallback on failure).
3. Install Playwright Chromium browser binary so the `WebTool` can use headless browsing.

---

## Changes

### New file: `api/job_store.py`
- `JobStore` class — SQLite-backed, thread-safe, write-through with in-memory hot cache.
- Schema: `jobs` table with `job_id`, `status`, `phase`, `task_type`, `summary`, `files_created` (JSON),
  `error`, `session_id`, `task`, `full_response`, `created_at`, `updated_at`.
- `create()`, `get()`, `update()`, `list_jobs()` public API.
- On `load()` (called at startup): stale `running` jobs are marked `failed`; expired jobs (>24 h) are pruned.
- TTL expiry also triggered on each `create()` call — no unbounded growth.

### New file: `config/task_classifier.yaml`
- Zero-shot classification prompt for the 6 task types: `develop`, `review`, `test`, `architect`,
  `research`, `chat`.
- `timeout_seconds: 3` — LLM call is capped; any failure falls through to keyword method.
- Config-driven per project architecture rules; no hardcoded prompts in Python.

### Modified: `agent/orchestrator.py`
- `_detect_task_type()` → renamed `_detect_task_type_keyword()` (unchanged logic).
- New `_detect_task_type_llm()` — async, loads classifier YAML, calls active model with 3 s timeout,
  validates response against `valid_types`, raises on bad output.
- New `_detect_task_type()` — async wrapper: tries LLM, logs and falls back to keyword on any exception.
- `run_task()`: `task_type = await self._detect_task_type(task)` (was sync call).

### Modified: `api/main.py`
- Import `JobStore`; replace `_jobs: Dict[str, dict]` with `_job_store = JobStore("data/jobs.db")`.
- `startup_event()`: calls `_job_store.load()` before agent init.
- `start_task_background()`: uses `_job_store.create()` / `_job_store.update()`; `await`s
  `_detect_task_type()` (now async).
- `get_job_status()`, `get_job_result()`, `cancel_job()`: use `_job_store.get()` / `_job_store.update()`.
- New `GET /jobs` endpoint — paginated job list (limit/offset), no `full_response` field.

### Playwright Chromium
- `pip install playwright && python -m playwright install chromium` run in project Python environment.
- Binary installed to `%APPDATA%\Local\ms-playwright\chromium-1208`.
- `WebTool` Playwright path is now unblocked.

---

## Architecture Notes
- `JobStore` is an API-layer concern only — not coupled to `SessionMemory` or any agent layer.
- All `files_created` lists are serialized as JSON text in SQLite; deserialized back on read.
- The `_full_response` field is never exposed by `GET /jobs` or `GET /task/{job_id}` — only via
  `GET /task/{job_id}/result` for completed jobs.

---

## Known Remaining Issues (carry-forward from phase-08)
- Job TTL is 24 h hard-coded in `job_store.py`; could be env-configurable.
- `_detect_task_type_llm()` waits for the LLM on every task — if the model is slow to respond
  (even within 3 s), the `/task/start` endpoint will feel sluggish. Consider a background
  pre-classification approach in future.
- `memory_wiki.py` lint() only checks orphans — contradiction detection not implemented.
- Prometheus metrics only collect process-level stats — no auto-instrumentation middleware.
- Subagent results not merged back into parent RAG index after completion.
