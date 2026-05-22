"""System routes: health, ready, stats, metrics, restart, environment, skills, memory, MCP."""
from __future__ import annotations

import asyncio
import time as _time
from pathlib import Path

import structlog
from fastapi import APIRouter, HTTPException, Request, Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

from api.deps import app_state, job_store

logger = structlog.get_logger()
router = APIRouter()

_SERVER_START_TIME = _time.time()


@router.get("/")
async def root():
    return {"name": "Local Coding Agent API", "version": "0.1.0", "status": "running"}


@router.get("/health")
async def health_check():
    active_jobs = 0
    try:
        rows = job_store.list_jobs(limit=20)
        active_jobs = sum(1 for j in rows if j.get("status") == "running")
    except Exception:
        pass
    return {
        "status": "healthy",
        "agent_ready": app_state.orchestrator is not None,
        "active_jobs": active_jobs,
        "uptime_seconds": int(_time.time() - _SERVER_START_TIME),
        "timestamp": _time.time(),
    }


@router.get("/ready")
async def readiness_check():
    """Readiness check — verifies model availability before accepting traffic."""
    if not app_state.orchestrator:
        return {"ready": False, "reason": "agent_not_initialized"}
    config = app_state.orchestrator.model_router.get_model("coding")
    if not config:
        return {"ready": False, "reason": "no_model_configured"}
    try:
        model_ok = await app_state.orchestrator.model_router.health_check(config)
        healthy_models = app_state.orchestrator.model_router.get_healthy_models()
        return {
            "ready": model_ok,
            "primary_model": config.name,
            "model_type": config.type,
            "healthy_models": healthy_models,
        }
    except Exception as e:
        logger.error("readiness_check_failed", error=str(e))
        return {"ready": False, "reason": "Readiness check failed"}


@router.get("/stats")
async def get_stats():
    """Agent statistics including cost tracking and session counts."""
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    cost_summary = app_state.orchestrator.model_router.get_cost_summary()
    sessions = app_state.orchestrator.list_sessions(limit=100)
    return {
        "sessions": {"total": len(sessions), "recent": sessions[:5]},
        "cost": cost_summary,
        "healthy_models": app_state.orchestrator.model_router.get_healthy_models(),
    }


@router.get("/llm/health")
async def get_llm_health():
    """Detailed LLM health status including circuit breaker states."""
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    from llm.model_resilience import create_resilience_manager

    model_router = app_state.orchestrator.model_router
    config = model_router.get_model("coding")

    resilience = create_resilience_manager(
        ollama_endpoint=config.endpoint if config and config.type == "local" else "http://127.0.0.1:11434"
    )

    diagnostics = await resilience.get_diagnostics()
    cost_summary = model_router.get_cost_summary()
    rate_status = {}
    if config:
        rate_status = model_router.rate_limiter.get_status(config.name)

    return {"resilience": diagnostics, "rate_limiter": rate_status, "cost": cost_summary}


@router.get("/metrics", include_in_schema=False)
async def metrics():
    """Prometheus scrape endpoint (includes FastAPI instrumentation metrics)."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@router.post("/restart", status_code=202)
async def request_restart(req: Request):
    """Signal the supervisor to restart both services.

    Writes .state/restart.flag at the project root; the supervisor polls for
    it and performs an ordered shutdown → restart of the API and bot.

    Only accepted from localhost — remote callers receive 403.
    """
    client_host = req.client.host if req.client else ""
    if client_host not in ("127.0.0.1", "::1", "localhost"):
        raise HTTPException(status_code=403, detail="Restart is only allowed from localhost")

    # __file__ is api/routes/system.py — go up three levels to reach project root
    state_dir = Path(__file__).parent.parent.parent / ".state"
    state_dir.mkdir(parents=True, exist_ok=True)

    flag = state_dir / "restart.flag"
    flag.touch()

    heartbeat_file = state_dir / "supervisor.heartbeat"
    supervisor_running = False
    try:
        age = _time.time() - float(heartbeat_file.read_text().strip())
        supervisor_running = age < 30
    except Exception:
        pass

    logger.info("restart_requested", client=client_host, supervisor_running=supervisor_running)
    return {
        "status": "restarting" if supervisor_running else "flag_written",
        "supervisor_running": supervisor_running,
        "message": (
            "Restart flag written. Supervisor will restart services shortly."
            if supervisor_running
            else "Restart flag written, but supervisor.py does not appear to be running. "
                 "Start it with: python supervisor.py"
        ),
    }


@router.get("/environment")
async def get_environment():
    """Return detected paths for all external tools (git, Playwright, Node, etc.)."""
    from agent.tools.environment_probe import get_environment_probe
    probe = get_environment_probe()
    return {"platform": probe._platform, "tools": probe.get_all()}


@router.post("/environment/reprobe")
async def reprobe_environment():
    """Force a fresh tool detection (ignores cached data/environment.json)."""
    from agent.tools.environment_probe import get_environment_probe
    probe = get_environment_probe()
    await asyncio.to_thread(probe.reprobe)
    return {"tools": probe.get_all()}


@router.get("/skills")
async def list_skills():
    """List all locally loaded skills."""
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    skills = app_state.orchestrator.skill_manager.list_skills()
    return {"skills": skills, "count": len(skills)}


@router.post("/skills/fetch")
async def fetch_remote_skills():
    """Download skills from the configured remote registry into skills/."""
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    try:
        result = await asyncio.to_thread(app_state.orchestrator.skill_manager.fetch_remote)
        return result
    except Exception as e:
        logger.error("skills_fetch_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/memory/stats")
async def get_memory_stats():
    """Get statistics about the vector store and MemoryWiki graph (with lint results)."""
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    try:
        rag_stats = app_state.orchestrator.codebase_memory.get_stats()
        wiki_stats = app_state.orchestrator.memory_wiki.get_statistics()
        lint_results = app_state.orchestrator.memory_wiki.lint()
        return {"rag": rag_stats, "wiki": wiki_stats, "lint": lint_results}
    except Exception as e:
        logger.error("stats_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/mcp/tools")
async def list_mcp_tools():
    """List available MCP tools."""
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    from mcp.server import create_mcp_server
    mcp_server = create_mcp_server(app_state.current_workspace)
    return {"tools": mcp_server.list_tools()}


@router.post("/mcp/tools/{tool_name}")
async def call_mcp_tool(tool_name: str, arguments: dict = None):
    """Call an MCP tool by name."""
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    from mcp.server import create_mcp_server
    mcp_server = create_mcp_server(app_state.current_workspace)
    try:
        result = await mcp_server.call_tool(tool_name, arguments or {})
        return {"success": True, "result": result}
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("mcp_tool_failed", tool=tool_name, error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")
