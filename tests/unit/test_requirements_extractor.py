"""Unit tests for RequirementsExtractor."""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from agent.orchestration.requirements_extractor import RequirementsExtractor


def _make_extractor(llm_response: str = "") -> RequirementsExtractor:
    router = MagicMock()
    router.get_model.return_value = MagicMock(name="test-model")
    router.generate = AsyncMock(return_value=llm_response)
    return RequirementsExtractor(router)


class TestRequirementsExtractor:

    @pytest.mark.asyncio
    async def test_returns_list_of_strings(self, tmp_path):
        extractor = _make_extractor('["App shows score > 0", "command exits 0: python app.py --test"]')
        result = await extractor.extract("build a game", tmp_path)
        assert isinstance(result, list)
        assert all(isinstance(c, str) for c in result)

    @pytest.mark.asyncio
    async def test_reads_architecture_md_when_present(self, tmp_path):
        (tmp_path / "ARCHITECTURE.md").write_text("## Stack\nPython + pygame")
        extractor = _make_extractor('["command exits 0: python main.py"]')
        await extractor.extract("build a game", tmp_path)
        prompt_arg = extractor.model_router.generate.call_args[0][0]
        assert "ARCHITECTURE.md" in prompt_arg or "Python + pygame" in prompt_arg

    @pytest.mark.asyncio
    async def test_reads_game_design_md_when_present(self, tmp_path):
        (tmp_path / "GAME_DESIGN.md").write_text("## Win Condition\nScore 100 points")
        extractor = _make_extractor('["App shows score > 0"]')
        await extractor.extract("build a snake game", tmp_path)
        prompt_arg = extractor.model_router.generate.call_args[0][0]
        assert "Score 100 points" in prompt_arg

    @pytest.mark.asyncio
    async def test_caps_at_seven_criteria(self, tmp_path):
        many = '["c1", "c2", "c3", "c4", "c5", "c6", "c7", "c8", "c9"]'
        extractor = _make_extractor(many)
        result = await extractor.extract("build something", tmp_path)
        assert len(result) <= 7

    @pytest.mark.asyncio
    async def test_returns_empty_list_on_llm_failure(self, tmp_path):
        router = MagicMock()
        router.get_model.return_value = MagicMock()
        router.generate = AsyncMock(side_effect=RuntimeError("LLM unavailable"))
        extractor = RequirementsExtractor(router)
        result = await extractor.extract("build something", tmp_path)
        assert result == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_no_model(self, tmp_path):
        router = MagicMock()
        router.get_model.return_value = None
        extractor = RequirementsExtractor(router)
        result = await extractor.extract("build something", tmp_path)
        assert result == []

    @pytest.mark.asyncio
    async def test_gracefully_handles_missing_workspace_docs(self, tmp_path):
        # No docs exist in tmp_path — should not raise
        extractor = _make_extractor('["command exits 0: python app.py"]')
        result = await extractor.extract("build something", tmp_path)
        assert isinstance(result, list)

    @pytest.mark.asyncio
    async def test_strips_empty_strings_from_result(self, tmp_path):
        extractor = _make_extractor('["valid criterion", "", "  ", "another valid"]')
        result = await extractor.extract("build something", tmp_path)
        assert all(c.strip() for c in result)

    @pytest.mark.asyncio
    async def test_find_command_rewritten_to_portable_file_exists(self, tmp_path):
        """Regression test: 'find <dir> -type f -name X | grep -q .' fails
        unconditionally on Windows and is redundant on Linux — must come out
        as the OS-agnostic 'file exists: <glob>' check instead.
        """
        extractor = _make_extractor(
            '["command exits 0: find src-tauri/db/ -type f -name \'*.sql\' | grep -q ."]'
        )
        result = await extractor.extract("build the db layer", tmp_path)
        assert result == ["file exists: src-tauri/db/**/*.sql"]

    @pytest.mark.asyncio
    async def test_unportable_command_dropped_not_kept_broken(self, tmp_path):
        extractor = _make_extractor(
            '["command exits 0: npm run build", "command exits 0: cat package.json | grep version"]'
        )
        result = await extractor.extract("build something", tmp_path)
        assert result == ["command exits 0: npm run build"]
