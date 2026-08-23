"""Unit tests for PlanReviewerAgent.review()."""
import json
import pytest
from unittest.mock import AsyncMock, MagicMock

from agent.agents.plan_reviewer_agent import PlanReviewerAgent
from agent.agents.planner_agent import _MAX_DEVELOP_TASKS


def _make_reviewer(response_json: str) -> PlanReviewerAgent:
    router = MagicMock()
    router.get_model.return_value = MagicMock(name="test-model")
    router.generate = AsyncMock(return_value=response_json)
    return PlanReviewerAgent(router)


class TestReviewChecklist:
    """Regression coverage for the sizing guidance added so the review pass
    can't undo the planner's per-file decomposition by re-expanding into
    oversized single-file-append tasks."""

    @pytest.mark.asyncio
    async def test_checklist_mentions_size_and_same_file_repetition(self):
        reviewer = _make_reviewer('[{"description": "x", "agent_type": "develop"}]')
        await reviewer.review(
            [{"description": "orig", "agent_type": "develop"}], "Build something"
        )

        prompt_sent = reviewer.model_router.generate.call_args.args[0]
        assert "300 lines" in prompt_sent
        assert "SAME file" in prompt_sent


class TestReviewTaskCap:
    @pytest.mark.asyncio
    async def test_truncates_expansion_at_max_develop_tasks(self):
        oversized = [
            {"description": f"Write file_{i}.py", "agent_type": "develop"}
            for i in range(_MAX_DEVELOP_TASKS + 5)
        ]
        reviewer = _make_reviewer(json.dumps(oversized))

        result = await reviewer.review(
            [{"description": "orig", "agent_type": "develop"}], "Build a big app"
        )

        assert len(result) == _MAX_DEVELOP_TASKS

    @pytest.mark.asyncio
    async def test_does_not_truncate_when_within_cap(self):
        small = [
            {"description": "Write file_1.py", "agent_type": "develop"},
            {"description": "Write file_2.py", "agent_type": "develop"},
        ]
        reviewer = _make_reviewer(json.dumps(small))

        result = await reviewer.review(
            [{"description": "orig", "agent_type": "develop"}], "Build a small app"
        )

        assert len(result) == 2


class TestReviewFallback:
    @pytest.mark.asyncio
    async def test_falls_back_to_original_on_unparseable_response(self):
        reviewer = _make_reviewer("not json at all")
        original = [{"description": "orig", "agent_type": "develop"}]

        result = await reviewer.review(original, "Build something")

        assert result == original

    @pytest.mark.asyncio
    async def test_falls_back_to_original_when_no_model(self):
        router = MagicMock()
        router.get_model.return_value = None
        reviewer = PlanReviewerAgent(router)
        original = [{"description": "orig", "agent_type": "develop"}]

        result = await reviewer.review(original, "Build something")

        assert result == original
