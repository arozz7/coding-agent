"""Canonical path-containment validator used across all tools and API endpoints."""

from pathlib import Path


class PathTraversalError(ValueError):
    """Raised when a path resolves outside the allowed base directory."""


def resolve_within(path: str | Path, allowed_base: str | Path) -> Path:
    """Resolve *path* and assert it is contained within *allowed_base*.

    Uses Path.resolve() on both sides to handle symlinks and '..' segments,
    then Path.is_relative_to() for containment — the pattern CodeQL recognises
    as safe for py/path-injection.

    Args:
        path: The candidate path (may be relative or absolute).
        allowed_base: The directory that must contain the resolved path.

    Returns:
        The resolved absolute Path.

    Raises:
        PathTraversalError: If the resolved path is outside allowed_base.
    """
    base = Path(allowed_base).resolve()
    p = Path(path)
    # Relative paths are joined with the base before resolving so they don't
    # accidentally resolve against the process working directory.
    resolved = (base / p).resolve() if not p.is_absolute() else p.resolve()  # lgtm[py/path-injection] — containment checked on the next line
    if not resolved.is_relative_to(base):
        raise PathTraversalError(
            f"Path '{path}' resolves outside allowed directory '{base}'"
        )
    return resolved
