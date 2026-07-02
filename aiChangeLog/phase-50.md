# Phase 50 — Structural debt: model_router.py (Phase C task 2/6)

Source: `docs/plans/codebase-improvement-plan.md`, Phase C.

## Changes

- **New `llm/config_loader.py`** — `_expand_env` (renamed `expand_env`, now
  a public module function) and the YAML-read + `ModelConfig` construction
  parts of `_load_configs` extracted as pure functions (`load_config_file`,
  `build_model_config`). Neither was referenced externally (checked via
  grep before moving). `ModelRouter._load_configs` still owns wiring the
  parsed configs into `self.configs`/`self.config_by_name`/`self.rate_limiter`
  — that part stays a method since it's inherently about mutating router
  state, not config parsing.
- **New `llm/evaluator_selector.py`** — `get_evaluator_model`,
  `_score_free_model`, `_record_openrouter_rate_limit`,
  `_openrouter_cooldown_active`, `_is_rate_limit_error` moved into an
  `EvaluatorSelectorMixin`, mixed into `ModelRouter` via inheritance
  (`class ModelRouter(EvaluatorSelectorMixin)`) rather than composed as a
  separate object. Composition was the original plan, but
  `tests/integration/test_model_fallback.py` pokes `router._evaluator_cache`,
  `router._openrouter_cooldown_until`, etc. directly as flat attributes and
  calls `router.get_evaluator_model()` / `router._openrouter_cooldown_active()`
  as router methods — a composed object would have required rewriting every
  one of those. The mixin keeps the exact same flat `self` attribute/method
  surface, so **zero test changes were needed** — all 567 tests passed on
  the first run after the extraction.

## Verification

- `python -m pytest tests -q` → 567 passed, no test modifications required.
- `ruff check agent api llm mcp observability` → 0 errors.
- `wc -l`: `llm/model_router.py` 776 → 617 lines. New `config_loader.py`:
  74 lines. New `evaluator_selector.py`: 152 lines.

## Known gap: still 17 lines over the 600 hard limit

`model_router.py` is at 617, not under 600. The remaining large method is
`generate()` (~251 lines) — the core LLM-call retry/fallback/circuit-breaker
orchestration. Unlike config-loading and evaluator-selection, this isn't a
separable side-concern; it's ModelRouter's central responsibility, deeply
coupled to `self.rate_limiter`, `self.cost_tracker`, `self.health_checker`,
and the switch-callback mechanism. Forcing a split here to shave 17 lines
would trade a clean, fully-verified extraction for a much higher-risk one
with worse payoff. Left as-is; flagging rather than force-fitting.

## Remaining Phase C tasks

3. `agent/orchestration/verifier_coordinator.py` → `agent/orchestration/criterion_evaluator.py`
4. `agent/orchestration/task_loop.py` → `task_exec_ctx.py` + `task_loop_cycles.py`
5. `agent/agents/research_agent.py` → extract module-level routing logic
6. `agent/agents/developer_agent.py` → `output_blocks.py` + `fix_loop.py` (highest risk)
