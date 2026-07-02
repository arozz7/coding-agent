# Phase 44 — Stabilize (Improvement Plan Phase A)

Source: `docs/plans/codebase-improvement-plan.md`, Phase A ("Stabilize — do before merging `phase-33/orchestrator-refactor`").

## Summary

The phase-33 orchestrator refactor left the test suite red (16 failed, 48 errors
out of 565 tests) because tests still patched old import locations
(`api.main._orchestrator`, `agent.orchestrator.DeveloperAgent`, etc.) that moved
during the split into `api/deps.py`, `api/routes/*`, and
`agent/orchestration/agent_factory.py`. This phase brings the suite back to
565/565 green and adds the guardrails to keep it there.

## Changes

### Test fixes (stale import paths → current architecture)

- `tests/integration/conftest.py` — `client` fixture now sets
  `api.deps.app_state.orchestrator` directly (a shared singleton, visible to
  every route module) instead of patching a `api.main._orchestrator` global
  that no longer exists. `job_store` is patched per-module
  (`api.routes.tasks.job_store`, `api.routes.system.job_store`) since each
  route module holds its own `from api.deps import job_store` binding.
  `make_mock_orchestrator()` now sets `router.ollama.list_all_models` as an
  `AsyncMock` — a pre-existing mock-completeness gap that was previously
  masked because every `client`-fixture test errored during setup before
  reaching that code path.
- `tests/integration/test_model_fallback.py` — `TestKeywordClassifier` now
  builds a bare `TaskRouter` (`agent.orchestration.task_router.TaskRouter`)
  instead of a full `AgentOrchestrator` with 16 patches targeting classes no
  longer imported into `agent.orchestrator`. `_detect_task_type_keyword` is a
  one-line passthrough to `TaskRouter._detect_keyword`, so the router is the
  right unit to test directly.
- `tests/integration/test_task_loop.py` — `TestTaskLoop` rewritten to build a
  `TaskLoopDeps` bundle and drive `agent.orchestration.task_loop.TaskLoop`
  directly, matching the phase-33 extraction of the loop out of
  `AgentOrchestrator._run_task_loop`.
- `tests/unit/test_project_delete.py` — dropped 12 patches targeting agent
  classes (`DeveloperAgent`, `PlanAgent`, ...) that moved to
  `agent_factory.create_agents()`. Verified all twelve constructors are cheap
  attribute assignments with no I/O, so they're built for real against a
  mocked `model_router` instead.
- `tests/unit/test_verifier_coordinator_criteria.py` — LLM-fallback criterion
  evaluation now calls `ModelRouter.get_evaluator_model()` (the dynamic
  free-model selector), not `get_model("coding")`; the mock router didn't
  provide it as an `AsyncMock`, so `await` on a `MagicMock` was silently
  caught by the broad `except Exception` and produced `"(evaluation error)"`
  for every case. Fixed the mock.
- `tests/integration/test_search.py` — `TestGoogleSearch`'s two failing tests
  called the public `search()` method while only setting
  `GOOGLE_SEARCH_API_KEY`/`GOOGLE_SEARCH_CX`. Since `search()` now tries
  Brave → DuckDuckGo → Playwright-Google before Google CSE (Google is
  deprecated-for-full-web-as-of-Jan-2026 and demoted to last resort), and the
  tests never cleared `BRAVE_SEARCH_API_KEY`, a real key from the local
  `.env` leaked in and caused live network calls. Rewrote both tests to call
  `_search_google()` directly (matching the existing pattern in
  `TestDuckDuckGoSearch`), and added the missing `monkeypatch.delenv("BRAVE_SEARCH_API_KEY", ...)`
  to the two adjacent tests that had the same latent leak but happened not to
  assert on response content.

### Real bugs found and fixed while stabilizing (not stale patches)

- `api/routes/sessions.py` — `DELETE /sessions/{id}` ignored
  `session_memory.delete_session()`'s return value and always reported
  success. Now returns 404 when the session didn't exist, matching
  `SessionMemory.delete_session`'s documented `bool` contract.
- `agent/agents/research_agent.py` — the local-vs-web routing formula let
  `_LOCAL_TASK_RE` (a broad pattern matching phrases like "the docs") override
  explicit web-search signals ("search the web"), contradicting the code's
  own comment: "Tasks that want both still get web search." Extracted the
  formula into a named `_needs_web_search()` function (fixing the bug: an
  explicit web signal now always wins) and pointed the test at the real
  function instead of a hand-duplicated copy of the formula — the duplicate
  is what let the regression go undetected.

### Guardrails (Phase A items 2–4)

- `pyproject.toml` — registered an `external` pytest marker
  (`-m "not external"` for CI). Audited the suite for genuinely
  network-dependent tests; found none — every test that constructs a bare
  `WebTool()` or model-router client either mocks the transport or calls a
  private per-backend method directly, so nothing needed the marker today. It
  exists for the next contributor to use.
- `.github/workflows/ci.yml` — new: runs `ruff check` and
  `pytest -m "not external"` on push to `main` and on every PR.
- Repo hygiene: deleted the stray `nul` file (Windows redirection accident),
  removed the 135 KB `results.sarif` scan artifact from git, deleted the
  root-level `test_ollama.py` ad-hoc script (not a pytest test — no
  assertions, ran a live network call on import), and added `*.sarif`,
  `codeql-db/`, `.codeqldb/`, `/nul` to `.gitignore`.

## Verification

`python -m pytest tests -q` → **565 passed** (run twice to confirm no flakiness).
Before: 501 passed, 16 failed, 48 errors.

## Not done in this phase

Phase B (env-var taint-laundering race, orchestrator workspace-path
unification, session-ID entropy, packaging cleanup) and Phase C (structural
file splits) remain per `docs/plans/codebase-improvement-plan.md`.
