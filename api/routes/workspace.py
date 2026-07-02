"""Workspace, project, wiki, screenshot, index, and search routes."""
from __future__ import annotations

import os
import re
from pathlib import Path

import structlog
from fastapi import APIRouter, Depends, HTTPException

from api.deps import (
    app_state, WORKSPACE_PATH, _is_path_allowed, require_api_key, save_persisted_project,
)

logger = structlog.get_logger()
router = APIRouter()

_PROJECT_NAME_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]*$')


@router.get("/workspace")
async def get_workspace():
    """Get current workspace path."""
    _ws = os.getenv("AGENT_EFFECTIVE_WORKSPACE", WORKSPACE_PATH)
    return {"workspace": _ws, "exists": Path(_ws).exists()}


@router.post("/workspace", dependencies=[Depends(require_api_key)])
async def set_workspace(request: dict):
    """Set new workspace path."""
    new_path = request.get("path")
    if not new_path:
        raise HTTPException(status_code=400, detail="path is required")

    workspace_root = Path(WORKSPACE_PATH).resolve()

    path = (workspace_root / new_path).resolve()
    if not path.is_relative_to(workspace_root):
        raise HTTPException(status_code=403, detail="Cannot set workspace outside configured root")
    if not _is_path_allowed(str(path)):
        raise HTTPException(status_code=403, detail="Cannot set workspace to system folder")
    if not path.exists():
        raise HTTPException(status_code=404, detail="Path does not exist")
    if not path.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")

    # AGENT_EFFECTIVE_WORKSPACE is the cross-process source of truth for the
    # active workspace (read by agent/workspace_context.py, git_tool.py,
    # mcp/server.py, ...) — set it for those consumers, but use the
    # already-validated `path` object here rather than reading it back.
    os.environ["AGENT_EFFECTIVE_WORKSPACE"] = str(path)
    app_state.current_workspace = str(path)
    _project_rel = (
        str(Path(app_state.current_workspace).relative_to(workspace_root))
        if Path(app_state.current_workspace) != workspace_root
        else ""
    )
    save_persisted_project(_project_rel)

    from local_coding_agent import create_agent
    app_state.orchestrator = create_agent(app_state.current_workspace, "config/models.yaml")

    return {"success": True, "workspace": app_state.current_workspace}


@router.get("/workspace/file")
async def read_workspace_file(path: str):
    """Read a file from the workspace by relative path."""
    _ws_root = Path(os.getenv("WORKSPACE_PATH", "./workspace")).resolve()
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


@router.get("/workspace/project")
async def get_project():
    """Return the active project name and workspace root."""
    base = Path(os.getenv("WORKSPACE_PATH", "./workspace")).resolve()
    current = Path(app_state.current_workspace)  # already a resolved string
    try:
        project = str(current.relative_to(base)) if current != base else None
    except ValueError:
        project = None
    return {"project": project, "workspace": str(current), "workspace_root": str(base)}


@router.get("/workspace/directories")
async def list_workspace_directories():
    """List available directories in workspace."""
    workspace = Path(os.getenv("AGENT_EFFECTIVE_WORKSPACE", WORKSPACE_PATH))
    if not workspace.exists():
        return {"error": "Workspace does not exist"}
    try:
        items = [
            {"name": item.name, "type": "directory" if item.is_dir() else "file", "path": str(item)}
            for item in workspace.iterdir()
        ]
        return {"workspace": str(workspace), "items": items}
    except Exception as e:
        logger.error("workspace_list_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Could not list workspace")


@router.post("/workspace/project", dependencies=[Depends(require_api_key)])
async def set_project(request: dict):
    """Switch the active project subdirectory within the workspace root.

    Body: {"name": "<project-name>"}  — switch to WORKSPACE_PATH/<name>
          {"name": ""}  or {"name": null} — clear back to workspace root
    """
    raw_name = (request.get("name") or "").strip()
    workspace_root = Path(WORKSPACE_PATH).resolve()

    if raw_name:
        if not re.fullmatch(r"[A-Za-z0-9._\-/]+", raw_name):
            raise HTTPException(status_code=400, detail="Invalid project name")
        parts = Path(raw_name).parts
        if any(part in ("", ".", "..") for part in parts):
            raise HTTPException(status_code=400, detail="Invalid project name")
        resolved_target = (workspace_root / raw_name).resolve()
        if not resolved_target.is_relative_to(workspace_root):
            raise HTTPException(status_code=403, detail="Path not allowed")
    else:
        resolved_target = workspace_root

    if not _is_path_allowed(str(resolved_target)):
        raise HTTPException(status_code=403, detail="Path not allowed")

    # AGENT_EFFECTIVE_WORKSPACE is the cross-process source of truth for the
    # active workspace — set it for downstream consumers, but use the
    # already-validated `resolved_target` object here rather than reading it back.
    os.environ["AGENT_EFFECTIVE_WORKSPACE"] = str(resolved_target)
    resolved_target.mkdir(parents=True, exist_ok=True)
    app_state.current_workspace = str(resolved_target)
    save_persisted_project(raw_name)

    from local_coding_agent import create_agent
    app_state.orchestrator = create_agent(app_state.current_workspace, "config/models.yaml")

    logger.info("project_switched", project=raw_name or "(root)", workspace=app_state.current_workspace)
    return {"success": True, "project": raw_name or None, "workspace": app_state.current_workspace}


@router.get("/wiki/status")
async def wiki_status():
    """Return a summary of the active wiki: entry count, project breakdown, last entry."""
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    return app_state.orchestrator.wiki_manager.status()


@router.post("/wiki/clean", dependencies=[Depends(require_api_key)])
async def wiki_clean():
    """Remove index entries that are out of scope for the current project."""
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    result = app_state.orchestrator.wiki_manager.clean()
    return {"success": True, **result}


@router.post("/wiki/migrate", dependencies=[Depends(require_api_key)])
async def wiki_migrate(request: dict):
    """Migrate entries tagged with a given project out of the current wiki."""
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    project = (request.get("project") or "").strip()
    if not project or not re.fullmatch(r"[A-Za-z0-9._\-]+", project):
        raise HTTPException(status_code=400, detail="Invalid project name")
    safe_project = os.path.basename(project)
    if safe_project != project:
        raise HTTPException(status_code=400, detail="Invalid project name")
    workspace_root = Path(WORKSPACE_PATH).resolve()
    target_path = (workspace_root / safe_project).resolve()
    if not target_path.is_relative_to(workspace_root):
        raise HTTPException(status_code=400, detail="Invalid project name")
    target_path.mkdir(parents=True, exist_ok=True)
    result = app_state.orchestrator.wiki_manager.migrate_to(project, str(target_path))
    return {"success": True, "project": project, **result}


@router.get("/wiki/query")
async def wiki_query(terms: str = ""):
    """Query the wiki for matching entries (respects project scope).

    Query param: ?terms=word1,word2
    """
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    term_list = [t.strip() for t in terms.split(",") if t.strip()]
    if not term_list:
        raise HTTPException(status_code=400, detail="Provide at least one search term")
    result = app_state.orchestrator.wiki_manager.query(term_list)
    return {"result": result or "(no matches)"}


@router.post("/screenshot")
async def take_screenshot(request: dict):
    """Take a screenshot of a running dev server."""
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")

    workspace_root = Path(os.getenv("AGENT_EFFECTIVE_WORKSPACE", WORKSPACE_PATH))

    raw_workspace = request.get("workspace")
    if raw_workspace:
        candidate = (workspace_root / raw_workspace).resolve()
        if not candidate.is_relative_to(workspace_root):
            raise HTTPException(status_code=403, detail="Workspace path is outside the allowed workspace root")
        if not _is_path_allowed(str(candidate)):
            raise HTTPException(status_code=403, detail="Workspace path is not allowed")
        workspace = str(candidate)
    else:
        workspace = str(workspace_root)
        game_path = workspace_root / "space-adventure"
        if game_path.exists() and (game_path / "package.json").exists():
            workspace = str(game_path)

    from agent.tools.browser_tool import BrowserTool
    import asyncio
    browser = BrowserTool(workspace)
    try:
        result = asyncio.run(browser.run_and_screenshot())
        return result
    except Exception as e:
        logger.error("screenshot_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Screenshot capture failed")


@router.post("/index")
async def index_workspace(request: dict = None):
    """Index all files in the workspace for RAG search."""
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    project_id = request.get("project_id") if request else None
    try:
        result = app_state.orchestrator.index_workspace(project_id)
        return {"success": True, "result": result}
    except Exception as e:
        logger.error("index_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/search")
async def search_codebase(q: str, limit: int = 5):
    """Search the codebase using vector similarity."""
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    try:
        project_id = Path(app_state.current_workspace).name
        results = app_state.orchestrator.codebase_memory.search_files(q, n_results=limit, project_id=project_id)
        return {"query": q, "results": results}
    except Exception as e:
        logger.error("search_failed", error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


@router.get("/projects/{project_name}/delete-preview")
async def preview_delete_project(project_name: str):
    """Return a count of what would be removed by DELETE /projects/{project_name}.

    No data is modified.
    """
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    if not _PROJECT_NAME_RE.match(project_name):
        raise HTTPException(status_code=400, detail="Invalid project name")
    try:
        return app_state.orchestrator.delete_project(project_name, dry_run=True)
    except ValueError:
        raise HTTPException(status_code=400, detail="Project name escapes workspace root")
    except Exception as e:
        logger.error("preview_delete_failed", project=project_name, error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")


@router.delete("/projects/{project_name}")
async def delete_project(project_name: str):
    """Remove all agent-managed data for a project.

    Deletes: Chroma vectors, jobs, agent_tasks, sessions, and .agent-wiki/.
    Project source files are never touched.
    """
    if not app_state.orchestrator:
        raise HTTPException(status_code=503, detail="Agent not initialized")
    if not _PROJECT_NAME_RE.match(project_name):
        raise HTTPException(status_code=400, detail="Invalid project name")
    try:
        return app_state.orchestrator.delete_project(project_name, dry_run=False)
    except ValueError:
        raise HTTPException(status_code=400, detail="Project name escapes workspace root")
    except Exception as e:
        logger.error("delete_project_failed", project=project_name, error=str(e))
        raise HTTPException(status_code=500, detail="Internal server error")
