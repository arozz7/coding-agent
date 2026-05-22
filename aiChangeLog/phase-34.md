# Phase 34 — API Routes Split

## Goal
Split `api/main.py` (1375 lines) into domain-specific route modules under `api/routes/`,
rewriting `main.py` as a thin app factory under 200 lines.

## Files Created

### `api/deps.py` (152 lines)
- Shared singleton `AppState` (orchestrator, current_workspace, pending_switch_events)
- Eagerly-created `job_store` and `task_store` (SQLite-backed)
- Constants: `WORKSPACE_PATH`, `DISALLOWED_PATHS`, `_AGENT_API_KEY`
- Helpers: `load_persisted_project()`, `save_persisted_project()`, `_is_path_allowed()`,
  `require_api_key()` (FastAPI dependency), `summarize_response()`
- Avoids circular import by typing `AppState.orchestrator` as `Any` (no top-level import of `AgentOrchestrator`)

### `api/routes/__init__.py` (empty)

### `api/routes/tasks.py` (222 lines)
- `TaskRequest`, `TaskResponse` Pydantic models
- Routes: POST `/task`, POST `/task/start`, POST `/task/stream`,
  GET/DELETE `/task/{job_id}`, GET `/task/{job_id}/result`, GET `/task/{job_id}/tasks`,
  GET `/jobs`, GET `/chains`

### `api/routes/workspace.py` (320 lines)
- Routes: GET/POST `/workspace`, GET `/workspace/file`, GET `/workspace/project`,
  GET `/workspace/directories`, POST `/workspace/project`,
  GET+POST `/wiki/*`, POST `/screenshot`,
  POST `/index`, GET `/search`,
  GET `/projects/{name}/delete-preview`, DELETE `/projects/{name}`

### `api/routes/models.py` (112 lines)
- Routes: GET `/models`, GET/POST `/models/active`, GET `/events/model-switches`

### `api/routes/sessions.py` (121 lines)
- Routes: GET/DELETE `/sessions`, GET `/sessions/{id}`, DELETE `/sessions/{id}`,
  POST `/wake/{id}`, POST `/subagent/spawn`, POST `/subagent/spawn-batch`,
  GET `/subagent`, GET `/subagent/{id}`

### `api/routes/system.py` (227 lines)
- Routes: GET `/`, GET `/health`, GET `/ready`, GET `/stats`, GET `/llm/health`,
  GET `/metrics`, POST `/restart`, GET/POST `/environment/*`,
  GET/POST `/skills/*`, GET `/memory/stats`, GET/POST `/mcp/tools*`

## Files Modified

### `api/main.py` (1375 → 167 lines, −88%)
- Startup project resolution (persisted project + effective workspace init)
- FastAPI app construction + CORS middleware
- `app.include_router(...)` calls for all five route modules
- `_init_agent_background()` + `startup_event` (startup logic kept here; wires `app_state`)

## Key Architectural Decisions
- **`AppState` singleton in `deps.py`**: replaces module-level `_orchestrator`, `_current_workspace`,
  `_pending_switch_events` in the old monolith; all routes mutate `app_state` directly
- **No circular import**: `deps.py` never imports `AgentOrchestrator`; routes import `app_state`
  (typed `Any`) and call orchestrator methods dynamically
- **`state_dir` path in `/restart`**: adjusted from `Path(__file__).parent.parent` to
  `Path(__file__).parent.parent.parent` to account for the extra `routes/` nesting level

## Behavior
No logic changes — pure extraction. All URL paths, response shapes, and security guards
are identical to the pre-refactor `main.py`.
