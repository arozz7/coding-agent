from typing import Optional, List
from agent.tools import GitTool

import structlog

logger = structlog.get_logger()


class GitMCPServer:
    def __init__(self, repo_path: str):
        self.git_tool = GitTool(repo_path)
        self.logger = logger.bind(component="git_mcp_server")

    async def status(self, short: bool = True) -> dict:
        return self.git_tool.status(short)

    async def diff(self, file_path: Optional[str] = None) -> dict:
        return self.git_tool.diff(file_path)

    async def diff_staged(self, file_path: Optional[str] = None) -> dict:
        return self.git_tool.diff_staged(file_path)

    async def commit(self, message: str, files: Optional[List[str]] = None) -> dict:
        return self.git_tool.commit(message, files)

    async def log(self, n: int = 10) -> dict:
        return self.git_tool.log(n)

    async def branch(self, list_all: bool = True) -> dict:
        return self.git_tool.branch(list_all)

    async def add(self, files: List[str]) -> dict:
        return self.git_tool.add(files)

    async def restore(self, files: List[str]) -> dict:
        return self.git_tool.restore(files)
