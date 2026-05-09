# Phase 29 — Acceptance-Test-Driven Fix Loop

**Date:** 2026-05-09

## Objective

Add a behavioral acceptance-testing layer that launches the running application,
screenshots it, and evaluates visual/behavioral criteria via LLM — feeding rich
failure detail back into the fix loop so the agent can fix what the user actually
sees, not just what the code says.

## Problem Solved

The existing Phase 28 criterion-driven fix loop only checks build artifacts
(files, exit codes, text output). It cannot detect visual regressions or runtime
behavioral failures — e.g., a game that builds but renders a blank screen, or a
web app that starts but shows no data. Phase 29 adds a second verification layer
that runs the app and evaluates its behavior.

## New Files

| File | Role |
|------|------|
| `agent/orchestration/requirements_extractor.py` | `RequirementsExtractor.extract(objective, workspace) -> List[str]` — reads workspace docs, generates 3–7 behavioral acceptance criteria |
| `agent/orchestration/app_probe.py` | `AppProbe`, `AppHandle`, `detect_start_command()`, `detect_port()` — launches app, polls for readiness, screenshots, tears down |
| `agent/agents/acceptance_tester_agent.py` | `AcceptanceTesterAgent`, `AcceptanceResult` — single LLM call evaluates all criteria with screenshot context |
| `tests/unit/test_requirements_extractor.py` | 8 tests |
| `tests/unit/test_acceptance_tester_agent.py` | 8 tests |
| `tests/unit/test_app_probe.py` | 15 tests |

## Modified Files

| File | Change |
|------|--------|
| `agent/agents/planner_agent.py` | `PlanResult` gains `acceptance_criteria: List[str]`; `plan_with_criteria()` accepts optional `workspace` param and calls `RequirementsExtractor`; `PlannerAgent.__init__` accepts optional `requirements_extractor` |
| `agent/orchestration/verifier_coordinator.py` | Added `run_acceptance_tests(criteria, workspace, app_probe, acceptance_tester) -> List[AcceptanceResult]`; `make_targeted_fix_spec()` gains `screenshot_path` param |
| `agent/orchestrator.py` | Imports `RequirementsExtractor`, `AppProbe`, `AcceptanceTesterAgent`; instantiates all three; passes `workspace` to `plan_with_criteria()`; extracts `_acceptance_criteria`; adds `ACCEPTANCE_BUDGET` env var (default 5); acceptance test loop runs after build-criteria loop in both job-id and no-persistence paths |

## Architecture

```
plan_with_criteria()
  ├── RequirementsExtractor.extract()  →  acceptance_criteria (List[str])
  └── _generate_criteria()             →  completion_criteria (List[str])

Build-criteria loop (Phase 28)
  └── when done →

Acceptance test loop (Phase 29, per round):
  AppProbe.launch()          — detect entry point, start process, poll readiness
  AppProbe.screenshot()      — npx playwright screenshot
  AcceptanceTesterAgent.run_tests()  — single LLM call, one result per criterion
  AppProbe.teardown()        — kill process tree
  → failing criteria → make_targeted_fix_spec() with screenshot_path → injected fix task
```

## Key Design Decisions

- **Single launch per round** — avoids port-rebinding conflicts; all criteria evaluated against one screenshot
- **Rich text detail** — `AcceptanceResult.detail` is the primary fix signal (screenshot path is supplementary)
- **Graceful fallback** — launch failure → all criteria failed with "(app failed to launch)" detail; LLM failure → all failed with error detail
- **Separate budget** — `ACCEPTANCE_BUDGET` (default 5) independent from `FIX_BUDGET` (default 20)
- **Backward compatible** — `acceptance_criteria=[]` when no extractor or workspace provided; existing callers unaffected

## Test Count

| Before | After | Delta |
|--------|-------|-------|
| 349 | 380 | +31 |

## Environment Variables Added

| Variable | Default | Description |
|----------|---------|-------------|
| `ACCEPTANCE_BUDGET` | `5` | Max acceptance fix rounds per SDLC/develop task |
