# Phase 11 — Web Search Overhaul, Date Awareness, OpenRouter Integration, Integration Tests

## Goals
Fix broken web search, add reliable multi-provider search fallback chain,
inject live date awareness into agents, integrate OpenRouter cloud models with
automatic rate-limit fallback, and build a full integration test suite that
simulates Discord bot interactions without a real LLM or Discord connection.

---

## 11.0 — OpenRouter Cloud Model Integration

### `llm/cloud_api_client.py`
- Added `_OpenRouterRateLimitError` exception class — captures `Retry-After` header value.
- Added `_OPENROUTER_BASE`, `_OPENROUTER_REFERER`, `_OPENROUTER_TITLE` module constants.
- Added `_endpoint_type()` helper: inspects endpoint URL for `"anthropic"`, `"openrouter.ai"`,
  or `"openai"` to route to the correct request format.
- Added `_openrouter_generate()` and `_openrouter_stream()` methods — OpenAI-compatible format
  with `HTTP-Referer` and `X-Title` headers required by OpenRouter.
- 429 responses from OpenRouter raise `_OpenRouterRateLimitError` immediately (no retry loop)
  so the router can fall back to a local model without burning retry budget.

### `llm/model_router.py`
- Added `_get_local_fallback(exclude_name)` — returns the first healthy local model that is
  not the failing model; falls back to any local model if only one exists.
- Added `_is_rate_limit_error(exc)` — detects generic "429 Too Many Requests" strings from
  non-OpenRouter remote providers.
- `generate()` gains `_is_fallback: bool = False` parameter to prevent infinite recursion.
- Catches `_OpenRouterRateLimitError` before the generic `Exception` handler; immediately
  calls `_get_local_fallback()` and retries there.
- Generic remote 429 strings also trigger the same local fallback path.

### `config/models.yaml`
- Added `google/gemma-4-31b-it:free` OpenRouter entry.
- `defaults.coding_model` set to `qwen3.5-35b-a3b`; `defaults.fallback_model` to the
  OpenRouter model.

---

## 11.1 — Web Search Overhaul

### `agent/tools/web_tool.py` — complete rewrite of search layer

**Search priority chain (first available wins):**
```
1. Brave Search API        — set BRAVE_SEARCH_API_KEY; 2 000 free queries/month; full web
2. DuckDuckGo              — no key required; always-on fallback
3. Playwright google.com   — browser scrape; no key; uses existing Chromium install
4. Google CSE API          — deprecated for full-web (Jan 2026); last resort
```

**New methods:**
- `_search_brave(query, max_results, api_key)` — calls `api.search.brave.com/res/v1/web/search`
  with `X-Subscription-Token` header.
- `_search_playwright_google(query, max_results)` — launches headless Chromium, navigates to
  `google.com/search`, extracts `a:has(h3)` anchors for titles/URLs/snippets. Handles EU
  cookie-consent overlay. Graceful `ImportError` if Playwright not installed.
- `_search_duckduckgo()` updated — imports `ddgs.DDGS` first, falls back to legacy
  `duckduckgo_search.DDGS` import name.
- `_search_google()` demoted to last-resort position in the chain.

**Date-aware query expansion:**
- `_resolve_query_date(query)` — appends the actual calendar date to queries containing
  relative terms (`"last night"`, `"yesterday"`, `"today"`, `"tonight"`, `"this week"`,
  `"last week"`, `"right now"`, `"currently"`, etc.).
  - "last night" / "yesterday" → appends yesterday's date (`April 11 2026`).
  - All other relative terms → appends today's date (`April 12 2026`).
  - Queries with no relative terms are returned unchanged.
- Called as the first step of `search()` before any backend is tried.

**Module docstring** updated to describe all four backends and their priority.

---

## 11.2 — Agent Date Awareness

### `agent/agents/chat_agent.py`
- `ChatRole.get_system_prompt()` now begins with `Today's date is {weekday, Month DD, YYYY}.`
  computed from `datetime.date.today()` at call time.
- The model always knows the current date and can reason about temporal questions
  ("last night's game", "this week's earnings", etc.) without guessing.

### `agent/agents/research_agent.py`
- Same date injection in `ResearchRole.get_system_prompt()`.

---

## 11.3 — Web Search Result Ordering Fix

### `agent/agents/chat_agent.py`
- Live web data is placed **before** the user question in the prompt, not after.
  Local models read prompts top-to-bottom; data after the question is frequently ignored.
- Labels changed to `[LIVE WEB SEARCH RESULTS]` and `[FETCHED PAGE CONTENT: {url}]` — both
  listed in the system prompt's `CRITICAL INSTRUCTION` block so the model treats them as
  authoritative live data, not fictional context.

### `agent/agents/research_agent.py`
- Same prompt ordering fix: gathered data before task text.
- Same `[LIVE WEB SEARCH RESULTS]` / `[FETCHED PAGE CONTENT]` label names.

---

## 11.4 — Bug Fixes

### `agent/agents/developer_agent.py`
- Line 157: `"shell_output": shell_output` → `"shell_output": shell_outputs`
  (`shell_output` was undefined; `shell_outputs` is the correct list variable).

### `agent/orchestrator.py`
- `_run_specialized_agent()` now fetches conversation history via
  `_build_context_from_events(session_id)` and appends it to `enriched_context`.
  Previously, history was fetched for the orchestrator's own routing logic but never
  passed into the specialist agent's context, so agents had no memory of prior turns.

---

## 11.5 — Dependency Updates

### `pyproject.toml`
- `[project.dependencies]`: `duckduckgo-search>=6.0.0` → `ddgs>=1.0.0`
  (package was renamed; old package returns 0 results silently).
- `[tool.poetry.dependencies]`: same rename (`duckduckgo-search = "^6.0"` → `ddgs = ">=1.0"`).

### `.env.example`
- Added `BRAVE_SEARCH_API_KEY` section with setup instructions.
- Updated Google search section to document the January 2026 deprecation of full-web PSE.
- `OPENROUTER_API_KEY` section with setup instructions and `config/models.yaml` reference.
- `GOOGLE_SEARCH_API_KEY` / `GOOGLE_SEARCH_CX` retained as last-resort documentation.

---

## 11.6 — Integration Test Suite

### `tests/integration/conftest.py` (new)
Shared fixtures for all integration tests:
- `make_mock_orchestrator(task_type, response)` — builds a realistic `MagicMock` with
  `run_task`, `model_router`, `session_memory`, `skill_manager`, etc. pre-configured.
- `test_store` fixture — isolated `JobStore` backed by a `tmp_path` SQLite file.
- `client` fixture — `httpx.AsyncClient` with `ASGITransport(app=app)`, patches
  `api.main._orchestrator` and `api.main._job_store` directly (startup event not triggered
  by httpx ASGI transport; direct module patching is more reliable).
- `poll_until_done(ac, job_id, timeout, interval)` — polls `GET /task/{job_id}` until
  terminal status, raises `TimeoutError` after deadline.

### `tests/integration/test_discord_sim.py` (new)
Simulates the full Discord bot interaction cycle:
- `POST /task/start` → asserts `job_id`, `session_id`, `task_type` in response.
- Polls `GET /task/{job_id}` until `status=done`, then `GET /task/{job_id}/result`.
- Task type detection via `_detect_task_type_keyword` (`develop`, `research` variants).
- 404 for unknown job IDs.
- Result endpoint returns `{status, result: null}` while job is still running.
- Job cancellation via `DELETE /task/{job_id}`.
- `GET /jobs` returns list with correct count.
- Model switch: `POST /models/active`, unknown model → 404, null → revert to default.
- `GET /models`, `GET /health`, `GET /ready`, `GET /` health endpoints.

### `tests/integration/test_sessions.py` (new)
- Two tasks with same `session_id` both complete successfully.
- `run_task` called with the correct `session_id` (verified via `call_args`).
- Auto-generated session IDs match the `session_YYYYMMDD_HHMMSS` format.
- `include_history=True` propagated to `run_task`.
- Session CRUD: `GET /sessions`, `GET /sessions/{id}`, `DELETE /sessions/{id}` (404 on missing).

### `tests/integration/test_search.py` (new)
Component-level tests against `WebTool` directly (no HTTP):
- **Brave**: happy path, error → DDG fallback, no key → DDG directly.
- **DuckDuckGo**: success, empty results, exception → error dict, snippet truncation to 400 chars.
- **Google**: happy path, HTTP error → DDG fallback, error-key result → DDG fallback.
- **Playwright Google**: results parsed, error → error dict, not installed → error dict.
- **Date resolver**: `_resolve_query_date` unit tests for yesterday/today/no-change cases.
- **URL fetch**: httpx fallback path, text truncation to `_MAX_PAGE_CHARS`.
- All DDG tests use `sys.modules` injection (`_make_ddgs_module`) — works regardless of
  whether `ddgs` or `duckduckgo_search` is installed in the test environment.

### `tests/integration/test_model_fallback.py` (new)
- `_get_local_fallback`: returns first local, excludes named model, last-resort same-name,
  returns `None` when no local models exist.
- OpenRouter 429 → local fallback succeeds; no local → `LLMError` raised;
  fallback error propagates (no infinite loop); `record_rate_limit` called.
- Generic 429 string → local fallback; local model 429 → not treated as rate limit.
- `_detect_task_type_keyword` parametrized across 14 task strings covering all 6 types
  (`develop`, `review`, `test`, `architect`, `research`, `chat`).

---

## Files Changed

| File | Change |
|---|---|
| `agent/tools/web_tool.py` | Full search overhaul: Brave primary, DDG fallback, Playwright Google 3rd fallback, Google CSE last resort, date resolver |
| `agent/agents/chat_agent.py` | Date injection in system prompt, live data before question in prompt |
| `agent/agents/research_agent.py` | Date injection in system prompt, live data before task in prompt |
| `agent/agents/developer_agent.py` | Fix `shell_output` → `shell_outputs` NameError |
| `agent/orchestrator.py` | Pass conversation history into `_run_specialized_agent` context |
| `llm/cloud_api_client.py` | OpenRouter integration: typed 429 error, dedicated generate/stream methods |
| `llm/model_router.py` | Local fallback on OpenRouter 429 and generic remote rate limits |
| `config/models.yaml` | OpenRouter model entry, updated defaults |
| `.env.example` | Brave Search, OpenRouter, updated Google deprecation notice |
| `pyproject.toml` | `ddgs` replaces `duckduckgo-search` in both dependency sections |
| `tests/integration/conftest.py` | New — shared ASGI test fixtures |
| `tests/integration/test_discord_sim.py` | New — Discord start/poll/result cycle tests |
| `tests/integration/test_sessions.py` | New — session continuity and CRUD tests |
| `tests/integration/test_search.py` | New — search backend and date resolver tests |
| `tests/integration/test_model_fallback.py` | New — rate limit fallback and classifier tests |
