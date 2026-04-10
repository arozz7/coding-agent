"""Unit tests for MCP server."""
import pytest


class TestMCPServer:
    def test_server_initialization(self):
        from mcp import MCPServer

        server = MCPServer("test")
        assert server.name == "test"
        assert server.tools == {}

    def test_register_tool(self):
        from mcp import MCPServer

        server = MCPServer("test")

        async def dummy_handler(path: str) -> dict:
            return {"success": True}

        server.register_tool(
            name="read_file",
            description="Read a file",
            input_schema={"type": "object"},
            handler=dummy_handler,
        )

        assert "read_file" in server.tools

    def test_list_tools(self):
        from mcp import MCPServer

        server = MCPServer("test")

        async def dummy_handler() -> dict:
            return {}

        server.register_tool(
            name="test_tool",
            description="A test tool",
            input_schema={"type": "object"},
            handler=dummy_handler,
        )

        tools = server.list_tools()
        assert len(tools) == 1
        assert tools[0]["name"] == "test_tool"


class TestFileSystemMCPServer:
    def test_server_creation(self, tmp_path):
        from mcp.tools.filesystem_server import FileSystemMCPServer

        server = FileSystemMCPServer(str(tmp_path))
        assert server.fs_tool is not None
