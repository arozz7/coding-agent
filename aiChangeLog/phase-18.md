# Phase 18 — Supervisor Process Manager, Fix Loop Hardening, LLM Timeout Reliability

## Summary

This phase hardened the runtime reliability of the coding agent across four areas: a new supervisor process manager, fix loop robustness, Discord bot resilience, and LLM pipeline correctness for Qwen3 thinking models with Python 3.12.

---

## Changes

### `supervisor.py` (new — repo root)
- New process manager that starts the API server (via `sys.executable + uvicorn`) and waits for `/health` before launching the Discord bot.
- Polls `.state/restart.flag` every 2 seconds to trigger hot restarts.
- Bot crash backoff: detects fast-fail (< 10s uptime), increments a consecutive-fail counter, and gives up after 5 consecutive crashes.
- Uses `taskkill /F /T /PID` for Windows process-tree teardown.

### `api/main.py`
- Added `POST /restart` endpoint (returns 202) with a localhost-only guard.
- Writes `.state/restart.flag` to signal the supervisor.
- Added `Request` import.

### `api/discord_bot.py`
- Added `.state/last_channel` persistence: `on_ready` reads the last active channel and sends a "back online" message after a restart.
- Added `AgentClient.restart()` method that calls `POST /restart`.
- Added `!restart` / `!reboot` commands.
- Added `_safe_edit()` helper that swallows Discord 5xx errors on all `status_msg.edit()` calls (resilience against transient 503s).
- Added `_on_poll_done` callback on `create_task` to log unhandled poll exceptions.
- Added `from pathlib import Path`; `_STATE_DIR` / `_LAST_CHANNEL_FILE` constants.

### `agent/agents/developer_agent.py`
- `MAX_FIX_ITERATIONS` increased from 3 → 5.
- Fix loop now extracts `verify_cmd` from the first failed shell entry and re-runs it explicitly after each fix iteration — no longer relies on the LLM to include a shell block.
- Fix prompt updated to instruct the LLM to write `FILE:` blocks only.
- Full error text passed (removed `[:3000]` truncation).
- Cumulative `files_fixed_history` passed to every fix iteration for better LLM context.

### `llm/ollama_client.py`
- `generate()` now accepts `enable_thinking` (passes `"enable_thinking": false` to LM Studio when set) and `timeout: float = 600.0`.
- Raises `RuntimeError` on empty content instead of silently returning the raw reasoning trace (fixes Qwen3 empty-content bug).

### `llm/model_router.py`
- `generate()` accepts `timeout: float = 600.0`; forwards both `enable_thinking` and `timeout` to `ollama.generate()`.

### `llm/config.py`
- Added `enable_thinking: Optional[bool] = None` to `ModelConfig`.

### `agent/orchestrator.py`
- Classifier no longer wraps the LLM call in `asyncio.wait_for` — passes `timeout=timeout_s` directly to `model_router.generate()` so httpx enforces the deadline at the TCP level.
- Fixes the Python 3.12 hang where `asyncio.wait_for` could not cancel a mid-request httpx call cooperatively.

### `config/task_classifier.yaml`
- `timeout_seconds` increased from 3 → 10.

---

## Root Causes Fixed

| Bug | Root Cause | Fix |
|-----|-----------|-----|
| Fix loop exits after first iteration | LLM omitted re-run shell block | Orchestrator extracts and re-runs `verify_cmd` explicitly |
| Qwen3 returns empty content | Model returns only `reasoning_content` | Raise `RuntimeError`; caller retries or falls back |
| Classifier hangs indefinitely | `asyncio.wait_for` cannot cancel httpx mid-request in Python 3.12 | Pass timeout directly to httpx |
| Discord 503 crashes status edit | Discord transient server errors | `_safe_edit()` swallows 5xx |
| No recovery after process crash | No supervisor | `supervisor.py` with crash backoff and restart flag |

---

## Follow-on Hardening Commits

### `3e24908` — Fix classifier misfires and long debug session stability

#### `agent/orchestrator.py`
- Added `_DEFINITIVE_DEVELOP` regex for pre-LLM classification: if the task text contains keywords like `fix`, `implement`, `refactor`, `build`, `write`, etc., it is classified as `develop` without an LLM call.
- Added `force_task_type` field on `TaskRequest` — callers can override classification entirely.
- `MAX_FIX_ITERATIONS` raised from 5 → 10 to survive longer debug sessions.
- Error text passed to the fix prompt capped at 4 000 chars (avoids bloated context).
- Fix-attempt prose capped at 3 code blocks per iteration.

#### `api/discord_bot.py`
- Added `!dev <task>` command — forces `force_task_type=develop`, bypassing classifier.
- Added `!continue [note]` command — resumes the current debugging session with an optional note.

---

### `b0af718` — Fix UnicodeDecodeError in ShellTool on Windows

#### `agent/tools/shell_tool.py`
- `ShellTool.run()` now passes `encoding='utf-8', errors='replace'` to `subprocess.run()`.
- Fixes `UnicodeDecodeError` raised when `npm` or other Windows tools emit non-UTF-8 bytes (e.g., `cp1252` characters in error output).

---

### `af96f81` — Handle model TTL eviction and unreliable restarts gracefully

#### `llm/ollama_client.py`
- Added `ModelNotReadyError` exception raised when LM Studio returns HTTP 503 or a "model not loaded" response.

#### `llm/model_router.py`
- Wraps `ollama_client.generate()` in a retry loop: 120-second wait × 3 attempts on `ModelNotReadyError` before propagating.
- `asyncio.wait_for` hard wall-clock timeout wraps the entire generate call.

#### `supervisor.py`
- Added heartbeat writer: writes `.state/supervisor.heartbeat` timestamp every 30 seconds.
- Added stale-job watchdog: scans active jobs every 5 minutes; alerts Discord if any job has been in the same phase for > 45 minutes.

#### `api/main.py`
- `POST /restart` now returns a `supervisor_running` boolean in the response body (derived from heartbeat file recency).

#### `api/discord_bot.py`
- `!restart` warns the user if `supervisor_running` is `false` ("supervisor.py is not running — restart not possible") instead of going silent.
- `_poll_job` emits a stale-phase alert in Discord when a job exceeds 45 minutes in the same phase.

---

### `ef7ed5e` — Fix API startup blocking /health and causing supervisor timeout

#### `api/main.py`
- `startup_event` now calls `asyncio.create_task(_init_agent_background())` immediately on startup.
- `/health` endpoint is reachable during agent initialization — previously the blocking init caused the supervisor's 120-second health-check timeout to fire before the server was ready.
- `API_STARTUP_TIMEOUT` raised from 60 → 120 seconds in `supervisor.py`.

---

### Fix localhost IPv6 resolution on Windows 11

#### `.env`, `supervisor.py`, `api/discord_bot.py`
- Changed `AGENT_API_URL` default from `http://localhost:5005` → `http://127.0.0.1:5005`.
- On Windows 11, `localhost` resolves to `::1` (IPv6), but uvicorn binds to `0.0.0.0` (IPv4 only). The IPv6 address hit a different service returning 404, causing supervisor health-check failures and bot HTTPStatusError loops.

---

### Models Discovery View (DMV)

#### `llm/ollama_client.py`
- Added `list_all_models()`: calls `/api/v0/models` and returns the full list of LM Studio downloaded models with their `state` field.
- `check_model_state()` / `health_check()` / `warmup()`: now use `/api/v0/models` state field instead of `/v1/models` (which lists all downloadable models regardless of VRAM state).

#### `api/main.py`
- `GET /models`: extended response with:
  - `state` field per configured local model (from LM Studio `/api/v0/models`)
  - `lm_studio_available`: list of `{id, state}` for models downloaded in LM Studio but not yet in `models.yaml`

#### `api/discord_bot.py`
- `!models`: redesigned output with two sections:
  - **Configured Models** — each with 🟢 (loaded) / ⚪ (not-loaded) state icon for local models
  - **Available in LM Studio (not configured)** — discovery list of downloaded-but-unconfigured models
  - Truncated to 1900 chars to stay within Discord's 2000-char limit

---

### Wiki Flow Hardening

#### `agent/skills/skill_executor.py`
- `_wiki_compile` synthesis `model_router.generate()` call now passes `enable_thinking=False` — the structured output (TITLE/TAGS/CATEGORY/CONFIDENCE) is deterministic and doesn't benefit from a thinking trace; this eliminates a potential 10-30 min stall when a Qwen3 model is active.

#### `agent/orchestrator.py`
- `_run_task_loop`: added per-subtask `wiki-compile` call inside the success branch after each subtask completes. Learnings from Task 1 are now persisted to `.agent-wiki/` before Task 2 starts, enabling later subtasks to query relevant prior context. Failures are swallowed with a warning so a wiki error never stalls the task loop.

---

## Root Causes Fixed (Follow-on)

| Bug | Root Cause | Fix |
|-----|-----------|-----|
| Classifier misclassifies obvious dev tasks as chat | LLM called for clearly-develop prompts | Pre-LLM regex check `_DEFINITIVE_DEVELOP` |
| UnicodeDecodeError on npm output | `subprocess` defaults to system codepage on Windows | `encoding='utf-8', errors='replace'` |
| Agent silently hangs after model TTL eviction | LM Studio 503 not handled | `ModelNotReadyError` + 120s retry × 3 |
| Supervisor can't tell if restart will work | `POST /restart` returned no state | Returns `supervisor_running` bool from heartbeat |
| Supervisor health-check times out at startup | Blocking `startup_event` delayed `/health` | Background init task; `/health` responds immediately |
| Supervisor/bot health checks fail on Windows 11 | `localhost` resolves to `::1`, uvicorn binds IPv4 only | Changed default URL to `http://127.0.0.1:5005` |
| `health_check` returns True for unloaded models | `/v1/models` lists all downloadable models regardless of VRAM | Use `/api/v0/models` state field; fall back to `/v1/models` for plain Ollama |
| `!models` shows no LM Studio state or discovery | Only read `models.yaml` config | Added live state per configured local model + unconfigured downloaded models list |
| Wiki synthesis call could stall 10-30 min on Qwen3 | Thinking mode on for structured output generation | `enable_thinking=False` passed to synthesis generate call |
| Subtask learnings not available to later subtasks in same job | `_run_task_loop` only did wiki-query pre-task, never wiki-compile post-subtask | Per-subtask wiki-compile added inside success branch; failures swallowed |
| Job stuck at "preparing" for 26+ minutes | Qwen3 generates `<think>` block before every response, including 1-word classifier answers, which can take 10-30 min; `asyncio.wait_for` cannot cancel httpx mid-read on Python 3.12 | (1) `_do_generate` now uses `asyncio.to_thread` + sync `httpx.Client` so `wait_for` can cancel on Python 3.12; (2) `model_router.generate` accepts `enable_thinking` override; classifier passes `enable_thinking=False` since it only needs one word — all real coding/planning calls keep thinking enabled |
| Task stuck for 1+ hour; entire API server frozen | `httpx.AsyncClient` blocks the asyncio event loop when stuck mid-read — all FastAPI endpoints (incl. `/health`, `/jobs`) become unreachable; supervisor watchdog can't reach API to detect stale job | `asyncio.to_thread` fix moves httpx off the event loop; supervisor `_check_stale_job` now counts consecutive API-unreachable cycles and forces restart after 15 min of unreachable API |
| Timeout retries waste 1800s (3× 600s) before failing | Generic `except Exception` in `model_router.generate` retried all errors including timeouts | Timeout errors (containing "timeout" in message) now fail fast without retry |

---

## Testing Checklist

- [ ] `python supervisor.py` — API starts, bot connects
- [ ] `!restart` from Discord — "back online" message appears in same channel; warns if supervisor not running
- [ ] Submit a develop task — phase transitions `preparing → developing` within ~15 seconds (classifier no longer hangs)
- [ ] Submit a task with build errors — agent runs up to 10 fix iterations, each followed by explicit re-run
- [ ] `!dev <task>` — forces develop path, skips classifier
- [ ] `!continue` — resumes active debug session
- [ ] Unload model in LM Studio mid-task — logs show `model_not_ready_waiting`; retries 3× before failing
- [ ] Confirm no `UnicodeDecodeError` in logs when `npm` commands run
- [ ] `!models` — shows configured models with 🟢/⚪ state, plus LM Studio discovery section
