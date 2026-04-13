# Phase 15 — Workspace Scoping, .env Fix, and Infinite Reconnect

## Summary
Three production-stability improvements: the `.env` `WORKSPACE_PATH` value is
now actually read at startup (was silently ignored); a `PROJECT_DIR` env var
lets the agent scope all file operations to an active project subdirectory
without creating spurious nested folders; and both the API and Discord bot
now retry indefinitely on network outages using a backoff curve that slows
from 2 s to 5 min and then holds there — neither service ever gives up.

---

## Modified Files

| File | Change |
|------|--------|
| `api/main.py` | Load `.env` at module import time via `load_dotenv()` before `os.getenv()` runs; add `PROJECT_DIR` env var; compute effective workspace as `WORKSPACE_PATH/PROJECT_DIR`; keep `os.environ["WORKSPACE_PATH"]` in sync for `GitTool`; create effective workspace directory on startup; `startup_event` retries `create_agent()` indefinitely with backoff instead of failing once |
| `api/discord_bot.py` | Add `_backoff()` helper and `_http_retry()` coroutine (retries forever on connection errors + HTTP 429/502/503/504); `AgentClient._get/_post/_delete` now delegate through `_http_retry`; replace sync `is_reachable()` with async `wait_until_reachable()` (backoff loop, never gives up); `run_bot` runs inside `asyncio.run()` and awaits the probe before connecting to Discord; `_poll_job` drops the hard 6-failure bail-out — retries indefinitely and shows "reconnected" when the API comes back |
| `agent/orchestrator.py` | Add `import os`; `_build_environment_context()` injects `Active project: <name>` line when `PROJECT_DIR` env var is set so agents know not to create a nested subdirectory |
| `agent/agents/developer_agent.py` | PROJECT DIRECTORY RULE rewritten: write at workspace root when `active_project` is in context; only infer a new subdirectory name for genuinely new projects with no active project set |
| `tests/unit/test_git.py` | `test_initialization_invalid_repo` now sets `WORKSPACE_PATH` via `monkeypatch` (required since `GitTool` reads only from env) |

---

## Architecture — Workspace Scoping

```
.env
  WORKSPACE_PATH=J:\Projects\agent-workspace
  PROJECT_DIR=Shadows-of-Eldoria

api/main.py (module import)
  load_dotenv()                          ← fires before any os.getenv()
  WORKSPACE_PATH = os.getenv(...)        ← J:\Projects\agent-workspace
  PROJECT_DIR    = os.getenv(...)        ← Shadows-of-Eldoria
  _current_workspace = WORKSPACE_PATH/PROJECT_DIR
  os.environ["WORKSPACE_PATH"] = _current_workspace   ← GitTool reads this

orchestrator context block (every task)
  "Active project: Shadows-of-Eldoria   ← write files at workspace root"

developer agent rule
  active_project set → write at root    ← no nested subdirectory
  no active_project  → infer name       ← new project flow unchanged
```

---

## Architecture — Infinite Reconnect

### Backoff curve (shared)
```
attempt:  0    1    2     3     4    5     6+
delay:    2s   5s   15s   30s   60s  120s  300s (holds at 5 min forever)
```

### API startup (`api/main.py`)
```
startup_event()
  while orchestrator is None:
    try: create_agent()        ← succeeds → break
    except: log warning, sleep _startup_backoff(attempt), attempt++
  # API serves 503 on /task while degraded — never crashes out
```

### Bot HTTP calls (`api/discord_bot.py`)
```
AgentClient._get/_post/_delete
  → _http_retry(lambda: raw_call(), label)
      loop:
        try: await coro_factory()   ← success → return
        except ConnectError/Timeout/429/502/503/504:
            sleep _backoff(attempt), attempt++
        except other: re-raise immediately   ← 404/401 go to caller
```

### Bot startup probe
```
run_bot(token)
  asyncio.run(_start_bot(token))
    await client.wait_until_reachable()   ← backoff loop, never gives up
    await bot.start(token)                ← connect to Discord
```

### Poll loop (`_poll_job`)
```
while True:
  await get_job(job_id)      ← _http_retry handles transient errors
  on failure (non-retriable):
    show "connection lost, retry in Xs"
    sleep _backoff(consecutive_failures)
  on recovery:
    show "reconnected"
    reset consecutive_failures = 0
```

---

## .env Changes
```ini
# Added:
PROJECT_DIR=Shadows-of-Eldoria
```

---

## Test Results
- All 219 unit tests pass (unchanged count — no new test files this phase)
- `test_git.py::test_initialization_invalid_repo` fixed to work with env-only `GitTool`
