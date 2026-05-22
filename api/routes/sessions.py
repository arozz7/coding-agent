"""Session and subagent routes."""
from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException

from api.deps import app_state

logger = structlog.get_logger()
router = APIRouter()


@router.get("/sessions")
async def list_sessions(limit: int = 20):
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    sessions = app_state.orchestrator.list_sessions(limit=limit)
    return {"sessions": sessions}


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    history = app_state.orchestrator.get_session_history(session_id)
    return {"session_id": session_id, "history": history}


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    app_state.orchestrator.session_memory.delete_session(session_id)
    return {"success": True, "session_id": session_id}


@router.post("/wake/{session_id}")
async def wake_session(session_id: str):
    """Resume an interrupted session (Anthropic Managed Agents wake pattern)."""
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    result = await app_state.orchestrator.wake(session_id)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error", "Session not found"))
    return result


@router.post("/subagent/spawn")
async def spawn_subagent(request: dict):
    """Spawn a subagent with isolated context."""
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    task = request.get("task")
    role = request.get("role", "developer")
    parent_session_id = request.get("parent_session_id")
    context_limits = request.get("context_limits")

    if not task:
        raise HTTPException(status_code=400, detail="task is required")

    try:
        result = await app_state.orchestrator.spawn_subagent(
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


@router.post("/subagent/spawn-batch")
async def spawn_subagent_batch(request: dict):
    """Spawn multiple subagents in parallel."""
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    tasks = request.get("tasks", [])
    roles = request.get("roles")
    parent_session_id = request.get("parent_session_id")

    if not tasks:
        raise HTTPException(status_code=400, detail="tasks is required")

    try:
        results = await app_state.orchestrator.spawn_multiple_subagents(
            tasks=tasks,
            roles=roles,
            parent_session_id=parent_session_id,
        )
        return {"results": results}
    except Exception as e:
        logger.error("subagent_batch_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/subagent")
async def list_subagents():
    """List all subagent sessions."""
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    return {"subagents": app_state.orchestrator.list_subagents()}


@router.get("/subagent/{subagent_id}")
async def get_subagent(subagent_id: str):
    """Get result from a specific subagent."""
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    result = app_state.orchestrator.get_subagent_result(subagent_id)
    if "error" in result and result["error"] == "Subagent not found":
        raise HTTPException(status_code=404, detail="Subagent not found")
    return result
