from fastapi import FastAPI, HTTPException, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
import asyncio
import structlog

from llm import ModelRouter
from agent.orchestrator import AgentOrchestrator

logger = structlog.get_logger()

app = FastAPI(
    title="Local Coding Agent API",
    description="REST API for interacting with the local coding agent",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
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


@app.on_event("startup")
async def startup_event():
    global _model_router, _orchestrator
    from local_coding_agent import create_agent
    
    logger.info("starting_api", component="api")
    
    try:
        _orchestrator = create_agent("./workspace", "config/models.yaml")
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
        logger.error("task_failed", error=str(e))
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
    
    from agent.memory import SessionMemory
    
    try:
        memory = SessionMemory("data/memory.db")
        memory.delete_session(session_id)
        return {"success": True, "session_id": session_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/models")
async def list_models():
    if not _orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    
    from llm.config import load_models
    
    models = load_models("config/models.yaml")
    return {"models": [m.name for m in models]}


if __name__ == "__main__":
    import uvicorn
    import os
    
    port = int(os.getenv("PORT", "5005"))
    uvicorn.run(app, host="0.0.0.0", port=port)