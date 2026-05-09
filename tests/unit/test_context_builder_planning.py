"""Unit tests for ContextBuilder.build_planning_context()."""
import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


def _make_builder(workspace: str = "/tmp/ws") -> "ContextBuilder":  # noqa: F821
    from agent.orchestration.context_builder import ContextBuilder

    session_memory = MagicMock()
    session_memory.get_similar_tasks.return_value = []

    builder = ContextBuilder(
        model_router=MagicMock(),
        skill_executor=MagicMock(),
        codebase_memory=MagicMock(),
        session_memory=session_memory,
        skill_manager=MagicMock(),
        memory_wiki=None,
    )
    return builder


class TestBuildPlanningContext:
    @pytest.mark.asyncio
    async def test_returns_string(self):
        builder = _make_builder()
        with patch("agent.orchestration.context_builder.get_workspace", return_value="/nonexistent"):
            result = await builder.build_planning_context("build a web app")
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_detects_node_stack(self):
        builder = _make_builder()
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "package.json").write_text("{}", encoding="utf-8")
            with patch("agent.orchestration.context_builder.get_workspace", return_value=d):
                result = await builder.build_planning_context("build a node app")
        assert "Node.js/JavaScript" in result

    @pytest.mark.asyncio
    async def test_detects_rust_stack(self):
        builder = _make_builder()
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "Cargo.toml").write_text("[package]\nname=\"x\"", encoding="utf-8")
            with patch("agent.orchestration.context_builder.get_workspace", return_value=d):
                result = await builder.build_planning_context("build a rust crate")
        assert "Rust" in result

    @pytest.mark.asyncio
    async def test_detects_python_requirements(self):
        builder = _make_builder()
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "requirements.txt").write_text("flask\n", encoding="utf-8")
            with patch("agent.orchestration.context_builder.get_workspace", return_value=d):
                result = await builder.build_planning_context("build a flask app")
        assert "Python" in result

    @pytest.mark.asyncio
    async def test_episodic_memories_included(self):
        builder = _make_builder()
        builder.session_memory.get_similar_tasks.return_value = [
            {"task_type": "develop", "score": 8, "task_text": "build game engine", "result_summary": "Used SDL2 bindings"},
        ]
        with patch("agent.orchestration.context_builder.get_workspace", return_value="/nonexistent"):
            result = await builder.build_planning_context("build a game")
        assert "game engine" in result or "Similar past work" in result

    @pytest.mark.asyncio
    async def test_capped_at_2000_chars(self):
        builder = _make_builder()
        builder.session_memory.get_similar_tasks.return_value = [
            {"task_type": "develop", "score": 9, "task_text": "x" * 200, "result_summary": "y" * 300}
            for _ in range(10)
        ]
        with patch("agent.orchestration.context_builder.get_workspace", return_value="/nonexistent"):
            result = await builder.build_planning_context("objective")
        assert len(result) <= 2000

    @pytest.mark.asyncio
    async def test_no_crash_on_missing_workspace(self):
        builder = _make_builder()
        with patch("agent.orchestration.context_builder.get_workspace", return_value="/does/not/exist/9999"):
            result = await builder.build_planning_context("anything")
        assert isinstance(result, str)
