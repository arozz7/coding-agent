# Phase 61 — Correction to phase-60's stagnation-stop fix

## What was wrong with phase-60

Commit `a5e8489` (phase-60) changed the verifier quality gate so that a
stagnation stop, or exhausting the verifier-round budget, would `break` out
of `TaskLoop.run()`'s main loop entirely — ending the run rather than
falling through to the acceptance-criterion loop. The stated reasoning was
that the acceptance loop's fall-through was letting the run silently
re-trigger fresh verification passes after the gate had already "given up,"
based on `logs/api-20260710-100353.log` showing 8 verification rounds over
~40 minutes without converging.

That reasoning was incomplete. Replaying the same log's score sequence
(1, 1, 2, 4, 4, 2, 2, 4, 3, 4) through `check_stagnation()` shows the
2-point-drop stop condition first fires at round 6 (score drops from 4 to
2) — which is **exactly** where the log shows the acceptance-criterion loop
first engaging (`criterion_evaluator: total=6, passed=3` immediately
followed by the first `acceptance_fix_injected`). That loop went on to
converge from 3/6 to 6/6 acceptance criteria over the next several rounds.
A hard `break` at round 6 would have ended the run before the acceptance
loop — the mechanism that was actually making progress — ever got a turn.
This was caught by an advisor review before merging further, and confirmed
by scripting the actual `check_stagnation()` trace against the real score
sequence rather than trusting code inspection alone.

## The corrected fix

Replaced the `break` with a `_quality_gate_exhausted` flag. Once the
holistic-verifier quality gate stagnates (or exhausts its own round budget),
it permanently stops re-injecting *its own* fix tasks and stops re-logging
"Verifier gate" messages — that repetition was the real, verifiable waste.
It no longer stops the whole run: the acceptance-criterion loop still runs
to its own budget afterward, since it operates on specific named criteria
independently of the holistic score and was demonstrably still converging
in the run that motivated this fix in the first place.

## Tests

Two regression tests cover both ways `_quality_gate_exhausted` gets set:
- `test_quality_gate_exhaustion_does_not_reinject_but_lets_acceptance_loop_run`
  — a 2-point score drop triggers `check_stagnation`'s stop condition;
  confirms exactly one "Verifier gate" line appears (no re-injection) and
  the acceptance loop still runs. Verified to fail against the phase-60
  `break` version.
- `test_verifier_round_budget_exhaustion_without_stagnation_still_lets_acceptance_run`
  — a 3/4-oscillating score sequence that never trips
  `check_stagnation`'s drop/plateau conditions, exhausting
  `_MAX_VERIFIER_ROUNDS` (6) via the round-budget check instead; confirms
  the same behavior through the branch the first regression test didn't
  cover (flagged by advisor review as the one path "verified against real
  data" didn't actually verify).

`pytest -q` — 627 passed (626 prior + 1 net new test — one test was rewritten
in place, one added). `ruff check` clean.

## Files

- `agent/orchestration/task_loop.py` — `_quality_gate_exhausted` flag replaces the `break`
- `tests/integration/test_task_loop.py` — regression test rewritten + new round-budget-exhaustion test
