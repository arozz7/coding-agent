# Phase 52 — Structural debt: task_loop.py (Phase C task 4/6)

Source: `docs/plans/codebase-improvement-plan.md`, Phase C.

## Changes

- **New `agent/orchestration/task_exec_ctx.py`** — `_TaskExecCtx` (job_id
  vs. no-persistence task-list abstraction) and `TaskLoopDeps` (the
  dependency bundle `TaskLoop` is constructed with) moved out, zero
  coupling to the loop's own control flow. `task_loop.py` imports both back
  (`from agent.orchestration.task_exec_ctx import TaskLoopDeps, _TaskExecCtx`),
  and `TaskLoopDeps` is re-exported so the existing
  `from agent.orchestration.task_loop import TaskLoop, TaskLoopDeps` import
  (used by `tests/integration/test_task_loop.py`) keeps working unchanged.
- **New `agent/orchestration/task_loop_cycles.py`** — the four helpers
  called from `TaskLoop.run()`'s main loop that don't participate in the
  loop's own control-flow state:
  - `check_stagnation` and `build_research_extra`: were already
    `@staticmethod` with zero `self` usage, so they're plain module
    functions now.
  - `run_criterion_loop` and `run_acceptance_loop`: need `TaskLoopDeps` +
    a logger, grouped into a small `_FixCycleRunner` class (same
    composition pattern as `CriterionEvaluator` from Phase 51) rather than
    free functions with 8+ parameters each.
  `TaskLoop.__init__` now constructs `self._fix_cycles = _FixCycleRunner(deps)`;
  `run()`'s four call sites became `self._fix_cycles.run_criterion_loop(...)`,
  `self._fix_cycles.run_acceptance_loop(...)`, `check_stagnation(...)`,
  `build_research_extra(...)`.
- Checked test coupling first: no test references `_run_criterion_loop`,
  `_run_acceptance_loop`, `_check_stagnation`, `_build_research_extra`, or
  `_TaskExecCtx` directly — only `TaskLoop` and `TaskLoopDeps` are imported
  — so this extraction needed zero test changes.

## Verification

- `python -m pytest tests -q` → 567 passed, zero test changes needed.
- `ruff check agent api llm mcp observability` → 0 errors.
- `wc -l`: `task_loop.py` 725 → 384 lines. New `task_exec_ctx.py`: 114
  lines. New `task_loop_cycles.py`: 267 lines.

## Remaining Phase C tasks

5. `agent/agents/research_agent.py` → extract module-level routing logic
6. `agent/agents/developer_agent.py` → `output_blocks.py` + `fix_loop.py` (highest risk)
