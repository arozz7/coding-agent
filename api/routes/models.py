"""Model listing, active-model management, and event routes."""
from __future__ import annotations

import structlog
from fastapi import APIRouter, HTTPException

from api.deps import app_state

logger = structlog.get_logger()
router = APIRouter()


@router.get("/models")
async def list_models():
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    model_router = app_state.orchestrator.model_router
    active = model_router.get_active_model_name()

    lm_all: list[dict] = await model_router.ollama.list_all_models()
    lm_state_by_id: dict[str, str] = {
        m.get("id", ""): m.get("state", "unknown") for m in lm_all
    }

    configured_names: set[str] = set()
    configured_entries = []
    for c in model_router.configs:
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


@router.get("/models/active")
async def get_active_model():
    """Return the currently active model."""
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    model_router = app_state.orchestrator.model_router
    name = model_router.get_active_model_name()
    config = model_router.get_model("coding")
    if not config:
        raise HTTPException(status_code=404, detail="No models configured")

    return {
        "active_model": name,
        "effective_model": config.name,
        "type": config.type,
        "endpoint": config.endpoint,
        "context_window": config.context_window,
    }


@router.post("/models/active")
async def set_active_model(body: dict):
    """Switch the active model by name. Pass {"model": null} to revert to default."""
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    model_router = app_state.orchestrator.model_router
    name = body.get("model")

    if name is None:
        model_router.clear_active_model()
        effective = model_router.get_active_model_name()
        return {"active_model": effective, "message": "Reverted to default"}

    try:
        config = model_router.set_active_model(name)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))

    return {
        "active_model": config.name,
        "type": config.type,
        "endpoint": config.endpoint,
        "message": f"Switched to {config.name}",
    }


@router.get("/events/model-switches")
async def get_model_switch_events():
    """Return and clear pending model-switch events.

    The Discord bot polls this endpoint to notify users when the router
    has fallen back from a local model to a remote one.
    """
    events = list(app_state.pending_switch_events)
    app_state.pending_switch_events.clear()
    return {"events": events}
