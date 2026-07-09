# Phase 41 — fix(shell_tool): route compound commands through cmd.exe on Windows

## Root cause

`logs/api-20260628-141657.log` showed two oscillating criteria that never
resolved within their fix budget:

- `command exits 0: node -c index.js` (`criterion_fix_injected` ×2)
- `command exits 0: cargo check --manifest-path src-tauri/Cargo.toml`
  (`acceptance_fix_injected` ×2), followed by the unrelated visual criterion
  also failing twice once the cargo criterion exhausted its budget.

The auto/visual criteria split and the LLM-evidence-threading fix (from
earlier phases) were already correctly in place — this was a different bug.

`ShellTool._resolve_args` only set `shell=True` (routing through `cmd.exe`)
when the command's first token was a known Windows builtin (`dir`, `type`,
...) or resolved to a `.cmd`/`.bat` script. `evaluate_criteria()` appends
`" & echo __EXIT__%ERRORLEVEL%"` to every `command exits 0:` check, and the
developer agent independently issued diagnostic commands like
`cargo check ... 2>&1 || true`. Both `cargo` and `node` resolve to real
`.exe` files, so `_resolve_args` returned `shell=False` with the command
split into literal argv tokens — `cargo.exe` received `2>&1`, `||`, `true`
as literal arguments and failed immediately with an "unexpected argument"
error, regardless of whether the actual code compiled. The criterion could
never pass, so every fix round burned budget chasing a phantom failure.

## Fix

`agent/tools/shell_tool.py`: added `_has_unquoted_shell_operators()`, which
scans for `&`, `|`, `>`, `<` outside of quoted spans. `_resolve_args` now
routes a command through `shell=True` if it is a builtin **or** contains an
unquoted shell operator, so `cmd.exe` interprets `&&`, `||`, `2>&1`, `|`,
`>` correctly instead of passing them through as literal arguments to the
resolved executable.

## Verification

- Added `tests/unit/test_shell_tool.py` covering operator detection and
  `_resolve_args` routing for compound vs. simple `.exe` commands — 8/8 pass.
- `python -m pytest tests/unit -k "shell or task_loop or verifier or acceptance"`
  — 74 passed, 2 pre-existing failures unrelated to this change (confirmed
  via `git stash` they fail identically on the prior commit).
