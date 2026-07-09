# Phase 58 — Small-model task sizing + run outcome ledger

## Problem

Two remaining gaps from the original diagnosis (docs from the Discord/log
review): plans for `develop` objectives targeted 5-8 tasks with no upper
bound enforced beyond the prompt itself, and task descriptions weren't
required to name their target file(s) — exactly the generic-task pattern
seen in the reviewed log ("Implement the first task listed in
NEXT_STEPS.md..."). There was also no way to tell, run over run, whether
Phases 1-3 actually reduced wasted cycles — only anecdote from reading logs.

## Change

**Tighter, enforced task sizing** (planner_agent.py): the develop/sdlc
planning prompt now asks for 3-6 tasks (was 5-8) with an explicit
justification ("the smaller local model executing each one does better with
fewer, narrower tasks"), and every develop/test task description must name
its target file(s) or directory. Since small local models don't reliably
follow prompt-only constraints, `plan()` also hard-truncates any
develop/sdlc plan to `_MAX_DEVELOP_TASKS = 6` regardless of what the model
returns — a model that ignores the instruction can no longer generate an
unbounded plan.

**Run outcome ledger** (new `agent/orchestration/run_ledger.py`): `TaskLoop`
now appends one JSON line per develop/sdlc run to `data/run_ledger.jsonl` —
verifier score, whether it actually passed, real completed/failed task
counts, criterion/acceptance fix-round counts, and duration. This is the
same signal set the original diagnosis had to reconstruct by hand from raw
logs; going forward it's queryable directly. Never raises — a ledger write
failure is logged and swallowed, never breaks the task loop it's observing.
Wired as an optional `TaskLoopDeps.run_ledger` field (default `None`, same
pattern as `objective_resolver`) so it's a no-op wherever not configured.

## Files

- `agent/agents/planner_agent.py` — tightened develop/sdlc task-count guidance, target-file requirement, `_MAX_DEVELOP_TASKS` hard cap
- `agent/orchestration/run_ledger.py` — new
- `agent/orchestration/task_exec_ctx.py` — added `run_ledger` field to `TaskLoopDeps`
- `agent/orchestration/task_loop.py` — timing + ledger record at the end of `run()`
- `agent/orchestrator.py` — construct and wire `RunLedger`
- `tests/unit/test_run_ledger.py` — new, 6 tests
- `tests/integration/test_task_loop.py` — 1 test for the plan-length cap, 3 for ledger wiring

## Tests

`pytest -q` — 601 passed (591 prior + 10 new). `ruff check` clean.

## Phase summary — agent-quality improvement plan complete

| Phase | Problem | Fix |
|---|---|---|
| 55 | Vague objectives planned with no file context | `ObjectiveResolver` grounds the objective in the referenced task file before planning |
| 56 | Criteria passing declared victory over a failing verifier; task counts double-counted status lines | Verifier quality-gates completion; honest completed/failed counters; `needs_review` reporting |
| 57 | Criteria and fix instructions gameable via literal string-insertion | Banned markdown `file contains` criteria; manifest files get package-manager fix commands, not APPEND |
| 58 | Plans too large for small local models; no way to measure impact | 3-6 task cap (down from 5-8) + hard truncation; target-file requirement; `RunLedger` for run-over-run measurement |
