"""Unit tests for ResearchRole._scan_task_dirs and local-dir routing fix."""
import pytest
from unittest.mock import AsyncMock, MagicMock


def _make_role():
    from agent.agents.research_agent import ResearchRole
    return ResearchRole()


_ROOT_LISTING = (
    "📁 docs\n"
    "📁 research-cache\n"
    "📁 src\n"
    "📄 README.md\n"
    "📄 AGENTS.md\n"
)

_DOCS_LISTING = (
    "📄 SELF_IMPROVING_AGENTS_REPORT.md\n"
    "📄 self-improving-agents.md\n"
)

_CACHE_LISTING = (
    "📄 fix-round-1-research-raw.md\n"
    "📄 investigate-optimal-approaches-raw.md\n"
    "📄 research-challenges-raw.md\n"
)


class TestScanTaskDirs:

    @pytest.mark.asyncio
    async def test_returns_content_for_mentioned_dir(self):
        role = _make_role()
        tool_executor = MagicMock()
        tool_executor.execute = AsyncMock(side_effect=lambda name, inp: (
            _DOCS_LISTING if inp.get("path") == "docs" else
            "Report content " * 100
        ))
        sections = await role._scan_task_dirs(
            "Read all files in the docs directory", _ROOT_LISTING, tool_executor
        )
        assert any("Contents of docs/" in s for s in sections)
        assert any("SELF_IMPROVING_AGENTS_REPORT.md" in s for s in sections)

    @pytest.mark.asyncio
    async def test_ignores_dirs_not_mentioned_in_task(self):
        role = _make_role()
        tool_executor = MagicMock()
        tool_executor.execute = AsyncMock(return_value=_DOCS_LISTING)
        sections = await role._scan_task_dirs(
            "Find web articles about machine learning", _ROOT_LISTING, tool_executor
        )
        assert sections == []

    @pytest.mark.asyncio
    async def test_scans_research_cache_dir(self):
        role = _make_role()
        tool_executor = MagicMock()
        tool_executor.execute = AsyncMock(side_effect=lambda name, inp: (
            _CACHE_LISTING if inp.get("path") == "research-cache" else
            "Cached content " * 50
        ))
        sections = await role._scan_task_dirs(
            "Read all markdown files in research-cache", _ROOT_LISTING, tool_executor
        )
        assert any("Contents of research-cache/" in s for s in sections)

    @pytest.mark.asyncio
    async def test_returns_empty_on_empty_listing(self):
        role = _make_role()
        tool_executor = MagicMock()
        sections = await role._scan_task_dirs("review docs directory", "", tool_executor)
        assert sections == []

    @pytest.mark.asyncio
    async def test_skips_dir_on_tool_error(self):
        role = _make_role()
        tool_executor = MagicMock()
        tool_executor.execute = AsyncMock(side_effect=RuntimeError("permission denied"))
        # Should not raise
        sections = await role._scan_task_dirs(
            "Scan the docs directory", _ROOT_LISTING, tool_executor
        )
        assert isinstance(sections, list)

    @pytest.mark.asyncio
    async def test_caps_files_per_dir(self):
        role = _make_role()
        # Listing with 10 markdown files
        many_files = "\n".join(f"📄 file{i}.md" for i in range(10))
        tool_executor = MagicMock()
        tool_executor.execute = AsyncMock(side_effect=lambda name, inp: (
            many_files if inp.get("path") == "docs" else "content"
        ))
        sections = await role._scan_task_dirs(
            "review docs", _ROOT_LISTING, tool_executor
        )
        # At most _MAX_FILES_PER_DIR (6) file reads + 1 listing section
        assert len(sections) <= 7

    @pytest.mark.asyncio
    async def test_does_not_include_non_text_files(self):
        role = _make_role()
        mixed_listing = "📄 report.md\n📄 image.png\n📄 data.csv\n📄 notes.txt\n"
        tool_executor = MagicMock()
        tool_executor.execute = AsyncMock(side_effect=lambda name, inp: (
            mixed_listing if inp.get("path") == "docs" else "content"
        ))
        sections = await role._scan_task_dirs(
            "review docs", _ROOT_LISTING, tool_executor
        )
        file_paths = [s.split(" ---")[0].lstrip("--- ") for s in sections if s.startswith("---")]
        # Only .md and .txt files should be read
        assert all(p.endswith((".md", ".txt")) for p in file_paths if p)


class TestLocalDirRoutingOverride:
    """_scan_task_dirs results should suppress web search only when dirs were read
    and the task has no explicit web-search signals."""

    def _needs_web(self, task: str, local_sections: list) -> bool:
        # Exercise the real routing function rather than a hand-duplicated
        # copy of its formula — a duplicate previously drifted out of sync
        # with a routing fix and silently stopped catching the regression.
        from agent.agents.research_agent import _needs_web_search
        return _needs_web_search(task, local_sections)

    def test_suppresses_web_when_local_dirs_found_no_web_signals(self):
        """Pure local task: local dirs found, no web signals → web suppressed."""
        task = "Scan the docs and research-cache directories to review existing research"
        sections = ["Contents of docs/:\n📄 report.md\n", "--- docs/report.md ---\nContent"]
        assert not self._needs_web(task, sections)

    def test_allows_web_when_local_dirs_found_but_web_signals_present(self):
        """Hybrid task: local dirs found AND explicit web signals → web still fires."""
        task = "Review the docs directory and search the web for any gaps we missed"
        sections = ["Contents of docs/:\n📄 report.md\n", "--- docs/report.md ---\nContent"]
        assert self._needs_web(task, sections)

    def test_allows_web_when_no_local_dirs(self):
        """No local dir content → web search still fires (default research path)."""
        task = "Research architectural patterns for self-improving agents"
        sections = ["Workspace contents:\n📁 docs\n📄 README.md"]
        assert self._needs_web(task, sections)

    def test_allows_web_for_pure_research_task(self):
        """Explicit research/investigate task with no local dirs → web search."""
        task = "Investigate state-of-the-art recursive self-improvement techniques"
        assert self._needs_web(task, [])
