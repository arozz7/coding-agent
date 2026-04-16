# Phase 20 — Bug Fixes & Interactive Testing Tools

## Summary

Fixed four bugs discovered in log `api-20260416-093528.log` (job `job_a374274007f1`,
Shadows-of-Eldoria Node.js game task), plus a related stdin-blocking issue found during
verification, and added two new cross-platform interactive testing tools.

---

## Bug Fixes

### Bug 1 — Path double-nesting (`agent/tools/file_system_tool.py`)

**Symptom:** Agent wrote `Shadows-of-Eldoria/package.json` inside a workspace already
scoped to `Shadows-of-Eldoria/`, creating `Shadows-of-Eldoria/Shadows-of-Eldoria/package.json`.

**Fix:** Strip redundant leading project-prefix from relative paths in `_validate_path()`.
When the first path component equals `self.allowed_base.name`, it is removed before
resolution.

---

### Bug 2 — Redundant `cd <project> &&` in shell commands (`agent/tools/tool_executor.py`)

**Symptom:** The agent prefixed many shell commands with `cd Shadows-of-Eldoria &&` even
though the `ShellTool` cwd is already scoped to the project root, causing commands to
navigate one level too deep.

**Fix:** Added `_strip_redundant_cd()` module-level helper and `_CD_PREFIX_RE` regex.
`_run_shell()` now strips this prefix when the cd target matches the workspace root name.

---

### Bug 3 — npm install never auto-triggered (`agent/agents/developer_agent.py`)

**Symptom:** "Cannot find module webpack" error repeated indefinitely across all fix
iterations without any attempt to install dependencies.

**Fix:** Added `_looks_like_npm_missing()` and `_npm_install_cmd()` helpers. The fix loop
now detects missing node_modules from error text and runs `npm install` once automatically
before continuing.

---

### Bug 4 — Research agent always web-searched local tasks (`agent/tools/tool_executor.py`, `agent/agents/research_agent.py`)

**Root cause:** `_list_files` was calling `self.fs_tool.workspace` (AttributeError,
silently caught), causing `local_content_found` to be `False` for every research task,
which routed all tasks through the iterative web-research path.

**Fix (a):** Updated `_list_files` to use `Path(self.workspace_path)` correctly.

**Fix (b):** Added `_LOCAL_TASK_RE` regex to detect tasks about local workspace content
(last job, errors, files, codebase). When matched, skips web search even if no specific
file content was read.

---

### Bug 5 — Interactive CLI apps blocking `subprocess.run` (`agent/tools/shell_tool.py`)

**Symptom:** Job stopped at step 3/5 when the developer agent tried to verify by running
`npm start`, which launched a readline game that waited for stdin — blocking indefinitely.

**Fix:** Added `stdin=subprocess.DEVNULL` to `subprocess.run()` so the process receives
EOF immediately and cannot block waiting for terminal input.

---

## New Features

### `InteractiveShellTool` (`agent/tools/interactive_shell_tool.py`) — NEW

Asyncio-based interactive subprocess driver for CLI apps that require stdin/stdout
interaction (REPLs, text-adventure games, wizard scripts, etc.).

- **Cross-platform:** Windows uses `create_subprocess_shell`; Unix uses
  `create_subprocess_exec` with `shlex.split` fallback to shell.
- **Script format:** list of `{expect, send, wait}` dicts — each step is optional;
  combine freely.
- **`_read_until()`** reads in 512-byte chunks until a regex matches or timeout elapses;
  uses `asyncio.get_running_loop().time()` (not deprecated).
- **Returns:** `{success, transcript, returncode}` with `[sent]` markers in transcript.
- Imports `_TOOL_ENV` from `shell_tool` for consistent PATH resolution.

**Registered in ToolExecutor as:** `interactive_shell`

---

### `BrowserTool.interact()` (`agent/tools/browser_tool.py`) — NEW METHOD

Playwright-backed browser automation for web apps.

- **Cross-platform** via Playwright's Chromium driver (Windows/macOS/Linux).
- **Action types:** `navigate`, `click`, `fill`, `press`, `screenshot`, `text`,
  `wait_for`, `wait`.
- **Returns:** `{success, transcript, screenshots: [...], error?}`.
- Graceful ImportError message when `playwright` is not installed.

**Registered in ToolExecutor as:** `browser_interact`

---

## Tool Executor Changes (`agent/tools/tool_executor.py`)

- Added `import re` and `from pathlib import Path` at module level (previously missing).
- Added `_CD_PREFIX_RE` and `_strip_redundant_cd()` module-level helpers.
- Fixed `_list_files` to use `Path(self.workspace_path)`.
- Instantiates `InteractiveShellTool` alongside other tools in `_register_builtin_tools`.
- Registered `interactive_shell` → `_run_interactive_shell()`.
- Registered `browser_interact` → `_browser_interact()`.

---

## Test Results

```
219 passed, 92 warnings in 16.05s
```

All existing tests pass. No new unit tests were added for the two new tools in this
phase (they require subprocess/Playwright; integration testing recommended).

---

## Files Changed

| File | Change |
|------|--------|
| `agent/tools/file_system_tool.py` | Strip redundant project-prefix in `_validate_path` |
| `agent/tools/shell_tool.py` | `stdin=subprocess.DEVNULL` in `subprocess.run` |
| `agent/tools/tool_executor.py` | Fix `_list_files`; add `_strip_redundant_cd`; register new tools |
| `agent/agents/developer_agent.py` | Auto-npm-install on missing-module errors |
| `agent/agents/research_agent.py` | `_LOCAL_TASK_RE`; updated routing logic |
| `agent/tools/interactive_shell_tool.py` | **NEW** — asyncio interactive subprocess driver |
| `agent/tools/browser_tool.py` | **NEW METHOD** — `interact()` Playwright action runner |
