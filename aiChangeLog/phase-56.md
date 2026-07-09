# Phase 56 — Verifier gates completion; honest task-count reporting

## Problem

Two related bugs let the task loop self-report success on low-quality work:

1. `run_criterion_loop` (task_loop_cycles.py) ran the holistic verifier when
   all auto-checkable criteria passed, but **discarded its score** — it
   always returned `criteria_done=True` regardless of the verifier's
   pass/fail. The summary line read `✅ All auto-checkable criteria
   satisfied — score 1/10`, a checkmark on a failing score. Auto-checkable
   criteria (file exists / file contains / command exits 0) are also
   satisfiable via literal string-insertion fix tasks (see
   `make_targeted_fix_spec`), so criteria passing was a weak signal to begin
   with — see logs/api-20260705-233402.log, where 4 of 5 `!dev` runs scored
   0-2/10 from the verifier while criteria showed 4/4 or 5/5.

2. The Discord summary header (`f"{done_count}/{task_num} tasks
   completed"`) computed `done_count` by counting every summary line
   prefixed with `✅`, including non-task status lines like "All
   auto-checkable criteria satisfied" and "All N acceptance criteria
   satisfied". A 7-task run with both status lines present reported
   "9/7 tasks completed".

## Change

**Verifier quality gate** (task_loop.py): after the criterion-fix loop
resolves (criteria satisfied, budget exhausted, or abandoned), if the
verifier that already ran inside that resolution did NOT pass, don't
proceed to "done" — inject the same gap-driven fix tasks the holistic
(no-criteria) verifier path already uses (`make_fix_specs`), bounded by the
same `check_stagnation` logic (stops after 2 rounds at score 0, a 2+ point
score drop, or a plateau at score ≥5) and the same `DEVELOP_VERIFIER_ROUNDS`
cap. This reuses existing, already-tested machinery rather than adding a new
fix-loop shape.

**Honest reporting**: replaced the `✅`-line-scanning done_count with
explicit `_completed_task_count`/`_failed_task_count` counters incremented
only when a real task executes. The job summary header now also surfaces
`needs_review` (verifier ran and did not pass) with an explicit
`⚠️ verifier score N/10 (needs review)` marker and adjusted next-steps text,
instead of implying success whenever no task literally errored.

The `✅ All auto-checkable criteria satisfied` line itself now renders `⚠️`
instead of `✅` when the verifier didn't pass, so the line's own mark no
longer contradicts the score printed right next to it.

## Files

- `agent/orchestration/task_loop.py` — quality gate after criterion loop; dedicated task counters; `needs_review` reporting
- `agent/orchestration/task_loop_cycles.py` — honest ✅/⚠️ mark on the criteria-satisfied summary line
- `tests/integration/test_task_loop.py` — `TestVerifierQualityGate`: gate passes cleanly when verifier agrees, injects a fix + flags review when it doesn't, and a regression test locking in that status lines no longer inflate the completed-task count

## Tests

`pytest -q` — 587 passed (584 prior + 3 new). `ruff check` clean.
