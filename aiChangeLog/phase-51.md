# Phase 51 — Structural debt: verifier_coordinator.py (Phase C task 3/6)

Source: `docs/plans/codebase-improvement-plan.md`, Phase C.

## Changes

- **New `agent/orchestration/criterion_evaluator.py`** — `evaluate_criteria`,
  `_eval_one`, `_llm_eval_criterion`, plus the `CriterionResult` dataclass
  and their private helpers (`_glob_filtered`, `_EXCLUDE_DIRS`) moved into a
  `CriterionEvaluator` class. This cluster only depends on `model_router`
  (for the LLM-fallback path on behavioral/visual criteria) and a logger —
  lighter coupling than `run_verification`/`run_acceptance_tests`/
  `make_fix_specs`, which need the `verifier_agent` and richer coordinator
  state. Used composition here (not the mixin pattern from Phase 50) since
  checking callers first showed nothing pokes `_eval_one`/`_llm_eval_criterion`
  directly or constructs a criterion-evaluator object — every caller
  (`agent/orchestration/task_loop.py`, `tests/unit/test_verifier_coordinator_criteria.py`)
  goes through the public `coord.evaluate_criteria(...)` method or imports
  `CriterionResult`, both preserved.
- `VerifierCoordinator.__init__` now constructs
  `self._criterion_evaluator = CriterionEvaluator(model_router)`;
  `VerifierCoordinator.evaluate_criteria()` is a 3-line delegator.
  `CriterionResult` is re-exported from `verifier_coordinator.py`
  (`from agent.orchestration.criterion_evaluator import CriterionEvaluator, CriterionResult`)
  so the existing `from agent.orchestration.verifier_coordinator import
  CriterionResult, VerifierCoordinator` import in tests keeps working
  unchanged.
- `_select_primary_file` (used by `make_fix_specs`, unrelated to criterion
  evaluation) stays in `verifier_coordinator.py`.

## Verification

- `python -m pytest tests -q` → 567 passed, zero test changes needed.
- `ruff check agent api llm mcp observability` → 0 errors.
- `wc -l`: `verifier_coordinator.py` 729 → 571 lines (now under the 600
  hard limit). New `criterion_evaluator.py`: 208 lines.

## Remaining Phase C tasks

4. `agent/orchestration/task_loop.py` → `task_exec_ctx.py` + `task_loop_cycles.py`
5. `agent/agents/research_agent.py` → extract module-level routing logic
6. `agent/agents/developer_agent.py` → `output_blocks.py` + `fix_loop.py` (highest risk)
