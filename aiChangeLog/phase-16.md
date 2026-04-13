# Phase 16 — Developer Agent Fixes + Automatic Context Bridge

## Summary
Two sets of improvements shipped together.

**Developer agent fixes** address four concrete bugs observed in production:
a broken `FILE:` format example that silently prevented file creation; uncapped
shell output that flooded the LLM context with tens of thousands of lines; an
insufficiently concrete PROJECT_DIR rule that caused path double-nesting; and
missing post-write verification so the bot reported files as created when they
weren't.

**Automatic Context Bridge** monitors the session's token budget before every
task. When utilisation hits 82 % it generates a structured handover document
(using `skills/handover/SKILL.md` as the template), creates a new session
pre-seeded with that document, and continues the current task in the clean
session. The Discord bot detects the bridge, swaps the user's session silently,
and posts a one-line notice. At 75 % a warning is sent instead.

---

## Modified Files

| File | Change |
|------|--------|
| `agent/tools/tool_executor.py` | Added `_cap_shell_output()` helper; `_run_shell` now truncates stdout at 500 lines / 20 KB and appends a count of omitted lines |
| `agent/agents/developer_agent.py` | (1) Fixed broken `FILE:` format example in `execute()` prompt — path was missing from the example line, causing the regex to silently drop all file writes. (2) Strengthened PROJECT_DIR rule with explicit WRONG/CORRECT path examples. (3) Added post-write verification: every `file_write` is followed by a `file_read` probe; files that can't be read back are excluded from `files_created` and logged as warnings. |
| `agent/orchestrator.py` | Added `import subprocess`; added `_HANDOVER_FALLBACK` template constant; added `_estimate_context_tokens()`, `_check_context_budget()`, and `_run_handover()` methods; wired budget check into `run_task()` — swaps session at 82 %, adds `context_budget="warn"` flag at 75 %; `handover_triggered`, `original_session_id`, `context_budget`, and `handover_bridge` fields added to success result dict |
| `api/main.py` | Background job `_run()` passes `handover_triggered`, `new_session_id`, and `context_budget` into the job store cache so the Discord bot can read them from `GET /task/{job_id}` |
| `api/discord_bot.py` | `_poll_job()` detects `handover_triggered` — auto-swaps `bot.user_sessions[user_id]` to the new session and posts a bridge notice; detects `context_budget="warn"` and posts a heads-up message |

---

## Architecture — Context Bridge Flow

```
run_task(session_id, task)
  _check_context_budget(session_id, task)
    history = _build_context_from_events()   ← last 20 events, capped
    estimated_tokens = len(history + task) // 4 + 4_500 overhead
    ratio = estimated_tokens / model.context_window

    ratio < 0.75  → "ok"   — proceed normally
    ratio 0.75–0.82 → "warn" — flag in result, bot sends heads-up
    ratio ≥ 0.82  → "bridge" — trigger handover

  if "bridge":
    _run_handover(session_id, task)
      load skills/handover/SKILL.md (or fallback template)
      git log --oneline -10  (best-effort)
      generate Context Bridge via model_router
      new_session_id = session_YYYYMMDDHHMMSS_bridge
      session_memory.save_message(new_session_id, bridge_text)
      return (bridge_text, new_session_id)
    session_id ← new_session_id    # all further work in clean session

  _run_specialized_agent(task, task_type, session_id)   ← normal flow
  return result + {handover_triggered, new_session_id, context_budget}
```

## Architecture — FILE: Fix

The bug: `execute()` showed this example in the prompt:
```
FILE:
```language
content
```
```
The regex `r'FILE:\s*(.+?)\n```\w*\n(.*?)```'` requires the path on the same
line as `FILE:`. Example was wrong → regex never matched → no files written.

Fixed example:
```
FILE: path/to/file.ext
```language
content
```
```

## Shell Output Cap

`_cap_shell_output(text)` truncates at 500 lines OR 20 KB (whichever is hit
first), appending a count of truncated lines/bytes. Applied to both success
and failure branches of `_run_shell`. Prevents 44 k-line dir listings from
exhausting the context window.

---

## Test Results
- All 219 unit tests pass (unchanged count)
