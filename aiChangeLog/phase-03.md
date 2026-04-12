# Phase 03 — MCP Server, Subagents, RAG Memory, Skills System & Security Hardening

**Date:** 2026-04-10
**Plan version:** v2.3
**Phases covered:** Phase 1 (completion), Phase 2 (completion), Phase 3 (partial)

---

## Summary

This phase completes the foundation and memory integration phases of the implementation plan, adds the multi-agent infrastructure, and applies a full security hardening pass (Batch 1 of the audit findings).

---

## New Files

| File | Purpose |
|------|---------|
| `agent/tools/shell_tool.py` | Cross-platform shell execution with command-injection protection |
| `agent/tools/browser_tool.py` | Playwright-based screenshot and browser automation |
| `agent/tools/tool_executor.py` | Formal `execute(name, input) → string` interface (Anthropic Managed Agents pattern) |
| `agent/skills/skill_loader.py` | Skill discovery, SKILL.md parsing, keyword-trigger detection |
| `mcp/server.py` | MCP server with filesystem, git, shell, test-runner, and code-analysis tools |
| `skills/` | Skill definitions: wiki-compile, wiki-query, wiki-lint, tdd-enforcer, security-auditor, architect-adr, codebase-mapper, workspace-janitor, handover, playwright-cli |
| `api/discord_bot.py` | Discord bot integration for remote task submission |
| `test_ollama.py` | Connectivity test for Ollama endpoint |

---

## Modified Files

### `agent/orchestrator.py` — Major Refactor

- Added `SkillManager` integration (skill discovery, trigger detection, context injection)
- Added `CodebaseMemory` for RAG vector search via ChromaDB
- Added `MCP server` creation and exposure
- Added `BrowserTool` and `ShellTool` wiring
- Added `ToolExecutor` with formal execute interface
- Added subagent spawning: `spawn_subagent()`, `spawn_multiple_subagents()`, `get_subagent_result()`, `list_subagents()`
- Added task-type detection (`_detect_task_type`) routing to specialist agents
- Added `_load_wiki_context()` — reads `.agent-wiki/index.md` for relevant entries
- Added `_load_rag_context()` — retrieves top-k code chunks from ChromaDB per task
- Added `index_workspace()` — triggers full workspace indexing
- Added skill trigger detection (`_detect_skills`, `_get_skill_context`)

### `agent/memory/codebase_memory.py` — Full Implementation

- Code-aware chunking: respects function/class boundaries per language
- Language detection by file extension (Python, JS/TS, Rust, Go, Java, C/C++)
- Auto-indexing on `index_workspace()` call
- `get_relevant_context(task, project_id, max_chunks)` for RAG integration
- `search_files(query, n_results)` for direct semantic search
- `get_stats()` for vector store diagnostics

### `agent/memory/session_memory.py` — Extensions

- Added `get_events(session_id, offset, limit)` — paginated event access per Anthropic Managed Agents pattern
- Added `get_event_count(session_id)` — total event count for a session
- Added `list_sessions(limit, status)` — filterable session listing
- Added `get_or_create_session()` — idempotent session initialisation
- Added `update_session_status()`
- **Batch 1:** Added `delete_session()` — atomic deletion of session + messages + tasks

### `agent/agents/` — Role Agent Updates

- All four roles (architect, developer, reviewer, tester) updated with proper BaseAgent/AgentRole hierarchy
- `base_agent.py` created with `AgentRole` ABC and `BaseAgent` runner
- `developer_agent.py`: file extraction, shell execution, screenshot capture
- **Batch 1:** Replaced `asyncio.run(browser_tool.run_and_screenshot())` with `await` — was crashing in async context

### `api/main.py` — API Surface Expansion

**New endpoints:**
- `GET /workspace` — current workspace path
- `GET /workspace/directories` — list workspace contents
- `POST /workspace` — switch workspace with path validation
- `POST /screenshot` — capture browser screenshot
- `GET /mcp/tools` — list registered MCP tools
- `POST /mcp/tools/{name}` — invoke MCP tool
- `POST /subagent/spawn` — spawn isolated subagent
- `POST /subagent/spawn-batch` — parallel subagent execution
- `GET /subagent` — list active subagents
- `GET /subagent/{id}` — get subagent result
- `POST /index` — index workspace for RAG
- `GET /search` — semantic code search
- `GET /memory/stats` — vector store statistics
- `GET /ready` — readiness check with model health
- `GET /stats` — cost + session statistics
- `GET /llm/health` — full resilience diagnostics (circuit breakers, rate limits, costs)

**Batch 1 security fixes:**
- CORS: replaced `allow_origins=["*"]` + `allow_credentials=True` (CSRF risk) with explicit localhost allowlist, configurable via `CORS_ORIGINS` env var; `allow_credentials=False`
- `DELETE /sessions/{id}`: fixed to use `_orchestrator.session_memory.delete_session()` (was instantiating a new disconnected SessionMemory)
- `GET /models`: removed broken `from llm.config import load_models` import (function doesn't exist); now reads from `model_router.configs` directly

### `agent/tools/shell_tool.py` — **Batch 1: Command Injection Fix**

Critical security fix. Previous implementation used `subprocess.run(cmd, shell=True)` with LLM-generated commands — a direct command injection vector.

Changes:
- Added `_BLOCKED_PATTERNS` (14 regexes): blocks `rm -rf /`, fork bombs, pipe-to-shell, `shutdown`/`reboot`, raw device writes, Windows system path writes, chained destructive commands
- Added `_validate_command()` called before every execution on every platform
- Unix path: `shlex.split(cmd)` + `shell=False` — shell metacharacters cannot inject
- Windows path: built-in commands (`dir`, `type`, `del`, etc.) still use `shell=True` (required); all external executables use `shlex.split()` + `shell=False`

### `agent/tools/tool_executor.py` — **Batch 1: Async Fix**

- `execute()` promoted to `async def`
- Replaced `asyncio.run(tool_func(input))` with `inspect.iscoroutinefunction()` + `await` — calling `asyncio.run()` inside a running event loop raises `RuntimeError`

### `llm/ollama_client.py`

- Port updated to `1234` (local Ollama instance)
- Timeout extended to 600s for large model inference

### `mcp/server.py` — MCP Tool Registration

- FileSystem tools: read, write, list, search
- Git tools: status, diff, log
- Shell tool: arbitrary command execution
- Test runner: pytest integration
- Code analysis: structure and dependency inspection

### `docs/plans/implementation_plan_v2.md` — Plan Updated to v2.3

- Marked completed items across Phase 1 and Phase 2
- Added resilience API endpoints section
- Noted pending Batch 2–5 work

---

## Security Fixes (Batch 1)

| ID | Severity | File | Fix |
|----|----------|------|-----|
| B1-1 | Critical | `agent/tools/shell_tool.py` | `shell=True` + LLM input → blocklist + `shlex.split()` + `shell=False` |
| B1-2 | Critical | `api/main.py` | `allow_origins=["*"]` + `allow_credentials=True` → localhost allowlist, credentials disabled |
| B1-3 | High | `api/main.py` | `DELETE /sessions` instantiated disconnected `SessionMemory` → uses orchestrator's instance |
| B1-4 | High | `api/main.py` | `GET /models` imported nonexistent `load_models` → reads `model_router.configs` |
| B1-5 | High | `agent/agents/developer_agent.py` | `asyncio.run()` in async context → `await` |
| B1-6 | High | `agent/tools/tool_executor.py` | `asyncio.run()` in potential async context → async `execute()` with `inspect.iscoroutinefunction()` |

---

## Known Remaining Issues (Batch 2+)

- Skills are detected and loaded but not **executed** — `skill_manager.execute_skill()` never called
- `ToolExecutor` interface exists but agents bypass it (call tools directly)
- `emitEvent()` during task execution not implemented — only start/end saved to session
- `wake(sessionId)` harness recovery not implemented
- `getEvents()` exists but orchestrator uses `get_conversation_history()` (full load)
- `memory_wiki.py` NetworkX graph not implemented
- Prometheus `/metrics` endpoint not implemented
- Git tools in MCP not registered (repo_path never passed to `create_mcp_server()`)
- `OllamaModelManager` uses blocking `httpx.Client` — blocks event loop
- Two circuit breaker implementations (`health.py` vs `circuit_breaker.py`) not consolidated
- Token estimation uses `len(text)//4` — switch to `tiktoken`
