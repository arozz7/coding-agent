"""Unit tests for file system tools."""
import pytest
import tempfile
from pathlib import Path


class TestFileSystemTool:
    def test_initialization(self):
        from agent.tools import FileSystemTool

        with tempfile.TemporaryDirectory() as tmpdir:
            tool = FileSystemTool(tmpdir)
            assert tool.allowed_base == Path(tmpdir).resolve()

    def test_write_and_read_file(self):
        from agent.tools import FileSystemTool

        with tempfile.TemporaryDirectory() as tmpdir:
            tool = FileSystemTool(tmpdir)
            tool.write_file("test.txt", "Hello, World!")
            content = tool.read_file("test.txt")
            assert content == "Hello, World!"

    def test_list_directory(self):
        from agent.tools import FileSystemTool

        with tempfile.TemporaryDirectory() as tmpdir:
            tool = FileSystemTool(tmpdir)
            tool.write_file("test1.txt", "Content 1")
            tool.write_file("test2.txt", "Content 2")
            entries = tool.list_directory(".")
            names = [e["name"] for e in entries]
            assert "test1.txt" in names
            assert "test2.txt" in names

    def test_path_traversal_blocked(self):
        from agent.tools import FileSystemTool, PathTraversalError

        with tempfile.TemporaryDirectory() as tmpdir:
            tool = FileSystemTool(tmpdir)
            with pytest.raises(PathTraversalError):
                tool.read_file("../etc/passwd")

    def test_file_not_found(self):
        from agent.tools import FileSystemTool, FileOperationError

        with tempfile.TemporaryDirectory() as tmpdir:
            tool = FileSystemTool(tmpdir)
            with pytest.raises(FileOperationError):
                tool.read_file("nonexistent.txt")

    def test_search_files(self):
        from agent.tools import FileSystemTool

        with tempfile.TemporaryDirectory() as tmpdir:
            tool = FileSystemTool(tmpdir)
            tool.write_file("test.py", "# Python file")
            tool.write_file("test.txt", "Text file")
            matches = tool.search_files("*.py", ".")
            assert len(matches) >= 1

    def test_delete_file(self):
        from agent.tools import FileSystemTool

        with tempfile.TemporaryDirectory() as tmpdir:
            tool = FileSystemTool(tmpdir)
            tool.write_file("delete_me.txt", "Content")
            assert tool.file_exists("delete_me.txt")
            tool.delete_file("delete_me.txt")
            assert not tool.file_exists("delete_me.txt")
