"""Task and job routes."""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import structlog
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from api.deps import app_state, job_store, task_store, require_api_key, summarize_response

logger = structlog.get_logger()
router = APIRouter()


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


@router.post("/task", response_model=TaskResponse, dependencies=[Depends(require_api_key)])
async def run_task(request: TaskRequest):
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    try:
        result = await app_state.orchestrator.run_task(
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


@router.post("/task/start", dependencies=[Depends(require_api_key)])
async def start_task_background(request: TaskRequest):
    """Submit a task and return a job_id immediately. Poll GET /task/{job_id} for status."""
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    job_id = f"job_{uuid.uuid4().hex[:12]}"
    session_id = request.session_id or f"session_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

    if request.force_task_type:
        task_type = request.force_task_type
    else:
        task_type = app_state.orchestrator._detect_task_type_keyword(request.task)
    _phase_labels = {
        "develop": "developing",
        "review": "reviewing",
        "test": "testing",
        "architect": "designing",
        "research": "researching",
        "chat": "thinking",
    }
    _phase_label = _phase_labels.get(task_type, "working")

    job_store.create(
        job_id=job_id,
        session_id=session_id,
        task=request.task,
        task_type=task_type,
        phase=_phase_label,
    )

    def _on_phase(label: str) -> None:
        job_store.update(job_id, phase=label)

    async def _run():
        from agent.workspace_context import set_workspace, reset_workspace
        _ws_token = set_workspace(os.getenv("AGENT_EFFECTIVE_WORKSPACE", ""))
        job_store.update(job_id, status="running")
        try:
            result = await app_state.orchestrator.run_task(
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
                    summary=inner.get("job_summary") or summarize_response(full_response),
                    _full_response=full_response,
                    screenshot_path=inner.get("screenshot_path"),
                )
                if result.get("handover_triggered"):
                    update_kwargs["handover_triggered"] = True
                    update_kwargs["new_session_id"] = result.get("session_id")
                    update_kwargs["context_budget"] = result.get("context_budget", "bridge")
                elif result.get("context_budget") == "warn":
                    update_kwargs["context_budget"] = "warn"
                job_store.update(job_id, **update_kwargs)
            else:
                job_store.update(
                    job_id,
                    status="failed",
                    error=result.get("error", "Unknown error"),
                )
        except Exception as e:
            import traceback
            logger.error("background_job_failed", job_id=job_id, error=str(e),
                         traceback=traceback.format_exc())
            job_store.update(job_id, status="failed", error=str(e))
        finally:
            reset_workspace(_ws_token)

    asyncio.create_task(_run())
    return {"job_id": job_id, "session_id": session_id, "task_type": task_type}


@router.post("/task/stream", dependencies=[Depends(require_api_key)])
async def run_task_stream(request: TaskRequest):
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    from fastapi.responses import StreamingResponse
    import json

    async def event_generator():
        try:
            async for chunk in app_state.orchestrator.run_stream(
                task=request.task,
                session_id=request.session_id,
                include_history=request.include_history,
            ):
                yield f"data: {json.dumps(chunk)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/task/{job_id}")
async def get_job_status(job_id: str):
    """Poll a background job for its current status and summary."""
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return {k: v for k, v in job.items() if k != "_full_response"}


@router.get("/task/{job_id}/result")
async def get_job_result(job_id: str):
    """Retrieve the full agent response for a completed job."""
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if job["status"] != "done":
        return {"status": job["status"], "result": None}
    return {"status": "done", "result": job.get("_full_response", "")}


@router.get("/task/{job_id}/tasks")
async def get_job_tasks(job_id: str):
    """Return the task list for a job (created by the planner)."""
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    tasks = task_store.list_tasks(job_id)
    counts = task_store.task_counts(job_id)
    return {
        "job_id": job_id,
        "tasks": [t.to_dict() for t in tasks],
        "counts": counts,
        "total": len(tasks),
        "all_done": task_store.all_done(job_id),
    }


@router.delete("/task/{job_id}")
async def cancel_job(job_id: str):
    """Request cancellation of a pending or running job."""
    job = job_store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if job["status"] in ("pending", "running"):
        job_store.update(job_id, status="cancelled", phase="cancelled")
        job = job_store.get(job_id)
    return {"cancelled": True, "job_id": job_id, "status": job["status"]}


@router.get("/jobs")
async def list_jobs(limit: int = 50, offset: int = 0):
    """List all jobs ordered by creation time descending (no full_response)."""
    return {"jobs": job_store.list_jobs(limit=limit, offset=offset)}


@router.get("/chains")
async def list_chains():
    """List all available agent chains from agent-chain.yaml."""
    chains = app_state.orchestrator.chain_runner.list_chains(app_state.orchestrator.workspace_path)
    return {"chains": chains}
