# Phase 04 — Batch 2: Anthropic Managed Agents Architecture

**Date:** 2026-04-10
**Plan version:** v2.3
**Covers:** Batch 2 — emitEvent, ToolExecutor wiring, EventEmittingExecutor, wake() recovery

---

## Summary

Makes the Anthropic Managed Agents architecture real. Previously the ToolExecutor existed
as dead code (all agents bypassed it), events were never emitted mid-execution, and crashed
sessions had no recovery path. This batch completes all three gaps.

---

## Modified Files

### `agent/tools/tool_executor.py` — ToolExecutor extensions + EventEmittingExecutor

**Step 2 — New tools registered:**
- `ToolExecutor.__init__()` now accepts `code_analyzer=None` and `pytest_tool=None`
- Added `_analyze_code()` handler — delegates to `code_analyzer.analyze_file()`
- Added `_run_tests()` handler — delegates to `pytest_tool.run()`
- `analyze` and `test` tools registered only when the corresponding dependency is provided

**Step 3 — EventEmittingExecutor (new class):**
- Thin wrapper around `ToolExecutor` that emits `tool_call` / `tool_result` events
  to `SessionMemory` before/after each tool invocation
- Keeps `ToolExecutor` session-agnostic — the wrapper is session-bound
- Truncates `tool_result` output to 1 000 chars in the event payload (prevents bloat)
- Exposes `list_tools()` and `register_tool()` forwarding methods for interface parity

### `agent/agents/developer_agent.py` — Step 4

- `DeveloperRole.execute()` now uses `context["tool_executor"]` for all side effects:
  - File writes: `executor.execute("file_write", …)` (was `file_system_tool.write_file()`)
  - Shell commands: `executor.execute("shell", …)` (was `shell_tool.run()`)
  - Screenshots: `executor.execute("screenshot", …)` (was `browser_tool.run_and_screenshot()`)
- `DeveloperAgent.run()` simplified — no longer injects legacy tool refs into context

### `agent/agents/architect_agent.py` — Step 4

- `ArchitectRole.execute()` uses `executor.execute("file_list", …)` for workspace listing
- File writes use `executor.execute("file_write", …)` (was `file_system_tool.write_file()`)
- `ArchitectAgent.run()` simplified

### `agent/agents/reviewer_agent.py` — Step 4

- `ReviewerRole.execute()` uses `executor.execute("analyze", …)` (was `code_analyzer.analyze_file()`)
- `ReviewerAgent.run()` simplified

### `agent/agents/tester_agent.py` — Step 4

- `TesterRole.execute()` uses `executor.execute("file_write", …)` and `executor.execute("test", …)`
  (was `file_system_tool.write_file()` + `pytest_tool.run()`)
- `TesterAgent.run()` simplified

### `agent/orchestrator.py` — Steps 5, 6, 7, 8

**Step 5 — `_run_general_agent()` deleted:**
- "general" task type now routes to `DeveloperAgent` (same as "develop") inside
  `_run_specialized_agent()`
- Eliminates ~60 lines of duplicated file-write and shell-exec logic

**Step 6 — `EventEmittingExecutor` wired per session:**
- `ToolExecutor` constructed with `code_analyzer` and `pytest_tool` at startup
- `_create_session_executor(session_id)` creates an `EventEmittingExecutor` bound
  to the active session
- `_run_specialized_agent()` passes `tool_executor=session_executor` in context
- All four agent roles receive the same executor interface

**Step 7 — `_build_context_from_events()` replaces `get_conversation_history()`:**
- Fetches last 20 events via `get_events(offset=-20, limit=20)` (paginated)
- Truncates `tool_result` event payloads to 500 chars to avoid prompt bloat
- `_build_context()` delegates to `_build_context_from_events()` (used by `run_stream`)
- `run_task()` context building now goes through events (not full history load)

**Step 8 — `wake(session_id)` added:**
- Reads last 10 events, finds most recent `user` message
- Emits `status:wake` event, sets session status to `active`
- Returns `{ session_id, message_count, last_user_task, status }` so caller can replay

**Status events in `run_task()`:**
- `status:start` (with `task_type`) emitted before agent dispatch
- `status:complete` (with `files`) emitted after successful run
- `status:error` (with `error`) emitted on exception

### `api/main.py` — Step 8

- Added `POST /wake/{session_id}` endpoint
- Returns `{ success, session_id, message_count, last_user_task, status }`
- 404 if session not found or empty

---

## Architecture After Batch 2

```
run_task()
  └─ _create_session_executor(session_id)
       └─ EventEmittingExecutor(tool_executor, session_memory, session_id)
  └─ _run_specialized_agent(task, task_type, session_id)
       └─ agent.run(task, context={..., tool_executor: session_executor})
            └─ role.execute(context)
                 └─ executor.execute(tool_name, input)
                      ├─ emit_event(session_id, "tool_call", ...)   ← observability
                      ├─ tool_executor.execute(tool_name, input)    ← actual work
                      └─ emit_event(session_id, "tool_result", ...) ← observability
```

All tool calls are now:
1. Routed through a single formal interface (`execute(name, input) → string`)
2. Persisted as structured events in SQLite for crash recovery and observability
3. Accessible via `GET /sessions/{id}` → `get_events(offset, limit)`

---

## Known Remaining Issues (Batch 3+)

- Skills triggered but not executed — `skill_manager.execute_skill()` never called (Batch 3)
- wiki-compile skill writes files but `log.md` not maintained (Batch 3)
- `memory_wiki.py` NetworkX graph not implemented (Batch 5)
- Subagent spawning does not pass `tool_executor` into isolated context (Batch 5)
- `OllamaModelManager` uses blocking `httpx.Client` — blocks event loop (Batch 4)
- Two circuit breaker implementations not consolidated (Batch 4)
- Git tools in MCP not registered — `repo_path` never passed (Batch 4)
- Token estimation uses `len(text)//4` — switch to `tiktoken` (Batch 5)
- Prometheus `/metrics` endpoint not implemented (Batch 5)
