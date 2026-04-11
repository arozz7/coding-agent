"""
Tool Executor - Formal interface for tool execution.

Implements the pattern from Anthropic's Managed Agents:
execute(name, input) → string

This decouples the brain from the hands, making tools replaceable
and enabling crash recovery.

Use `await executor.execute(name, input)` from async contexts.
The method is a coroutine and handles both sync and async tools.

EventEmittingExecutor wraps ToolExecutor to persist tool_call/tool_result
events to SessionMemory for observability and crash recovery.
"""
import asyncio
import inspect
from typing import Any, Callable, Dict, Optional
import structlog

logger = structlog.get_logger()


class ToolExecutor:
    """Formal tool execution interface.

    Usage:
        executor = ToolExecutor(workspace_path, code_analyzer, pytest_tool)

        # Execute a tool by name
        result = await executor.execute("shell", {"command": "npm run build"})
        result = await executor.execute("file_read", {"path": "src/main.py"})
        result = await executor.execute("analyze", {"path": "src/main.py"})
        result = await executor.execute("test", {"path": "tests/"})
    """

    def __init__(self, workspace_path: str, code_analyzer=None, pytest_tool=None):
        self.workspace_path = workspace_path
        self.tools: Dict[str, Callable] = {}
        self.logger = logger.bind(component="tool_executor")
        self._register_builtin_tools(code_analyzer, pytest_tool)

    def _register_builtin_tools(self, code_analyzer=None, pytest_tool=None) -> None:
        """Register built-in tools."""
        from agent.tools.shell_tool import ShellTool
        from agent.tools.file_system_tool import FileSystemTool
        from agent.tools.browser_tool import BrowserTool
        from agent.tools.web_tool import WebTool
        from agent.tools.document_tool import DocumentTool

        self.shell_tool = ShellTool(self.workspace_path)
        self.fs_tool = FileSystemTool(self.workspace_path)
        self.browser_tool = BrowserTool(self.workspace_path)
        self.web_tool = WebTool()
        self.document_tool = DocumentTool()
        self.code_analyzer = code_analyzer
        self.pytest_tool = pytest_tool

        # File system
        self.tools["shell"] = self._run_shell
        self.tools["file_read"] = self._read_file
        self.tools["file_write"] = self._write_file
        self.tools["file_list"] = self._list_files
        self.tools["search"] = self._search_files

        # Screenshots (dev-server) — kept for backward compat
        self.tools["screenshot"] = self._take_screenshot

        # Web tools
        self.tools["web_fetch"] = self._web_fetch          # fetch any URL
        self.tools["web_search"] = self._web_search        # DuckDuckGo search
        self.tools["screenshot_url"] = self._screenshot_url  # screenshot any URL

        # Document tools
        self.tools["read_document"] = self._read_document  # PDF/DOCX/XLSX/CSV

        if code_analyzer is not None:
            self.tools["analyze"] = self._analyze_code
        if pytest_tool is not None:
            self.tools["test"] = self._run_tests
    
    def _run_shell(self, input: Dict[str, Any]) -> str:
        """Execute a shell command and return stdout + stderr with exit code on failure."""
        command = input.get("command", "")
        result = self.shell_tool.run(command)
        stdout = (result.get("stdout") or "").strip()
        stderr = (result.get("stderr") or "").strip()
        rc = result.get("returncode")

        if result.get("success"):
            return stdout or "(command completed, no output)"

        parts = []
        if stdout:
            parts.append(f"stdout:\n{stdout}")
        if stderr:
            parts.append(f"stderr:\n{stderr}")
        body = "\n".join(parts) if parts else result.get("error", "unknown error")
        rc_str = f" (exit {rc})" if rc is not None else ""
        return f"Command failed{rc_str}:\n{body}"
    
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

    async def _web_fetch(self, input: Dict[str, Any]) -> str:
        """Fetch a URL and return its rendered text content."""
        url = input.get("url", "")
        if not url:
            return "Error: 'url' is required"
        try:
            result = await self.web_tool.fetch_url(url)
            if not result.get("success"):
                return f"Fetch failed: {result.get('error')}"
            title = result.get("title", "")
            text = result.get("text", "")
            truncated = " [truncated]" if result.get("truncated") else ""
            return f"Title: {title}\nURL: {url}{truncated}\n\n{text}"
        except Exception as e:
            return f"Error fetching {url}: {e}"

    async def _web_search(self, input: Dict[str, Any]) -> str:
        """Search the web via DuckDuckGo and return top results."""
        query = input.get("query", "")
        if not query:
            return "Error: 'query' is required"
        max_results = int(input.get("max_results", 5))
        try:
            results = await self.web_tool.search(query, max_results=max_results)
            if not results:
                return "No results found."
            if "error" in results[0]:
                return f"Search error: {results[0]['error']}"
            lines = [f"Search results for: {query}\n"]
            for i, r in enumerate(results, 1):
                lines.append(f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}")
            return "\n\n".join(lines)
        except Exception as e:
            return f"Error searching: {e}"

    async def _screenshot_url(self, input: Dict[str, Any]) -> str:
        """Take a screenshot of any URL (not just localhost)."""
        url = input.get("url", "")
        if not url:
            return "Error: 'url' is required"
        output_path = input.get("path")
        try:
            result = await self.web_tool.screenshot_url(url, output_path)
            if result.get("success"):
                return f"Screenshot saved to: {result['path']}"
            return f"Screenshot failed: {result.get('error')}"
        except Exception as e:
            return f"Error taking screenshot of {url}: {e}"

    def _read_document(self, input: Dict[str, Any]) -> str:
        """Read a PDF, DOCX, XLSX, or CSV file and return its content."""
        path = input.get("path", "")
        if not path:
            return "Error: 'path' is required"
        try:
            result = self.document_tool.read(path)
            if not result.get("success"):
                return f"Document read failed: {result.get('error')}"
            doc_type = result.get("type", "")
            truncated = " [truncated — file is larger]" if result.get("truncated") else ""

            if doc_type in ("pdf", "docx"):
                return f"[{doc_type.upper()}: {path}{truncated}]\n\n{result.get('text', '')}"
            elif doc_type in ("xlsx", "xls"):
                sheets = result.get("data", {})
                parts = []
                for sheet, rows in sheets.items():
                    rows_txt = "\n".join("\t".join(row) for row in rows[:20])
                    parts.append(f"Sheet: {sheet}\n{rows_txt}")
                return f"[XLSX: {path}{truncated}]\n\n" + "\n\n".join(parts)
            elif doc_type == "csv":
                rows = result.get("data", [])
                rows_txt = "\n".join(",".join(row) for row in rows[:30])
                return f"[CSV: {path}{truncated}]\n\n{rows_txt}"
            else:
                return str(result)
        except Exception as e:
            return f"Error reading document {path}: {e}"

    def _analyze_code(self, input: Dict[str, Any]) -> str:
        """Analyze a source file for structure and dependencies."""
        if self.code_analyzer is None:
            return "Error: code_analyzer not available"
        path = input.get("path", "")
        try:
            result = self.code_analyzer.analyze_file(path)
            if not result.get("success"):
                return f"Analysis failed: {result.get('error', 'unknown error')}"
            lines = [
                f"Functions: {len(result.get('functions', []))}",
                f"Classes: {len(result.get('classes', []))}",
                f"Imports: {result.get('imports', [])}",
                f"Total lines: {result.get('total_lines', 0)}",
            ]
            return "\n".join(lines)
        except Exception as e:
            return f"Error analyzing {path}: {str(e)}"

    def _run_tests(self, input: Dict[str, Any]) -> str:
        """Run pytest on a path."""
        if self.pytest_tool is None:
            return "Error: pytest_tool not available"
        path = input.get("path", "")
        try:
            result = self.pytest_tool.run(path=path)
            output = result.get("stdout", "") + result.get("stderr", "")
            status = "passed" if result.get("success") else f"failed (exit {result.get('returncode')})"
            return f"Tests {status}:\n{output}"
        except Exception as e:
            return f"Error running tests: {str(e)}"

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


class EventEmittingExecutor:
    """Thin wrapper around ToolExecutor that emits tool_call/tool_result events.

    Keeps ToolExecutor session-agnostic while adding per-session observability
    for the Anthropic Managed Agents emitEvent pattern.

    Usage:
        executor = EventEmittingExecutor(tool_executor, session_memory, session_id)
        result = await executor.execute("shell", {"command": "pytest"})
    """

    def __init__(self, executor: "ToolExecutor", session_memory, session_id: str):
        self.executor = executor
        self.session_memory = session_memory
        self.session_id = session_id
        self.logger = logger.bind(component="event_emitting_executor", session_id=session_id)

    async def execute(self, tool_name: str, input: Dict[str, Any]) -> str:
        self.session_memory.emit_event(
            self.session_id,
            "tool_call",
            {"tool": tool_name, "input": input},
        )
        result = await self.executor.execute(tool_name, input)
        self.session_memory.emit_event(
            self.session_id,
            "tool_result",
            {"tool": tool_name, "output": str(result)[:1000]},
        )
        return result

    def list_tools(self) -> list:
        return self.executor.list_tools()

    def register_tool(self, name: str, func) -> None:
        self.executor.register_tool(name, func)


__all__ = ["ToolExecutor", "EventEmittingExecutor"]