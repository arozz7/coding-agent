# Phase 54 — Structural debt: developer_agent.py (Phase C task 6/6, final)

Source: `docs/plans/codebase-improvement-plan.md`, Phase C. This closes out
Phase C — all 6 planned extractions are now done.

## Pre-work: characterization tests (this was the point of reading fully first)

`DeveloperRole.execute()`'s fix-and-rerun loop (~284 lines, a `for` loop with
multiple `break`/`continue` paths threaded through mutable state — `response`,
`files_created`, `shell_outputs`, `failed_outputs`, `verify_cmd`,
`files_fixed_history`, `_ran_npm_install`, `_prev_error_hash`) had **zero**
test coverage anywhere in the codebase before this phase:
`TestDeveloperRole` in `test_agents.py` never passes a `tool_executor`, so
every branch gated by `if tool_executor:` — including the entire fix loop —
was skipped; the SDLC integration tests mock `orch.developer_agent.run()`
wholesale instead of exercising `execute()`'s internals.

Added `tests/unit/test_developer_agent_fix_loop.py` (3 tests, committed
separately before touching any code) covering the loop's three distinct
exit paths: fix succeeds and verify passes, identical-error cycling
detection aborts early (bounded, not exhausting `MAX_FIX_ITERATIONS`), and
no-locatable-source-files aborts before a second model call. All three
passed on first write, confirming the mental model of the loop's control
flow before extraction — real evidence the extraction preserved behavior,
not just "tests still pass because they never exercised this code."

## Changes

- **New `agent/agents/output_blocks.py`** (part A, lower risk) — the seven
  `_*_BLOCK_RE` regexes, `_format_file_with_lines`, `_extract_file_writes`,
  `_extract_file_appends`, `_execute_append`, `_extract_file_edits`,
  `_extract_line_replacements`, `_apply_line_replacement`. All pure parsing
  functions or functions that already took `tool_executor` as an explicit
  parameter — the only coupling to `self` was `self.logger` in
  `_execute_append`/`_apply_line_replacement`, which now take `logger` as an
  explicit parameter instead. Checked first: `architect_agent.py` and
  `tester_agent.py` have their own separate, unrelated
  `_extract_file_writes` methods; `skill_writer.py` has its own separate
  `_SKILL_BLOCK_RE` — no actual coupling to developer_agent.py's copies.
- **New `agent/agents/fix_loop.py`** (part B, highest risk in all of Phase C)
  — `run_fix_loop()`, a free async function taking every dependency
  explicitly (`model_router`, `tool_executor`, `on_phase`, `system_prompt`,
  `logger`, `run_shell_blocks_fn`, plus the mutable `files_created`/
  `shell_outputs` lists it appends to in place) rather than reading `self.*`.
  Returns the updated `response` string (strings are immutable, so this is
  reassigned by the caller rather than mutated in place). Also moved:
  `is_readonly_probe` (re-exported — used 3× elsewhere in
  `developer_agent.py` for computing `real_failures`/`_write_phase_real_failures`
  outside the loop), `_looks_like_npm_missing`, `_npm_install_cmd`,
  `_MISSING_TOOL_NAMES`, `_MAX_ERROR_CHARS`, `_ERROR_FILE_RE`,
  `_SKIP_PATH_PREFIXES`, `_MAX_FIX_FILE_CONTEXT`, `_MAX_FIX_FILE_PER_FILE`,
  `_MAX_RESPONSE_HISTORY`, `MAX_FIX_ITERATIONS` — verified each was used
  only inside the extracted loop before dropping the re-export.
  `DeveloperRole.execute()`'s fix-loop section is now a single
  `response = await run_fix_loop(...)` call.

## Verification

- `python -m pytest tests -q` → 570 passed (567 + the 3 characterization
  tests added before extraction), zero further test changes needed for the
  extraction itself.
- `ruff check agent api llm mcp observability` → 0 errors.
- `wc -l`: `developer_agent.py` 906 → 434 lines (now comfortably under the
  600 hard limit). New `output_blocks.py`: 179 lines. New `fix_loop.py`:
  385 lines (over the 300 soft limit — inherent to the fix loop's actual
  branching complexity; not force-split further given the risk/reward
  established throughout Phase C).

## Phase C summary (all 6 tasks)

| File | Before | After | Extracted to |
|---|---|---|---|
| `agent/orchestrator.py` | 608 | 538 | `agent/project_lifecycle.py` (99) |
| `llm/model_router.py` | 776 | 617* | `llm/config_loader.py` (74), `llm/evaluator_selector.py` (152) |
| `agent/orchestration/verifier_coordinator.py` | 729 | 571 | `agent/orchestration/criterion_evaluator.py` (208) |
| `agent/orchestration/task_loop.py` | 725 | 384 | `task_exec_ctx.py` (114), `task_loop_cycles.py` (267) |
| `agent/agents/research_agent.py` | 703 | 591 | `agent/agents/research_routing.py` (130) |
| `agent/agents/developer_agent.py` | 906 | 434 | `output_blocks.py` (179), `fix_loop.py` (385) |

\* `model_router.py` is the one file still over the 600 hard limit (by 17
lines) — its remaining bulk is `generate()`, core retry/fallback logic that
isn't a separable side-concern the way config-loading/evaluator-selection
were. Flagged explicitly in `aiChangeLog/phase-50.md` rather than
force-split for marginal gain.

Every extraction preserved the existing public API and required either
zero test changes (4 of 6 tasks, via composition/mixin patterns chosen
after checking what tests actually poke) or additive characterization
tests written before the risky change (task 6). No behavior changes beyond
the bug fixes already made in Phase 47 (which happened to touch some of
these same files).
