# Phase 36 — Acceptance Loop Bug Fixes

## Goal
Fix two oscillation bugs discovered during live payment-tracker runs where the acceptance-fix loop injected 4–5 spurious tasks, corrupted workspace state, and caused verifier scores to collapse (8→2→0→0).

## Root Causes Identified

### Bug 1 — Auto-checkable criteria routed to visual/LLM evaluator
**Symptom:** `file exists: src/main.tsx` in `acceptance_criteria` triggered 4 acceptance fix rounds even after the file was created. Score: 8 → 2 → 0 → 0.

**Root cause:** The planner sometimes places `file exists:`, `file contains:`, or `command exits 0:` patterns in `acceptance_criteria` instead of `completion_criteria`. The acceptance loop passed all acceptance criteria directly to `AcceptanceTesterAgent`, which evaluates via screenshot + LLM — not the filesystem. The LLM couldn't see the file, so it always returned `passed: false`. Each fix round had the developer re-create an already-existing file, progressively corrupting the project.

### Bug 2 — Behavioral acceptance criteria evaluated with no evidence
**Symptom:** 5 behavioral criteria (`"Measurable end state: Phase 1 is expanded into at least five..."`) all returned `passed: false, screenshot: false` on every round, triggering 4 more acceptance fix loops.

**Root cause:** `AcceptanceTesterAgent.run_tests()` was given only the workspace path when no screenshot was available. The LLM prompt had no evidence of what the agent actually produced — it was forced to fail all criteria blindly.

## Files Modified

### `agent/orchestration/task_loop.py`
- **`_run_acceptance_loop`**: Before calling `run_acceptance_tests`, splits acceptance criteria into:
  - `auto_crit` (`file exists:`, `file contains:`, `command exits 0:` prefixes) → evaluated via `evaluate_criteria()` (filesystem/shell, no LLM)
  - `visual_crit` (everything else) → evaluated via `run_acceptance_tests()` (app launch + screenshot + LLM)
  - This prevents misclassified structural criteria from ever reaching the visual evaluator.
- Passes `combined_response` (all task outputs joined) into `_run_acceptance_loop` so the acceptance tester has evidence.

### `agent/orchestration/verifier_coordinator.py`
- **`run_acceptance_tests`**: Added `agent_output: str = ""` parameter; threads it through to `acceptance_tester.run_tests`.

### `agent/agents/acceptance_tester_agent.py`
- **`run_tests`**: Added `agent_output: str = ""` parameter.
- When `screenshot_path` is None but `agent_output` is present, prepends a 6,000-char excerpt to the LLM prompt as evidence so behavioral criteria can be evaluated against actual agent output.

## Verification
- Run 1 (before fix): `file exists: src/main.tsx` → 4 acceptance fix loops, score 8→2→0→0
- Run 2 (after fix 1): `file exists:` bug gone; new bug surfaced — 5 behavioral criteria all `0/5 passed, screenshot: false` → 4 more loops
- Fix 2 applied: behavioral criteria now receive `agent_output` as evidence when no screenshot is available
