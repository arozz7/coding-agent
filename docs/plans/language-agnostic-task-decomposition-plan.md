# Language-Agnostic Task Decomposition & Static-App Verification Plan

**Date:** 2026-08-23
**Trigger:** `logs/api-20260822-210550.log` — a 19,662s quake-remake run that produced an
unverified 1,727-line single `index.html`, vs. Pi Coding Agent producing a modular,
behaviorally-tested equivalent in a fraction of the time from the same prompt and model.
Full root-cause writeup: see chat history 2026-08-23 (not yet an aiChangeLog entry —
this doc is the plan, not the postmortem).
**Constraint from user:** fixes must be language/framework-agnostic (this agent builds
arbitrary projects — Python, Rust, web, CLI tools, etc., not just browser games), and
must bias local-model task granularity smaller, since local models move faster and
hallucinate less on narrow, pinpointed instructions.
**Status:** All three phases plus both out-of-scope items implemented and verified
2026-08-23 (see §8) — see `aiChangeLog/phase-69.md` for the commit-ready summary.

---

## 1. Root causes (recap)

1. **`agent/agents/planner_agent.py`'s CREATION strategy hardcodes a single-file,
   web-specific template.** Lines ~255–284 (`_strategy_hint`) tell the planner, for any
   "complex output (canvas animation, game, simulation, multi-section document,
   > ~150 lines)" objective: write one `FILE:`, then 2 `APPEND:` tasks to the *same*
   file, with a literal "HTML skeleton, CSS, opening `<script>`" template baked into
   the example. This is (a) web/HTML-specific — nonsensical for a Python package, Rust
   crate, or CLI tool — and (b) caps out at "2–3 tasks... each under 150 lines" with no
   mechanism to scale task *count* with actual scope, so a project that clearly needs
   ten files and 2,000+ lines gets funneled into 2–3 giant appends to one file instead.
   This is what produced the single 1,727-line `index.html` and the four 1500s
   hard-timeouts against it.
2. **No file-size/task-size governance survives the plan-review pass.**
   `planner_agent.py` caps the *initial* plan at `_MAX_DEVELOP_TASKS = 6`, but
   `plan_reviewer_agent.py`'s `review()` has no equivalent cap and no sizing guidance —
   its checklist only asks "are any steps too vague or too large for one agent call?"
   qualitatively, without a concrete number to anchor against. In the reviewed run,
   it expanded 4 tasks to 14 (past the 6-task planner cap) while keeping the single-file
   append pattern, adding more oversized append-to-one-file tasks rather than
   redistributing scope across files.
3. **`agent/orchestration/app_probe.py` can't launch or verify a static (no build step,
   no server) deliverable.** `_ENTRY_CANDIDATES` only recognizes `package.json`,
   `app.py`, `main.py`, `server.py`, `index.js`, `server.js` — a bare `index.html`
   (exactly what a "no build step" objective like this one, or `ADR-001`'s own explicit
   choice, produces) isn't in it. `verifier_coordinator.py::run_acceptance_tests()`
   falls back to a `file://` screenshot but then `return []` *without* calling
   `acceptance_tester.run_tests()` on it — so any static single-file/static-site
   deliverable never gets real acceptance verification. This is orthogonal to root
   cause 1 (it affects *any* static app regardless of how many files it's split
   across) and is the reason the run's own final round showed
   `acceptance_skipped_no_entry_point` — the delivered game was never confirmed to
   actually run.

---

## 2. Design constraints

- **No language/framework-specific templates in planner prompts.** Replace the
  HTML-specific CREATION example with a generic decomposition principle the LLM
  applies using whatever module/file boundary is idiomatic for the language and
  framework actually in play — the planner should never need updating for a new
  language.
- **Reuse this project's own file-size convention as the sizing anchor** — this
  repo's `CLAUDE.md` (soft limit 300 lines, hard limit 600 lines per file, "Refactoring
  Protocol" triggered past the soft limit) already encodes exactly the judgment call
  needed here: a defensible, non-arbitrary, language-agnostic number instead of the
  current unenforced "~150 lines." Task descriptions should target work sized to stay
  under that soft limit per file/module, not per arbitrary "chunk."
- **Task count should scale with scope, not be a fixed constant.** Replace "2–3 tasks"
  with: for objectives whose scope clearly exceeds one small file (multiple distinct
  responsibilities — e.g. "player, enemies, weapons, rendering, HUD" is 5+ natural
  modules), the plan should include one task per logical file/module, each producing
  one complete, self-contained file — not N append tasks against a single growing file.
  This is what naturally caps individual generation size (a full new small file, not an
  append against an ever-larger one) and is exactly the shape Pi's output took.
- **`app_probe.py` fix must stay generic.** No new per-framework entry-point special
  case; add a *fallback class* — "no recognized server entry point, but there's a
  servable static file" — handled once, generically (serve the workspace directory
  over a plain static HTTP server), not per file type.
- **Verify no regression to non-web / non-single-file projects.** Existing entry-point
  detection, task counts, and acceptance-testing behavior for e.g. a Python CLI tool or
  a properly-modular JS project must be unchanged.

---

## 3. Phase 1 — `app_probe.py`: static-app fallback actually gets verified

**Goal:** any workspace with no recognized server entry point but at least one HTML
file gets served (not just screenshotted via `file://`, which breaks CORS-gated
features like ES module imports) and *actually acceptance-tested*, generically.

1. `detect_start_command()` — when no `_ENTRY_CANDIDATES` match, check for `*.html`
   files; if found, return a `python -m http.server <port>` command instead of `None`.
   Confirmed via grep: no existing static-serving helper exists elsewhere in this
   codebase to reuse (`criterion_evaluator.py` only *blocklists* `python -m
   http.server` as a criterion-check command, it doesn't launch one) — this flows
   through the same `subprocess.Popen(start_cmd, shell=True, ...)` path `launch()`
   already uses for every other entry point, so it's one new candidate, not a second
   implementation.
2. `run_acceptance_tests()` — remove the early `return []` in the no-entry-point
   branch. Once (1) makes `app_probe.launch()` succeed for static apps via the normal
   path, this branch mostly stops being hit; keep a narrower true-fallback (zero HTML
   files at all) that still returns `[]` since there's genuinely nothing to serve.
3. Confirm `detect_port()` handles the static-server case (fixed known port or parsed
   from the launched process's output, whichever the static-serving helper provides).

**Tests to add:** `detect_start_command()` returns a static-serve command for a
workspace with only `index.html` and no `package.json`; `run_acceptance_tests()`
calls `acceptance_tester.run_tests()` (not `[]`) in that case; existing entry-point
tests (`package.json`, `app.py`, etc.) unchanged.

---

## 4. Phase 2 — `planner_agent.py`: generic, scope-scaled decomposition

**Goal:** replace the CREATION strategy's hardcoded web template with a
language-agnostic principle that produces one task per cohesive file/module for
anything beyond trivial scope, sized to this repo's own 300-line soft limit.

1. Rewrite the `CREATION` branch of `_strategy_hint()` (currently lines ~257–270):
   drop the *single* literal "HTML skeleton, CSS, opening `<script>`" example — one
   example from one language is exactly what over-generalized into "always single
   file, always web." Replace with the principle plus **2–3 short concrete examples
   spanning different languages**, so a weak local model still has something concrete
   to pattern-match against (a fully abstract "decide the module boundaries yourself"
   instruction is its own hallucination risk — the fix is showing the *shape* is
   language-dependent, not removing shape entirely):
   - Simple output (fits comfortably under ~300 lines in one file): 1 task, write the
     complete file. (Unchanged from today.)
   - Larger scope: one `develop` task per logical file/module, each producing one
     complete, self-contained file sized to the ~300-line convention — never N
     `APPEND:` tasks against one growing file as the default shape. Example sketches
     to include in the prompt (illustrative, not literal templates the model should
     copy verbatim):
     - *Browser app*: separate `index.html` (skeleton/markup only), `style.css`,
       and one `.js` file per responsibility (e.g. `player.js`, `world.js`,
       `weapons.js`) — mirrors how Pi Coding Agent split the same objective.
     - *Python*: `main.py` (entry point) plus one module per responsibility under a
       package directory (e.g. `game/player.py`, `game/world.py`).
     - *Rust*: `src/main.rs` (entry point) plus one file per responsibility under
       `src/` (e.g. `src/player.rs`, `src/world.rs`), following normal crate
       conventions.
     The prompt should frame these as *examples of the pattern*, explicitly stating
     the model should pick the idiomatic file layout for whatever language/framework
     the objective actually implies — not restrict itself to one of the three.
     `APPEND:`/`EDIT:`/`REPLACE:` remain available for later legitimate modification
     (`output_blocks.py` is unchanged); they just shouldn't be the initial-creation
     default for anything beyond the "simple" case above.
   - Keep an explicit final task type that actually runs/builds the result
     (unchanged from today — already language-agnostic via examples like `npm start`
     / `python main.py` / `cargo run`).
2. `_MAX_DEVELOP_TASKS` (currently 6) most likely needs raising for this branch, since
   "one task per file" for a multi-module project can legitimately exceed 6 — confirm
   the right number empirically (start from Pi's ~9-file quake-remake as a reference
   point) rather than guessing.

**Tests to add:** a fixture objective with clearly multi-module scope (mocking the
LLM's plan response) produces multiple `develop` tasks, each naming a distinct file,
none of them using an `APPEND:`-first pattern; a trivially small objective still
produces a single task (no regression for the simple case).

---

## 5. Phase 3 — `plan_reviewer_agent.py`: enforce the same sizing anchor as a safety net

**Goal:** the review pass — which currently has no cap and can freely re-expand a plan
back into the single-file-append shape Phase 2 removed from the planner — checks for
and corrects it.

1. Add to the review checklist (`review()`'s prompt): explicitly flag any develop task
   whose description implies appending to the same file 3+ times in a row, or whose
   scope clearly exceeds the ~300-line convention for one task, and require the
   reviewer to split it into per-file tasks instead — mirroring Phase 2's principle so
   the two stages can't fight each other.
2. Consider whether `review()` needs its own task-count ceiling (today it has none) —
   decide based on Phase 2's chosen `_MAX_DEVELOP_TASKS` value.

**Tests to add:** a plan containing an oversized single-file-append pattern gets split
by `review()`'s corrected output (mocked LLM response); a well-formed multi-file plan
passes through unchanged.

---

## 6. Verification

- Full `pytest tests/unit tests/integration -q` after each phase — no regressions.
- Re-run (or dry-run against a recorded transcript of) an equivalent "build a
  multi-feature app" objective and confirm: (a) the plan comes back as multiple
  bounded per-file tasks rather than single-file appends, (b) the final app_probe step
  reaches real acceptance testing instead of `acceptance_skipped_no_entry_point`.

---

## 7. Out of scope items — implemented anyway per user request 2026-08-23

- **Fenced shell-block parsing fragility** — fixed. `output_blocks.py` gained
  `looks_like_leaked_non_shell_content()`; `developer_agent.py._run_shell_blocks`
  skips (logs, doesn't execute) any ```shell block whose content contains another
  block-format marker or a stray fence.
- **Mid-run verification checkpoints** — fixed, conservatively scoped. `task_loop.py`
  now runs the auto-checkable (LLM-free) subset of `completion_criteria` every
  `CHECKPOINT_TASK_INTERVAL` (default 3) completed tasks, logging `mid_run_checkpoint`.
  Pure observability — does not gate, inject fixes, or touch the stagnation/quality-gate
  state machine, to avoid destabilizing the existing verifier control flow.
- Local-model timeout/retry tuning beyond what Phase 2's smaller task sizes already
  buy for free — left as-is; smaller per-file tasks mean smaller generations, which
  already reduces how often the 1500s ceiling gets hit.

## 8. Verification performed

- `pytest tests/unit tests/integration -q` — 678 passed (was 653 before this plan;
  +25 new tests across all three phases plus the two out-of-scope fixes).
- **Live dry-run against the real local model** (`Qwen3.8-27B-Q4_K_S` via
  TurboQuantLoader) with the actual quake-remake objective: `PlannerAgent.plan()`
  returned **8 tasks, one per file** (`index.html` skeleton-only, `style.css`,
  `js/main.js`, `js/player.js`, `js/weapons.js`, `js/level.js`, `js/enemy.js`,
  `js/boss.js`) — zero `APPEND:` tasks, matching Pi Coding Agent's modular shape
  almost file-for-file. This is the direct confirmation the CREATION strategy
  rewrite changes real model behavior, not just what the mocked unit tests assert.
- Checked `_MAX_DEVELOP_TASKS` (6→12) against `FIX_BUDGET` (20) and
  `_MAX_VERIFIER_ROUNDS` (6, develop) — independent budgets, not consumed by the
  initial task count, so raising the cap doesn't create contention.
- Checked `looks_like_leaked_non_shell_content()`'s bare-fence trigger for false
  positives on legitimate heredocs with nested markdown fences — grepped all
  `logs/*.log` for heredoc patterns (`<<'EOF'` etc.) inside shell blocks: zero hits.
  Accepted as a low-risk tradeoff given the fix's whole purpose is catching stray
  fences.
