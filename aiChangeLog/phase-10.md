# Phase 10 — Full Feature Completion: Environment, Discord UX, Skills, Observability

## Goals
Complete all remaining features in priority order, plus a cross-cutting environment &
secrets layer that makes the project portable across machines and operating systems.

---

## 10.0 — Environment & Secrets Foundation

### `.env.example` (rewritten)
Full template of every configurable variable with inline documentation.
Covers LM Studio URL, Discord token, API settings, storage paths, external tool overrides,
and observability toggles. The file is committed; `.env` (with real values) is git-ignored.

### `config/environment.yaml` (new)
Registry of external tools (Playwright Chromium, git, Node.js, Python) with:
- Per-tool `env_override` — env var that always wins over auto-detection
- Platform-specific `search_paths` for Windows, macOS, Linux
- `is_directory` flag for tools identified by a directory (Playwright binary store)
- `skills_registry` section pointing to the remote skill source (GitHub repo + branch + path)

### `agent/tools/environment_probe.py` (new)
`EnvironmentProbe` class:
- Reads `config/environment.yaml` on init
- Probes PATH (`shutil.which`) then platform search dirs in order
- Expands `{LOCALAPPDATA}` / `{APPDATA}` / `~` in Windows/macOS/Linux paths
- Caches results to `data/environment.json` — re-reads on next startup if platform changes
- `get_tool_path(name)` → `Optional[str]`; `get_all()` → full status dict for API
- `reprobe()` forces a fresh probe and saves cache
- Module-level singleton via `get_environment_probe()`

### `config/models.yaml`
`endpoint` fields now use `${LM_STUDIO_URL}` instead of the hardcoded `127.0.0.1:1234`.
Any machine sets `LM_STUDIO_URL` in its `.env` and no YAML edit is needed.

### `llm/model_router.py`
Added `_expand_env(value)` static method: replaces `${VAR}` references using `os.environ`.
Unknown variables are left as-is (visible in logs) rather than silently going empty.
Called for every string field when loading model configs.

### `local_coding_agent/__init__.py`
`_load_env()` called at import time: loads `.env` via `python-dotenv` (silently skipped
if file absent or package missing). Env vars are present before any config reads.

### API: `GET /environment`, `POST /environment/reprobe`
New endpoints expose probe results and allow remote re-detection.

---

## 10.1 — High Priority Features

### `!jobs` Discord command (`api/discord_bot.py`)
Lists recent jobs newest-first with status icon, task type, age, and task preview.
Calls `GET /jobs`. `!jobs 20` increases the limit up to 50.

### Live phase streaming (`agent/orchestrator.py`, `api/main.py`)
`run_task()` now accepts `on_phase: Optional[Callable[[str], None]]`.
The callback fires at key milestones: `"preparing"` (before context build),
the agent-specific label (`"developing"`, `"researching"`, etc.), and `"complete"`.
`api/main.py`'s `_run()` closure passes `_on_phase → _job_store.update(phase=...)`.
Discord poller now sees real-time phase changes, not just the initial label.

### `start_task_background` instant response
`start_task_background()` now uses `_detect_task_type_keyword()` (0 ms) for the
immediate API response. The LLM classifier runs inside `run_task()` in parallel
with `_build_enriched_context()` via `asyncio.gather()`, so neither adds serial latency.

### `!show` file size guard (`api/discord_bot.py`)
- Known binary extensions (png, pdf, exe, db, …) rejected before fetch with clear message.
- Files > 7 MB refused with size info (Discord cap is 8 MB).
- Files 1800 chars – 7 MB sent as attachment (unchanged).
- Files ≤ 1800 chars shown inline (unchanged).

---

## 10.2 — Medium Priority Features

### Anthropic skills remote fetch (`agent/skills/skill_loader.py`)
`SkillManager.fetch_remote()`:
- Reads registry from `config/environment.yaml` (`skills_registry` section).
- Hits GitHub Contents API (no auth — public repo).
- Recursively finds `SKILL.md` files up to one directory deep.
- Skips files that haven't changed (SHA-256 comparison).
- Re-runs `discover_skills()` after successful fetch.
- Returns `{fetched, skipped, errors, skills}`.

New API endpoints: `GET /skills`, `POST /skills/fetch`.
New Discord commands: `!skills` (list), `!skills fetch` (download).

### Subagent RAG merge-back (`agent/orchestrator.py`)
After `spawn_subagent()` completes successfully, any files listed in `files_created`
are indexed back into `codebase_memory` (the parent's vector store). Future RAG
searches in the parent session can now find content the subagent wrote.
Index failures are logged as warnings and do not fail the subagent result.

### Observability token estimation bug fix (`observability/logging.py`)
`log_llm_call()` received `prompt_length` / `response_length` as integer character
counts and divided by 4 with `// 4` — correct. But callers sometimes pass
pre-computed token integers. Added `_to_tokens(val)` guard: applies `// 4` for ints
(treating them as char counts) and falls back to `len(str(val)) // 4` for safety.

---

## 10.3 — Low Priority Features

### MemoryWiki contradiction detection (`agent/memory/memory_wiki.py`)
`lint()` method added:
1. **Orphan nodes** — nodes with no edges (existing behaviour, now surfaced via API).
2. **Duplicate functions** — same bare function name in ≥2 unrelated files.
3. **Duplicate classes** — same bare class name in ≥2 unrelated files.

"Related" = any import edge between the two files (checked in both directions).
Results included in `GET /memory/stats` response under `lint` key.

### Prometheus FastAPI auto-instrumentation (`api/main.py`, `pyproject.toml`)
`prometheus-fastapi-instrumentator` wired at app creation:
- Per-route request count, latency histogram (p50/p95/p99), in-flight gauge.
- Metrics registered in the default Prometheus registry → exposed by existing `/metrics`.
- `ImportError` is caught so the app still starts if the package is missing.
- Added to `pyproject.toml` dependencies.

### Parallel LLM classifier (`agent/orchestrator.py`)
`run_task()` now runs `_detect_task_type()` and `_build_enriched_context()` via
`asyncio.gather()`. Total latency = `max(classify, context)` instead of sequential sum.
`start_task_background()` uses keyword classifier for the API response so `/task/start`
is always instant.

---

## Files Changed

| File | Change |
|---|---|
| `.env.example` | Full rewrite with all variables documented |
| `config/environment.yaml` | New — tool registry + skills registry |
| `config/models.yaml` | `${LM_STUDIO_URL}` for endpoints |
| `agent/tools/environment_probe.py` | New — cross-platform probe + cache |
| `agent/skills/skill_loader.py` | `fetch_remote()` added |
| `agent/memory/memory_wiki.py` | `lint()` + `_files_are_related()` added |
| `agent/orchestrator.py` | `on_phase` callback, parallel classify+context, subagent RAG merge |
| `observability/logging.py` | Token estimation guard |
| `llm/model_router.py` | `_expand_env()` + env var expansion in `_load_configs()` |
| `local_coding_agent/__init__.py` | `_load_env()` / `load_dotenv` at import |
| `api/main.py` | `on_phase` wiring, `/environment`, `/skills`, Prometheus, lint in `/memory/stats` |
| `api/discord_bot.py` | `!jobs`, `!skills`, `!show` guard, `!helpme` update |
| `pyproject.toml` | Added `python-dotenv`, `prometheus-fastapi-instrumentator` |
