from fastapi import FastAPI, HTTPException, BackgroundTasks, Response, Request, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
import uuid
import re
import time as _time
from datetime import datetime, timezone
import asyncio
import structlog

_SERVER_START_TIME = _time.time()

from llm import ModelRouter
from agent.orchestrator import AgentOrchestrator
from api.job_store import JobStore
from api.task_store import TaskStore

logger = structlog.get_logger()

# Environment variables
import os
from pathlib import Path

# Load .env before reading any env vars — must happen at module import time
# so that WORKSPACE_PATH and PROJECT_DIR are available for module-level constants.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(override=False)
except ImportError:
    pass

WORKSPACE_PATH = os.getenv("WORKSPACE_PATH", os.path.abspath("./workspace"))

# Optional active-project subdirectory within the workspace root.
# When set, the agent always operates inside WORKSPACE_PATH/PROJECT_DIR
# instead of the bare workspace root.
PROJECT_DIR = os.getenv("PROJECT_DIR", "").strip()

# Security: Disallowed paths (critical system folders)
DISALLOWED_PATHS = [
    "C:\\Windows",
    "C:\\Program Files",
    "C:\\Program Files (x86)",
    "C:\\ProgramData",
    "C:\\System32",
    "C:\\SysWOW64",
    "C:\\Users\\Public",
    "/Windows",
    "/System",
    "/Library",
    "/System32",
    "/usr/bin",
    "/usr/local/bin",
    "/bin",
    "/sbin",
]

def _is_path_allowed(path: str) -> bool:
    """Check if a pre-resolved absolute path string is not a critical system folder.

    *path* must already be an absolute, resolved path string (no further Path.resolve())
    so that this function is never a CodeQL path-injection sink.
    """
    for disallowed in DISALLOWED_PATHS:
        if path.lower().startswith(disallowed.lower()):
            return False
    return True

# Ensure workspace exists (base path always; effective path when PROJECT_DIR is set)
Path(WORKSPACE_PATH).mkdir(parents=True, exist_ok=True)


app = FastAPI(
    title="Local Coding Agent API",
    description="REST API for interacting with the local coding agent",
    version="0.1.0",
)

# Per-route request count, latency histogram, and in-flight gauge.
# Metrics are exposed via the existing /metrics endpoint (generate_latest()).
try:
    from prometheus_fastapi_instrumentator import Instrumentator
    Instrumentator(
        should_group_status_codes=True,
        should_ignore_untemplated=True,
        excluded_handlers=["/metrics", "/health"],
    ).instrument(app)
except ImportError:
    pass  # Optional dependency — degrades gracefully if not installed

# CORS: default to localhost only; override via CORS_ORIGINS env var
# (comma-separated list of allowed origins).
# Never use allow_origins=["*"] with allow_credentials=True — that is
# an invalid combination that enables CSRF on permissive clients.
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

# Optional API key guard for mutating endpoints.
# Set AGENT_API_KEY in .env to enable.  When the env var is absent every
# request is allowed (local-dev default).  When set, callers must supply the
# key in the X-API-Key header.
_AGENT_API_KEY: str = os.getenv("AGENT_API_KEY", "")


async def _require_api_key(x_api_key: str = Header(default="")) -> None:
    if _AGENT_API_KEY and x_api_key != _AGENT_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


class TaskRequest(BaseModel):
    task: str
    session_id: Optional[str] = None
    include_history: bool = True
    force_task_type: Optional[str] = None  # bypass classifier when set


class TaskResponse(BaseModel):
    success: bool
    session_id: str
    response: Optional[str] = None
    error: Optional[str] = None


class SessionInfo(BaseModel):
    session_id: str
    message_count: int
    status: str
    created_at: str


_model_router: Optional[ModelRouter] = None
_orchestrator: Optional[AgentOrchestrator] = None

# Model-switch events emitted outside of a running job (e.g. task classifier).
# Drained by GET /events/model-switches so the Discord bot can notify the user.
_pending_switch_events: list[dict] = []

# Effective workspace: WORKSPACE_PATH/PROJECT_DIR when PROJECT_DIR is set,
# otherwise bare WORKSPACE_PATH.  GitTool reads WORKSPACE_PATH from os.environ,
# so we update it here to match the effective path.
def _effective_workspace(base: str = WORKSPACE_PATH, project: str = PROJECT_DIR) -> str:
    if project:
        return str(Path(base) / project)
    return base

_current_workspace: str = _effective_workspace()
# Ensure the effective workspace directory exists.
Path(_current_workspace).mkdir(parents=True, exist_ok=True)
# Publish the effective workspace in a dedicated env var that GitTool reads.
# We deliberately do NOT overwrite WORKSPACE_PATH — that variable must stay as
# the base path from .env so that module reloads don't double-append PROJECT_DIR.
os.environ["AGENT_EFFECTIVE_WORKSPACE"] = _current_workspace

# SQLite-backed job store (write-through, in-memory hot cache)
_job_store: JobStore = JobStore("data/jobs.db")

# Task store — shares the same SQLite file, different table
_task_store: TaskStore = TaskStore("data/jobs.db")


def _summarize_response(text: str, max_chars: int = 500) -> str:
    """Return a short prose summary with shell output snippet preserved.

    Fenced code blocks are stripped from the prose portion, but any
    **Shell Output:** section is extracted first and appended in truncated
    form so Discord users can see what ran without needing !result.

    Uses plain string operations (no regex on user input) to avoid ReDoS.
    """
    safe_text = text[:20_000]

    # --- Extract shell output block using plain string search (no regex) ---
    shell_snippet = ""
    shell_marker = "**Shell Output:**"
    marker_pos = safe_text.find(shell_marker)
    if marker_pos != -1:
        fence_pos = safe_text.find("```", marker_pos)
        if fence_pos != -1:
            nl_pos = safe_text.find("\n", fence_pos)
            if nl_pos != -1:
                close_pos = safe_text.find("```", nl_pos + 1)
                if close_pos != -1:
                    output = safe_text[nl_pos + 1:close_pos].strip()
                    lines = output.splitlines()
                    truncated = "\n".join(lines[:15])
                    if len(lines) > 15:
                        truncated += f"\n… ({len(lines)} lines total)"
                    shell_snippet = f"\n\n**Shell output:**\n```\n{truncated}\n```"

    # --- Strip code blocks using split (no regex on user input) ---
    # Splitting on ``` gives alternating outside/inside-fence segments.
    # Even-indexed parts are outside fences; odd-indexed are inside (discard).
    parts = safe_text.split("```")
    prose = "".join(parts[i] for i in range(0, len(parts), 2)).strip()
    # Collapse excessive blank lines
    while "\n\n\n" in prose:
        prose = prose.replace("\n\n\n", "\n\n")
    if len(prose) > max_chars:
        prose = prose[:max_chars].rstrip() + "…"
    return (prose or "(task completed)") + shell_snippet


# Backoff schedule shared with the bot (seconds → holds at 5 min indefinitely).
_STARTUP_BACKOFF = [2, 5, 15, 30, 60, 120, 300]


def _startup_backoff(attempt: int) -> float:
    return float(_STARTUP_BACKOFF[min(attempt, len(_STARTUP_BACKOFF) - 1)])


async def _init_agent_background() -> None:
    """Initialise the orchestrator in the background so uvicorn can serve
    requests (especially GET /health) immediately.

    /task endpoints return 503 until the orchestrator is ready.  Retries
    indefinitely with the same backoff curve used before this refactor.

    After the orchestrator is ready, probes the primary local model so that
    LM Studio loads it into VRAM now rather than on the first user request.
    A warning is logged (visible in the supervisor console) if the model is
    not loaded, giving the user time to open LM Studio and load it.
    """
    global _orchestrator
    from local_coding_agent import create_agent

    attempt = 0
    while _orchestrator is None:
        try:
            _orchestrator = create_agent(_current_workspace, "config/models.yaml")
            logger.info("agent_initialized", attempt=attempt)
        except Exception as e:
            delay = _startup_backoff(attempt)
            logger.warning(
                "agent_init_failed",
                error=str(e),
                attempt=attempt,
                retry_in=delay,
            )
            attempt += 1
            await asyncio.sleep(delay)

    # Register a module-level switch callback so events that fire outside of a
    # running task (e.g. the task-type classifier) still surface via the API.
    def _api_switch_callback(event) -> None:
        _pending_switch_events.append({
            "from_model": event.from_model,
            "to_model": event.to_model,
            "reason": event.reason,
            "timestamp": event.timestamp.isoformat(),
        })
    _orchestrator.model_router.register_switch_callback(_api_switch_callback)

    # Probe the primary local model.  This is intentionally fire-and-forget:
    # a failed probe does not block startup — the ModelNotReadyError retry
    # loop in model_router handles the case where the model loads later.
    primary = _orchestrator.model_router.get_model("coding")
    if primary and primary.type == "local":
        logger.info("model_probe_start", model=primary.name)
        ok = await _orchestrator.model_router.ollama.warmup(primary.name)
        if not ok:
            logger.warning(
                "model_not_ready_at_startup",
                model=primary.name,
                action="Open LM Studio and load the model — tasks will block until it is ready.",
            )


@app.on_event("startup")
async def startup_event():
    logger.info(
        "starting_api",
        component="api",
        workspace=_current_workspace,
        project_dir=PROJECT_DIR or "(none)",
    )

    # Restore persisted jobs; mark stale running jobs as failed.
    _job_store.load()

    # Kick off agent init as a background task so uvicorn starts accepting
    # requests immediately.  GET /health returns 200 straight away; POST /task
    # returns 503 until _orchestrator is set by the background task.
    asyncio.create_task(_init_agent_background())


@app.get("/")
async def root():
    return {
        "name": "Local Coding Agent API",
        "version": "0.1.0",
        "status": "running",
    }


@app.get("/health")
async def health_check():
    active_jobs = 0
    try:
        rows = _job_store.list_jobs(limit=20)
        active_jobs = sum(1 for j in rows if j.get("status") == "running")
    except Exception:
        pass
    return {
        "status": "healthy",
        "agent_ready": _orchestrator is not None,
        "active_jobs": active_jobs,
        "uptime_seconds": int(_time.time() - _SERVER_START_TIME),
        "timestamp": _time.time(),
    }


@app.post("/task", response_model=TaskResponse, dependencies=[Depends(_require_api_key)])
async def run_task(request: TaskRequest):
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    
    try:
        result = await _orchestrator.run_task(
            task=request.task,
            session_id=request.session_id,
            include_history=request.include_history,
        )
        
        return TaskResponse(
            success=result.get("success", False),
            session_id=result.get("session_id", ""),
            response=result.get("result", {}).get("response"),
            error=result.get("error"),
        )
        
    except Exception as e:
        import traceback
        logger.error("task_failed", error=str(e), traceback=traceback.format_exc())
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/task/start", dependencies=[Depends(_require_api_key)])
async def start_task_background(request: TaskRequest):
    """Submit a task and return a job_id immediately. Poll GET /task/{job_id} for status."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    job_id = f"job_{uuid.uuid4().hex[:12]}"
    session_id = request.session_id or f"session_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    # Use force_task_type when provided; otherwise keyword-classify for instant
    # response (0 ms) — the LLM classifier runs later inside run_task() in
    # parallel with context building.
    if request.force_task_type:
        task_type = request.force_task_type
    else:
        task_type = _orchestrator._detect_task_type_keyword(request.task)
    _phase_labels = {
        "develop": "developing",
        "review": "reviewing",
        "test": "testing",
        "architect": "designing",
        "research": "researching",
        "chat": "thinking",
    }
    _phase_label = _phase_labels.get(task_type, "working")

    _job_store.create(
        job_id=job_id,
        session_id=session_id,
        task=request.task,
        task_type=task_type,
        phase=_phase_label,
    )

    def _on_phase(label: str) -> None:
        """Callback fired by run_task() at each milestone to update job phase."""
        _job_store.update(job_id, phase=label)

    async def _run():
        _job_store.update(job_id, status="running")
        try:
            result = await _orchestrator.run_task(
                task=request.task,
                session_id=session_id,
                include_history=request.include_history,
                on_phase=_on_phase,
                job_id=job_id,
                force_task_type=request.force_task_type,
            )
            if result.get("success"):
                inner = result.get("result", {})
                full_response = inner.get("response", "")
                update_kwargs: dict = dict(
                    status="done",
                    phase="complete",
                    task_type=inner.get("task_type", task_type),
                    files_created=inner.get("files_created", []),
                    summary=inner.get("job_summary") or _summarize_response(full_response),
                    _full_response=full_response,
                    screenshot_path=inner.get("screenshot_path"),
                )
                # Handover metadata — stored in the in-memory job cache so the
                # Discord bot can read it via GET /task/{job_id}.
                if result.get("handover_triggered"):
                    update_kwargs["handover_triggered"] = True
                    update_kwargs["new_session_id"] = result.get("session_id")
                    update_kwargs["context_budget"] = result.get("context_budget", "bridge")
                elif result.get("context_budget") == "warn":
                    update_kwargs["context_budget"] = "warn"
                _job_store.update(job_id, **update_kwargs)
            else:
                _job_store.update(
                    job_id,
                    status="failed",
                    error=result.get("error", "Unknown error"),
                )
        except Exception as e:
            import traceback
            logger.error("background_job_failed", job_id=job_id, error=str(e),
                         traceback=traceback.format_exc())
            _job_store.update(job_id, status="failed", error=str(e))

    asyncio.create_task(_run())
    return {"job_id": job_id, "session_id": session_id, "task_type": task_type}


@app.get("/chains")
async def list_chains():
    """List all available agent chains from agent-chain.yaml."""
    chains = _orchestrator.chain_runner.list_chains(_orchestrator.workspace_path)
    return {"chains": chains}


@app.get("/jobs")
async def list_jobs(limit: int = 50, offset: int = 0):
    """List all jobs ordered by creation time descending (no full_response)."""
    return {"jobs": _job_store.list_jobs(limit=limit, offset=offset)}


@app.get("/task/{job_id}")
async def get_job_status(job_id: str):
    """Poll a background job for its current status and summary."""
    job = _job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    # Never expose the full response here — use /task/{job_id}/result for that
    return {k: v for k, v in job.items() if k != "_full_response"}


@app.get("/task/{job_id}/result")
async def get_job_result(job_id: str):
    """Retrieve the full agent response for a completed job."""
    job = _job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if job["status"] != "done":
        return {"status": job["status"], "result": None}
    return {"status": "done", "result": job.get("_full_response", "")}


@app.get("/task/{job_id}/tasks")
async def get_job_tasks(job_id: str):
    """Return the task list for a job (created by the planner)."""
    job = _job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    tasks = _task_store.list_tasks(job_id)
    counts = _task_store.task_counts(job_id)
    return {
        "job_id": job_id,
        "tasks": [t.to_dict() for t in tasks],
        "counts": counts,
        "total": len(tasks),
        "all_done": _task_store.all_done(job_id),
    }


@app.delete("/task/{job_id}")
async def cancel_job(job_id: str):
    """Request cancellation of a pending or running job."""
    job = _job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if job["status"] in ("pending", "running"):
        _job_store.update(job_id, status="cancelled", phase="cancelled")
        job = _job_store.get(job_id)
    return {"cancelled": True, "job_id": job_id, "status": job["status"]}


@app.get("/workspace/file")
async def read_workspace_file(path: str):
    """Read a file from the workspace by relative path."""
    # Anchor to the configured WORKSPACE_PATH root (trusted env var), not the
    # mutable _current_workspace module variable, so CodeQL sees no taint flow.
    _ws_root = Path(os.getenv("WORKSPACE_PATH", "./workspace")).resolve()
    # Inline containment check — the pattern CodeQL recognises as safe.
    try:
        target = (_ws_root / path).resolve()
    except Exception:
        logger.warning("workspace_file_path_error", path=path)
        raise HTTPException(status_code=400, detail="Invalid path")
    if not target.is_relative_to(_ws_root):
        raise HTTPException(status_code=403, detail="Path is outside workspace")

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {path}")
    if not target.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {path}")

    try:
        content = target.read_text(encoding="utf-8", errors="replace")
        return {
            "path": path,
            "content": content,
            "lines": len(content.splitlines()),
            "size": len(content),
        }
    except Exception as e:
        logger.error("workspace_file_read_error", path=path, error=str(e))
        raise HTTPException(status_code=500, detail="Could not read file")


@app.post("/task/stream", dependencies=[Depends(_require_api_key)])
async def run_task_stream(request: TaskRequest):
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    
    from fastapi.responses import StreamingResponse
    import json
    
    async def event_generator():
        try:
            async for chunk in _orchestrator.run_stream(
                task=request.task,
                session_id=request.session_id,
                include_history=request.include_history,
            ):
                yield f"data: {json.dumps(chunk)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"
    
    return StreamingResponse(event_generator(), media_type="text/event-stream")


@app.get("/sessions")
async def list_sessions(limit: int = 20):
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    
    sessions = _orchestrator.list_sessions(limit=limit)
    return {"sessions": sessions}


@app.get("/sessions/{session_id}")
async def get_session(session_id: str):
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    
    history = _orchestrator.get_session_history(session_id)
    return {"session_id": session_id, "history": history}


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    deleted = _orchestrator.session_memory.delete_session(session_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Session not found")
    return {"success": True, "session_id": session_id}


@app.post("/wake/{session_id}")
async def wake_session(session_id: str):
    """Resume an interrupted session (Anthropic Managed Agents wake pattern)."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    result = await _orchestrator.wake(session_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error", "Session not found"))
    return result


@app.get("/events/model-switches")
async def get_model_switch_events():
    """Return and clear pending model-switch events.

    The Discord bot polls this endpoint to notify users when the router
    has fallen back from a local model to a remote one.
    """
    events = list(_pending_switch_events)
    _pending_switch_events.clear()
    return {"events": events}


@app.get("/models")
async def list_models():
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    router = _orchestrator.model_router
    active = router.get_active_model_name()

    # Fetch live LM Studio model list once; fall back gracefully to empty.
    lm_all: list[dict] = await router.ollama.list_all_models()
    lm_state_by_id: dict[str, str] = {
        m.get("id", ""): m.get("state", "unknown")
        for m in lm_all
    }

    # Build configured-model entries with live state for local models.
    configured_names: set[str] = set()
    configured_entries = []
    for c in router.configs:
        configured_names.add(c.name)
        entry: dict = {
            "name": c.name,
            "type": c.type,
            "endpoint": c.endpoint,
            "coding_optimized": c.is_coding_optimized,
            "context_window": c.context_window,
            "is_active": c.name == active,
        }
        if c.type == "local":
            entry["state"] = lm_state_by_id.get(c.name, "unknown")
        configured_entries.append(entry)

    # LM Studio models that are downloaded but not yet in models.yaml.
    lm_available = [
        {"id": m.get("id", ""), "state": m.get("state", "unknown")}
        for m in lm_all
        if m.get("id", "") not in configured_names
    ]

    return {
        "active_model": active,
        "models": configured_entries,
        "lm_studio_available": lm_available,
    }


@app.get("/models/active")
async def get_active_model():
    """Return the currently active model."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    router = _orchestrator.model_router
    name = router.get_active_model_name()
    config = router.get_model("coding")
    if not config:
        raise HTTPException(status_code=404, detail="No models configured")

    return {
        "active_model": name,
        "effective_model": config.name,
        "type": config.type,
        "endpoint": config.endpoint,
        "context_window": config.context_window,
    }


@app.post("/models/active")
async def set_active_model(body: dict):
    """Switch the active model by name. Pass {\"model\": \"<name>\"}.
    Pass {\"model\": null} to revert to the default from models.yaml."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    router = _orchestrator.model_router
    name = body.get("model")

    if name is None:
        router.clear_active_model()
        effective = router.get_active_model_name()
        return {"active_model": effective, "message": "Reverted to default"}

    try:
        config = router.set_active_model(name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {
        "active_model": config.name,
        "type": config.type,
        "endpoint": config.endpoint,
        "message": f"Switched to {config.name}",
    }


@app.get("/workspace")
async def get_workspace():
    """Get current workspace path"""
    return {
        "workspace": _current_workspace,
        "exists": Path(_current_workspace).exists()
    }


@app.get("/workspace/project")
async def get_project():
    """Return the active project name and workspace root."""
    base = Path(os.getenv("WORKSPACE_PATH", "./workspace")).resolve()
    current = Path(_current_workspace)  # already resolved string — no .resolve() needed
    try:
        # project name is the relative part, if any
        project = str(current.relative_to(base)) if current != base else None
    except ValueError:
        project = None
    return {
        "project": project,
        "workspace": str(current),
        "workspace_root": str(base),
    }


@app.get("/workspace/directories")
async def list_workspace_directories():
    """List available directories in workspace"""
    # _current_workspace is a pre-resolved path string — use it directly without .resolve()
    # so that CodeQL does not see a taint-sink call on the stored module variable.
    workspace = Path(_current_workspace)  # already stored as a resolved path
    if not workspace.exists():
        return {"error": "Workspace does not exist"}

    try:
        items = []
        for item in workspace.iterdir():
            items.append({
                "name": item.name,
                "type": "directory" if item.is_dir() else "file",
                "path": str(item),
            })
        return {"workspace": str(workspace), "items": items}
    except Exception as e:
        logger.error("workspace_list_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Could not list workspace")


@app.post("/workspace/project", dependencies=[Depends(_require_api_key)])
async def set_project(request: dict):
    """Switch the active project subdirectory within the workspace root.

    Body: {"name": "<project-name>"}   — switch to WORKSPACE_PATH/<name>
          {"name": ""}  or {"name": null} — clear back to workspace root
    The project directory is created if it does not yet exist.
    """
    global _current_workspace, _orchestrator

    raw_name = (request.get("name") or "").strip()

    workspace_root = Path(WORKSPACE_PATH).resolve()

    if raw_name:
        # Allow only safe project path characters and reject dangerous segments.
        if not re.fullmatch(r"[A-Za-z0-9._\-/]+", raw_name):
            raise HTTPException(status_code=400, detail="Invalid project name")
        parts = Path(raw_name).parts
        if any(part in ("", ".", "..") for part in parts):
            raise HTTPException(status_code=400, detail="Invalid project name")
        # Inline containment check — the pattern CodeQL recognises as safe for py/path-injection.
        resolved_target = (workspace_root / raw_name).resolve()
        if not resolved_target.is_relative_to(workspace_root):
            raise HTTPException(status_code=403, detail="Path not allowed")
    else:
        resolved_target = workspace_root

    if not _is_path_allowed(str(resolved_target)):
        raise HTTPException(status_code=403, detail="Path not allowed")

    resolved_target.mkdir(parents=True, exist_ok=True)
    _current_workspace = str(resolved_target)
    os.environ["AGENT_EFFECTIVE_WORKSPACE"] = _current_workspace

    from local_coding_agent import create_agent
    _orchestrator = create_agent(_current_workspace, "config/models.yaml")

    logger.info(
        "project_switched",
        project=raw_name or "(root)",
        workspace=_current_workspace,
    )
    return {
        "success": True,
        "project": raw_name or None,
        "workspace": _current_workspace,
    }


@app.post("/workspace")
async def set_workspace(request: dict):
    """Set new workspace path"""
    global _current_workspace, _orchestrator
    
    new_path = request.get("path")
    if not new_path:
        raise HTTPException(status_code=400, detail="path is required")

    workspace_root = Path(WORKSPACE_PATH).resolve()

    # Inline containment check — the pattern CodeQL recognises as safe for py/path-injection.
    path = (workspace_root / new_path).resolve()
    if not path.is_relative_to(workspace_root):
        raise HTTPException(status_code=403, detail="Cannot set workspace outside configured root")

    if not _is_path_allowed(str(path)):
        raise HTTPException(status_code=403, detail="Cannot set workspace to system folder")

    # Validate path exists and is a directory
    if not path.exists():
        raise HTTPException(status_code=404, detail="Path does not exist")
    if not path.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")

    _current_workspace = str(path)
    # Keep GitTool in sync with the new workspace.
    os.environ["AGENT_EFFECTIVE_WORKSPACE"] = _current_workspace

    # Recreate orchestrator with new workspace
    from local_coding_agent import create_agent
    _orchestrator = create_agent(_current_workspace, "config/models.yaml")
    
    return {
        "success": True,
        "workspace": _current_workspace
    }


@app.post("/screenshot")
async def take_screenshot(request: dict):
    """Take a screenshot of a running dev server"""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    
    url = request.get("url", "http://localhost:8080")
    workspace_root = Path(_current_workspace).resolve()

    # User may supply a sub-directory of the workspace; default to workspace root.
    raw_workspace = request.get("workspace")
    if raw_workspace:
        # Inline containment check — the pattern CodeQL recognises as safe for py/path-injection.
        candidate = (workspace_root / raw_workspace).resolve()
        # Security: must be inside the current workspace and not a system path
        if not candidate.is_relative_to(workspace_root):
            raise HTTPException(status_code=403, detail="Workspace path is outside the allowed workspace root")
        if not _is_path_allowed(str(candidate)):
            raise HTTPException(status_code=403, detail="Workspace path is not allowed")
        workspace = str(candidate)
    else:
        # Auto-detect project sub-directory only within workspace_root
        workspace = str(workspace_root)
        game_path = workspace_root / "space-adventure"
        if game_path.exists() and (game_path / "package.json").exists():
            workspace = str(game_path)

    from agent.tools.browser_tool import BrowserTool
    browser = BrowserTool(workspace)

    try:
        import asyncio
        result = asyncio.run(browser.run_and_screenshot())
        return result
    except Exception as e:
        logger.error("screenshot_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Screenshot capture failed")


@app.get("/mcp/tools")
async def list_mcp_tools():
    """List available MCP tools"""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    
    from mcp.server import create_mcp_server
    mcp_server = create_mcp_server(_current_workspace)
    
    return {"tools": mcp_server.list_tools()}


@app.post("/mcp/tools/{tool_name}")
async def call_mcp_tool(tool_name: str, arguments: dict = None):
    """Call an MCP tool by name"""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    
    from mcp.server import create_mcp_server
    mcp_server = create_mcp_server(_current_workspace)
    
    try:
        result = await mcp_server.call_tool(tool_name, arguments or {})
        return {"success": True, "result": result}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("mcp_tool_failed", tool=tool_name, error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


@app.post("/subagent/spawn")
async def spawn_subagent(request: dict):
    """Spawn a subagent with isolated context."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    
    task = request.get("task")
    role = request.get("role", "developer")
    parent_session_id = request.get("parent_session_id")
    context_limits = request.get("context_limits")
    
    if not task:
        raise HTTPException(status_code=400, detail="task is required")
    
    try:
        result = await _orchestrator.spawn_subagent(
            task=task,
            role=role,
            parent_session_id=parent_session_id,
            context_limits=context_limits,
        )
        # Sanitize: internal exception strings must not flow into the HTTP response.
        if "error" in result and not result.get("success"):
            logger.error("subagent_internal_error", error=result.get("error"), subagent_id=result.get("subagent_id"))
            result = {k: v for k, v in result.items() if k != "error"}
            result["error"] = "Subagent execution failed"
        return result
    except Exception as e:
        logger.error("subagent_spawn_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Subagent spawn failed")


@app.post("/subagent/spawn-batch")
async def spawn_subagent_batch(request: dict):
    """Spawn multiple subagents in parallel."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    
    tasks = request.get("tasks", [])
    roles = request.get("roles")
    parent_session_id = request.get("parent_session_id")
    
    if not tasks:
        raise HTTPException(status_code=400, detail="tasks is required")
    
    try:
        results = await _orchestrator.spawn_multiple_subagents(
            tasks=tasks,
            roles=roles,
            parent_session_id=parent_session_id,
        )
        return {"results": results}
    except Exception as e:
        logger.error("subagent_batch_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/subagent")
async def list_subagents():
    """List all subagent sessions."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    
    return {"subagents": _orchestrator.list_subagents()}


@app.get("/subagent/{subagent_id}")
async def get_subagent(subagent_id: str):
    """Get result from a specific subagent."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    
    result = _orchestrator.get_subagent_result(subagent_id)
    if "error" in result and result["error"] == "Subagent not found":
        raise HTTPException(status_code=404, detail="Subagent not found")
    
    return result


@app.post("/index")
async def index_workspace(request: dict = None):
    """Index all files in the workspace for RAG search."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    
    project_id = request.get("project_id") if request else None
    
    try:
        result = _orchestrator.index_workspace(project_id)
        return {"success": True, "result": result}
    except Exception as e:
        logger.error("index_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/search")
async def search_codebase(q: str, limit: int = 5):
    """Search the codebase using vector similarity."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    
    try:
        project_id = Path(_current_workspace).name
        results = _orchestrator.codebase_memory.search_files(q, n_results=limit)
        return {"query": q, "results": results}
    except Exception as e:
        logger.error("search_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/memory/stats")
async def get_memory_stats():
    """Get statistics about the vector store and MemoryWiki graph (with lint results)."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    try:
        rag_stats = _orchestrator.codebase_memory.get_stats()
        wiki_stats = _orchestrator.memory_wiki.get_statistics()
        lint_results = _orchestrator.memory_wiki.lint()
        return {
            "rag": rag_stats,
            "wiki": wiki_stats,
            "lint": lint_results,
        }
    except Exception as e:
        logger.error("stats_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


@app.get("/ready")
async def readiness_check():
    """Readiness check — verifies model availability before accepting traffic."""
    if not _orchestrator:
        return {"ready": False, "reason": "agent_not_initialized"}

    config = _orchestrator.model_router.get_model("coding")
    if not config:
        return {"ready": False, "reason": "no_model_configured"}

    try:
        model_ok = await _orchestrator.model_router.health_check(config)
        healthy_models = _orchestrator.model_router.get_healthy_models()
        return {
            "ready": model_ok,
            "primary_model": config.name,
            "model_type": config.type,
            "healthy_models": healthy_models,
        }
    except Exception as e:
        logger.error("readiness_check_failed", error=str(e))
        return {"ready": False, "reason": "Readiness check failed"}


@app.get("/stats")
async def get_stats():
    """Agent statistics including cost tracking and session counts."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    cost_summary = _orchestrator.model_router.get_cost_summary()
    sessions = _orchestrator.list_sessions(limit=100)

    return {
        "sessions": {
            "total": len(sessions),
            "recent": sessions[:5],
        },
        "cost": cost_summary,
        "healthy_models": _orchestrator.model_router.get_healthy_models(),
    }


@app.get("/llm/health")
async def get_llm_health():
    """Detailed LLM health status including circuit breaker states."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    from llm.model_resilience import create_resilience_manager

    router = _orchestrator.model_router
    config = router.get_model("coding")

    resilience = create_resilience_manager(
        ollama_endpoint=config.endpoint if config and config.type == "local" else "http://127.0.0.1:11434"
    )

    diagnostics = await resilience.get_diagnostics()
    cost_summary = router.get_cost_summary()
    rate_status = {}
    if config:
        rate_status = router.rate_limiter.get_status(config.name)

    return {
        "resilience": diagnostics,
        "rate_limiter": rate_status,
        "cost": cost_summary,
    }


@app.post("/restart", status_code=202)
async def request_restart(req: Request):
    """Signal the supervisor to restart both services.

    Writes .state/restart.flag at the project root; the supervisor polls for
    it and performs an ordered shutdown → restart of the API and bot.

    Only accepted from localhost — remote callers receive 403.
    """
    client_host = req.client.host if req.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(
            status_code=403,
            detail="Restart is only allowed from localhost",
        )

    state_dir = Path(__file__).parent.parent / ".state"
    state_dir.mkdir(parents=True, exist_ok=True)

    flag = state_dir / "restart.flag"
    flag.touch()

    # Check if the supervisor is alive by reading its heartbeat file.
    # The supervisor writes a Unix timestamp every 5 seconds; if the file is
    # missing or older than 30 seconds the supervisor is likely not running.
    import time as _time
    heartbeat_file = state_dir / "supervisor.heartbeat"
    supervisor_running = False
    try:
        age = _time.time() - float(heartbeat_file.read_text().strip())
        supervisor_running = age < 30
    except Exception:
        pass

    logger.info(
        "restart_requested",
        client=client_host,
        supervisor_running=supervisor_running,
    )
    return {
        "status": "restarting" if supervisor_running else "flag_written",
        "supervisor_running": supervisor_running,
        "message": (
            "Restart flag written. Supervisor will restart services shortly."
            if supervisor_running
            else
            "Restart flag written, but supervisor.py does not appear to be running. "
            "Start it with: python supervisor.py"
        ),
    }


@app.get("/metrics", include_in_schema=False)
async def metrics():
    """Prometheus scrape endpoint (includes FastAPI instrumentation metrics)."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/environment")
async def get_environment():
    """Return detected paths for all external tools (git, Playwright, Node, etc.)."""
    from agent.tools.environment_probe import get_environment_probe
    probe = get_environment_probe()
    return {"platform": probe._platform, "tools": probe.get_all()}


@app.post("/environment/reprobe")
async def reprobe_environment():
    """Force a fresh tool detection (ignores cached data/environment.json)."""
    from agent.tools.environment_probe import get_environment_probe
    probe = get_environment_probe()
    await asyncio.to_thread(probe.reprobe)
    return {"tools": probe.get_all()}


@app.get("/skills")
async def list_skills():
    """List all locally loaded skills."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    skills = _orchestrator.skill_manager.list_skills()
    return {"skills": skills, "count": len(skills)}


@app.post("/skills/fetch")
async def fetch_remote_skills():
    """Download skills from the configured remote registry into skills/."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    try:
        result = await asyncio.to_thread(_orchestrator.skill_manager.fetch_remote)
        return result
    except Exception as e:
        logger.error("skills_fetch_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


if __name__ == "__main__":
    import uvicorn
    import os

    port = int(os.getenv("PORT", "5005"))
    uvicorn.run(app, host="0.0.0.0", port=port)