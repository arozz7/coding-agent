# Phase 60 — Ground acceptance criteria in the real plan; make stagnation-stop actually stop

## Problem

Reviewing logs/api-20260710-100353.log (the same `payment-tracker` project,
after phase-59's shell-portability fix) surfaced two more issues driving a
14-task, 3577s run that still ended at 4/10 (needs review):

**1. Hallucinated acceptance-criterion filename.** `RequirementsExtractor`
runs before any code exists — at 14:22, using only ARCHITECTURE.md/STACK.md
context — and generated `file contains:
src-tauri/db/migrations/00000000000000_create...`, a Diesel-style
timestamped migration filename. The project uses sqlx with numbered
migrations; the *actual* task (executed later) correctly created
`src-tauri/db/migrations/0001_init.sql`. The extractor's own prompt already
said "Do NOT generate 'file exists' or 'file contains' from naming
conventions" but had no grounding to do otherwise — it can't avoid a
convention-based guess if it never sees what the real plan will build. The
agent spent 3 fix rounds trying to satisfy the fabricated filename, and
ended up creating a second near-empty migration
(`00000000000000_create_expenses.sql`, `-- Initial migration for expense
tracking`) — the third distinct, inconsistent migration file across the two
reviewed runs on this project (the second run's log also produced a
conflicting `001_create_payments.sql`).

**2. Stagnation-stop didn't actually stop.** Traced the verifier score
across this run: 1, 1, 2, 4, 4, 2, 2, 4, 3, 4 — eight full verification
rounds over roughly 40 minutes, never reaching the pass threshold. The
Phase 56 quality gate's `check_stagnation()` call does fire and does append
a "stopping" message, but the code only skipped injecting *that* gate's own
fix — it fell through to the acceptance-criterion loop regardless, which
can inject its own fix task and, on the next iteration, re-enter the same
completion-criteria block and run an entirely fresh holistic verification
pass. The stagnation "stop" was cosmetic; nothing structural actually ended
the run early. `_prev_verifier_score` was also only updated inside the
non-stopping branch, so a later re-entry compared against a stale baseline.

## Change

**Ground acceptance criteria in the plan** (`requirements_extractor.py`,
`planner_agent.py`): `RequirementsExtractor.extract()` now accepts the
already-planned task list (`plan_with_criteria()` computes it before calling
`extract()` — it was simply never passed through) and includes it in the
prompt. Strengthened the "no naming conventions" rule: file-path criteria
must use the exact path named in the tasks, or a glob, never a guessed
filename from a framework convention not otherwise visible in the project.

**Stagnation-stop actually stops** (`task_loop.py`): when the quality gate's
`check_stagnation()` signals stop (or the verifier-round budget is
exhausted), the loop now `break`s out entirely instead of falling through to
the acceptance-criterion loop. `_prev_verifier_score` is updated on every
gate pass, not just the ones that inject a fix, so stagnation comparisons
never use a stale baseline. A regression test confirms the acceptance loop
is never invoked after a stagnation stop (and fails against the pre-fix code
to prove it's a real regression guard, not a vacuous one).

## Files

- `agent/orchestration/requirements_extractor.py` — `extract(..., tasks=None)`, task-grounded prompt, strengthened anti-hallucination rule
- `agent/agents/planner_agent.py` — pass `tasks` through to `extract()`
- `agent/orchestration/task_loop.py` — stagnation/budget-exhaustion breaks the run; always advance `_prev_verifier_score`
- `tests/unit/test_requirements_extractor.py` — 3 new tests (task-grounded prompt, prompt rule, backward-compat without tasks)
- `tests/integration/test_task_loop.py` — `_build_task_loop` gained `acceptance_criteria` param; 1 new regression test verified to fail pre-fix

## Tests

`pytest -q` — 626 passed (622 prior + 4 new). `ruff check` clean.
