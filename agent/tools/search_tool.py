"""
Search Tool — Native Python file search and grep.

Cross-platform replacements for shell ``find`` and ``grep`` commands.
All operations are sandboxed to the workspace directory.

Benefits over shell equivalents:
- Works on Windows without MSYS / Git Bash.
- No shell injection surface.
- Automatically skips noisy directories (node_modules, .git, __pycache__).
- Structured output for reliable LLM consumption.
"""

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

import structlog

from agent.security.paths import PathTraversalError

logger = structlog.get_logger()

# Directories skipped by default for both find and grep operations.
_DEFAULT_SKIP_DIRS = frozenset([
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    ".mypy_cache", ".pytest_cache", "dist", "build", ".next",
    ".nuxt", "coverage", ".tox", "eggs", ".eggs",
])

# Binary-file extensions skipped by grep to avoid garbled output.
_BINARY_EXTENSIONS = frozenset([
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".webp",
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
    ".exe", ".dll", ".so", ".dylib", ".pyc", ".pyd",
    ".wasm", ".bin", ".dat", ".db", ".sqlite", ".sqlite3",
    ".mp3", ".mp4", ".wav", ".ogg", ".mov", ".avi", ".mkv",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".lock",  # package-lock.json / yarn.lock are valid text but rarely useful to grep
])

_DEFAULT_MAX_RESULTS = 200


@dataclass
class GrepMatch:
    file: str        # workspace-relative path
    line: int        # 1-indexed
    snippet: str     # the matching line content (stripped)


class SearchTool:
    """Provides ``find_files`` and ``grep_code`` operations inside a workspace."""

    def __init__(self, allowed_base_path: str):  # noqa: ARG002 — kept for API compat
        """Initialise the search tool with the configured workspace root.

        The *allowed_base_path* parameter is accepted for backward-compatibility
        but is intentionally ignored — the actual workspace is read from the
        trusted AGENT_EFFECTIVE_WORKSPACE / WORKSPACE_PATH environment variables
        so that no HTTP-tainted value ever flows into a path operation (GitTool
        pattern, CodeQL py/path-injection safe).
        """
        effective = os.getenv("AGENT_EFFECTIVE_WORKSPACE", "").strip()
        if effective:
            _ws = effective
        else:
            _ws = os.getenv("WORKSPACE_PATH", "./workspace")
        base = Path(_ws).resolve()
        if not base.exists() or not base.is_dir():
            raise ValueError(f"Configured workspace does not exist: {_ws!r}")
        self.allowed_base = base
        self.logger = logger.bind(component="search_tool")


    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _validate_dir(self, path: str) -> Path:
        """Resolve *path* inside the workspace; raise ValueError if outside."""
        if not path or path == ".":
            return self.allowed_base
        # Use PurePath to construct the reference path (avoids CodeQL taint
        # analysis on the user-provided string).  Then resolve and validate
        # containment with relative_to() which correctly handles symlinks.
        pure = Path(path)  # nosec B108 -- validated below via relative_to()
        if pure.is_absolute():
            resolved = pure.resolve()
        else:
            resolved = (self.allowed_base / path).resolve()
        try:
            resolved.relative_to(self.allowed_base)
        except ValueError:
            raise ValueError(f"Path '{path}' is outside the workspace.")
        return resolved

    @staticmethod
    def _should_skip_dir(d: Path) -> bool:
        return d.name in _DEFAULT_SKIP_DIRS or d.name.startswith(".")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find_files(
        self,
        pattern: str,
        path: str = ".",
        *,
        max_results: int = _DEFAULT_MAX_RESULTS,
    ) -> str:
        """Glob for files matching *pattern* under *path*.

        Uses ``pathlib.Path.rglob`` so standard glob patterns work:
        ``*.py``, ``**/*.ts``, ``src/**/*.json``, etc.

        Returns a formatted string listing relative paths, one per line.
        Skips :data:`_DEFAULT_SKIP_DIRS` automatically.
        """
        try:
            base = self._validate_dir(path)
        except ValueError as e:
            return f"Error: {e}"

        results: list[str] = []
        try:
            for match in base.rglob(pattern):
                # Skip entries inside ignored directories.
                if any(self._should_skip_dir(p) for p in match.parents):
                    continue
                if match.is_file():
                    rel = str(match.relative_to(self.allowed_base))
                    results.append(rel)
                    if len(results) >= max_results:
                        break
        except Exception as e:
            self.logger.error("find_files_error", pattern=pattern, error=str(e))
            return f"Error searching for '{pattern}': {e}"

        if not results:
            return f"No files matching '{pattern}' found."

        truncation_note = (
            f"\n… (showing first {max_results} of more matches)"
            if len(results) == max_results
            else ""
        )
        return "\n".join(sorted(results)) + truncation_note

    def grep_code(
        self,
        pattern: str,
        path: str = ".",
        *,
        case_sensitive: bool = True,
        max_results: int = _DEFAULT_MAX_RESULTS,
    ) -> str:
        """Search file contents for lines matching regex *pattern*.

        Returns a formatted string with ``file:line: snippet`` entries,
        one per match. Skips binary files and :data:`_DEFAULT_SKIP_DIRS`.
        """
        try:
            base = self._validate_dir(path)
        except ValueError as e:
            return f"Error: {e}"

        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            regex = re.compile(pattern, flags)
        except re.error as e:
            return f"Error: Invalid regex pattern '{pattern}': {e}"

        matches: list[GrepMatch] = []

        for file_path in sorted(base.rglob("*")):
            if len(matches) >= max_results:
                break
            # Skip directories, symlinks, and noisy paths.
            if not file_path.is_file():
                continue
            if any(self._should_skip_dir(p) for p in file_path.parents):
                continue
            if file_path.suffix.lower() in _BINARY_EXTENSIONS:
                continue

            try:
                text = file_path.read_text(encoding="utf-8", errors="strict")
            except (UnicodeDecodeError, PermissionError):
                continue  # skip binary / unreadable files
            except Exception:
                continue

            rel = str(file_path.relative_to(self.allowed_base))
            for lineno, line in enumerate(text.splitlines(), start=1):
                if len(matches) >= max_results:
                    break
                if regex.search(line):
                    matches.append(GrepMatch(file=rel, line=lineno, snippet=line.strip()))

        if not matches:
            return f"No matches for '{pattern}'."

        truncation_note = (
            f"\n… (showing first {max_results} of more matches)"
            if len(matches) == max_results
            else ""
        )
        lines = [f"{m.file}:{m.line}: {m.snippet}" for m in matches]
        return "\n".join(lines) + truncation_note
