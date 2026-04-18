# Phase 24 — Security Hardening + Data Integrity

## Summary
Combined security and reliability pass addressing CodeQL path-injection alerts,
prompt injection risks, SQLite concurrency, and deprecated datetime usage.

---

## Phase 24-A: Security Layer

### New Files
- `agent/security/__init__.py` — package marker
- `agent/security/paths.py` — canonical `resolve_within(path, base)` utility; uses
  `Path.resolve()` + `is_relative_to()` (CodeQL-safe pattern); all future tools should
  import from here instead of reimplementing
- `agent/security/prompt_guard.py` — `sanitize_user_input()` strips control characters;
  `detect_injection()` pattern-matches 12 known injection phrases; `guard_task()` combines
  both and raises `ValueError` on detection

### `agent/tools/shell_tool.py`
- **Critical bug fix:** `_validate_command()` now runs on the ORIGINAL command BEFORE
  `_translate_unix_to_windows()` and AGAIN after — fixes bypass where `rm -rf /` translated
  to `del /` which no longer matched the `rm` blocker
- Added 10 new `_BLOCKED_PATTERNS`: `rm --no-preserve-root`, `del /s`, `rd /s`, `rmdir /s`,
  `Remove-Item -Recurse`, PowerShell `ri -r`, `> /etc/`, `setx PATH`, `export PATH=/tmp`
- Added `_check_path_containment(cmd, workspace)` — rejects any file-targeting command
  (`rm`, `del`, `copy`, `move`, `Remove-Item`, …) that references an absolute path outside
  the workspace; also blocks output-redirect targets (`echo foo > /outside/path`); uses
  `resolve_within()` from `agent.security.paths` for containment check
- Removed `lgtm[py/path-injection]` suppression comment

### `agent/tools/file_system_tool.py`
- Replaced fragile `str(resolved).startswith(str(base))` check with
  `resolved.is_relative_to(self.allowed_base)` — robust against Unicode normalization and
  symlink edge cases
- Removed two `lgtm[py/path-injection]` suppression comments

### `agent/orchestrator.py`
- Imports and calls `guard_task()` at the top of `run_task()` — sanitizes and rejects
  injected input before it enters any LLM prompt
- Also guards `run_stream()` — yields `{"error": ..., "chunk": "", "full_response": ""}` and
  returns early on injection detection, consistent with the streaming response contract
- Removed all `lgtm[py/path-injection]` suppression comments

### `api/main.py`
- Added `_require_api_key` FastAPI dependency (reads `AGENT_API_KEY` env var)
- Applied to: `POST /task`, `POST /task/start`, `POST /task/stream`,
  `POST /workspace/project` — optional when env var is unset
- Removed all `lgtm[py/path-injection]` suppression comments
- Added `X-API-Key` to CORS allowed headers

### Other lgtm cleanups
- `agent/tools/browser_tool.py` — removed suppression
- `agent/tools/test_runner_tool.py` — removed suppression
- `mcp/server.py` — removed suppression

### `.env.example`
- Added `AGENT_API_KEY` entry with documentation

---

## Phase 24-B: Data Integrity

### `agent/memory/session_memory.py`
- Added `threading.RLock` (`self._lock`) protecting all write methods:
  `create_session`, `save_message`, `update_task_status`, `update_session_status`,
  `delete_session`
- Enabled WAL mode: `PRAGMA journal_mode=WAL` on connection init — allows concurrent
  readers alongside the single writer, preventing "database is locked" errors

---

## Phase 24-C: datetime Deprecation Fix

Replaced `datetime.utcnow()` (deprecated in Python 3.12+, returns naive datetime) with
`datetime.now(timezone.utc)` (timezone-aware) across all files:

| File | Occurrences fixed |
|------|-------------------|
| `agent/orchestrator.py` | 3 |
| `api/main.py` | 1 |
| `agent/memory/session_memory.py` | import updated |
| `llm/circuit_breaker.py` | 1 |
| `llm/rate_limiter.py` | 2 |
| `llm/model_resilience.py` | 9 |
| `llm/health.py` | 6 |
| `llm/cost_tracker.py` | 2 |
| `llm/model_router.py` | 1 (dataclass field default) |
| `agent/subagent/spawner.py` | 2 |
| `agent/human_loop/human_in_the_loop.py` | 3 |
| `tests/unit/test_model_resilience.py` | 6 |
