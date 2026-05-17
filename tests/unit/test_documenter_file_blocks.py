"""Tests for _extract_file_blocks and _extract_append_blocks."""
import pytest
from agent.agents.documenter_agent import _extract_file_blocks, _extract_append_blocks


class TestExtractFileBlocks:
    def test_simple_markdown_file(self):
        response = "FILE: README.md\n```markdown\n# Hello\nWorld\n```"
        result = _extract_file_blocks(response)
        assert len(result) == 1
        path, content = result[0]
        assert path == "README.md"
        assert "# Hello" in content
        assert "World" in content

    def test_nested_mermaid_fence_captured(self):
        """The classic failure: mermaid block inside markdown breaks naive regex."""
        response = (
            "FILE: docs/ARCHITECTURE_OVERVIEW.md\n"
            "```markdown\n"
            "# Architecture\n\n"
            "```mermaid\n"
            "graph TD\n"
            "  A --> B\n"
            "```\n"
            "```"
        )
        result = _extract_file_blocks(response)
        assert len(result) == 1
        path, content = result[0]
        assert path == "docs/ARCHITECTURE_OVERVIEW.md"
        assert "```mermaid" in content
        assert "graph TD" in content
        assert "A --> B" in content

    def test_multiple_nested_fences_all_captured(self):
        response = (
            "FILE: guide.md\n"
            "```markdown\n"
            "# Guide\n\n"
            "```mermaid\n"
            "flowchart LR\n"
            "  X --> Y\n"
            "```\n\n"
            "```python\n"
            "print('hello')\n"
            "```\n"
            "```"
        )
        result = _extract_file_blocks(response)
        assert len(result) == 1
        _, content = result[0]
        assert "```mermaid" in content
        assert "```python" in content
        assert "print('hello')" in content

    def test_multiple_file_blocks_extracted(self):
        response = (
            "FILE: first.md\n```markdown\n# First\n```\n\n"
            "FILE: second.md\n```markdown\n# Second\n```"
        )
        result = _extract_file_blocks(response)
        assert len(result) == 2
        assert result[0][0] == "first.md"
        assert result[1][0] == "second.md"

    def test_second_file_has_mermaid_first_file_plain(self):
        response = (
            "FILE: plain.md\n```markdown\n# Plain\n```\n\n"
            "FILE: diag.md\n```markdown\n# Diagram\n\n```mermaid\ngraph LR\n  A-->B\n```\n```"
        )
        result = _extract_file_blocks(response)
        assert len(result) == 2
        assert result[0][0] == "plain.md"
        assert "# Plain" in result[0][1]
        assert result[1][0] == "diag.md"
        assert "```mermaid" in result[1][1]

    def test_no_file_blocks_returns_empty(self):
        response = "Here is some prose with no FILE: markers."
        assert _extract_file_blocks(response) == []

    def test_preamble_text_before_first_file_ignored(self):
        response = (
            "Here is my analysis.\n\n"
            "FILE: out.md\n```markdown\n# Doc\n```"
        )
        result = _extract_file_blocks(response)
        assert len(result) == 1
        assert result[0][0] == "out.md"

    def test_content_stripped_properly(self):
        response = "FILE: f.md\n```markdown\n\n  # Title  \n\n```"
        result = _extract_file_blocks(response)
        assert len(result) == 1
        content = result[0][1]
        assert content == "# Title"

    def test_no_closing_fence_still_extracts(self):
        """Graceful handling when model stops generating before closing fence."""
        response = "FILE: f.md\n```markdown\n# Incomplete content\nwith some text"
        result = _extract_file_blocks(response)
        assert len(result) == 1
        _, content = result[0]
        assert "# Incomplete content" in content


class TestSelectPrimaryFile:
    def test_prefers_root_over_docs(self):
        from agent.orchestration.verifier_coordinator import _select_primary_file
        files = ["docs/ARCHITECTURE_REVIEW.md", "ARCHITECTURE_REVIEW.md"]
        assert _select_primary_file(files) == "ARCHITECTURE_REVIEW.md"

    def test_prefers_first_root_file(self):
        from agent.orchestration.verifier_coordinator import _select_primary_file
        files = ["docs/a.md", "root.md", "also_root.md"]
        assert _select_primary_file(files) == "root.md"

    def test_falls_back_to_first_when_all_in_subdirs(self):
        from agent.orchestration.verifier_coordinator import _select_primary_file
        files = ["docs/a.md", "docs/b.md"]
        assert _select_primary_file(files) == "docs/a.md"

    def test_backslash_paths_treated_as_subdir(self):
        from agent.orchestration.verifier_coordinator import _select_primary_file
        files = ["docs\\a.md", "root.md"]
        assert _select_primary_file(files) == "root.md"

    def test_single_root_file(self):
        from agent.orchestration.verifier_coordinator import _select_primary_file
        assert _select_primary_file(["README.md"]) == "README.md"


class TestExtractAppendBlocks:
    def test_simple_append_block(self):
        response = "APPEND: docs/guide.md\n```markdown\n## New Section\ncontent here\n```"
        result = _extract_append_blocks(response)
        assert len(result) == 1
        path, content = result[0]
        assert path == "docs/guide.md"
        assert "## New Section" in content
        assert "content here" in content

    def test_nested_fence_inside_append(self):
        response = (
            "APPEND: README.md\n"
            "```markdown\n"
            "## Examples\n\n"
            "```python\n"
            "print('hello')\n"
            "```\n"
            "```"
        )
        result = _extract_append_blocks(response)
        assert len(result) == 1
        _, content = result[0]
        assert "```python" in content
        assert "print('hello')" in content

    def test_no_append_blocks_returns_empty(self):
        response = "FILE: out.md\n```markdown\n# Doc\n```"
        assert _extract_append_blocks(response) == []

    def test_multiple_append_blocks(self):
        response = (
            "APPEND: a.md\n```markdown\n## Section A\nfoo\n```\n\n"
            "APPEND: b.md\n```markdown\n## Section B\nbar\n```"
        )
        result = _extract_append_blocks(response)
        assert len(result) == 2
        assert result[0][0] == "a.md"
        assert result[1][0] == "b.md"

    def test_file_and_append_blocks_dont_interfere(self):
        response = (
            "FILE: new.md\n```markdown\n# New File\n```\n\n"
            "APPEND: existing.md\n```markdown\n## Extra Section\ncontent\n```"
        )
        file_blocks = _extract_file_blocks(response)
        append_blocks = _extract_append_blocks(response)
        assert len(file_blocks) == 1
        assert file_blocks[0][0] == "new.md"
        assert len(append_blocks) == 1
        assert append_blocks[0][0] == "existing.md"

    def test_content_stripped(self):
        response = "APPEND: f.md\n```markdown\n\n## Title\n\n```"
        result = _extract_append_blocks(response)
        assert result[0][1] == "## Title"
