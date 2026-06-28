import json
import re
from pathlib import Path
from typing import List, Optional
import os
import structlog

from agent.security.paths import PathTraversalError  # noqa: F401 – re-exported

logger = structlog.get_logger()


class FileOperationError(Exception):
    pass


class PathTraversalError(FileOperationError):
    pass


class FileNotFoundError_(FileOperationError):
    pass


class PermissionDeniedError(FileOperationError):
    pass


class InvalidPathError(FileOperationError):
    pass


class FileSystemTool:
    def __init__(self, allowed_base_path: str):  # noqa: ARG002 — kept for API compat
        """Initialise the tool with the configured workspace root.

        The *allowed_base_path* parameter is accepted for backward-compatibility
        but is intentionally ignored.  The actual workspace is read from the
        per-task ContextVar (agent.workspace_context.get_workspace) so that
        concurrent jobs stay isolated and a mid-run project switch cannot
        redirect an in-flight job.  Value is always derived from trusted env
        vars — no HTTP-tainted input ever reaches a path operation.
        """
        from agent.workspace_context import get_workspace
        _ws = get_workspace()
        self.allowed_base = Path(_ws).resolve()
        self.logger = logger.bind(component="file_system_tool")
        if not self.allowed_base.exists():
            self.allowed_base.mkdir(parents=True, exist_ok=True)


    def _validate_path(self, path: str) -> Path:
        try:
            if not os.path.isabs(path):
                # Strip redundant workspace-name prefix: if the LLM writes
                # "my-project/src/main.py" but the workspace is already scoped
                # to "my-project/", remove the leading component so the file
                # lands in the right place instead of being double-nested.
                # NOTE: this assumes the project root name is not intentionally
                # repeated as a subdirectory (e.g. Python src-layout mylib/mylib/
                # would be stripped). For this codebase (game/web projects) that
                # pattern does not arise.
                parts = Path(path).parts
                if parts and parts[0] == self.allowed_base.name and len(parts) > 1:
                    stripped = str(Path(*parts[1:]))
                    self.logger.debug(
                        "stripped_redundant_project_prefix",
                        original=path,
                        stripped=stripped,
                        workspace_name=self.allowed_base.name,
                    )
                    path = stripped
                resolved = (self.allowed_base / path).resolve()
            else:
                resolved = Path(path).resolve()

            if not resolved.is_relative_to(self.allowed_base):
                self.logger.warning(
                    "path_traversal_attempt",
                    path=path,
                    allowed_base=str(self.allowed_base),
                )
                raise PathTraversalError(
                    f"Path '{path}' is outside allowed directory"
                )

            return resolved

        except (OSError, ValueError) as e:
            self.logger.error("invalid_path", path=path, error=str(e))
            raise InvalidPathError(f"Invalid path: {path}") from e

    def read_file(self, file_path: str) -> str:
        validated = self._validate_path(file_path)

        if not validated.exists():
            raise FileNotFoundError_(f"File not found: {file_path}")

        if not validated.is_file():
            raise FileOperationError(f"Not a file: {file_path}")

        try:
            with open(validated, "r", encoding="utf-8") as f:
                return f.read()
        except UnicodeDecodeError as e:
            raise FileOperationError(
                f"File is not valid UTF-8: {file_path}"
            ) from e
        except PermissionError as e:
            raise PermissionDeniedError(
                f"Permission denied reading: {file_path}"
            ) from e

    # Prose path detection: a valid file path never contains apostrophes, question
    # marks, or looks like a complete English sentence (multiple words, no extension).
    _PROSE_PATH_RE = re.compile(
        r"['\?]|"                          # apostrophe or question mark in path
        r"\b(won't|can't|let me|first to|notation)\b",  # English clause fragments
        re.IGNORECASE,
    )

    def write_file(self, file_path: str, content: str) -> None:
        # Reject paths that look like model chain-of-thought leaking into the argument.
        if len(file_path) > 200 or self._PROSE_PATH_RE.search(file_path):
            msg = (
                f"Invalid file path — looks like prose, not a path: {file_path[:80]!r}. "
                "Use file_write with a real relative path like 'src/main.py'."
            )
            self.logger.warning("file_write_prose_path_rejected", path=file_path[:120])
            raise InvalidPathError(msg)

        validated = self._validate_path(file_path)

        # For JSON files, validate that content is actually valid JSON before
        # overwriting the file.  This prevents the model from writing prose or
        # a single word (e.g. "reading") into package.json or tsconfig.json.
        if validated.suffix.lower() == ".json" and content.strip():
            try:
                parsed = json.loads(content)
            except json.JSONDecodeError as exc:
                msg = (
                    f"Refusing to write invalid JSON to {file_path!r}: {exc}. "
                    "Content must be valid JSON. Read the current file first, "
                    "then write the full corrected JSON."
                )
                self.logger.warning(
                    "file_write_invalid_json_rejected",
                    path=file_path,
                    content_preview=content[:60],
                )
                raise FileOperationError(msg) from exc

            # package.json with no name/version is functionally broken — npm
            # and node refuse to run scripts from an empty object.
            if validated.name == "package.json" and isinstance(parsed, dict):
                missing = [f for f in ("name", "version") if f not in parsed]
                if missing:
                    msg = (
                        f"Refusing to write package.json missing required fields: {missing}. "
                        "A valid package.json must have at least 'name' and 'version'. "
                        "Read the current file first, then write a complete replacement."
                    )
                    self.logger.warning(
                        "file_write_incomplete_package_json_rejected",
                        path=file_path,
                        missing_fields=missing,
                    )
                    raise FileOperationError(msg)

        try:
            validated.parent.mkdir(parents=True, exist_ok=True)

            # Preserve the original file's line endings if it already exists.
            # This prevents silently converting CRLF → LF on Windows workspaces.
            original_ending = "\n"
            if validated.exists():
                try:
                    sample = validated.read_bytes()[:4096].decode("utf-8", errors="replace")
                    if "\r\n" in sample:
                        original_ending = "\r\n"
                except Exception:
                    pass  # fall back to LF on any read error

            # Normalise incoming content to LF then restore target endings.
            normalised = content.replace("\r\n", "\n").replace("\r", "\n")
            if original_ending == "\r\n":
                normalised = normalised.replace("\n", "\r\n")

            with open(validated, "w", encoding="utf-8", newline="") as f:
                f.write(normalised)
            self.logger.info(
                "file_written",
                path=str(validated),
                size=len(normalised),
                line_ending="CRLF" if original_ending == "\r\n" else "LF",
            )
        except PermissionError as e:
            raise PermissionDeniedError(
                f"Permission denied writing: {file_path}"
            ) from e
        except OSError as e:
            raise FileOperationError(f"Error writing file: {file_path}") from e

    def list_directory(self, dir_path: str = ".") -> List[dict]:
        validated = self._validate_path(dir_path)

        if not validated.exists():
            raise FileNotFoundError_(f"Directory not found: {dir_path}")

        if not validated.is_dir():
            raise FileOperationError(f"Not a directory: {dir_path}")

        entries = []
        try:
            for item in sorted(validated.iterdir()):
                try:
                    stat = item.stat()
                    entries.append(
                        {
                            "name": item.name,
                            "type": "directory" if item.is_dir() else "file",
                            "size": stat.st_size if item.is_file() else None,
                            "modified": stat.st_mtime,
                            "permissions": oct(stat.st_mode)[-3:],
                        }
                    )
                except PermissionError:
                    continue
        except PermissionError as e:
            raise PermissionDeniedError(
                f"Permission denied listing: {dir_path}"
            ) from e

        return entries

    def search_files(self, pattern: str, dir_path: str = ".") -> List[str]:
        validated = self._validate_path(dir_path)

        matches = []
        try:
            for p in validated.rglob(pattern):
                if str(p).startswith(str(self.allowed_base)):
                    matches.append(str(p.relative_to(validated)))
        except (OSError, PermissionError) as e:
            self.logger.error("search_error", pattern=pattern, error=str(e))

        return matches

    def file_exists(self, file_path: str) -> bool:
        try:
            validated = self._validate_path(file_path)
            return validated.exists()
        except Exception:
            return False

    def delete_file(self, file_path: str) -> None:
        validated = self._validate_path(file_path)
        if not validated.exists():
            raise FileNotFoundError_(f"File not found: {file_path}")
        if not validated.is_file():
            raise FileOperationError(f"Not a file: {file_path}")
        try:
            validated.unlink()
            self.logger.info("file_deleted", path=str(validated))
        except PermissionError as e:
            raise PermissionDeniedError(
                f"Permission denied deleting: {file_path}"
            ) from e
