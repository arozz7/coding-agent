# Phase 68 — Discord status-edit failures no longer kill the poll task silently

## Root cause

Two compounding bugs, found while diagnosing "Discord stopped showing job
status but the agent is still working":

1. `api/discord/poller.py`'s `_safe_edit()` only swallowed Discord *server*
   errors (5xx / `DiscordServerError`). Any 4xx on a status-message edit
   (message deleted, missing channel perms, etc.) propagated out of
   `_poll_job`, killing the background polling task — the job itself kept
   running unaffected, but Discord silently stopped reflecting it.
2. `supervisor.py` launches the bot subprocess with plain
   `subprocess.Popen([...], stdout=_bot_log, ...)` — no `-u` flag, no
   `PYTHONUNBUFFERED`. On Windows, a Python process with stdout redirected to
   a file is fully block-buffered, so the bot's `print()` calls (including
   the crash trace `_on_poll_done` was supposed to surface) never reached
   `logs/bot-*.log`. Only structlog's `PrintLogger` calls got through, because
   it explicitly passes `flush=True` — everything else, including the
   evidence for bug 1, was invisible.

## The fix

- `api/discord/poller.py` — `_safe_edit()` now swallows any
  `discord.errors.HTTPException` (logged via structlog, not silently) instead
  of only 5xx; `_on_poll_done()` logs through structlog instead of a bare
  `print()`.
- `supervisor.py` — `_start_bot()` sets `PYTHONUNBUFFERED=1` on the bot
  subprocess's env, so all of its output actually reaches the log file going
  forward.

## Files

- `api/discord/poller.py`
- `supervisor.py`
