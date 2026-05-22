"""Shared application state, stores, constants, and helper functions."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import structlog
from fastapi import Header, HTTPException

from api.job_store import JobStore
from api.task_store import TaskStore

logger = structlog.get_logger()

try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(override=False)
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
WORKSPACE_PATH: str = os.getenv("WORKSPACE_PATH", os.path.abspath("./workspace"))

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

_AGENT_API_KEY: str = os.getenv("AGENT_API_KEY", "")

# ---------------------------------------------------------------------------
# Runtime project persistence
# ---------------------------------------------------------------------------
_STATE_DIR = Path(".state")
_ACTIVE_PROJECT_FILE = _STATE_DIR / "active_project"


def load_persisted_project() -> str | None:
    """Return last runtime project ('' = root cleared), or None if never set."""
    try:
        if _ACTIVE_PROJECT_FILE.exists():
            return _ACTIVE_PROJECT_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        pass
    return None


def save_persisted_project(name: str) -> None:
    """Persist the active project name ('' = workspace root) across restarts."""
    try:
        _STATE_DIR.mkdir(exist_ok=True)
        _ACTIVE_PROJECT_FILE.write_text(name, encoding="utf-8")
    except Exception as _e:
        logger.warning("persist_project_failed", error=str(_e))


# ---------------------------------------------------------------------------
# Shared mutable application state
# ---------------------------------------------------------------------------
class AppState:
    def __init__(self) -> None:
        self.orchestrator: Any = None
        self.current_workspace: str = ""
        self.pending_switch_events: list[dict] = []


app_state = AppState()

# ---------------------------------------------------------------------------
# Shared stores (eagerly created; backed by SQLite)
# ---------------------------------------------------------------------------
job_store = JobStore("data/jobs.db")
task_store = TaskStore("data/jobs.db")

# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------


def _is_path_allowed(path: str) -> bool:
    """Check if a pre-resolved absolute path string is not a critical system folder.

    *path* must already be an absolute, resolved path string (no further Path.resolve())
    so that this function is never a CodeQL path-injection sink.
    """
    for disallowed in DISALLOWED_PATHS:
        if path.lower().startswith(disallowed.lower()):
            return False
    return True


async def require_api_key(x_api_key: str = Header(default="")) -> None:
    if _AGENT_API_KEY and x_api_key != _AGENT_API_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def summarize_response(text: str, max_chars: int = 500) -> str:
    """Return a short prose summary with shell output snippet preserved.

    Fenced code blocks are stripped from the prose portion, but any
    **Shell Output:** section is extracted first and appended in truncated
    form so Discord users can see what ran without needing !result.

    Uses plain string operations (no regex on user input) to avoid ReDoS.
    """
    safe_text = text[:20_000]

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

    parts = safe_text.split("```")
    prose = "".join(parts[i] for i in range(0, len(parts), 2)).strip()
    while "\n\n\n" in prose:
        prose = prose.replace("\n\n\n", "\n\n")
    if len(prose) > max_chars:
        prose = prose[:max_chars].rstrip() + "…"
    return (prose or "(task completed)") + shell_snippet
