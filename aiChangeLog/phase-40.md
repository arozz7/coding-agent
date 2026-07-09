# Phase 40 — Windows cmd.exe exit-code parsing fix

## Root Cause

**Log:** `logs/api-20260525-230750.log`  
**Workspace:** `J:\Projects\agent-workspace\payment-tracker` (Tauri + Vite + React + TypeScript)

In `agent/orchestration/verifier_coordinator.py`, `_check_single_criterion()` used bash syntax to capture exit codes:

```python
out = await shell_fn(f"{cmd}; echo __EXIT__$?")
```

On Windows, `subprocess.Popen(shell=True)` routes through `cmd.exe`. In `cmd.exe`:
- `;` is **not** a command separator (unlike bash) — it's treated as a literal character
- `$?` is a **literal string**, not the exit code variable

Result: `echo __EXIT__$?` never executes. `re.search(r"__EXIT__(\d+)", out)` always returns `None`. Every `command exits 0:` criterion is unconditionally `passed=False` on Windows regardless of actual command outcome.

**Log evidence:**
- `command exits 0: npm test` → `criterion_fix_injected` at lines 692, 775, 858 (3 spurious rounds)
- `command exits 0: npm install` → `acceptance_fix_injected` at lines 1169, 1253, 1340, 1448 (4 spurious rounds)
- Verifier score regression: 0 → 0 → 3 → 1 → 1 (spurious fix tasks destabilizing workspace)

## Files Modified

### `agent/orchestration/verifier_coordinator.py`

1. Added `import platform` to module imports (after `import os`, before `import re`)

2. Replaced the single shell invocation with a platform-aware separator:

```python
# Before
out = await shell_fn(f"{cmd}; echo __EXIT__$?")

# After
_sep = " & echo __EXIT__%ERRORLEVEL%" if platform.system() == "Windows" else "; echo __EXIT__$?"
out = await shell_fn(f"{cmd}{_sep}")
```

On Windows: `cmd.exe` uses `&` as a command separator and `%ERRORLEVEL%` as the exit code variable.  
On Linux/macOS: unchanged — `;` separator and `$?` variable.

## Verification

- Before fix: all `command exits 0:` criteria returned `passed=False` on Windows; 7 combined spurious fix rounds per run
- After fix: `command exits 0:` criteria evaluate correctly; no spurious fix injection for passing commands
- Zero additional files changed; workspace-destabilizing fix tasks eliminated
