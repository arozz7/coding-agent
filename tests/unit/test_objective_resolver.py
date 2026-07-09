"""Unit tests for ObjectiveResolver."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from agent.orchestration.objective_resolver import ObjectiveResolver


def _make_resolver(llm_response: str = "") -> ObjectiveResolver:
    router = MagicMock()
    router.get_model.return_value = MagicMock(name="test-model")
    router.generate = AsyncMock(return_value=llm_response)
    return ObjectiveResolver(router)


class TestObjectiveResolverDetection:

    def test_detects_named_task_file(self, tmp_path):
        (tmp_path / "NEXT_STEPS.md").write_text("- [ ] do the thing")
        resolver = _make_resolver()
        assert resolver._find_task_file("move on to the next tasks in NEXT_STEPS.md", tmp_path) is not None

    def test_detects_vague_continuation_phrasing(self, tmp_path):
        (tmp_path / "NEXT_STEPS.md").write_text("- [ ] do the thing")
        resolver = _make_resolver()
        found = resolver._find_task_file("continue with the next steps", tmp_path)
        assert found is not None
        assert found.name == "NEXT_STEPS.md"

    def test_falls_back_through_candidate_names(self, tmp_path):
        (tmp_path / "TODO.md").write_text("- [ ] item")
        resolver = _make_resolver()
        found = resolver._find_task_file("what's next?", tmp_path)
        assert found is not None
        assert found.name == "TODO.md"

    def test_returns_none_when_no_task_file_and_not_vague(self, tmp_path):
        resolver = _make_resolver()
        assert resolver._find_task_file("add a login button to the navbar", tmp_path) is None

    def test_returns_none_when_objective_is_already_concrete(self, tmp_path):
        (tmp_path / "NEXT_STEPS.md").write_text("- [ ] do the thing")
        resolver = _make_resolver()
        # Names a concrete file + concrete action — not vague, no task-file phrasing.
        found = resolver._find_task_file("fix the null pointer bug in src/app.py line 42", tmp_path)
        assert found is None


class TestObjectiveResolverResolve:

    @pytest.mark.asyncio
    async def test_passes_through_when_no_task_file(self, tmp_path):
        resolver = _make_resolver()
        result = await resolver.resolve("add a login button to the navbar", tmp_path)
        assert result.objective == "add a login button to the navbar"
        assert result.context_excerpt == ""
        resolver.model_router.generate.assert_not_called()

    @pytest.mark.asyncio
    async def test_rewrites_vague_objective_using_task_file(self, tmp_path):
        (tmp_path / "NEXT_STEPS.md").write_text(
            "## Pending\n- [ ] Initialize SQLite schema in src-tauri/db/ using sqlx\n"
            "- [ ] Scaffold React app in src/\n"
        )
        resolver = _make_resolver(
            "Initialize SQLite schema and migrations in src-tauri/db/ using sqlx, "
            "covering the expense-tracking tables."
        )
        result = await resolver.resolve("move on to the next tasks in the NEXT_STEPS.md file", tmp_path)
        assert "sqlx" in result.objective
        assert "src-tauri/db" in result.objective
        assert "NEXT_STEPS.md" in result.context_excerpt

    @pytest.mark.asyncio
    async def test_falls_back_to_original_objective_on_llm_failure(self, tmp_path):
        (tmp_path / "NEXT_STEPS.md").write_text("- [ ] Initialize SQLite schema\n")
        router = MagicMock()
        router.get_model.return_value = MagicMock()
        router.generate = AsyncMock(side_effect=RuntimeError("LLM unavailable"))
        resolver = ObjectiveResolver(router)
        result = await resolver.resolve("continue with next steps", tmp_path)
        assert result.objective == "continue with next steps"

    @pytest.mark.asyncio
    async def test_falls_back_when_llm_returns_empty(self, tmp_path):
        (tmp_path / "NEXT_STEPS.md").write_text("- [ ] Initialize SQLite schema\n")
        resolver = _make_resolver("   ")
        result = await resolver.resolve("continue with next steps", tmp_path)
        assert result.objective == "continue with next steps"

    @pytest.mark.asyncio
    async def test_falls_back_when_no_model_available(self, tmp_path):
        (tmp_path / "NEXT_STEPS.md").write_text("- [ ] Initialize SQLite schema\n")
        router = MagicMock()
        router.get_model.return_value = None
        resolver = ObjectiveResolver(router)
        result = await resolver.resolve("continue with next steps", tmp_path)
        assert result.objective == "continue with next steps"

    @pytest.mark.asyncio
    async def test_context_excerpt_capped(self, tmp_path):
        huge = "- [ ] item\n" * 5000
        (tmp_path / "NEXT_STEPS.md").write_text(huge)
        resolver = _make_resolver("Do the first pending item concretely.")
        result = await resolver.resolve("continue with next steps", tmp_path)
        assert len(result.context_excerpt) < len(huge)
