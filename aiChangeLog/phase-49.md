# Phase 49 — Structural debt: orchestrator.py (Phase C task 1/6)

Source: `docs/plans/codebase-improvement-plan.md`, Phase C.

## Changes

- **New `agent/project_lifecycle.py`** — `delete_project()` moved out of
  `AgentOrchestrator` as a plain function taking explicit dependencies
  (`session_memory`, `task_store`, `codebase_memory`, `workspace_path`)
  instead of reading them off `self`. Pure storage-layer coordination, no
  reasoning-layer logic, independently testable without constructing a full
  orchestrator. `AgentOrchestrator.delete_project()` is now a 9-line
  delegator — the public API (`orch.delete_project(name, dry_run=...)`,
  used by `api/routes/workspace.py` and `tests/unit/test_project_delete.py`)
  is unchanged.
- **Agent-dispatch registry** — `_run_specialized_agent`'s 10-branch
  if/elif chain replaced with a dict lookup (`self.agents[...]`) plus a
  3-entry alias map (`_TASK_TYPE_TO_AGENT_KEY`) for the task types that
  don't match their agent-dict key 1:1 (`review`→`reviewer`,
  `test`→`tester`, `security`→`red_team`); everything else maps directly,
  with `developer` as the fallback — matching the original `else` branch.
  `AgentOrchestrator.__init__` now keeps only 3 of the previous 14
  individual `self.<name>_agent` attributes (`plan_agent`, `developer_agent`,
  `tester_agent`) — checked first via grep that `agent/sdlc_workflow.py`
  reads exactly these three off a live orchestrator instance
  (`self.orch.plan_agent`, `.developer_agent`, `.tester_agent`) outside the
  dispatch path, so they can't be folded into `self.agents` without breaking
  that caller. The other 11 (`verifier`, `planner`, `plan_reviewer`,
  `acceptance_tester`, `reviewer`, `architect`, `chat`, `research`, `mapper`,
  `red_team`, `documenter`) were only ever read once each, at their single
  construction call site (`VerifierCoordinator(...)`, `TaskLoopDeps(...)`,
  or the old dispatch chain) — those call sites now read straight from the
  `_agents` dict returned by `create_agents()`, so no attribute is unused.

## Verification

- `python -m pytest tests -q` → 567 passed.
- `ruff check agent api llm mcp observability` → 0 errors.
- `wc -l`: `agent/orchestrator.py` 608 → 538 lines. New
  `agent/project_lifecycle.py`: 99 lines.

## Remaining Phase C tasks

2. `llm/model_router.py` → `llm/config_loader.py` + `llm/evaluator_selector.py`
3. `agent/orchestration/verifier_coordinator.py` → `agent/orchestration/criterion_evaluator.py`
4. `agent/orchestration/task_loop.py` → `task_exec_ctx.py` + `task_loop_cycles.py`
5. `agent/agents/research_agent.py` → extract module-level routing logic
6. `agent/agents/developer_agent.py` → `output_blocks.py` + `fix_loop.py` (highest risk; needs characterization-test check first)
