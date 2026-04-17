"""
Edit Tool — Multi-hunk file editing with unified diff output.

Inspired by badlogic/pi-mono's edit tool design:
- Accepts a list of { old_text, new_text } edits for a single file.
- All edits are matched against the *original* file simultaneously (not
  incrementally), so overlapping edits are detected and rejected up-front
  rather than silently corrupting the file.
- Preserves original line endings (CRLF / LF) and BOM.
- Per-path asyncio Lock prevents concurrent writes from racing.
- Returns a unified diff string alongside the success flag.
"""

import asyncio
import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import structlog

logger = structlog.get_logger()

# ----------------------------------------------------------------------------
# Data types
# ----------------------------------------------------------------------------

@dataclass
class EditHunk:
    old_text: str
    new_text: str


@dataclass
class EditResult:
    success: bool
    path: str
    diff: str = ""
    first_changed_line: Optional[int] = None
    error: Optional[str] = None


# ----------------------------------------------------------------------------
# Exceptions
# ----------------------------------------------------------------------------

class EditError(Exception):
    """Base for all edit errors returned to the caller."""


class FileNotFoundEditError(EditError):
    pass


class NoMatchError(EditError):
    pass


class AmbiguousMatchError(EditError):
    pass


class OverlappingEditsError(EditError):
    pass


# ----------------------------------------------------------------------------
# Line ending helpers
# ----------------------------------------------------------------------------

def _detect_line_ending(text: str) -> str:
    """Return '\\r\\n' if CRLF is dominant, else '\\n'."""
    crlf_count = text.count("\r\n")
    lf_only = text.count("\n") - crlf_count
    return "\r\n" if crlf_count >= lf_only and crlf_count > 0 else "\n"


def _normalize_to_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _restore_line_endings(text: str, ending: str) -> str:
    if ending == "\r\n":
        return text.replace("\n", "\r\n")
    return text


def _strip_bom(text: str) -> Tuple[str, str]:
    """Return (bom, text_without_bom)."""
    if text.startswith("\ufeff"):
        return "\ufeff", text[1:]
    return "", text


# ----------------------------------------------------------------------------
# Diff helpers
# ----------------------------------------------------------------------------

def _generate_unified_diff(before: str, after: str, path: str = "") -> Tuple[str, Optional[int]]:
    """Return (unified_diff_string, first_changed_line_number_1indexed)."""
    before_lines = before.splitlines(keepends=True)
    after_lines = after.splitlines(keepends=True)
    diff_lines = list(
        difflib.unified_diff(before_lines, after_lines, fromfile=f"a/{path}", tofile=f"b/{path}")
    )
    diff_text = "".join(diff_lines)

    first_changed: Optional[int] = None
    for dl in diff_lines:
        if dl.startswith("@@"):
            # Parse @@ -X,Y +A,B @@
            import re
            m = re.search(r"\+(\d+)", dl)
            if m:
                first_changed = int(m.group(1))
                break

    return diff_text, first_changed


# ----------------------------------------------------------------------------
# Core edit logic
# ----------------------------------------------------------------------------

def _apply_edits(original: str, edits: List[EditHunk], rel_path: str) -> str:
    """Apply all hunks to *original* and return the new content.

    Raises EditError subclasses on any validation failure.
    All hunks are matched against the *original* text, not incrementally.
    """
    # Validate each hunk independently first.
    hunk_ranges: list[Tuple[int, int]] = []
    for i, hunk in enumerate(edits):
        if not hunk.old_text:
            raise EditError(f"Hunk {i}: old_text must not be empty.")
        idx = original.find(hunk.old_text)
        if idx == -1:
            raise NoMatchError(
                f"Hunk {i}: old_text not found in {rel_path!r}.\n"
                f"  Looking for: {hunk.old_text[:120]!r}"
            )
        # Check for ambiguous match (more than one occurrence).
        second = original.find(hunk.old_text, idx + 1)
        if second != -1:
            raise AmbiguousMatchError(
                f"Hunk {i}: old_text matches more than once in {rel_path!r}. "
                f"Make old_text more specific."
            )
        hunk_ranges.append((idx, idx + len(hunk.old_text)))

    # Check for overlapping ranges.
    sorted_ranges = sorted(zip(hunk_ranges, edits), key=lambda x: x[0][0])
    for k in range(len(sorted_ranges) - 1):
        (start_a, end_a), _ = sorted_ranges[k]
        (start_b, _), hunk_b = sorted_ranges[k + 1]
        if start_b < end_a:
            raise OverlappingEditsError(
                f"Hunks {k} and {k + 1} overlap in {rel_path!r}. "
                f"Merge nearby changes into a single hunk."
            )

    # Apply in reverse order (highest offset last) so earlier indices stay valid.
    result = original
    for (start, end), hunk in reversed(sorted_ranges):
        result = result[:start] + hunk.new_text + result[end:]

    return result


# ----------------------------------------------------------------------------
# Public class
# ----------------------------------------------------------------------------

# Global per-path locks to serialise concurrent edits.
_path_locks: Dict[str, asyncio.Lock] = {}
_path_locks_meta = asyncio.Lock()


async def _get_path_lock(abs_path: str) -> asyncio.Lock:
    async with _path_locks_meta:
        if abs_path not in _path_locks:
            _path_locks[abs_path] = asyncio.Lock()
        return _path_locks[abs_path]


class EditTool:
    """Surgical multi-hunk file editor.

    Usage::

        tool = EditTool(workspace_path)
        result = await tool.apply_edits(
            "src/main.py",
            [EditHunk(old_text="x = 1", new_text="x = 2")],
        )
        if result.success:
            print(result.diff)
        else:
            print(result.error)
    """

    def __init__(self, allowed_base_path: str):
        # allowed_base_path comes from config / env, not user input.
        self.allowed_base = Path(allowed_base_path).resolve()  # lgtm[py/path-injection]
        self.logger = logger.bind(component="edit_tool")

    def _validate_path(self, path: str) -> Path:
        if Path(path).is_absolute():
            resolved = Path(path).resolve()
        else:
            # Strip redundant workspace-name prefix (same logic as FileSystemTool).
            parts = Path(path).parts
            if parts and parts[0] == self.allowed_base.name and len(parts) > 1:
                path = str(Path(*parts[1:]))
            resolved = (self.allowed_base / path).resolve()
        if not str(resolved).startswith(str(self.allowed_base)):
            raise EditError(f"Path '{path}' is outside the workspace.")
        return resolved

    async def apply_edits(
        self, path: str, edits: List[EditHunk]
    ) -> EditResult:
        """Apply *edits* to *path* and return an :class:`EditResult`.

        Thread-safe: concurrent calls for the same file are serialised via a
        per-path asyncio Lock.
        """
        if not edits:
            return EditResult(success=False, path=path, error="No edits provided.")

        try:
            abs_path = self._validate_path(path)
        except EditError as e:
            return EditResult(success=False, path=path, error=str(e))

        if not abs_path.exists():
            return EditResult(success=False, path=path, error=f"File not found: {path}")

        lock = await _get_path_lock(str(abs_path))
        async with lock:
            return await self._apply_locked(abs_path, path, edits)

    async def _apply_locked(
        self, abs_path: Path, rel_path: str, edits: List[EditHunk]
    ) -> EditResult:
        """Internal — caller holds the per-path lock."""
        try:
            raw = await asyncio.to_thread(abs_path.read_bytes)
            raw_text = raw.decode("utf-8", errors="replace")
        except Exception as e:
            return EditResult(success=False, path=rel_path, error=f"Could not read file: {e}")

        bom, text = _strip_bom(raw_text)
        original_ending = _detect_line_ending(text)
        normalized = _normalize_to_lf(text)

        # Normalise hunks too so matching is LF-consistent.
        norm_edits = [
            EditHunk(
                old_text=_normalize_to_lf(h.old_text),
                new_text=_normalize_to_lf(h.new_text),
            )
            for h in edits
        ]

        try:
            new_normalized = _apply_edits(normalized, norm_edits, rel_path)
        except EditError as e:
            self.logger.warning("edit_failed", path=rel_path, error=str(e))
            return EditResult(success=False, path=rel_path, error=str(e))

        # Restore line endings and BOM.
        final_text = bom + _restore_line_endings(new_normalized, original_ending)

        try:
            await asyncio.to_thread(abs_path.write_bytes, final_text.encode("utf-8"))
        except Exception as e:
            return EditResult(success=False, path=rel_path, error=f"Could not write file: {e}")

        diff, first_line = _generate_unified_diff(normalized, new_normalized, rel_path)
        self.logger.info(
            "edit_applied",
            path=rel_path,
            hunks=len(edits),
            changed_line=first_line,
        )
        return EditResult(
            success=True,
            path=rel_path,
            diff=diff,
            first_changed_line=first_line,
        )
