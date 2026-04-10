from typing import List
from agent.tools import FileSystemTool

import structlog

logger = structlog.get_logger()


class FileSystemMCPServer:
    def __init__(self, workspace_path: str):
        self.fs_tool = FileSystemTool(workspace_path)
        self.logger = logger.bind(component="filesystem_mcp_server")

    async def read_file(self, path: str) -> dict:
        try:
            content = self.fs_tool.read_file(path)
            return {
                "success": True,
                "content": content,
                "path": path,
            }
        except Exception as e:
            self.logger.error("read_file_error", path=path, error=str(e))
            return {"success": False, "error": str(e)}

    async def write_file(self, path: str, content: str) -> dict:
        try:
            self.fs_tool.write_file(path, content)
            return {"success": True, "path": path}
        except Exception as e:
            self.logger.error("write_file_error", path=path, error=str(e))
            return {"success": False, "error": str(e)}

    async def list_directory(self, path: str = ".") -> dict:
        try:
            entries = self.fs_tool.list_directory(path)
            return {"success": True, "entries": entries}
        except Exception as e:
            self.logger.error(
                "list_directory_error", path=path, error=str(e)
            )
            return {"success": False, "error": str(e)}

    async def search_files(self, pattern: str, path: str = ".") -> dict:
        try:
            matches = self.fs_tool.search_files(pattern, path)
            return {"success": True, "matches": matches}
        except Exception as e:
            self.logger.error(
                "search_files_error", pattern=pattern, error=str(e)
            )
            return {"success": False, "error": str(e)}
