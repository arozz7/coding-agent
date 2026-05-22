"""FastAPI application factory — mounts route modules and wires startup."""
from __future__ import annotations

import asyncio
import os
import time as _time
from pathlib import Path

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.deps import app_state, job_store, WORKSPACE_PATH, load_persisted_project
from api.routes import tasks, workspace, models, sessions, system

logger = structlog.get_logger()

# ---------------------------------------------------------------------------
# Startup project resolution
# ---------------------------------------------------------------------------
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(override=False)
except ImportError:
    pass

PROJECT_DIR = os.getenv("PROJECT_DIR", "").strip()
_persisted = load_persisted_project()
_startup_project: str = _persisted if _persisted is not None else PROJECT_DIR


def _effective_workspace(base: str = WORKSPACE_PATH, project: str = "") -> str:
    if project:
        return str(Path(base) / project)
    return base


_initial_workspace = _effective_workspace(project=_startup_project)
Path(WORKSPACE_PATH).mkdir(parents=True, exist_ok=True)
Path(_initial_workspace).mkdir(parents=True, exist_ok=True)
os.environ["AGENT_EFFECTIVE_WORKSPACE"] = _initial_workspace
app_state.current_workspace = _initial_workspace

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Local Coding Agent API",
    description="REST API for interacting with the local coding agent",
    version="0.1.0",
)

try:
    from prometheus_fastapi_instrumentator import Instrumentator
    Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        excluded_handlers=["/metrics", "/health"],
    ).instrument(app)
except ImportError:
    pass

_default_origins = [
    "http://localhost:3000",
    "http://localhost:5005",
    "http://localhost:8080",
    "http://127.0.0.1:3000",
    "http://127.0.0.1:5005",
    "http://127.0.0.1:8080",
]
_cors_origins_env = os.getenv("CORS_ORIGINS", "")
CORS_ORIGINS = (
    [o.strip() for o in _cors_origins_env.split(",") if o.strip()]
    if _cors_origins_env
    else _default_origins
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-API-Key"],
)

# ---------------------------------------------------------------------------
# Route registration
# ---------------------------------------------------------------------------
app.include_router(tasks.router)
app.include_router(workspace.router)
app.include_router(models.router)
app.include_router(sessions.router)
app.include_router(system.router)

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
_STARTUP_BACKOFF = [2, 5, 15, 30, 60, 120, 300]


def _startup_backoff(attempt: int) -> float:
    return float(_STARTUP_BACKOFF[min(attempt, len(_STARTUP_BACKOFF) - 1)])


async def _init_agent_background() -> None:
    """Initialise the orchestrator in the background so uvicorn can serve
    requests (especially GET /health) immediately.

    /task endpoints return 503 until the orchestrator is ready.  Retries
    indefinitely with the same backoff curve used before this refactor.
    """
    from local_coding_agent import create_agent

    attempt = 0
    while app_state.orchestrator is None:
        try:
            app_state.orchestrator = create_agent(app_state.current_workspace, "config/models.yaml")
            logger.info("agent_initialized", attempt=attempt)
        except Exception as e:
            delay = _startup_backoff(attempt)
            logger.warning("agent_init_failed", error=str(e), attempt=attempt, retry_in=delay)
            attempt += 1
            await asyncio.sleep(delay)

    def _api_switch_callback(event) -> None:
        app_state.pending_switch_events.append({
            "from_model": event.from_model,
            "to_model": event.to_model,
            "reason": event.reason,
            "timestamp": event.timestamp.isoformat(),
        })
    app_state.orchestrator.model_router.register_switch_callback(_api_switch_callback)

    primary = app_state.orchestrator.model_router.get_model("coding")
    if primary and primary.type == "local":
        logger.info("model_probe_start", model=primary.name)
        ok = await app_state.orchestrator.model_router.ollama.warmup(primary.name)
        if not ok:
            action = (
                "Ensure TurboQuantLoader is running and the model is loaded."
                if primary.provider == "turboquant"
                else "Open LM Studio and load the model — tasks will block until it is ready."
            )
            logger.warning(
                "model_not_ready_at_startup",
                model=primary.name,
                provider=primary.provider,
                action=action,
            )


@app.on_event("startup")
async def startup_event():
    logger.info(
        "starting_api",
        component="api",
        workspace=app_state.current_workspace,
        project_dir=PROJECT_DIR or "(none)",
    )
    job_store.load()
    asyncio.create_task(_init_agent_background())


if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "5005"))
    uvicorn.run(app, host="0.0.0.0", port=port)
