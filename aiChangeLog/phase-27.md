# Phase 27 — Fix Loop Resilience

## Summary

Six improvements to make the developer agent fix loop more resilient for any
project type: score regression awareness, zero-score early stop, signal-based
phase detection, file-chunking directive, and general truncation detection.

## Changes

### Task 1 — Score regression feedback

**`agent/orchestrator.py`**
- `make_fix_specs()` now receives `prev_score=_prev_verifier_score`
- `_prev_verifier_score` is stored *after* `make_fix_specs()` is called so the
  previous round's score is always visible to the coordinator

**`agent/orchestration/verifier_coordinator.py`**
- `make_fix_specs()` gains `prev_score: int = -1` parameter
- When `prev_score >= 0` and `vresult.score < prev_score` and files were changed,
  a `⚠️ REGRESSION` block is injected naming the files and scores

### Task 2 — Zero-score consecutive stop

**`agent/orchestrator.py`** (both job_id and no-persistence paths)
- Added `_zero_score_count = 0` tracking variable
- If `vresult.score == 0` two rounds in a row → stop with "stuck at 0/10" message
- Counter resets to 0 whenever score > 0

### Task 3 — Dynamic phase detection from `test_output`

**`agent/orchestration/verifier_coordinator.py`**
- New `_detect_fix_phase(test_out, gaps_text, round_num) -> (phase, instruction)`
- Priority order: `[truncated]` → file-incomplete; syntax signals → syntax;
  runtime signals → runtime; test-failure signals → test-failures; else round_num fallback
- Signals are language-agnostic (SyntaxError, parse error, error TS, cargo check,
  Traceback, panic, FAILED, AssertionError, etc.)
- Round number is now a tiebreaker only

### Task 4 — File chunking directive

**`agent/orchestration/verifier_coordinator.py`**
- When `[truncated]` appears in `test_output` or `gaps_text`, a
  `⚠️ FILE TRUNCATION DETECTED` block is injected into the fix task
- Instructs agent to write files <150 lines, extract logic into modules, use imports
- Language-agnostic — applies to any project (Python, Rust, JS, etc.)

### Task 5 — General truncation detection

**`agent/agents/verifier_agent.py`**
- New `_detect_truncated_files(ws: Path) -> list[str]` method
- Checks last non-blank line of each source file using a tail heuristic:
  - JS/TS/Rust/Go/C/Java: ends with `{`, `(`, `,`
  - HTML: missing `</html>`
  - Python: last non-comment line is a `def`/`class`/control-flow header ending with `:`
  - JSON: content doesn't end with `}` or `]`
- Ignores `node_modules`, `.git`, `__pycache__`, `.agent-wiki`, `logs`
- Capped at 10 files; appends `[truncated] FAIL: ...` block to `test_output`
- `_run_tests()` refactored: project-type check produces one part, truncation check
  runs for ALL project types as a second pass, parts joined with `\n\n`
- LLM prompt updated to reference general truncation (not web-games only)

### Task 6 — Unit tests

**`tests/unit/test_verifier_coordinator.py`** (new)
- `TestDetectFixPhase`: 8 tests covering all signals and round-number fallback
- `TestMakeFixSpecsRegression`: 4 tests — regression present/absent cases
- `TestMakeFixSpecsChunking`: 3 tests — directive present/absent cases
- `TestMakeFixSpecsPhaseLabel`: 2 tests — phase label in description

**`tests/unit/test_verifier_agent.py`** (updated)
- Added `TestDetectTruncatedFiles`: 7 tests covering JS open-brace, HTML missing tag,
  Python def-header, JSON unclosed, node_modules ignored, complete files not flagged

## File Map

| File | Status |
|------|--------|
| `agent/orchestrator.py` | Modified |
| `agent/orchestration/verifier_coordinator.py` | Modified |
| `agent/agents/verifier_agent.py` | Modified |
| `tests/unit/test_verifier_coordinator.py` | **New** |
| `tests/unit/test_verifier_agent.py` | Modified |
