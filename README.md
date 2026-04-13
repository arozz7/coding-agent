# Local Coding Agent

An autonomous coding agent with LLM integration, multi-agent orchestration, SDLC pipeline, persistent task management, and Discord remote control.

## Features

- **Discord Remote Control** — Submit tasks, monitor progress, view results from any device via Discord
- **Agentic Task Manager** — Objectives are decomposed into ordered task lists; agents execute them sequentially and can add new tasks dynamically
- **SDLC Pipeline** — Full plan → build → test → debug loop → run → verify → complete workflow
- **Autonomous Run & Debug** — Agent runs shell commands, reads errors, fixes code, and re-runs automatically (up to 5 retries)
- **Multi-Agent Routing** — Tasks automatically routed to the right agent: developer, researcher, planner, tester, reviewer, architect
- **Local & Cloud LLM Support** — Flexible model routing between Ollama (local) and cloud APIs (OpenRouter, OpenAI-compatible)
- **Background Job API** — FastAPI server with SQLite-backed job store; poll for status, retrieve results
- **Session Persistence** — Conversation history survives restarts
- **RAG + Wiki Memory** — Codebase indexed in ChromaDB; wiki compiled per session
- **OS-Aware Execution** — Agent knows its OS and shell upfront; Windows command translation built in

## Documentation

- [User Manual](docs/user-manual.md) — Complete usage guide
- [Examples](docs/examples.md) — Practical examples and workflows
- [Capabilities](docs/capabilities.md) — What the agent can and cannot do

## Quick Start

### Prerequisites

- Python 3.11+
- Ollama or OpenAI-compatible API endpoint
- Discord bot token (for remote control)

### Installation

```powershell
cd J:\Projects\coding-agent
python -m pip install -e .
```

### Configuration

**Model config** — edit `config/models.yaml`:

```yaml
models:
  - name: qwen2.5-coder-32b
    type: local
    endpoint: http://127.0.0.1:1234
    context_window: 32768
    is_coding_optimized: true
```

**Discord bot** — set environment variables:

```powershell
$env:DISCORD_BOT_TOKEN = "your-token-here"
$env:AGENT_API_URL     = "http://localhost:5005"
```

### Running

```powershell
# Start the API server
python -m uvicorn api.main:app --host 0.0.0.0 --port 5005

# Start the Discord bot (separate terminal)
python -m api.discord_bot
```

## Discord Commands

| Command | Description |
|---------|-------------|
| `!ask <task>` | Submit a task — returns immediately, polls in background |
| `!tasks` | Show the current task plan with per-task status |
| `!result` | Full prose response from the last job |
| `!files` | List files created or modified by the last task |
| `!show <path>` | View a workspace file inline or as attachment |
| `!status` | Current job status |
| `!cancel` | Cancel the running job |
| `!session` | Show current session ID |

### Discord UX example

```
Zeus: !ask run and debug the Shadows-of-Eldoria game

Logan [APP]: Planning tasks… (2s)
Logan [APP]: Task 1/4 — Check package.json and note start script (8s)
Logan [APP]: Task 2/4 — Run npm start to capture the error (22s)
Logan [APP]: Task 3/4 — Fix the TypeError in src/game.js (67s)
Logan [APP]: Task 4/4 — Run npm start to verify the fix (89s)
Logan [APP]: Done [develop] · 92s
              Fixed TypeError in src/game.js: cannot read property 'length' of undefined
              Shell output:
              ```
              > shadows-of-eldoria@1.0.0 start
              Server running on http://localhost:3000
              ```
              Files created/modified:
                `Shadows-of-Eldoria/src/game.js`

Zeus: !tasks
Logan [APP]: Task plan (4/4 done)
             ✅ 1. [develop] Check package.json and note start script
             ✅ 2. [develop] Run npm start to capture the error
             ✅ 3. [develop] Fix the TypeError in src/game.js
             ✅ 4. [develop] Run npm start to verify the fix
```

## Agent Types

| Type | Keyword triggers | What it does |
|------|-----------------|--------------|
| `develop` | implement, fix, run and debug, build | Writes code, runs commands, auto-fixes errors |
| `research` | search for, find where, how does, investigate | Reads files, searches web, synthesizes reports |
| `sdlc` | build me a complete, end-to-end | Full plan→build→test→debug→run→verify pipeline |
| `plan` | create a plan, plan first | Architecture plan before implementation |
| `test` | write tests, run tests | Writes and runs test suites |
| `review` | code review, security audit | Audits code for bugs and security issues |
| `architect` | system design, write an ADR | High-level design and architecture decisions |
| `chat` | everything else | General questions and conversation |

## Task Manager

Every `develop` and `research` job is decomposed into an ordered task list by the **PlannerAgent** before execution starts.

```
User objective
    → PlannerAgent (1 LLM call)
    → TaskStore (SQLite agent_tasks table)
    → Task loop:
        ┌─ get next pending task
        ├─ route to agent (_direct=True, no re-planning)
        ├─ agent result may append new_tasks
        ├─ mark task done/failed
        └─ repeat until all_done()
```

Tasks persist in SQLite. `!tasks` in Discord shows live progress. The loop continues even if individual tasks fail.

## REST API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/task/start` | Submit a background task → returns `job_id` |
| `GET` | `/task/{job_id}` | Poll job status + summary |
| `GET` | `/task/{job_id}/result` | Full agent response |
| `GET` | `/task/{job_id}/tasks` | Task plan with per-task status |
| `DELETE` | `/task/{job_id}` | Cancel a job |
| `GET` | `/jobs` | List all jobs |
| `GET` | `/workspace/file?path=` | Read a workspace file |
| `GET` | `/models` | List configured models |
| `GET` | `/health` | Health check |

## Project Structure

```
coding-agent/
├── agent/
│   ├── orchestrator.py          # Main orchestrator + task loop
│   ├── sdlc_workflow.py         # SDLC pipeline (plan→build→test→debug→run→verify)
│   ├── agents/
│   │   ├── planner_agent.py     # Decomposes objectives into task lists
│   │   ├── developer_agent.py   # Writes code, runs commands, fix loop
│   │   ├── research_agent.py    # Web search, file reading, synthesis
│   │   ├── tester_agent.py      # Test generation and execution
│   │   ├── reviewer_agent.py    # Code review and security audit
│   │   ├── architect_agent.py   # System design and ADRs
│   │   ├── plan_agent.py        # Implementation planning
│   │   └── chat_agent.py        # Conversational responses
│   ├── memory/
│   │   ├── session_memory.py    # SQLite conversation history
│   │   └── codebase_memory.py   # ChromaDB vector store (RAG)
│   └── tools/
│       ├── shell_tool.py        # Shell execution (OS-aware, Windows translation)
│       ├── browser_tool.py      # Playwright screenshots + server polling
│       ├── file_system_tool.py  # File CRUD
│       └── tool_executor.py     # Unified tool dispatch
├── api/
│   ├── main.py                  # FastAPI server
│   ├── job_store.py             # SQLite job store (write-through + in-memory cache)
│   ├── task_store.py            # SQLite task store (agent_tasks table)
│   └── discord_bot.py           # Discord bot with polling and file attachments
├── llm/
│   ├── model_router.py          # Model routing + fallback
│   ├── model_resilience.py      # Rate-limit handling + Ollama health checks
│   └── openrouter_client.py     # OpenRouter API client
├── mcp/                         # MCP server for tool exposure
├── config/
│   ├── models.yaml              # Model configuration
│   └── task_classifier.yaml     # LLM task-type classifier prompt
├── tests/
│   ├── unit/                    # Unit tests (TaskStore, model resilience, shell, …)
│   └── integration/             # Integration tests (SDLC, task loop, Discord sim)
└── aiChangeLog/                 # Per-phase change logs
```

## Running Tests

```powershell
python -m pytest tests/ -v
# 345 tests, all passing
```

## License

MIT
