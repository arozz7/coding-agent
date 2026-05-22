# Phase 33 — Orchestrator Refactor

## Goal
Split `agent/orchestrator.py` (1607 lines) into focused modules to satisfy the 600-line hard limit and enforce Single Responsibility Principle.

## Files Created

### `agent/orchestration/agent_factory.py` (~85 lines)
- **Moved from:** `AgentOrchestrator.__init__` lines 94–136
- `create_agents(model_router, fs_tool, shell_tool, browser_tool, code_analyzer, pytest_tool, requirements_extractor) -> dict[str, Any]`
- Returns 14 agents keyed by role: developer, plan, tester, reviewer, architect, chat, research, mapper, red_team, documenter, verifier, acceptance_tester, planner, plan_reviewer

### `agent/orchestration/subagent_manager.py` (~160 lines)
- **Moved from:** `AgentOrchestrator.spawn_subagent`, `spawn_multiple_subagents`, `get_subagent_result`, `list_subagents`
- `SubagentManager(agents, context_builder, codebase_memory, session_memory, EventEmittingExecutor, tool_executor)`
- Public API: `spawn()`, `spawn_multiple()`, `get_result()`, `list_all()`

### `agent/orchestration/task_loop.py` (~430 lines)
- **Moved from:** `AgentOrchestrator._run_task_loop` (lines 465–1206)
- **Improvement:** Unified the two duplicate verification paths (job_id vs. no-persistence) via `_TaskExecCtx` — eliminated ~340 lines of duplicated logic
- `_TaskExecCtx`: wraps `fetch_next()`, `add_task()`, `add_tasks()`, `mark_running()`, `mark_done()`, `persist_tasks()`
- `TaskLoopDeps`: dataclass bundling all loop dependencies
- `TaskLoop.run(objective, task_type, session_id, on_phase, job_id) -> dict`

## Files Modified

### `agent/orchestrator.py` (1607 → 599 lines, −63%)
- `__init__`: delegates agent creation to `create_agents()`, wires `SubagentManager` and `TaskLoop(TaskLoopDeps(...))`
- Subagent methods: thin delegation to `self.subagent_manager.*`
- `_run_task_loop`: single-line delegation to `self.task_loop.run(...)`
- All other methods unchanged (run_task, wake, index_workspace, run_stream, delete_project)

### `agent/orchestration/__init__.py`
- Added exports: `create_agents`, `SubagentManager`, `TaskLoop`, `TaskLoopDeps`

## Behavior
No logic changes — pure extraction. All test surfaces identical to pre-refactor.
