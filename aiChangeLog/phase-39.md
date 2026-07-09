# Phase 39 — Bayesian Score Store Poisoning Fix

## Goal
Prevent the criterion fix loop from prematurely abandoning legitimate criteria after the `command_exits_0` Bayesian confidence score was poisoned to 0.018 by 53 malformed-command failures from the phase-38 bug.

## Root Cause

**Symptom (api-20260525-205156.log):** `command exits 0: npm run lint` injected one criterion fix (fix_num=1), still failed, then the loop exited with `task_loop_complete` after only 8 tasks. Score stuck at 1/10. Phase 1 never completed.

**Root cause chain:**
1. Phase-38 bug: `command exits 0: npm install && cargo check | End state: ...` ran 53 times in the previous session (api-20260525-193648.log). The shell piped to nonexistent `End`, so every run returned non-zero. Each failure was recorded in `data/criterion_scores.json`.
2. After 53 failures: `command_exits_0` confidence = (0+1)/(53+2) = **0.018**.
3. `attempt_budget("command exits 0: npm run lint")` = **1** (conf < 0.35 threshold → "give up fast").
4. After 1 fix attempt for `npm run lint` (attempts=1, budget=1): `1 < 1 = False` → target exhausted → `target is None` → "All auto-checkable failing criteria abandoned" → loop exits.
5. The developer never got a second chance to fix the lint errors.

**Key observation:** All 53 recorded `command_exits_0` failures were from commands that could never succeed (prose-embedded), not from the developer failing to fix real code. The data was structurally poisoned, not an accurate reflection of fixability.

## Files Modified

### `agent/orchestration/criterion_score_store.py`
- **`attempt_budget`**: Changed minimum return value from `1` to `2` for `conf < _SKIP_THRESHOLD`. Even historically-low-confidence criterion types now get at least 2 fix attempts — preventing a single poisoned session from permanently cutting the budget to 1.

### `data/criterion_scores.json`
- **Reset `command_exits_0`**: Set `attempts` back to 0 (was 53, all from malformed commands). Confidence reverts to Bayesian prior of 0.5. Budget reverts to 3 (default, no data). This is a one-time data cleanup; the phase-38 fix prevents future poisoning from malformed commands.

## Verification

- Before fix: `attempt_budget("command exits 0: ...")` = 1 → 1 fix round → loop abandons → score 1/10
- After fix: `attempt_budget("command exits 0: ...")` = 3 (no data, default prior) → up to 3 fix rounds per criterion type → developer has more attempts to resolve real lint/build failures
- Minimum budget of 2 ensures that even if `command_exits_0` data accumulates low-confidence results in the future, the loop never gives up after a single attempt
