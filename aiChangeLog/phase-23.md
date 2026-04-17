# Phase 23 — pi-mono Feature Ports

**Date:** 2026-04-17  
**Branch:** phase-18  
**Author:** Antigravity AI

---

## Summary

Ported five high/medium-priority features from `badlogic/pi-mono/coding-agent`
(TypeScript) into our Python coding-agent. All changes are confined to the
Tools layer — no changes to the Supervisor, Orchestrator, or API surface.

---

## Files Created

| File | Purpose |
|---|---|
| `agent/tools/edit_tool.py` | Multi-hunk surgical file editor with unified diff output |
| `agent/tools/search_tool.py` | Native cross-platform find_files + grep_code |
| `tests/unit/test_edit_tool.py` | 11 unit tests for EditTool |
| `tests/unit/test_search_tool.py` | 15 unit tests for SearchTool |
| `aiChangeLog/phase-23.md` | This file |

## Files Modified

| File | Change |
|---|---|
| `agent/tools/shell_tool.py` | Popen + process-tree kill; async run_streaming() |
| `agent/tools/file_system_tool.py` | CRLF-preserving write_file() |
| `agent/tools/tool_executor.py` | Registered file_edit, find_files, grep_code; on_phase shell heartbeat |
| `agent/agents/developer_agent.py` | EDIT: block parser; updated system prompt and fix-loop prompt |

---

## Changes by Feature

### 1. Windows Process-Tree Kill (`shell_tool.py`)

**Problem:** `subprocess.run(timeout=N)` raises `TimeoutExpired` but only kills
the top-level shell wrapper. Child processes (e.g. `webpack`, `npm`, `node`)
survive as orphans on Windows, consuming CPU and holding file locks.

**Fix:** Replaced `subprocess.run` with `subprocess.Popen` + `.communicate(timeout)`.
Added `_kill_process_tree(pid)`:
- **Windows:** `taskkill /F /T /PID <pid>` — terminates the entire Windows job tree.
- **Unix:** `os.killpg(os.getpgid(pid), SIGKILL)` — kills the process group.

Added `run_streaming(command, timeout, on_data)` — async variant that calls
`on_data(chunk)` for each output chunk, used by Phase 5.

---

### 2. Multi-Hunk Edit Tool (`edit_tool.py` + `tool_executor.py`)

**Problem:** The agent's fix loop used `FILE:` full-file rewrites for every
correction, producing large diffs and increasing the risk of accidentally
reverting unrelated lines.

**Fix:** New `EditTool` class accepting `edits: list[EditHunk]`:
- All hunks matched against the **original** file simultaneously — overlapping
  edits raise `OverlappingEditsError` before any write occurs.
- BOM detection and restoration (`\ufeff`).
- CRLF / LF detection and restoration per-file.
- Per-path `asyncio.Lock` (file mutation queue) serialises concurrent writes.
- Returns `EditResult(success, diff, first_changed_line, error)`.
- Registered as `file_edit` tool in `ToolExecutor`.

New `EDIT:` block syntax taught to the LLM:
```
EDIT: path/to/file.ext
<<<OLD
exact text to replace
===
replacement text
>>>
```

The fix-loop now prefers `EDIT:` blocks over `FILE:` blocks. `FILE:` remains
as a backward-compat fallback for full rewrites.

---

### 3. Native `find_files` + `grep_code` (`search_tool.py`)

**Problem:** LLM-generated `find` / `grep` shell commands fail on Windows
without MSYS/Git-Bash. They also expose a shell injection surface.

**Fix:** New `SearchTool` using Python stdlib only:
- `find_files(pattern, path, max_results=200)` — `pathlib.Path.rglob` with
  automatic skip of `node_modules`, `.git`, `__pycache__`, etc.
- `grep_code(pattern, path, case_sensitive=True, max_results=200)` —
  `re.compile` + line-by-line scan; skips binary extensions gracefully.
- Registered as `find_files` and `grep_code` tools in `ToolExecutor`.
- System prompt updated to recommend these over shell equivalents.

---

### 4. CRLF Line-Ending Preservation (`file_system_tool.py` + `edit_tool.py`)

**Problem:** `write_file` always wrote LF, silently converting CRLF files
on Windows workspaces and causing noisy diffs in source control.

**Fix:** `write_file` now samples the first 4 KB of an existing file before
overwrite. If CRLF is detected, the normalised content is converted back to
CRLF before writing. Same logic is baked into `EditTool._apply_locked`.

---

### 5. Shell Streaming Heartbeat (`shell_tool.py` + `tool_executor.py`)

**Problem:** Long-running shell commands (e.g. `npm run build`, `pytest`) gave
no output to the supervisor watchdog, risking the stale-job timeout.

**Fix:** `ToolExecutor.execute("shell", ..., on_phase=callback)` now injects
the `on_phase` hook into a streaming shell run. Each output chunk from the
process fires `on_phase("shell:running")`, which resets the watchdog timer
the same way Phase 22's fix-loop heartbeats did.

---

## Test Results

```
26 passed in 1.10s
```

| Suite | Tests | Result |
|---|---|---|
| `test_edit_tool.py` | 11 | ✅ All passed |
| `test_search_tool.py` | 15 | ✅ All passed |

---

## Risks Identified

- `run_streaming` uses `asyncio.create_subprocess_exec` which does not support
  `shell=True`. Windows built-in commands (`dir`, `type`, etc.) still go
  through the synchronous `run()` path when `on_phase` is not provided; they
  will use the old code path. This is acceptable since built-ins are
  fast-exiting and don't need heartbeating.
- The `EDIT:` parser uses `re.DOTALL`. If the LLM emits `>>>` inside
  `new_text`, the regex will terminate early. This edge case will be addressed
  if observed in practice.
