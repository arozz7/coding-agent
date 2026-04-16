# Phase 19 — LM Studio Programmatic Load/Unload, Single-Model Enforcement, Remote Fallback Chain

## Summary

This phase adds robustness to local model management. Instead of blindly sleeping
and hoping a model reloads, the agent now actively manages the LM Studio lifecycle
via its native REST API, falls back to remote models intelligently, and notifies
users (via Discord) when a switch has been made.

---

## Changes

### `llm/config.py`
- Added `provider: str = "lmstudio"` field to `ModelConfig`.
  Values: `lmstudio` | `ollama` | `llama_cpp`. Controls whether programmatic
  load/unload via the LM Studio API is available.

### `llm/ollama_client.py`
- Added `load_model(identifier)` — `POST /api/v1/models/load`
- Added `unload_model(identifier)` — `POST /api/v1/models/unload`
- Added `get_loaded_local_models()` — returns IDs with `state=loaded`
- Added `poll_until_loaded(identifier, timeout, interval)` — polls
  `check_model_state()` until loaded or timeout; returns `bool`

### `llm/model_router.py`
- Added `ModelSwitchEvent` dataclass (from_model, to_model, reason, timestamp)
- Added `register_switch_callback(fn)` / `_fire_switch_event()` for observer pattern
- Added `_get_fallback_chain(exclude)` — replaces `_get_local_fallback`; returns
  ordered list: other locals first, then remotes with valid API keys
- Added `_ensure_single_local_model(config)` — unloads other loaded models when
  `single_model_only=True` in local_runtime config (LM Studio only)
- Added `_try_load_lmstudio_model(config)` — calls unload-others → load → poll
- Added `_run_fallback_chain(...)` — walks the chain, fires switch events, and
  delegates to `generate()` recursively with the remaining chain
- Updated `generate()` `ModelNotReadyError` handler:
  - LM Studio: calls `_try_load_lmstudio_model()` up to `max_load_attempts` times,
    then falls through to the full fallback chain (locals → remotes)
  - Other backends: retains blind 120 s sleep for compatibility
- Replaced all `_get_local_fallback` call sites with `_run_fallback_chain`
- Loaded `local_runtime` block from `models.yaml` defaults into `_local_runtime`

### `llm/__init__.py`
- Exported `ModelSwitchEvent`

### `config/models.yaml`
- Added `provider: lmstudio` to all four local model entries
- Added `local_runtime` block under `defaults`:
  ```yaml
  local_runtime:
    single_model_only: true      # unload others before loading
    load_timeout_secs: 300
    load_poll_interval_secs: 10
    max_load_attempts: 2
  ```

### `agent/orchestrator.py`
- Registered `_on_model_switch` callback on the router at `__init__`
- Added `_model_switch_notices: list[str]` buffer
- Added `_drain_switch_notices(on_phase)` — drains buffer, emits `model_switch:`
  phase label via `on_phase` callback
- `_run_task_loop` drains switch notices after each step and appends warnings to
  `all_responses` and `task_summaries`

### `api/main.py`
- Added `_pending_switch_events: list[dict]` for out-of-job switch events
- Registered `_api_switch_callback` on router after orchestrator init
- Added `GET /events/model-switches` endpoint — returns and clears the queue

### `api/discord_bot.py`
- `_poll_job` now handles `model_switch:` phase labels — posts a one-time channel
  alert and updates the status message label
- Added `_poll_model_switch_events()` background coroutine (started from `on_ready`)
  that polls `/events/model-switches` every 30 s and posts to `BOT_STATUS_CHANNEL_ID`
- New env var: `BOT_STATUS_CHANNEL_ID` — channel ID for model-switch alerts
  (feature disabled if unset)

---

## Behaviour Summary

| Scenario | Old behaviour | New behaviour |
|---|---|---|
| Local model `not-loaded` (LM Studio) | Sleep 120 s × 3, then local fallback only | Call load API → poll 300 s → retry; on failure walk full chain (locals → remotes) |
| Local model `not-loaded` (Ollama/llama.cpp) | Sleep 120 s × 3 | Unchanged — blind sleep still used |
| Switch to fallback | No notification | Discord alert in channel + phase label |
| Multiple local models in VRAM | No management | Auto-unload others before loading target |
| Switch outside running job | Invisible | Stored in API queue; bot polls and notifies |
