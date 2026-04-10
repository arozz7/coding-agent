"""
Tool Executor - Formal interface for tool execution.

Implements the pattern from Anthropic's Managed Agents:
execute(name, input) → string

This decouples the brain from the hands, making tools replaceable
and enabling crash recovery.

Use `await executor.execute(name, input)` from async contexts.
The method is a coroutine and handles both sync and async tools.
"""
import asyncio
import inspect
from typing import Any, Callable, Dict, Optional
import structlog

logger = structlog.get_logger()


class ToolExecutor:
    """Formal tool execution interface.
    
    Usage:
        executor = ToolExecutor(workspace_path)
        
        # Execute a tool by name
        result = executor.execute("shell", {"command": "npm run build"})
        result = executor.execute("file_read", {"path": "src/main.py"})
        result = executor.execute("screenshot", {"url": "http://localhost:8080"})
    """
    
    def __init__(self, workspace_path: str):
        self.workspace_path = workspace_path
        self.tools: Dict[str, Callable] = {}
        self.logger = logger.bind(component="tool_executor")
        self._register_builtin_tools()
    
    def _register_builtin_tools(self) -> None:
        """Register built-in tools."""
        from agent.tools.shell_tool import ShellTool
        from agent.tools.file_system_tool import FileSystemTool
        from agent.tools.browser_tool import BrowserTool
        
        self.shell_tool = ShellTool(self.workspace_path)
        self.fs_tool = FileSystemTool(self.workspace_path)
        self.browser_tool = BrowserTool(self.workspace_path)
        
        self.tools["shell"] = self._run_shell
        self.tools["file_read"] = self._read_file
        self.tools["file_write"] = self._write_file
        self.tools["file_list"] = self._list_files
        self.tools["screenshot"] = self._take_screenshot
        self.tools["search"] = self._search_files
    
    def _run_shell(self, input: Dict[str, Any]) -> str:
        """Execute a shell command."""
        command = input.get("command", "")
        result = self.shell_tool.run(command)
        if result.get("success"):
            return result.get("stdout", "") + result.get("stderr", "")
        return f"Error: {result.get('stderr', 'Command failed')}"
    
    def _read_file(self, input: Dict[str, Any]) -> str:
        """Read a file."""
        path = input.get("path", "")
        try:
            content = self.fs_tool.read_file(path)
            return content
        except Exception as e:
            return f"Error reading file: {str(e)}"
    
    def _write_file(self, input: Dict[str, Any]) -> str:
        """Write a file."""
        path = input.get("path", "")
        content = input.get("content", "")
        try:
            self.fs_tool.write_file(path, content)
            return f"Successfully wrote {len(content)} bytes to {path}"
        except Exception as e:
            return f"Error writing file: {str(e)}"
    
    def _list_files(self, input: Dict[str, Any]) -> str:
        """List files in workspace."""
        path = input.get("path", "")
        try:
            items = []
            full_path = self.fs_tool.workspace / path if path else self.fs_tool.workspace
            for item in full_path.iterdir():
                items.append(f"{'📁' if item.is_dir() else '📄'} {item.name}")
            return "\n".join(items) if items else "No files found"
        except Exception as e:
            return f"Error listing files: {str(e)}"
    
    async def _take_screenshot(self, input: Dict[str, Any]) -> str:
        """Take a screenshot."""
        url = input.get("url", "http://localhost:8080")
        try:
            result = await self.browser_tool.run_and_screenshot()
            if result.get("success"):
                return f"Screenshot saved to: {result.get('path')}"
            return f"Screenshot failed: {result.get('error')}"
        except Exception as e:
            return f"Error taking screenshot: {str(e)}"
    
    def _search_files(self, input: Dict[str, Any]) -> str:
        """Search files in workspace."""
        pattern = input.get("pattern", "")
        path = input.get("path", "")
        try:
            from pathlib import Path
            search_path = Path(self.workspace_path) / path if path else Path(self.workspace_path)
            results = []
            for p in search_path.rglob(pattern):
                results.append(str(p.relative_to(search_path)))
            return "\n".join(results) if results else "No matches found"
        except Exception as e:
            return f"Error searching: {str(e)}"
    
    async def execute(self, tool_name: str, input: Dict[str, Any]) -> str:
        """Execute a tool by name with input dict.

        This is a coroutine — call it with ``await executor.execute(name, input)``.
        Both sync and async tool functions are supported; coroutine functions are
        awaited, regular functions are called directly.

        Args:
            tool_name: Name of the tool to execute
            input: Dict of arguments for the tool

        Returns:
            String result from the tool
        """
        if tool_name not in self.tools:
            return f"Error: Unknown tool '{tool_name}'. Available: {list(self.tools.keys())}"

        self.logger.info("executing_tool", tool=tool_name, input=input)

        tool_func = self.tools[tool_name]

        try:
            if inspect.iscoroutinefunction(tool_func):
                result = await tool_func(input)
            else:
                result = tool_func(input)
            return result
        except Exception as e:
            self.logger.error("tool_execution_failed", tool=tool_name, error=str(e))
            return f"Error executing {tool_name}: {str(e)}"
    
    def register_tool(self, name: str, func: Callable) -> None:
        """Register a custom tool."""
        self.tools[name] = func
        self.logger.info("tool_registered", name=name)
    
    def list_tools(self) -> list:
        """List available tool names."""
        return list(self.tools.keys())


__all__ = ["ToolExecutor"]