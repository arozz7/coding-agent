# Local Coding Agent

An autonomous coding agent with LLM integration, multi-agent orchestration, SDLC pipeline, persistent task management, and Discord remote control.

## Features

- **Supervisor Process Manager** — Single `python supervisor.py` starts and monitors the API and bot; auto-restarts crashed or unresponsive processes, captures logs to dated files
- **Discord Remote Control** — Submit tasks, monitor progress, switch models, manage sessions, and restart services from any device
- **Agentic Task Manager** — Objectives decomposed into ordered task lists; agents execute sequentially, can add tasks dynamically, wiki knowledge persists between subtasks
- **SDLC Pipeline** — Full plan → build → test → debug → run → verify workflow
- **Autonomous Run & Debug** — Agent runs shell commands, reads errors, fixes code, re-runs (up to 10 iterations); explicit verify re-run after each fix
- **Multi-Agent Routing** — Tasks routed to the right agent automatically: developer, researcher, planner, tester, reviewer, architect, chat
- **Context Bridge** — Monitors token budget; at 82 % generates a structured handover and continues in a fresh session silently; Discord notifies at 75 %
- **LM Studio Integration** — Live model state (loaded / not-loaded) from `/api/v0/models`; discovers downloaded-but-unconfigured models; per-call thinking mode control
- **Local & Cloud LLM** — Flexible routing between LM Studio / Ollama (local) and OpenRouter / OpenAI-compatible APIs; circuit breaker, rate limiting, auto-fallback
- **Workspace Scoping** — `PROJECT_DIR` env var focuses all file operations on an active project subdirectory; no path double-nesting
- **Agent Wiki Memory** — `.agent-wiki/` knowledge base compiled per task; later subtasks query earlier ones; index deduplication; `!skills` to inspect
- **RAG Memory** — Codebase indexed in ChromaDB; retrieved context injected into every task
- **Shell PATH Auto-Discovery** — Scans 15+ common install dirs (nvm, volta, Homebrew, Cargo…) so npm/node/git are found even when the API starts with a minimal PATH

## Documentation

- [User Manual](docs/user-manual.md) — Complete usage guide
- [Examples](docs/examples.md) — Practical examples and workflows
- [Capabilities](docs/capabilities.md) — What the agent can and cannot do

---

## Quick Start

### Prerequisites

- Python 3.11+
- [LM Studio](https://lmstudio.ai) or Ollama running locally (or an OpenRouter API key)
- Discord bot token

### Installation

```powershell
cd J:\Projects\coding-agent
python -m pip install -e .
```

### Configuration

**1. Copy and edit `.env`:**

```dotenv
DISCORD_BOT_TOKEN=your-token-here
AGENT_API_URL=http://127.0.0.1:5005
LM_STUDIO_URL=http://127.0.0.1:1234   # default; change if LM Studio is on another port
WORKSPACE_PATH=J:\Projects\agent-workspace
PROJECT_DIR=my-project                  # optional — scope agent to a subdirectory
OPENROUTER_API_KEY=sk-or-...            # optional — enables cloud model fallback
```

**2. Edit `config/models.yaml`** to list your available models:

```yaml
models:
  - name: qwen3.5-35b-a3b
    type: local
    endpoint: ${LM_STUDIO_URL:-http://127.0.0.1:1234}
    context_window: 262144
    is_coding_optimized: true

defaults:
  coding_model: qwen3.5-35b-a3b
```

### Running

```powershell
# Recommended — starts API + bot, monitors both, auto-restarts on crash
python supervisor.py

# Manual (two separate terminals)
python -m uvicorn api.main:app --host 0.0.0.0 --port 5005
python -m api.discord_bot
```

---

## Discord Commands

### Task Submission

| Command | Description |
|---------|-------------|
| `!ask <task>` | Submit a task — auto-classifies type, polls in background |
| `!dev <task>` | Force develop path — bypasses classifier, guaranteed to run/fix/build |
| `!continue [note]` | Resume the current debugging session with an optional note |

### Job Monitoring

| Command | Description |
|---------|-------------|
| `!status` | Current job phase and elapsed time |
| `!tasks` | Task plan with per-task status (✅ / 🔄 / ❌) |
| `!result` | Full prose response from the last completed job |
| `!files` | Files created or modified by the last task |
| `!jobs [n]` | List last N jobs (default 10) |
| `!cancel` | Cancel the running job |

### File Access

| Command | Description |
|---------|-------------|
| `!show <path>` | View a workspace file inline (≤ 1900 chars) or as attachment |

### Session & Workspace

| Command | Description |
|---------|-------------|
| `!session` | Show current session ID |
| `!sessions` | List all saved sessions |
| `!history` | Show recent conversation history |
| `!clear` | Clear session history |
| `!workspace` | Show workspace path and active project |
| `!project [name]` | Get or set the active project directory |

### Model Management

| Command | Description |
|---------|-------------|
| `!models` | List configured models with live LM Studio state (🟢 loaded / ⚪ not-loaded) plus all downloaded-but-unconfigured models |
| `!model <name>` | Switch to a different model for this session |
| `!model reset` | Revert to the default model from `models.yaml` |

### Operations

| Command | Description |
|---------|-------------|
| `!git <args>` | Run a git command in the workspace (e.g. `!git log --oneline -5`) |
| `!skills [list]` | List available agent skills |
| `!restart` / `!reboot` | Restart both API and bot via supervisor; warns if supervisor is not running |
| `!helpme` | Show command reference in Discord |

### Discord UX example

```
Zeus: !ask run and debug the Shadows-of-Eldoria game

Logan [APP]: Preparing… (2s)
Logan [APP]: Task 1/4 — Check package.json and note start script (8s)
Logan [APP]: Task 2/4 — Run npm start to capture the error (22s)
Logan [APP]: Task 3/4 — Fix the TypeError in src/game.js (67s)
Logan [APP]: Task 4/4 — Run npm start to verify the fix (89s)
Logan [APP]: Done [develop] · 92s
              Fixed TypeError: cannot read property 'length' of undefined
              Files: src/game.js

Zeus: !models
Logan [APP]: Configured Models · active: qwen3.5-35b-a3b
              **[active]** `qwen3.5-35b-a3b` — local · 262k ctx 🟢
                     `google/gemma-4-31b-it:free` — remote · 262k ctx
             Available in LM Studio (not configured)
               `llama-3.3-70b` ⚪
```

---

## Agent Types

| Type | Triggered by | What it does |
|------|-------------|--------------|
| `develop` | implement, fix, run, build, debug, npm, compile, execute | Writes files, runs shell commands, auto-fixes errors (up to 10 iterations) |
| `research` | search for, find where, how does, investigate | Reads files, searches web, synthesises reports |
| `sdlc` | build me a complete, end-to-end | Full plan→build→test→debug→run→verify pipeline |
| `plan` | plan first, show me a plan, before we build | Architecture plan before any code is written |
| `test` | write tests, run tests, pytest | Writes and runs test suites |
| `review` | code review, security audit | Audits code for bugs and security issues |
| `architect` | system design, write an ADR | High-level design and architecture decisions |
| `chat` | everything else | General questions and conversation |

> **Tip:** Use `!dev <task>` to force the develop path if the classifier picks the wrong type.

---

## Supervisor

`supervisor.py` is the recommended way to run the agent in production.

```
python supervisor.py
```

What it does:
- Starts the API server, waits for `/health` before launching the bot
- **Continuous health probe** every 30s — kills and restarts if the API is alive but unresponsive (blocked event loop, deadlock)
- **Crash recovery** — restarts crashed API (both services) or bot (bot only) with exponential backoff
- **Stale-job watchdog** every 5 min — restarts if a job is stuck for > 45 min
- **Heartbeat** — writes `.state/supervisor.heartbeat` every 5s so the API can confirm it's alive
- **Log capture** — stdout/stderr of both processes written to `logs/api-YYYYMMDD-HHMMSS.log` and `logs/bot-*.log`

Environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `API_STARTUP_TIMEOUT` | `120` | Seconds to wait for `/health` on startup |
| `RESTART_DELAY_SECS` | `3` | Seconds between stop and start during restart |
| `BOT_PYTHON` | `sys.executable` | Python interpreter for the bot (set when bot uses a different venv) |

---

## REST API

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/task` | Submit a task synchronously |
| `POST` | `/task/start` | Submit a background task → returns `job_id` |
| `GET` | `/task/{job_id}` | Poll job status + summary |
| `GET` | `/task/{job_id}/result` | Full agent response |
| `GET` | `/task/{job_id}/tasks` | Task plan with per-task status |
| `DELETE` | `/task/{job_id}` | Cancel a job |
| `GET` | `/jobs` | List all jobs |
| `GET` | `/health` | Health check — returns `agent_ready`, `active_jobs`, `uptime_seconds` |
| `GET` | `/ready` | Returns 200 only when orchestrator is fully initialised |
| `GET` | `/models` | Configured models with live LM Studio state + unconfigured discovery list |
| `GET` | `/models/active` | Currently active model |
| `POST` | `/models/active` | Switch active model `{"model": "name"}` |
| `GET` | `/workspace` | Current workspace path and project |
| `GET` | `/workspace/file?path=` | Read a workspace file |
| `POST` | `/workspace/project` | Set active project `{"project": "name"}` |
| `GET` | `/sessions` | List sessions |
| `DELETE` | `/sessions/{id}` | Delete a session |
| `POST` | `/restart` | Signal supervisor to restart both services |
| `GET` | `/llm/health` | Detailed LLM health — circuit breaker, rate limiter, cost |
| `GET` | `/stats` | Runtime stats (jobs, sessions, costs) |
| `GET` | `/environment` | OS, shell, tool PATH details |

---

## Project Structure

```
coding-agent/
├── supervisor.py                  # Process manager (start here in production)
├── agent/
│   ├── orchestrator.py            # Main orchestrator, task loop, context bridge
│   ├── agents/
│   │   ├── developer_agent.py     # Write files, run commands, fix loop (10 iter)
│   │   ├── planner_agent.py       # Decompose objectives into task lists
│   │   ├── research_agent.py      # Web search, file reading, synthesis
│   │   ├── tester_agent.py        # Test generation and execution
│   │   ├── reviewer_agent.py      # Code review and security audit
│   │   ├── architect_agent.py     # System design and ADRs
│   │   ├── plan_agent.py          # Implementation planning
│   │   └── chat_agent.py          # Conversational responses
│   ├── memory/
│   │   ├── session_memory.py      # SQLite conversation history
│   │   └── codebase_memory.py     # ChromaDB vector store (RAG)
│   ├── skills/
│   │   ├── skill_executor.py      # Pre/post skill execution (wiki-query, wiki-compile)
│   │   ├── skill_loader.py        # Lazy skill content loading
│   │   └── wiki_manager.py        # .agent-wiki/ read/write, index upsert, lint
│   └── tools/
│       ├── shell_tool.py          # Shell execution, PATH auto-discovery, Windows .cmd fix
│       ├── file_system_tool.py    # File CRUD
│       ├── git_tool.py            # Git operations
│       ├── browser_tool.py        # Playwright screenshots + server polling
│       └── tool_executor.py       # Unified tool dispatch, output capping
├── api/
│   ├── main.py                    # FastAPI server, background init, all endpoints
│   ├── job_store.py               # SQLite job store (write-through + in-memory cache)
│   ├── task_store.py              # SQLite task store
│   └── discord_bot.py             # Discord bot, commands, polling, _safe_edit
├── llm/
│   ├── model_router.py            # Routing, fallback, enable_thinking override, timeout fail-fast
│   ├── ollama_client.py           # LM Studio / Ollama client (asyncio.to_thread, list_all_models)
│   ├── cloud_api_client.py        # OpenRouter / OpenAI-compatible client
│   ├── config.py                  # ModelConfig dataclass
│   ├── cost_tracker.py            # Token usage and cost tracking
│   ├── rate_limiter.py            # Per-model RPM enforcement
│   ├── health.py                  # Health checker with circuit breaker
│   └── circuit_breaker.py        # Circuit breaker (open/half-open/closed)
├── config/
│   ├── models.yaml                # Model configuration (local + remote + defaults)
│   └── task_classifier.yaml      # LLM classifier prompt for task type detection
├── .agent-wiki/                   # Per-session knowledge base (auto-generated)
│   ├── index.md                   # Entry catalog
│   ├── log.md                     # Compilation log
│   └── <category>/<slug>.md       # Individual knowledge entries
├── .state/                        # Runtime state (supervisor heartbeat, restart flag)
├── logs/                          # Timestamped child-process logs (auto-generated)
├── data/                          # SQLite databases (jobs, tasks, sessions)
├── workspace/                     # Default agent workspace directory
├── tests/
│   ├── unit/                      # Unit tests
│   └── integration/               # Integration tests
└── aiChangeLog/                   # Per-phase change logs (phase-03 through phase-18)
```

---

## Running Tests

```powershell
python -m pytest tests/ -v
```

---

## License

MIT
