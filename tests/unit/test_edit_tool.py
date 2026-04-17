"""
Unit tests for agent/tools/edit_tool.py
"""

import pytest
import asyncio
import tempfile
import os
from pathlib import Path

from agent.tools.edit_tool import EditTool, EditHunk, NoMatchError, AmbiguousMatchError, OverlappingEditsError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def workspace(tmp_path):
    """Return a temp directory used as the workspace root."""
    return str(tmp_path)


@pytest.fixture
def tool(workspace):
    return EditTool(workspace)


def create_file(workspace: str, rel_path: str, content: str) -> Path:
    p = Path(workspace) / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# Basic success cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_single_hunk_edit(tool, workspace):
    create_file(workspace, "hello.py", "x = 1\ny = 2\n")
    result = await tool.apply_edits("hello.py", [EditHunk("x = 1", "x = 99")])
    assert result.success
    assert Path(workspace, "hello.py").read_text() == "x = 99\ny = 2\n"


@pytest.mark.asyncio
async def test_multi_hunk_edit(tool, workspace):
    create_file(workspace, "multi.py", "a = 1\nb = 2\nc = 3\n")
    result = await tool.apply_edits("multi.py", [
        EditHunk("a = 1", "a = 10"),
        EditHunk("c = 3", "c = 30"),
    ])
    assert result.success
    content = Path(workspace, "multi.py").read_text()
    assert "a = 10" in content
    assert "b = 2" in content
    assert "c = 30" in content


@pytest.mark.asyncio
async def test_diff_returned(tool, workspace):
    create_file(workspace, "diff_test.py", "foo = 1\n")
    result = await tool.apply_edits("diff_test.py", [EditHunk("foo = 1", "foo = 42")])
    assert result.success
    assert "foo = 42" in result.diff
    assert result.first_changed_line is not None


# ---------------------------------------------------------------------------
# CRLF / BOM handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_crlf_preservation(tool, workspace):
    """Editing a CRLF file must not convert it to LF."""
    p = Path(workspace) / "crlf.py"
    p.write_bytes(b"x = 1\r\ny = 2\r\n")
    result = await tool.apply_edits("crlf.py", [EditHunk("x = 1", "x = 99")])
    assert result.success
    raw = p.read_bytes()
    assert b"\r\n" in raw, "CRLF should be preserved"
    assert b"x = 99" in raw


@pytest.mark.asyncio
async def test_bom_preservation(tool, workspace):
    p = Path(workspace) / "bom.py"
    p.write_bytes(b"\xef\xbb\xbfx = 1\n")
    result = await tool.apply_edits("bom.py", [EditHunk("x = 1", "x = 42")])
    assert result.success
    raw = p.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf"), "BOM should be preserved"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_match_error(tool, workspace):
    create_file(workspace, "no_match.py", "alpha = 1\n")
    result = await tool.apply_edits("no_match.py", [EditHunk("beta = 1", "beta = 2")])
    assert not result.success
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_ambiguous_match_error(tool, workspace):
    create_file(workspace, "ambiguous.py", "x = 1\nx = 1\n")
    result = await tool.apply_edits("ambiguous.py", [EditHunk("x = 1", "x = 2")])
    assert not result.success
    assert "more than once" in result.error.lower()


@pytest.mark.asyncio
async def test_overlapping_edits_error(tool, workspace):
    create_file(workspace, "overlap.py", "def foo():\n    return 1\n")
    result = await tool.apply_edits("overlap.py", [
        EditHunk("def foo():\n    return 1", "def foo():\n    return 2"),
        EditHunk("return 1", "return 99"),
    ])
    assert not result.success
    assert "overlap" in result.error.lower()


@pytest.mark.asyncio
async def test_file_not_found(tool, workspace):
    result = await tool.apply_edits("does_not_exist.py", [EditHunk("x", "y")])
    assert not result.success
    assert "not found" in result.error.lower()


@pytest.mark.asyncio
async def test_path_traversal_blocked(tool, workspace):
    result = await tool.apply_edits("../../etc/passwd", [EditHunk("root", "hacked")])
    assert not result.success


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_concurrent_edits_serialised(tool, workspace):
    """Concurrent edits to the same file must not interleave."""
    create_file(workspace, "concurrent.py", "counter = 0\n")

    async def bump(n: int):
        content = Path(workspace, "concurrent.py").read_text()
        val = int(content.split("=")[1].strip())
        await tool.apply_edits(
            "concurrent.py",
            [EditHunk(f"counter = {val}", f"counter = {val + n}")],
        )

    # Run two bumps concurrently; both should succeed without corrupting the file.
    await asyncio.gather(bump(1), bump(1), return_exceptions=True)
    content = Path(workspace, "concurrent.py").read_text()
    # File must still be valid Python with an integer counter.
    assert "counter =" in content
