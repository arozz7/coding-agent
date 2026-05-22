# Phase 35 — Discord Bot Split

## Goal
Split `api/discord_bot.py` (1628 lines) into a proper `api/discord/` subpackage,
rewriting the entry point as a thin module-loader under 35 lines.

## Files Created

### `api/discord/__init__.py` (empty)

### `api/discord/client.py` (176 lines)
- `API_URL`, `POLL_INTERVAL` constants
- `_BACKOFF_STEPS`, `_backoff()`, `_RETRIABLE_STATUSES`, `_http_retry()` — retry/backoff helpers
- `AgentClient` class — all REST calls (`_get`, `_post`, `_delete`, `start_task`, `get_job`,
  `get_job_result`, `cancel_job`, `get_job_tasks`, `get_file`, `set_project`, `preview_delete_project`,
  `delete_project`, `get_session_history`, `list_sessions`, `delete_session`, `restart`,
  `get_recent_jobs`, `wait_until_reachable`)

### `api/discord/helpers.py` (74 lines)
- `_MAX_ATTACHMENT_BYTES`, `_BINARY_EXTENSIONS` constants
- `strip_code_blocks()`, `_chunk()`, `_truncate()`, `_send_screenshot()` — text/attachment helpers

### `api/discord/bot_instance.py` (97 lines)
- `_STATE_DIR`, `_LAST_CHANNEL_FILE` — path uses three `.parent` calls to reach project root
  (file is now `api/discord/bot_instance.py`, one level deeper than original `api/discord_bot.py`)
- `DiscordAgentBot(commands.Bot)` — `on_ready`, `_poll_model_switch_events`, `on_message`
- `bot = DiscordAgentBot()` singleton
- `_start_bot()` — waits for API, then connects to Discord

### `api/discord/poller.py` (268 lines)
- `_PHASE_LABELS`, `_HEARTBEAT_INTERVAL`
- `_resolve_last_job_id()` — three-priority job recovery (explicit → in-memory → API fallback)
- `_safe_edit()` — swallows transient Discord 5xx errors
- `_on_poll_done()` — logs unhandled exceptions from poll tasks
- `_poll_job()` — full polling loop with phase labels, stale-phase watchdog, inline/summarise modes

### `api/discord/commands/__init__.py` (empty)

### `api/discord/commands/tasks.py` (151 lines)
- `_submit_task()` — shared submit logic; fixes pre-existing `logger` NameError (structlog was
  never imported in original `discord_bot.py` line 677)
- Commands: `!ask`, `!dev`, `!research`, `!chains`, `!chain`, `!continue`

### `api/discord/commands/jobs.py` (168 lines)
- `_STATUS_ICONS` mapping
- Commands: `!status`, `!cancel`, `!result`, `!files`, `!tasks`

### `api/discord/commands/workspace.py` (287 lines)
- Commands: `!show`, `!history`, `!sessions`, `!clear`, `!session`, `!workspace`, `!project`

### `api/discord/commands/models.py` (111 lines)
- Commands: `!git`, `!models`, `!model`

### `api/discord/commands/admin.py` (251 lines)
- Commands: `!jobs`, `!skills`, `!wiki`, `!restart` (alias `!reboot`), `!helpme`

## Files Modified

### `api/discord_bot.py` (1628 → 31 lines, −98%)
- Thin entry point: imports all five command modules (triggering `@bot.command()` registration
  at import time), then exposes `run_bot()` and `__main__` guard

## Key Architectural Decisions
- **`@bot.command()` at import time**: command modules import `bot` from `bot_instance.py` and
  register decorators when the module is imported; `discord_bot.py` just imports all five modules
- **Dependency graph (acyclic)**: `client` ← `helpers` ← `bot_instance` ← `poller` ← command
  modules ← `discord_bot.py`
- **`_STATE_DIR` depth fix**: moved from two `.parent` calls (original) to three (new nesting level)
- **`logger` bug fix**: `_submit_task()` in `commands/tasks.py` adds `import structlog` — the
  original `discord_bot.py` line 677 called `logger.warning()` but `structlog` was never imported

## Behavior
No logic changes — pure extraction. All command names, behaviors, and UX remain identical.
