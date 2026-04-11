# Phase 08 — Batch 6: Chat + Research Agents · Developer Fixes · Background Jobs · Discord UX

**Date:** 2026-04-10
**Plan version:** v2.3
**Covers:** Batch 6 — two new agents, three developer-agent bug fixes, background job API, Discord bot redesign

---

## Summary

Five areas addressed in this batch:

1. **New `ChatAgent`** — routes conversational questions, explanations, and general discussion away from the developer agent.
2. **New `ResearchAgent`** — handles codebase investigation ("where is X", "how does Y work") without writing any files.
3. **Developer agent fixes** — shell commands extracted only from fenced shell blocks (not inline backticks); shell output now shows stdout + stderr + exit code; screenshot only fires on explicit task intent.
4. **Background job API** — `POST /task/start` returns a `job_id` immediately; `GET /task/{id}` polls status; `GET /task/{id}/result` retrieves the full response; `DELETE /task/{id}` cancels.
5. **Discord bot redesign** — `!ask` works asynchronously, editing one message with live phase updates. Code is never dumped to the channel. New commands: `!status`, `!cancel`, `!result`, `!files`, `!show <path>`.

---

## Fix 1 — ChatAgent (`agent/agents/chat_agent.py`)

New agent and role. System prompt explicitly prohibits turning questions into coding tasks. Receives `enriched_context` for wiki/RAG enrichment but never writes files or runs commands.

Task-type routing: `_detect_task_type()` now returns `"chat"` as the default catch-all instead of `"general"`. Anything that doesn't match a specific coding/review/test/arch/research pattern goes to `ChatAgent`.

---

## Fix 2 — ResearchAgent (`agent/agents/research_agent.py`)

New agent and role. On `execute()`:
1. Lists the workspace (for orientation)
2. Reads up to 4 files explicitly mentioned in the task text (path-regex matching)
3. Combines gathered content with the task and asks the LLM for a structured report

Never creates files or runs shell commands. Routes via `_detect_task_type()` on patterns like "where is", "find the", "how does the existing", "explain this code".

---

## Fix 3 — Task-Type Routing (`agent/orchestrator.py`)

`_detect_task_type()` completely rewritten as a priority cascade:

| Priority | Pattern examples | Returned type |
|---|---|---|
| 1 | "implement", "refactor", "create a file", "fix the bug" | `develop` |
| 2 | "code review", "security audit", "review this file" | `review` |
| 3 | "write tests", "pytest", "test suite" | `test` |
| 4 | "system design", "write an adr", "architect the" | `architect` |
| 5 | "where is", "find the", "explain this code" | `research` |
| 6 | _(everything else)_ | `chat` |

`_run_specialized_agent()` updated with `research` and `chat` branches. `spawn_subagent()` updated with `"researcher"` and `"chat"` roles.

---

## Fix 4 — Developer Agent Shell Extraction (`agent/agents/developer_agent.py`)

**Before:** `re.findall(r'`([^`]+)`', response)` executed every inline backtick span as a shell command — including file names, package names, and code references in prose.

**After:** Only fenced blocks explicitly marked as shell/bash/sh/cmd/powershell/ps1 are executed. Each non-blank, non-comment line is run as a separate command.

```python
_SHELL_BLOCK_RE = re.compile(
    r'```(?:shell|bash|sh|cmd|powershell|ps1)\n(.*?)```',
    re.DOTALL | re.IGNORECASE,
)
```

**Screenshot trigger** changed from `re.search(r'screenshot', task)` (matched any word containing "screenshot") to:
```python
_SCREENSHOT_RE = re.compile(
    r'\b(take\s+a?\s*screenshot|capture\s+(?:a\s+)?screenshot|screenshot\s+of)\b',
    re.IGNORECASE,
)
```

---

## Fix 5 — Shell Output (`agent/tools/tool_executor.py`)

`_run_shell()` previously returned `result.get('stderr', 'Command failed')` on failure — stderr is often empty and stdout has the real error text.

New behaviour:
- Success: returns `stdout` or `"(command completed, no output)"`
- Failure: returns `"Command failed (exit N):\nstdout: ...\nstderr: ..."` — includes both streams and the exit code

---

## Fix 6 — Background Job API (`api/main.py`)

Four new endpoints added alongside the existing synchronous `/task`:

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/task/start` | Submit task, return `{job_id}` immediately |
| `GET` | `/task/{job_id}` | Poll status: `pending → running → done/failed/cancelled` |
| `GET` | `/task/{job_id}/result` | Retrieve full agent response (prose + code) |
| `DELETE` | `/task/{job_id}` | Request cancellation |

Also added:
- `GET /workspace/file?path=<rel>` — read a workspace file by relative path (path-traversal protected)

Jobs are stored in `_jobs: Dict[str, dict]` (in-memory; resets on restart). The stored `summary` field is the prose-only excerpt (code blocks stripped via `_summarize_response()`). The full response is stored under `_full_response` and only returned by the `/result` endpoint.

`_detect_task_type()` is called before the background task starts so the Discord bot can show a meaningful phase label immediately (e.g. "developing", "researching").

---

## Fix 7 — Discord Bot Redesign (`api/discord_bot.py`)

Complete rewrite. Key changes:

**`AgentClient` → `AsyncAgentClient`**
All HTTP calls use `httpx.AsyncClient` instead of the blocking `requests.Session`. Sync `is_reachable()` probe kept for startup only.

**`!ask` — fire-and-forget with live updates**
1. Calls `POST /task/start` → gets `job_id`
2. Stores job in `bot.user_jobs[user_id]`
3. Sends one status message
4. Spawns `_poll_job()` as an `asyncio.create_task()`
5. `_poll_job()` edits the same message every 5 s with phase label + elapsed time
6. On completion: message replaced with summary + file list + usage hint

**Code never shown in channel**
`strip_code_blocks(text)` replaces every fenced block with `[lang — N lines · use !show to view]`. Applied everywhere text goes to Discord.

**New commands**

| Command | Purpose |
|---|---|
| `!status` | Show current job phase |
| `!cancel` | Cancel running job |
| `!result` | Prose response from last job (code stripped) |
| `!files` | Files created in last task |
| `!show <path>` | Inline (≤1800 chars) or file attachment |

**Removed commands**
`!explain`, `!test`, `!refactor`, `!review`, `!docs`, `!cd`, `!load`, `!screenshot` — these sent raw code to the channel. Replaced by the unified `!ask` + `!show` pattern.

---

## Known Remaining Issues (Batch 7+)

- `_jobs` dict is in-memory — lost on API restart; could be persisted to SQLite
- No job expiry / cleanup — old jobs accumulate in `_jobs` indefinitely
- `!show` uploads to Discord (8 MB limit) — large binary files will fail
- Research agent reads up to 4 files per task — RAG results from `enriched_context` cover more, but deep multi-file traces require tool-call loops
- `_detect_task_type` uses keyword matching — an LLM-based classifier would be more robust
