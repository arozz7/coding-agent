# User Manual: Local Coding Agent

A comprehensive guide to using the Local Coding Agent.

## Table of Contents

1. [Getting Started](#getting-started)
2. [Configuration](#configuration)
3. [Starting the Agent](#starting-the-agent)
4. [Discord Commands Reference](#discord-commands-reference)
5. [Task Types & Routing](#task-types--routing)
6. [Workspace & Project Scoping](#workspace--project-scoping)
7. [Model Management](#model-management)
8. [Context Bridge](#context-bridge)
9. [Agent Wiki Memory](#agent-wiki-memory)
10. [REST API Reference](#rest-api-reference)
11. [Troubleshooting](#troubleshooting)

---

## Getting Started

### System Requirements

- Python 3.11 or higher
- Windows 10/11, macOS, or Linux
- [LM Studio](https://lmstudio.ai) or Ollama (local inference), or an [OpenRouter](https://openrouter.ai) API key
- A Discord bot token (for remote control)

### Installation

```powershell
cd J:\Projects\coding-agent
python -m pip install -e .
```

### First-time Setup

1. **Copy `.env.example` to `.env`** and fill in your values (see [Configuration](#configuration)).
2. **Edit `config/models.yaml`** to list the models you have available.
3. **Start with the supervisor:**
   ```powershell
   python supervisor.py
   ```
4. **Verify** by sending `!ask say hello` in your Discord channel.

---

## Configuration

### Environment Variables (`.env`)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DISCORD_BOT_TOKEN` | Yes | — | Your Discord bot token |
| `AGENT_API_URL` | No | `http://127.0.0.1:5005` | URL the bot uses to reach the API. Use `127.0.0.1` not `localhost` on Windows 11 (IPv6 resolution issue) |
| `LM_STUDIO_URL` | No | `http://127.0.0.1:1234` | LM Studio or Ollama endpoint |
| `WORKSPACE_PATH` | No | `./workspace` | Root directory for all agent file operations |
| `PROJECT_DIR` | No | _(none)_ | Active project subdirectory within `WORKSPACE_PATH`. When set, agents write to `WORKSPACE_PATH/PROJECT_DIR` and see it in context |
| `OPENROUTER_API_KEY` | No | — | Enables cloud model access via OpenRouter |
| `BRAVE_SEARCH_API_KEY` | No | — | Primary web search (2 000 free queries/month) |
| `EXTRA_PATH` | No | — | Comma-separated additional directories to add to the shell tool's PATH |
| `BOT_PYTHON` | No | `sys.executable` | Python interpreter for the bot subprocess (set when bot uses a different venv than the API) |
| `API_STARTUP_TIMEOUT` | No | `120` | Seconds supervisor waits for `/health` on startup |
| `RESTART_DELAY_SECS` | No | `3` | Seconds between stop and start during a supervisor restart |

### Model Configuration (`config/models.yaml`)

```yaml
models:
  # Local model via LM Studio or Ollama
  - name: qwen3.5-35b-a3b
    type: local
    endpoint: ${LM_STUDIO_URL:-http://127.0.0.1:1234}  # supports ${VAR:-default}
    context_window: 262144
    is_coding_optimized: true
    recommended_for: [coding, code_review, planning, research]
    rate_limit_rpm: 120

  # Remote model via OpenRouter
  - name: google/gemma-4-31b-it:free
    type: remote
    endpoint: https://openrouter.ai/api/v1
    api_key_env: OPENROUTER_API_KEY   # reads key from .env
    context_window: 262144
    is_coding_optimized: true
    rate_limit_rpm: 60

defaults:
  coding_model: qwen3.5-35b-a3b      # used for all develop/plan/test tasks
  planning_model: google_gemma-4-31b-it
  fallback_model: openai/gpt-oss-120b:free
```

**Key fields:**

| Field | Description |
|-------|-------------|
| `name` | Must exactly match the model ID in LM Studio / OpenRouter |
| `type` | `local` (Ollama/LM Studio) or `remote` (OpenRouter/OpenAI-compatible) |
| `endpoint` | API base URL; supports `${ENV_VAR:-default}` syntax |
| `context_window` | Token limit — used by the context bridge to decide when to hand over |
| `is_coding_optimized` | Marks model as preferred for coding tasks |
| `api_key_env` | Env var name that holds the API key (not the key itself) |
| `rate_limit_rpm` | Max requests per minute for this model |
| `enable_thinking` | Optional. Set to `false` to disable thinking mode globally for a model (e.g., for non-Qwen3 models that don't support it) |

---

## Starting the Agent

### Recommended: Supervisor

```powershell
python supervisor.py
```

The supervisor starts the API and bot in order, then watches them continuously:

| Condition | Action | Latency |
|-----------|--------|---------|
| API or bot process exits/crashes | Kill both, restart in order | ~2s |
| API alive but `/health` unresponsive (3 consecutive failures) | Kill and restart | ~90s |
| Bot crash loop (5 fast failures < 10s each) | Stop auto-restart, wait for `!restart` | immediate |
| Running job stuck for > 45 min | Force restart | ~5 min check |

Logs from both processes are saved to `logs/api-YYYYMMDD-HHMMSS.log` and `logs/bot-*.log`. Each restart opens a new file.

### Manual Start (development only)

```powershell
# Terminal 1 — API server
python -m uvicorn api.main:app --host 0.0.0.0 --port 5005 --log-level info

# Terminal 2 — Discord bot
python -m api.discord_bot
```

---

## Discord Commands Reference

All commands use the `!` prefix. The bot responds in the channel where the command was sent and remembers the last active channel across restarts.

### Task Submission

#### `!ask <task>`
Submit a task. The agent auto-classifies the type (develop, research, plan, etc.) and runs in the background. The bot posts live phase updates as the task progresses.

```
!ask implement a dark mode toggle for the settings page
!ask explain how the auth middleware works
!ask write unit tests for the user model
```

#### `!dev <task>`
Force the **develop** path — bypasses the LLM classifier entirely. Use this when the classifier misroutes an obvious coding/run/fix task.

```
!dev run npm install and fix any errors
!dev fix the TypeScript errors in src/index.ts
```

#### `!continue [note]`
Resume the current active debugging session. Optionally attach a note to guide the next iteration.

```
!continue
!continue the build is still failing on the test step
```

---

### Job Monitoring

#### `!status`
Shows the current job's phase, elapsed time, and job ID.

#### `!tasks`
Shows the full task plan with status for each subtask:

```
Task plan (3/4 done)
✅ 1. [develop] Read package.json
✅ 2. [develop] Run npm start to capture error
✅ 3. [develop] Fix TypeError in game.js
🔄 4. [develop] Verify fix with npm start
```

#### `!result`
Returns the full response text from the most recent completed job.

#### `!files`
Lists all files created or modified by the last task.

#### `!jobs [n]`
Lists the last N jobs (default 10) with status, type, and elapsed time.

#### `!cancel`
Cancels the currently running job.

---

### File Access

#### `!show <path>`
Displays the contents of a workspace file. Short files are shown inline; longer files are sent as attachments.

```
!show src/game.js
!show package.json
```

---

### Session Management

#### `!session`
Shows the current session ID.

#### `!sessions`
Lists all saved sessions with message count and last-active date.

#### `!history`
Shows recent conversation history for the current session.

#### `!clear`
Clears the current session's conversation history.

---

### Workspace

#### `!workspace`
Shows the current workspace root path and active project directory.

#### `!project [name]`
Get or set the active project:

```
!project                         # show current project
!project Shadows-of-Eldoria      # set active project
!project none                    # clear active project (use workspace root)
```

Setting a project scopes all agent file operations to `WORKSPACE_PATH/<name>`. Agents see `Active project: <name>` in their context and write files there without creating nested subdirectories.

---

### Model Management

#### `!models`
Lists all configured models with:
- Live LM Studio load state: 🟢 **loaded** (in VRAM) / ⚪ **not-loaded**
- Which model is currently active
- A **discovery section** showing models downloaded in LM Studio but not yet in `models.yaml`

```
Configured Models · active: qwen3.5-35b-a3b

**[active]** `qwen3.5-35b-a3b` — local · 262k ctx 🟢
       `google/gemma-4-31b-it:free` — remote · 262k ctx

Available in LM Studio (not configured)
  `llama-3.3-70b-instruct` ⚪
  `deepseek-r1-distill-qwen-14b` 🟢
```

#### `!model <name>`
Switch to a different model for all subsequent tasks in this session.

```
!model google/gemma-4-31b-it:free
```

#### `!model reset`
Revert to the default model from `config/models.yaml`.

---

### Operations

#### `!git <args>`
Run any git command in the workspace directory.

```
!git status
!git log --oneline -10
!git diff HEAD
```

#### `!skills [list]`
Lists all available agent skills (wiki-query, wiki-compile, handover, etc.).

#### `!restart` / `!reboot`
Signals the supervisor to restart both the API and the bot. The bot posts a "back online" message in the same channel after restart. If the supervisor is not running, you'll see a warning.

#### `!helpme`
Displays a command reference cheat sheet directly in Discord.

---

## Task Types & Routing

The agent auto-classifies every `!ask` task into one of these types:

| Type | When it fires | What happens |
|------|--------------|--------------|
| `develop` | Write, fix, run, build, debug, compile, npm, execute | `DeveloperAgent` — writes files, runs shell, fix loop up to 10 iterations |
| `research` | Investigate, find, explain, search for, how does | `ResearchAgent` — reads files, web search, synthesis report |
| `plan` | Plan first, show me a plan, roadmap | `PlanAgent` — creates implementation plan without writing code |
| `sdlc` | Build me a complete app, end-to-end | Full SDLC pipeline: plan → build → test → debug → run → verify |
| `test` | Write tests, pytest, unit tests | `TesterAgent` — writes and runs test suites |
| `review` | Code review, security audit | `ReviewerAgent` — audits for bugs, style, security |
| `architect` | System design, write an ADR | `ArchitectAgent` — high-level design decisions |
| `chat` | Everything else | `ChatAgent` — conversational Q&A (no file tools) |

**Pre-LLM keyword fast-path:** Common develop patterns (fix, run, npm, build, compile, debug) are classified immediately without an LLM call. Use `!dev` to force the develop path if auto-classification is wrong.

---

## Workspace & Project Scoping

### Workspace Root

All agent file operations happen inside `WORKSPACE_PATH`. The agent cannot read or write outside this boundary.

### Active Project

Set `PROJECT_DIR` in `.env` or use `!project <name>` to focus the agent on a subdirectory:

```
WORKSPACE_PATH=J:\Projects\agent-workspace
PROJECT_DIR=Shadows-of-Eldoria
```

Effect on agents:
- All file writes go to `WORKSPACE_PATH/Shadows-of-Eldoria/` — no `Shadows-of-Eldoria/Shadows-of-Eldoria/` nesting
- Every task prompt includes `Active project: Shadows-of-Eldoria`
- `!workspace` and `!project` reflect the current state

---

## Model Management

### Thinking Models (Qwen3, DeepSeek-R1)

Thinking models generate a `<think>...</think>` reasoning trace before responding. This is valuable for complex coding tasks but wasteful for classification (one-word answers).

The agent handles this automatically:
- **Classifier calls** always pass `enable_thinking=False` — no 10-30 min thinking trace for a one-word answer
- **All coding, planning, and review calls** use the model's default thinking setting (enabled)
- **Wiki synthesis** passes `enable_thinking=False` — structured output doesn't benefit from deep reasoning

You should not set `enable_thinking: false` in `models.yaml` — that would disable thinking globally for all tasks.

### Model Fallback

If a local model is unavailable (HTTP 503 / "model not loaded"):
1. Supervisor logs `model_not_ready_waiting`, waits 120s, retries up to 3 times
2. After 3 failed attempts, falls back to the next available local model
3. If no local fallback, falls back to the configured `fallback_model` in `models.yaml`

Timeout errors fail immediately without retry (a second attempt at 600s is wasteful).

### Switching Models at Runtime

```
!model qwen3.5-35b-a3b       # switch to a specific model
!model reset                  # revert to models.yaml default
```

Or via the API:
```
POST /models/active   {"model": "qwen3.5-35b-a3b"}
```

---

## Context Bridge

The agent monitors its token budget before every task. When the conversation history approaches the model's `context_window`:

| Budget level | Action |
|-------------|--------|
| < 75% | Continue normally |
| 75–82% | Post a heads-up warning in Discord: "Context is getting full" |
| ≥ 82% | **Context Bridge**: generate a structured handover document, create a new session pre-seeded with it, continue the task transparently |

The bridge is silent — the task continues in the new session without interruption. Discord shows a one-line notice: `↩ Context limit reached — continuing in new session`.

You can always resume the previous session with `!session` and `!sessions`.

---

## Agent Wiki Memory

The agent maintains a per-workspace knowledge base in `.agent-wiki/`:

```
.agent-wiki/
├── index.md              # Catalog of all entries
├── log.md                # Compilation history
├── tech-patterns/        # Discovered code patterns
├── bugs/                 # Bugs found and fixed
├── decisions/            # Architecture decisions
├── api-usage/            # API/SDK usage patterns
└── synthesis/            # Cross-task synthesis
```

**How it works:**

1. **Pre-task (wiki-query):** Before each task, the agent searches the wiki for relevant entries and injects them as context.
2. **Post-task (wiki-compile):** After each task succeeds, the LLM synthesises a structured wiki entry from the task + result. This happens for every subtask in a multi-task job, so Task 2 can see what Task 1 learned.
3. **Deduplication:** The index is upserted — re-compiling the same entry updates the row rather than appending a duplicate.

Use `!skills` to see available wiki skills. The wiki accumulates over time and improves agent performance on repeat work in the same codebase.

---

## REST API Reference

The API runs on port 5005 by default. All endpoints accept/return JSON.

### Core Task Endpoints

```
POST /task/start
  Body: {"task": "...", "session_id": "...", "force_task_type": "develop"}
  Returns: {"job_id": "...", "session_id": "..."}

GET /task/{job_id}
  Returns: {"job_id", "status", "phase", "summary", "created_at", ...}

GET /task/{job_id}/result
  Returns: {"response": "full agent output..."}

GET /task/{job_id}/tasks
  Returns: {"tasks": [{"sequence", "description", "status", "result"}...]}

DELETE /task/{job_id}
  Cancels the job.

GET /jobs?limit=50&offset=0
  Returns: {"jobs": [...]}
```

### Model Endpoints

```
GET /models
  Returns: {
    "active_model": "qwen3.5-35b-a3b",
    "models": [{"name", "type", "context_window", "state", "is_active"}, ...],
    "lm_studio_available": [{"id", "state"}, ...]   # downloaded but unconfigured
  }

POST /models/active
  Body: {"model": "model-name"}

GET /llm/health
  Returns detailed health: circuit breaker states, rate limiter, cost summary
```

### Workspace Endpoints

```
GET /workspace
  Returns: {"workspace_path": "...", "project_dir": "..."}

GET /workspace/file?path=relative/path
  Returns file contents as text.

POST /workspace/project
  Body: {"project": "project-name"}
  Sets PROJECT_DIR for the running process.
```

### Health & Operations

```
GET /health
  Returns: {"status": "healthy", "agent_ready": bool, "active_jobs": int,
            "uptime_seconds": int, "timestamp": float}

GET /ready
  Returns 200 only when the orchestrator is fully initialised.

POST /restart
  Signals supervisor to restart both services.
  Returns: {"status": "restart_requested", "supervisor_running": bool}
```

---

## Troubleshooting

### Bot says "supervisor.py is not running — restart not possible"

The `!restart` command writes a flag file that `supervisor.py` watches. If the supervisor is not running, the flag is ignored. Start the supervisor:
```powershell
python supervisor.py
```

### API returns 503 on `/task`

The orchestrator is still initialising (connecting to the LLM, indexing the workspace). Wait for the log line `agent_initialized` or check `GET /ready`. The supervisor's health probe will show `agent_ready=False` until then.

### Job stuck at "preparing" for a long time

The task classifier is waiting for the LLM. Possible causes:
- **Thinking model slow on a one-word answer** — should be fixed in phase-18 (`enable_thinking=False` on classifier calls). If still happening, check that your `ollama_client.py` is up to date.
- **Model not loaded in LM Studio** — open LM Studio and load the model. The supervisor will retry automatically.
- **asyncio event loop blocked** — the 30s health probe will detect this and restart the API automatically within 90s.

### "Model not ready" / HTTP 503 from LM Studio

The model was evicted from VRAM (TTL expiry). The agent waits 120s and retries up to 3 times automatically. You'll see `model_not_ready_waiting` in the logs. If it keeps happening, increase LM Studio's model TTL or keep the model pinned.

### npm / node / git not found in shell commands

The shell tool auto-discovers tools at startup. Check the API log for:
```
tool_path_discovery  npm=C:\...\npm.CMD  node=...  git=...
```
If a tool shows `NOT FOUND`, add its directory to `EXTRA_PATH` in `.env`:
```dotenv
EXTRA_PATH=C:\custom\tools\bin,C:\another\dir
```

### localhost connection refused (Windows 11)

Windows 11 resolves `localhost` to `::1` (IPv6), but uvicorn binds to `0.0.0.0` (IPv4). Set:
```dotenv
AGENT_API_URL=http://127.0.0.1:5005
```

### UnicodeDecodeError on npm output

Fixed in phase-18 — `shell_tool.py` now uses `encoding='utf-8', errors='replace'`. Update your branch.

### Context Bridge fires too frequently

The bridge triggers at 82% of `context_window`. If it fires too often, increase the `context_window` value in `models.yaml` to match your actual model's limit, or use a model with a larger context window.

### Rate limit exceeded (cloud models)

The agent automatically falls back to your local model when a cloud provider returns 429. Logged as `using_local_fallback`. To reduce rate limiting, lower `rate_limit_rpm` in `models.yaml` or add more fallback models.

### Web search returns no results

The search chain tries: **Brave → DuckDuckGo → Playwright Google → Google CSE**.

- **Brave key invalid or quota exhausted** — falls back to DuckDuckGo automatically.
- **DuckDuckGo blocked** — falls back to Playwright Google.
- **Google CSE 403** — Google deprecated full-web Programmable Search as of Jan 2026. Use Brave Search API instead.

### Viewing process logs

Supervisor writes child process output to `logs/`:
```powershell
Get-Content logs\api-20260415-120000.log -Wait   # tail the latest API log
Get-Content logs\bot-20260415-120005.log -Wait   # tail the latest bot log
```

---

*Last updated: 2026-04-15 — Phase 18*
