from pathlib import Path
from typing import List, Optional
import os
import structlog

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
    def __init__(self, allowed_base_path: str):
        self.allowed_base = Path(allowed_base_path).resolve()
        self.logger = logger.bind(component="file_system_tool")
        if not self.allowed_base.exists():
            self.allowed_base.mkdir(parents=True, exist_ok=True)

    def _validate_path(self, path: str) -> Path:
        try:
            if not os.path.isabs(path):
                resolved = (self.allowed_base / path).resolve()
            else:
                resolved = Path(path).resolve()

            if not str(resolved).startswith(str(self.allowed_base)):
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

    def write_file(self, file_path: str, content: str) -> None:
        validated = self._validate_path(file_path)

        try:
            validated.parent.mkdir(parents=True, exist_ok=True)
            with open(validated, "w", encoding="utf-8") as f:
                f.write(content)
            self.logger.info(
                "file_written", path=str(validated), size=len(content)
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
