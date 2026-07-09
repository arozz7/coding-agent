# Phase 38 — Malformed `command exits 0:` Criteria Fix

## Goal
Prevent acceptance-loop oscillation caused by `command exits 0:` criteria containing embedded prose (`| End state: ...` / `| Constraint: ...`) that the shell misinterprets as pipe operators, making every evaluation fail regardless of what code is written.

## Root Cause

**Symptom:** `command exits 0: npm install && cargo check --manifest-path src-tauri/Cargo.toml` fired `acceptance_fix_injected` 4 times (fix_nums 1–4). Verifier score regressed 3 → 1 → 1 → 0 → 1 across the fix rounds.

**Log evidence (api-20260525-193648.log, line 1002–1003):**
```
"command": "npm install && cargo check --manifest-path src-tauri/Cargo.toml
  | End state: All npm and Rust dependencies resolve and the backend compiles without errors
  | Constraint: Must not require manual environment variable setup or global toolchain modifications
  ; echo __EXIT__$?"
```

The shell interprets `|` as a pipe to `End` (a nonexistent program), so `__EXIT__` never appears in stdout, `re.search(r"__EXIT__(\d+)", ...)` finds no match, and `passed=False` is returned every time — 50 consecutive `command_exits_0` failures in the score store (confidence 0.02).

**Root cause in `requirements_extractor.py`:** The system prompt said "STRUCTURE — every criterion must have three parts: (1) measurable end state, (2) stated check, (3) constraint". The LLM treated this as an instruction to embed all three parts into the output string using `| End state:` and `| Constraint:` as separators, rather than using the three parts as a mental model to select the right format.

## Files Modified

### `agent/orchestration/requirements_extractor.py`
- **System prompt rewrite**: Replaced "STRUCTURE — every criterion must have three parts" with "THINKING (do NOT include in output)" — framing the three considerations as private reasoning, not output content.
- Added an explicit rule for `command exits 0:`: "the value after the colon must be a BARE SHELL COMMAND only — no 'End state:', no 'Constraint:', no pipe-separated prose."
- Added a correct multi-part example: `"command exits 0: npm install && cargo check --manifest-path src-tauri/Cargo.toml"` to show `&&` is fine but `|` prose is not.

### `agent/orchestration/verifier_coordinator.py`
- **`_check_single_criterion`** (line ~225): After extracting `cmd`, strip any ` | End state:` / ` | Constraint:` suffix before running in the shell. Handles both spaced (`" | end state:"`) and unspaced (`" |end state:"`) variants, case-insensitive.
- **`make_targeted_fix_spec`** (line ~435): Same strip applied to the fix-spec command so the developer's fix task description also shows a clean command.

## Verification

- Before fix: `command exits 0: npm install && cargo check ... | End state: ...` → shell runs `cargo check | End` → `End` not found → `passed=False` every round → 4 acceptance fix loops, score 3→1→1→0→1
- After fix (defensive strip): `cmd` is truncated to `npm install && cargo check --manifest-path src-tauri/Cargo.toml` before shell execution → evaluates the real build outcome
- After fix (prompt): new `RequirementsExtractor` runs should emit clean commands with no prose suffixes
