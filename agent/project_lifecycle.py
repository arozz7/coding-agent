"""Project lifecycle — delete/dry-run preview for agent-managed project data.

Extracted from AgentOrchestrator.delete_project() so storage-layer cleanup
logic (SessionMemory, TaskStore, CodebaseMemory, .agent-wiki/) is
independently testable without constructing a full orchestrator. Pure
storage-layer coordination — no reasoning-layer logic.
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from agent.memory import SessionMemory
    from agent.memory.codebase_memory import CodebaseMemory
    from api.task_store import TaskStore

logger = structlog.get_logger()

_PROJECT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]*$")


def delete_project(
    project_name: str,
    workspace_path: str,
    session_memory: "SessionMemory",
    task_store: "TaskStore",
    codebase_memory: "CodebaseMemory",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Remove all agent-managed data for *project_name* from every storage layer.

    Deletes: Chroma vectors, jobs, agent_tasks, sessions, and .agent-wiki/.
    Project source files are never touched. Raises ValueError if
    *project_name* is invalid or escapes *workspace_path*.
    """
    _project_name = project_name.strip()
    if (
        not _project_name
        or not _PROJECT_NAME_RE.match(_project_name)
        or _project_name in {".", ".."}
        or Path(_project_name).name != _project_name
    ):
        raise ValueError(f"Invalid project_name {project_name!r}")
    _ws_root = Path(workspace_path).resolve()
    _project_dir = (_ws_root / _project_name).resolve()
    if not _project_dir.is_relative_to(_ws_root):
        raise ValueError(
            f"project_name {project_name!r} is outside workspace root {_ws_root}"
        )

    _project_path_str = str(_project_dir)
    _project_short_name = _project_dir.name
    session_ids = session_memory.list_sessions_by_project(_project_path_str)
    job_count = task_store.count_by_session_ids(session_ids)
    chroma_chunks = codebase_memory.count_project_chunks(_project_short_name)
    wiki_entries = 0
    wiki_dir = (_project_dir / ".agent-wiki").resolve()
    if not wiki_dir.is_relative_to(_ws_root):
        raise ValueError(f"wiki_dir {wiki_dir!r} outside workspace root {_ws_root}")
    wiki_index = (wiki_dir / "index.md").resolve()
    if not wiki_index.is_relative_to(_ws_root):
        raise ValueError(f"wiki_index {wiki_index!r} outside workspace root {_ws_root}")
    if wiki_index.exists():
        try:
            lines = wiki_index.read_text(encoding="utf-8").splitlines()
            wiki_entries = sum(
                1 for ln in lines
                if ln.startswith("|") and ".md" in ln and "Path" not in ln
            )
        except OSError:
            pass
    summary = {
        "project_path": _project_path_str,
        "project_name": _project_short_name,
        "sessions": len(session_ids),
        "jobs": job_count,
        "chroma_chunks": chroma_chunks,
        "wiki_entries": wiki_entries,
        "dry_run": dry_run,
    }

    if dry_run:
        return summary
    codebase_memory.clear_project(_project_short_name)
    task_store.delete_by_session_ids(session_ids)
    deleted_sessions = session_memory.delete_sessions_by_project(_project_path_str)
    if wiki_dir.exists():
        shutil.rmtree(wiki_dir)
    summary["deleted_sessions"] = deleted_sessions
    logger.info(
        "project_deleted",
        **{k: v for k, v in summary.items() if isinstance(v, (str, int, bool, float))},
    )
    return summary
