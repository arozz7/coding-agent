"""Per-task workspace context variable.

Each asyncio task that runs an agent job sets this var to a snapshot of
AGENT_EFFECTIVE_WORKSPACE at the moment the job starts.  Tools read from
here rather than the global env var so that a mid-run !project switch
cannot redirect an in-flight job, and concurrent jobs stay isolated.

The value is always derived from trusted env vars (never from HTTP input),
so the CodeQL py/path-injection taint chain is never opened.
"""

import contextvars
import os

_workspace_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "agent_workspace",
    default="",
)


def get_workspace() -> str:
    """Return the workspace path for the current asyncio task.

    Falls back to AGENT_EFFECTIVE_WORKSPACE then WORKSPACE_PATH so
    non-job callers (CLI, tests) work without setting the context var.
    """
    val = _workspace_ctx.get()
    if val:
        return val
    val = os.getenv("AGENT_EFFECTIVE_WORKSPACE", "").strip()
    if val:
        return val
    return os.getenv("WORKSPACE_PATH", "./workspace")


def set_workspace(path: str) -> contextvars.Token:
    """Snapshot *path* into the current task context. Returns a reset token."""
    return _workspace_ctx.set(path)


def reset_workspace(token: contextvars.Token) -> None:
    """Restore context to the state before the matching set_workspace call."""
    _workspace_ctx.reset(token)
