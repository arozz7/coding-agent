# Phase 69 — Language-agnostic task decomposition + static-app verification

Full design doc: `docs/plans/language-agnostic-task-decomposition-plan.md`.
Trigger: `logs/api-20260822-210550.log` — a 19,662s quake-remake run that produced an
unverified 1,727-line single `index.html`, vs. Pi Coding Agent producing a modular,
behaviorally-tested equivalent from the same prompt/model in a fraction of the time.

## Root causes

1. `planner_agent.py`'s CREATION strategy hardcoded a single-file, web-specific
   template for any "complex" objective ("write FILE: HTML skeleton, CSS, opening
   `<script>`" then two `APPEND:` tasks to the same file) — nonsensical for a
   non-web project, and capped at "2–3 tasks... under 150 lines" with no mechanism to
   scale task count with actual scope. This directly produced the single giant
   `index.html` and the four 1500s hard-timeouts against it in the reviewed run.
2. `plan_reviewer_agent.py`'s review pass had no size/count governance and could
   freely re-expand a plan (4→14 tasks in the reviewed run) while preserving the same
   broken single-file-append shape.
3. `app_probe.py`'s entry-point detection didn't recognize a bare `index.html` (no
   `package.json`) as launchable, and `verifier_coordinator.py::run_acceptance_tests()`
   returned `[]` early in that case — so static single-file/no-build-step deliverables
   (a common output shape) never got real acceptance verification. The reviewed run's
   final round showed `acceptance_skipped_no_entry_point` — the game was never
   confirmed to actually run.

Two additional issues found during the same review, fixed alongside per user request:

4. A stray/unclosed ```shell fence let `SHELL_BLOCK_RE`'s non-greedy match swallow a
   `REPLACE:` block and bare JS statements as if they were shell commands, each
   failing with `[WinError 2]` (`logs/api-20260822-210550.log` 02:30:33-34).
5. The task loop produced zero verification signal until every planned task finished —
   for a long-running plan, that's hours with no early warning.

## The fix

- **`app_probe.py`** — `_static_serve_command()` (new): when no `_ENTRY_CANDIDATES`
  match but `*.html` files exist, returns a `python -m http.server <free-port>`
  command (port found via `_find_free_port()`, avoids collisions across concurrent
  workspaces). `detect_port()` recovers the embedded port. Generic — no new
  per-framework special case, one fallback covers any static HTML/CSS/JS output
  regardless of what produced it.
- **`verifier_coordinator.py`** — `run_acceptance_tests()`'s no-entry-point branch no
  longer discards the static `file://` screenshot with an early `return []`; it now
  calls `acceptance_tester.run_tests()` against it like the normal path does. Still
  returns `[]` (no change) when there's genuinely no HTML to screenshot, or the
  screenshot itself fails.
- **`planner_agent.py`** — CREATION strategy rewritten: simple output stays 1 task;
  larger scope gets **one task per logical file/module**, sized to this repo's own
  300-line convention (`CLAUDE.md`), with 2-3 concrete examples spanning different
  languages (browser/Python/Rust) so a weak local model has something to pattern-match
  without overfitting to one language. `APPEND:`/`EDIT:`/`REPLACE:` remain available
  for later modification, just not the default shape of a file's *first* creation.
  `task_count_hint` reworded — task count now explicitly scales with scope instead of
  a fixed "3-6 tasks" range. `_MAX_DEVELOP_TASKS` raised 6→12 to accommodate
  one-task-per-file plans (independent of `FIX_BUDGET`/`_MAX_VERIFIER_ROUNDS`, so no
  budget contention from the raise — verified).
- **`plan_reviewer_agent.py`** — review checklist gained the same sizing guidance
  (300-line target, flag 3+ consecutive same-file appends) so the review pass can't
  undo the planner's decomposition; review's output is now truncated at
  `_MAX_DEVELOP_TASKS` too (previously unbounded).
- **`output_blocks.py`** — `looks_like_leaked_non_shell_content()` (new): detects a
  ```shell block whose content contains another block marker (`FILE:`/`APPEND:`/
  `EDIT:`/`REPLACE:`/etc.) or a stray fence. **`developer_agent.py`** —
  `_run_shell_blocks` skips (logs, doesn't execute) any block that matches.
- **`task_loop.py`** — after every `CHECKPOINT_TASK_INTERVAL` (default 3) completed
  tasks, runs the auto-checkable (filesystem/shell, LLM-free) subset of
  `completion_criteria` and logs `mid_run_checkpoint`. Pure observability — doesn't
  gate the loop, inject fixes, or touch stagnation/quality-gate state.

## Verification

- `pytest tests/unit tests/integration -q` — 678 passed (was 653; +25 new tests).
- **Live dry-run against the real local model** (Qwen3.8-27B-Q4_K_S via
  TurboQuantLoader) with the actual quake-remake objective: `PlannerAgent.plan()`
  returned 8 tasks — one per file (`index.html` skeleton-only, `style.css`,
  `js/main.js`, `js/player.js`, `js/weapons.js`, `js/level.js`, `js/enemy.js`,
  `js/boss.js`), zero `APPEND:` tasks — confirming the prompt rewrite changes real
  model output, not just what mocked tests assert.
- Grepped all `logs/*.log` for heredoc-with-nested-fence patterns to check the new
  leaked-content guard's false-positive risk: zero hits.

## Files

- `agent/orchestration/app_probe.py`, `verifier_coordinator.py`
- `agent/agents/planner_agent.py`, `plan_reviewer_agent.py`, `output_blocks.py`,
  `developer_agent.py`
- `agent/orchestration/task_loop.py`
- `tests/unit/test_app_probe.py`, `test_verifier_coordinator_acceptance.py` (new),
  `test_planner_agent.py`, `test_plan_reviewer_agent.py` (new), `test_output_blocks.py`,
  `test_agents.py`
- `tests/integration/test_task_loop.py`
- `docs/plans/language-agnostic-task-decomposition-plan.md` (new)
