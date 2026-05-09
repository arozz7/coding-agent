# Phase 28 — Goal-Driven Planning Architecture

## Summary

Replaces the round-cap fix loop with a criterion-driven approach:
the planner generates testable completion criteria alongside tasks, and
the orchestrator targets one failing criterion per fix task until all
criteria pass or the fix budget is exhausted.  Pre-planning context
enrichment gives the planner tech-stack awareness before it generates
tasks or criteria.

## Changes

### Phase 1 — Pre-planning context enrichment

**`agent/orchestration/context_builder.py`**
- New `build_planning_context(objective) -> str` (max ~2000 chars):
  - Tech-stack fingerprint: scans workspace for `package.json`, `Cargo.toml`,
    `requirements.txt`, `pyproject.toml`, `go.mod`, `pom.xml`, `build.gradle`,
    `composer.json`, `Gemfile` → reports detected stacks
  - Workspace snapshot: top-level directory names (ignoring node_modules etc.)
  - Top-2 episodic memories from `session_memory.get_similar_tasks()` (score ≥ 6)
  - Top pitfalls from agent wiki (via `WikiManager.query_skills()`)
  - Does NOT call `build()` internally — deliberately lightweight

### Phase 2 — PlanResult + completion criteria

**`agent/agents/planner_agent.py`**
- New `PlanResult` dataclass: `tasks: List[Dict]`, `completion_criteria: List[str]`
  - Backward-compatible: implements `__iter__`, `__len__`, `__getitem__` so
    callers that treat the result as a plain list continue to work
- New `plan_with_criteria(objective, context, task_type) -> PlanResult`:
  - Calls `plan()` for the task list (unchanged logic)
  - For `develop`/`sdlc` task types: makes a second LLM call via
    `_generate_criteria()` to produce 3-5 testable criteria
  - Research task types skip criteria generation (empty list returned)
- New `_generate_criteria(objective, tasks, context) -> List[str]`:
  - Requests criteria in three auto-checkable formats when applicable:
    `"command exits 0: <cmd>"`, `"file exists: <path>"`,
    `"file contains: <path>:<substring>"`
  - Plain English for behavioral/visual criteria (LLM-evaluated)
  - Capped at 5 criteria; graceful fallback to `[]` on any failure

### Phase 3 — Criterion evaluation

**`agent/orchestration/verifier_coordinator.py`**
- New `CriterionResult` dataclass: `criterion`, `passed`, `detail`
- New `evaluate_criteria(criteria, ws, shell_fn, combined_response) -> list[CriterionResult]`:
  - `"command exits 0: <cmd>"` — runs cmd via `shell_fn`; guards against
    long-running server commands (`npm start`, `flask run`, `uvicorn`, etc.)
  - `"file exists: <rel-path>"` — stat check against workspace
  - `"file contains: <path>:<substring>"` — reads file, checks substring
  - Everything else → `_llm_eval_criterion()` (LLM pass/fail call)
- New `make_targeted_fix_spec(failing, objective, round_num, test_out) -> dict`:
  - Generates a single targeted fix task for one failing criterion
  - Reuses `_detect_fix_phase()` to pick the right phase instruction

### Phase 4 — Criterion-driven loop in orchestrator

**`agent/orchestrator.py`**
- Planning call updated:
  - `build_planning_context(objective)` replaces `build(objective)[:600]`
  - `plan_with_criteria(...)` replaces `plan(...)` → extracts `task_specs` and
    `completion_criteria`
- New loop variables: `_fix_budget`, `_criterion_fix_count`, `_criterion_attempts`
  - `FIX_BUDGET` env var (default 20) caps total criterion fix tasks
  - `_criterion_attempts: dict[str, int]` tracks per-criterion failure count
- Criterion-driven branch added to both the `job_id` path and the no-persistence
  path (guarded by `if completion_criteria and task_type in _VERIFIABLE_TYPES:`):
  - Evaluates all criteria each round
  - If all pass → run score-based verifier once for episodic memory → break
  - If fix budget exhausted → run final verifier → break
  - If all failing criteria abandoned (3 attempts each) → run final verifier → break
  - Otherwise → inject one targeted fix task for the first unabandoned criterion
- Score-based verifier loop unchanged as `elif` fallback (backward compat:
  when `completion_criteria` is empty the old loop runs exactly as before)

### Phase 5 — Unit tests

**`tests/unit/test_context_builder_planning.py`** (new, 7 tests):
- `build_planning_context()` returns string, detects Node/Rust/Python stacks,
  includes episodic memories, caps at 2000 chars, handles missing workspace

**`tests/unit/test_planner_agent.py`** (new, 7 tests):
- `PlanResult` iteration/indexing, default empty criteria
- `plan_with_criteria()` returns `PlanResult`, criteria are strings,
  research type skips criteria, backward-compat iteration, cap at 5, malformed JSON

**`tests/unit/test_verifier_coordinator_criteria.py`** (new, 15 tests):
- `CriterionResult` dataclass
- `file exists:` auto-check (pass/fail)
- `file contains:` auto-check (pass/fail/missing file)
- `command exits 0:` auto-check (pass/fail), server-command guard
- LLM fallback pass/fail
- `make_targeted_fix_spec()` dict shape, detail in description, round_num

## File Map

| File | Status |
|------|--------|
| `agent/orchestration/context_builder.py` | Modified |
| `agent/agents/planner_agent.py` | Modified |
| `agent/orchestration/verifier_coordinator.py` | Modified |
| `agent/orchestrator.py` | Modified |
| `tests/unit/test_context_builder_planning.py` | **New** |
| `tests/unit/test_planner_agent.py` | **New** |
| `tests/unit/test_verifier_coordinator_criteria.py` | **New** |
