# Phase 14 — Agentic Task Manager

## Summary
Introduced a persistent task manager that keeps agents aligned with principal
objectives. Instead of a single LLM call per job, develop and research tasks
are now decomposed into an ordered task list by a planner agent and executed
sequentially through a task loop. Agents can append new tasks dynamically.

## New Files
| File | Description |
|------|-------------|
| `api/task_store.py` | SQLite-backed task store (`agent_tasks` table in `data/jobs.db`) with `AgentTask` value object and `TaskStore` CRUD class |
| `agent/agents/planner_agent.py` | `PlannerAgent` — decomposes objectives into `[{description, agent_type}]` via one LLM call; falls back to single-task list on failure |
| `tests/unit/test_task_store.py` | 19 unit tests for TaskStore (CRUD, ordering, all_done, next_sequence, counts) |
| `tests/integration/test_task_loop.py` | 23 tests covering PlannerAgent parsing/strategy and orchestrator task loop (execute all, combine responses, persist tasks, failed task continues, dynamic new_tasks, dedup files, phase callbacks) |
| `aiChangeLog/phase-14.md` | This file |

## Modified Files
| File | Change |
|------|--------|
| `agent/orchestrator.py` | Added `PlannerAgent` + `TaskStore` init; added `_run_task_loop()` method; added `_direct` parameter to `_run_specialized_agent()` — develop/research now route through task loop unless called directly from within the loop |
| `api/main.py` | Added `TaskStore` import + instance; added `GET /task/{job_id}/tasks` endpoint |
| `api/discord_bot.py` | Added `get_job_tasks()` client method; added `!tasks` command with per-task status icons; added `task:N/M:description` phase label parsing; added `planning:tasks` label |

## Architecture

```
User: !ask run and debug the game
  → orchestrator.run_task()
    → _run_specialized_agent(task_type="develop")
      → _run_task_loop()                         ← NEW
          → PlannerAgent.plan() → [{desc, type}]
          → TaskStore.create_tasks(job_id, specs)
          → loop:
              task = TaskStore.get_next_pending(job_id)
              on_phase("task:1/4:Check package.json")
              _run_specialized_agent(..., _direct=True)  ← real agent
              result.new_tasks → TaskStore.create_task()
              TaskStore.update_task(task_id, "done")
          → return combined response
```

## Task Loop Guarantees
- A failed task marks itself `failed` and the loop continues with the next task (no abort)
- Agents returning `new_tasks` in their result get those tasks appended to the end of the queue
- File lists are deduplicated while preserving insertion order
- `all_done()` is True only when every task is in a terminal state (done / failed / skipped)
- When `job_id` is None (direct API calls), tasks run in-memory without persistence

## Discord UX
- Phase label updates: `Task 2/4 — Run npm start to capture…`
- `!tasks` command shows full task list with status icons:
  ```
  Task plan (2/4 done)
  ✅ 1. [develop] Check package.json and note start script
  ✅ 2. [develop] Run npm start to capture the error
  ▶️ 3. [develop] Fix the TypeError in src/game.js
  ⏳ 4. [develop] Run npm start again to verify the fix
  ```

## Test Results
- New tests: 42 (19 unit + 23 integration)
- Full suite: **345 passed** (was 303 before this phase)
