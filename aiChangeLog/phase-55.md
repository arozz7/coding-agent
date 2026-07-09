# Phase 55 — Objective grounding for the task loop

## Problem

`!dev` continuation requests ("move on to the next tasks in NEXT_STEPS.md")
were planned against a ~2000-char tech-stack fingerprint with no file
contents — `ContextBuilder.build_planning_context()` never reads the
referenced task file. Every downstream task description stayed generic
("Implement the first task listed in NEXT_STEPS.md..."), forcing each small
local-model call to re-derive what "the next task" even was, and routinely
producing stub/boilerplate output disconnected from the real pending work
(see logs/api-20260705-233402.log: payment-tracker App.tsx ended up as a
bare header component while NEXT_STEPS.md still listed the SQLite/IPC/
Tremor work as untouched).

## Change

Added `ObjectiveResolver` (agent/orchestration/objective_resolver.py):
runs once at the top of `TaskLoop.run()`, before planning. If the objective
names or vaguely implies a task-tracking file (NEXT_STEPS.md, TODO.md,
TASKS.md, ROADMAP.md, PLAN.md) that exists in the workspace, it reads that
file and asks the LLM to rewrite the objective as one concrete, file-naming
deliverable. The resolved objective (and a capped excerpt of the source
file) then flow into both `build_planning_context()` and
`plan_with_criteria()`, so the plan and completion criteria are generated
against real requirements instead of a vague restatement.

Falls back to the original objective, untouched, whenever:
- no task file applies (objective is already concrete / no candidate file exists)
- no coding model is available
- the LLM call fails or returns empty

Wired via a new optional `TaskLoopDeps.objective_resolver` field (defaults
to `None` so existing test fixtures and any future minimal TaskLoopDeps
construction keep working unchanged) and only runs for `develop`/`sdlc`
task types — research objectives are unaffected.

## Files

- `agent/orchestration/objective_resolver.py` — new
- `agent/orchestration/task_exec_ctx.py` — added `objective_resolver` field to `TaskLoopDeps`
- `agent/orchestration/task_loop.py` — resolve objective before `build_planning_context`/`plan_with_criteria`
- `agent/orchestrator.py` — construct and wire `ObjectiveResolver`
- `tests/unit/test_objective_resolver.py` — new, 11 tests
- `tests/integration/test_task_loop.py` — 3 new tests covering wiring (resolved, unresolved/no-resolver, skipped for research)

## Tests

`pytest -q` — 584 passed (570 baseline + 14 new). `ruff check` clean.
