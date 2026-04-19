"""
Unit tests for agent/tools/search_tool.py
"""

import pytest
from pathlib import Path

from agent.tools.search_tool import SearchTool


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def workspace(tmp_path):
    return str(tmp_path)


@pytest.fixture
def tool(workspace):
    return SearchTool(workspace)


def create_file(workspace: str, rel_path: str, content: str) -> Path:
    p = Path(workspace) / rel_path
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# find_files
# ---------------------------------------------------------------------------

def test_find_py_files(tool, workspace):
    create_file(workspace, "src/main.py", "x = 1")
    create_file(workspace, "src/utils.py", "y = 2")
    create_file(workspace, "README.md", "# Docs")
    result = tool.find_files("*.py")
    assert "src/main.py" in result or "src\\main.py" in result
    assert "src/utils.py" in result or "src\\utils.py" in result
    assert "README.md" not in result


def test_find_files_in_subdir(tool, workspace):
    create_file(workspace, "src/a.ts", "const x = 1")
    create_file(workspace, "other/b.ts", "const y = 2")
    result = tool.find_files("*.ts", "src")
    assert ("a.ts" in result or "src/a.ts" in result or "src\\a.ts" in result)
    assert "b.ts" not in result


def test_find_skips_node_modules(tool, workspace):
    create_file(workspace, "node_modules/lodash/index.js", "module.exports = {}")
    create_file(workspace, "src/app.js", "const x = 1")
    result = tool.find_files("*.js")
    assert "lodash" not in result
    assert "app.js" in result or "src/app.js" in result or "src\\app.js" in result


def test_find_no_match(tool, workspace):
    create_file(workspace, "src/main.py", "x = 1")
    result = tool.find_files("*.java")
    assert "No files" in result


def test_find_path_traversal_blocked(tool, workspace):
    result = tool.find_files("*.py", "../../etc")
    assert "Error" in result


def test_find_max_results(tool, workspace):
    for i in range(10):
        create_file(workspace, f"file_{i}.txt", f"content {i}")
    result = tool.find_files("*.txt", max_results=3)
    assert "more matches" in result


# ---------------------------------------------------------------------------
# grep_code
# ---------------------------------------------------------------------------

def test_grep_finds_match(tool, workspace):
    create_file(workspace, "app.py", "def hello():\n    print('world')\n")
    result = tool.grep_code("def hello")
    assert "app.py" in result
    assert "def hello" in result


def test_grep_with_line_numbers(tool, workspace):
    create_file(workspace, "nums.py", "x = 1\ny = 2\nz = 3\n")
    result = tool.grep_code("y = 2")
    assert ":2:" in result


def test_grep_case_insensitive(tool, workspace):
    create_file(workspace, "case.py", "Hello World\n")
    result = tool.grep_code("hello", case_sensitive=False)
    assert "case.py" in result


def test_grep_skips_node_modules(tool, workspace):
    create_file(workspace, "node_modules/pkg/index.js", "function secret() {}")
    create_file(workspace, "src/main.js", "function secret() {}")
    result = tool.grep_code("function secret")
    lines = result.splitlines()
    assert not any("node_modules" in l for l in lines)
    assert any("main.js" in l for l in lines)


def test_grep_no_match(tool, workspace):
    create_file(workspace, "empty.py", "x = 1\n")
    result = tool.grep_code("banana")
    assert "No matches" in result


def test_grep_invalid_regex(tool, workspace):
    result = tool.grep_code("[invalid(")
    assert "Error" in result


def test_grep_max_results(tool, workspace):
    content = "\n".join(f"needle_{i} = {i}" for i in range(20))
    create_file(workspace, "big.py", content)
    result = tool.grep_code("needle_", max_results=5)
    assert "more matches" in result


def test_grep_skips_binary_extensions(tool, workspace):
    p = Path(workspace) / "image.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
    result = tool.grep_code("PNG")
    # .png is in the binary skip list — should produce no matches from the png file
    assert "image.png" not in result


def test_grep_path_traversal_blocked(tool, workspace):
    result = tool.grep_code("root", path="../../etc")
    assert "Error" in result
