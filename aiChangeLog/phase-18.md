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

## Testing Checklist

- [ ] `python supervisor.py` — API starts, bot connects
- [ ] `!restart` from Discord — "back online" message appears in same channel
- [ ] Submit a develop task — phase transitions `preparing → developing` within ~15 seconds (classifier no longer hangs)
- [ ] Submit a task with build errors — agent runs up to 5 fix iterations, each followed by explicit re-run
