from fastapi import FastAPI, HTTPException, BackgroundTasks, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List, Dict
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
import uuid
import re
from datetime import datetime
import asyncio
import structlog

from llm import ModelRouter
from agent.orchestrator import AgentOrchestrator

logger = structlog.get_logger()

# Environment variables
import os
from pathlib import Path

WORKSPACE_PATH = os.getenv("WORKSPACE_PATH", os.path.abspath("./workspace"))

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
    """Check if path is not a critical system folder"""
    abs_path = str(Path(path).resolve())
    
    for disallowed in DISALLOWED_PATHS:
        if abs_path.lower().startswith(disallowed.lower()):
            return False
    return True

# Ensure workspace exists
Path(WORKSPACE_PATH).mkdir(parents=True, exist_ok=True)


app = FastAPI(
    title="Local Coding Agent API",
    description="REST API for interacting with the local coding agent",
    version="0.1.0",
)

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
    allow_headers=["Content-Type", "Authorization"],
)


class TaskRequest(BaseModel):
    task: str
    session_id: Optional[str] = None
    include_history: bool = True


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
_current_workspace: str = WORKSPACE_PATH

# In-memory job store for background tasks (keyed by job_id)
_jobs: Dict[str, dict] = {}


def _summarize_response(text: str, max_chars: int = 500) -> str:
    """Strip fenced code blocks and return a short prose summary."""
    prose = re.sub(r'```[\s\S]*?```', '', text).strip()
    prose = re.sub(r'\n{3,}', '\n\n', prose)
    if len(prose) > max_chars:
        return prose[:max_chars].rstrip() + "…"
    return prose or "(task completed)"


@app.on_event("startup")
async def startup_event():
    global _model_router, _orchestrator, _current_workspace
    from local_coding_agent import create_agent
    
    logger.info("starting_api", component="api", workspace=WORKSPACE_PATH)
    
    try:
        _orchestrator = create_agent(WORKSPACE_PATH, "config/models.yaml")
        logger.info("agent_initialized")
    except Exception as e:
        logger.error("agent_init_failed", error=str(e))


@app.get("/")
async def root():
    return {
        "name": "Local Coding Agent API",
        "version": "0.1.0",
        "status": "running",
    }


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "agent_ready": _orchestrator is not None,
    }


@app.post("/task", response_model=TaskResponse)
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
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/task/start")
async def start_task_background(request: TaskRequest):
    """Submit a task and return a job_id immediately. Poll GET /task/{job_id} for status."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    job_id = f"job_{uuid.uuid4().hex[:12]}"
    session_id = request.session_id or f"session_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

    # Detect task type early so the Discord bot can show a meaningful phase label
    task_type = _orchestrator._detect_task_type(request.task)
    _phase_label = {
        "develop": "developing",
        "review": "reviewing",
        "test": "testing",
        "architect": "designing",
        "research": "researching",
        "chat": "thinking",
    }.get(task_type, "working")

    _jobs[job_id] = {
        "job_id": job_id,
        "status": "pending",
        "phase": _phase_label,
        "task_type": task_type,
        "summary": None,
        "files_created": [],
        "error": None,
        "session_id": session_id,
        "task": request.task,
        "_full_response": None,
    }

    async def _run():
        _jobs[job_id]["status"] = "running"
        try:
            result = await _orchestrator.run_task(
                task=request.task,
                session_id=session_id,
                include_history=request.include_history,
            )
            if result.get("success"):
                inner = result.get("result", {})
                full_response = inner.get("response", "")
                _jobs[job_id].update({
                    "status": "done",
                    "phase": "complete",
                    "task_type": inner.get("task_type", task_type),
                    "files_created": inner.get("files_created", []),
                    "summary": _summarize_response(full_response),
                    "_full_response": full_response,
                })
            else:
                _jobs[job_id].update({
                    "status": "failed",
                    "error": result.get("error", "Unknown error"),
                })
        except Exception as e:
            import traceback
            logger.error("background_job_failed", job_id=job_id, error=str(e),
                         traceback=traceback.format_exc())
            _jobs[job_id].update({"status": "failed", "error": str(e)})

    asyncio.create_task(_run())
    return {"job_id": job_id, "session_id": session_id, "task_type": task_type}


@app.get("/task/{job_id}")
async def get_job_status(job_id: str):
    """Poll a background job for its current status and summary."""
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    job = _jobs[job_id]
    # Never expose the full response here — use /task/{job_id}/result for that
    return {k: v for k, v in job.items() if k != "_full_response"}


@app.get("/task/{job_id}/result")
async def get_job_result(job_id: str):
    """Retrieve the full agent response for a completed job."""
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    job = _jobs[job_id]
    if job["status"] != "done":
        return {"status": job["status"], "result": None}
    return {"status": "done", "result": job.get("_full_response", "")}


@app.delete("/task/{job_id}")
async def cancel_job(job_id: str):
    """Request cancellation of a pending or running job."""
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    job = _jobs[job_id]
    if job["status"] in ("pending", "running"):
        job["status"] = "cancelled"
        job["phase"] = "cancelled"
    return {"cancelled": True, "job_id": job_id, "status": job["status"]}


@app.get("/workspace/file")
async def read_workspace_file(path: str):
    """Read a file from the workspace by relative path."""
    workspace = Path(_current_workspace).resolve()
    try:
        target = (workspace / path).resolve()
        # Security: prevent path traversal
        target.relative_to(workspace)
    except ValueError:
        raise HTTPException(status_code=403, detail="Path is outside workspace")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

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
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/task/stream")
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


@app.get("/models")
async def list_models():
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    return {
        "models": [
            {"name": c.name, "type": c.type, "coding_optimized": c.is_coding_optimized}
            for c in _orchestrator.model_router.configs
        ]
    }


@app.get("/workspace")
async def get_workspace():
    """Get current workspace path"""
    return {
        "workspace": _current_workspace,
        "exists": Path(_current_workspace).exists()
    }


@app.get("/workspace/directories")
async def list_workspace_directories():
    """List available directories in workspace"""
    if not Path(_current_workspace).exists():
        return {"error": "Workspace does not exist"}
    
    try:
        items = []
        for item in Path(_current_workspace).iterdir():
            items.append({
                "name": item.name,
                "type": "directory" if item.is_dir() else "file",
                "path": str(item)
            })
        return {"workspace": _current_workspace, "items": items}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/workspace")
async def set_workspace(request: dict):
    """Set new workspace path"""
    global _current_workspace, _orchestrator
    
    new_path = request.get("path")
    if not new_path:
        raise HTTPException(status_code=400, detail="path is required")
    
    # Security: Check if path is allowed
    if not _is_path_allowed(new_path):
        raise HTTPException(status_code=403, detail="Cannot set workspace to system folder")
    
    # Validate path exists and is a directory
    path = Path(new_path).resolve()
    if not path.exists():
        raise HTTPException(status_code=404, detail="Path does not exist")
    if not path.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")
    
    # Double-check after resolve
    if not _is_path_allowed(str(path)):
        raise HTTPException(status_code=403, detail="Cannot set workspace to system folder")
    
    _current_workspace = str(path)
    
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
    workspace = request.get("workspace", _current_workspace)
    
    # Auto-detect game workspace if not specified
    if not request.get("workspace"):
        game_path = Path(_current_workspace) / "space-adventure"
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
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))


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
        return result
    except Exception as e:
        logger.error("subagent_spawn_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/memory/stats")
async def get_memory_stats():
    """Get statistics about the vector store."""
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    
    try:
        stats = _orchestrator.codebase_memory.get_stats()
        return stats
    except Exception as e:
        logger.error("stats_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


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
        return {"ready": False, "reason": str(e)}


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


@app.get("/metrics", include_in_schema=False)
async def metrics():
    """Prometheus scrape endpoint."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    import uvicorn
    import os

    port = int(os.getenv("PORT", "5005"))
    uvicorn.run(app, host="0.0.0.0", port=port)