from typing import Optional, Callable, Any, List
from .tools.filesystem_server import FileSystemMCPServer
from .tools.git_server import GitMCPServer

import structlog

logger = structlog.get_logger()


class MCPServer:
    def __init__(self, name: str = "local-coding-agent"):
        self.name = name
        self.tools: dict[str, Callable] = {}
        self.logger = logger.bind(component="mcp_server")

    def register_tool(
        self,
        name: str,
        description: str,
        input_schema: dict,
        handler: Callable,
    ) -> None:
        self.tools[name] = handler
        self.logger.info(
            "tool_registered",
            name=name,
            description=description,
        )

    async def call_tool(self, name: str, arguments: dict) -> Any:
        if name not in self.tools:
            raise ValueError(f"Unknown tool: {name}")
        self.logger.info("tool_called", name=name, args=arguments)
        return await self.tools[name](**arguments)

    def list_tools(self) -> list[dict]:
        return [
            {
                "name": name,
                "description": " MCP tool",
            }
            for name in self.tools
        ]


def create_mcp_server(workspace_path: str, repo_path: Optional[str] = None) -> MCPServer:
    server = MCPServer("local-coding-agent")
    fs_server = FileSystemMCPServer(workspace_path)

    # Auto-detect git repo when repo_path not explicitly provided
    if repo_path is None:
        from pathlib import Path as _Path
        if (_Path(workspace_path) / ".git").exists():
            repo_path = workspace_path
            logger.info("git_repo_detected", path=workspace_path)

    server.register_tool(
        name="read_file",
        description="Read contents of a file",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"}
            },
            "required": ["path"],
        },
        handler=fs_server.read_file,
    )

    server.register_tool(
        name="write_file",
        description="Write content to a file",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path"},
                "content": {"type": "string", "description": "File content"},
            },
            "required": ["path", "content"],
        },
        handler=fs_server.write_file,
    )

    server.register_tool(
        name="list_directory",
        description="List directory contents",
        input_schema={
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path",
                    "default": ".",
                }
            },
        },
        handler=fs_server.list_directory,
    )

    server.register_tool(
        name="search_files",
        description="Search for files matching a pattern",
        input_schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern"},
                "path": {
                    "type": "string",
                    "description": "Directory to search",
                    "default": ".",
                },
            },
            "required": ["pattern"],
        },
        handler=fs_server.search_files,
    )

    if repo_path:
        git_server = GitMCPServer(repo_path)

        server.register_tool(
            name="git_status",
            description="Show git working tree status",
            input_schema={
                "type": "object",
                "properties": {
                    "short": {
                        "type": "boolean",
                        "description": "Use short format",
                        "default": True,
                    }
                },
            },
            handler=git_server.status,
        )

        server.register_tool(
            name="git_diff",
            description="Show changes between commits or working tree",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "File path to diff",
                        "default": None,
                    }
                },
            },
            handler=git_server.diff,
        )

        server.register_tool(
            name="git_diff_staged",
            description="Show staged changes",
            input_schema={
                "type": "object",
                "properties": {
                    "file_path": {
                        "type": "string",
                        "description": "File path to diff",
                        "default": None,
                    }
                },
            },
            handler=git_server.diff_staged,
        )

        server.register_tool(
            name="git_commit",
            description="Commit staged changes",
            input_schema={
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Commit message"},
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Files to commit",
                        "default": None,
                    },
                },
                "required": ["message"],
            },
            handler=git_server.commit,
        )

        server.register_tool(
            name="git_log",
            description="Show commit logs",
            input_schema={
                "type": "object",
                "properties": {
                    "n": {
                        "type": "integer",
                        "description": "Number of commits to show",
                        "default": 10,
                    }
                },
            },
            handler=git_server.log,
        )

        server.register_tool(
            name="git_branch",
            description="List or show branches",
            input_schema={
                "type": "object",
                "properties": {
                    "list_all": {
                        "type": "boolean",
                        "description": "List all branches",
                        "default": True,
                    }
                },
            },
            handler=git_server.branch,
        )

        server.register_tool(
            name="git_add",
            description="Add files to staging",
            input_schema={
                "type": "object",
                "properties": {
                    "files": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Files to add",
                    },
                },
                "required": ["files"],
            },
                handler=git_server.add,
        )
    
    # Add shell execution tool
    from agent.tools.shell_tool import ShellTool
    shell_tool = ShellTool(workspace_path)
    
    async def run_shell(command: str) -> dict:
        result = shell_tool.run(command)
        return result
    
    server.register_tool(
        name="run_shell",
        description="Run a shell command in the workspace",
        input_schema={
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "Command to execute"},
            },
            "required": ["command"],
        },
        handler=run_shell,
    )
    
    # Add test runner tool
    from agent.tools.test_runner_tool import PytestTool
    pytest_tool = PytestTool(workspace_path)
    
    async def run_tests(path: Optional[str] = None, verbose: bool = False) -> dict:
        result = pytest_tool.run(path=path, verbose=verbose)
        return result
    
    server.register_tool(
        name="run_tests",
        description="Run pytest test suite",
        input_schema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Test file or directory path"},
                "verbose": {"type": "boolean", "description": "Verbose output", "default": False},
            },
        },
        handler=run_tests,
    )
    
    # Add code analysis tool
    from agent.tools.code_analysis_tool import CodeAnalyzer
    code_analyzer = CodeAnalyzer()
    
    async def analyze_code(file_path: str) -> dict:
        result = code_analyzer.analyze_file(file_path)
        return result
    
    server.register_tool(
        name="analyze_code",
        description="Analyze code file for structure, functions, classes",
        input_schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to code file"},
            },
            "required": ["file_path"],
        },
        handler=analyze_code,
    )
    
    return server
