"""Unit tests for AcceptanceTesterAgent."""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from agent.agents.acceptance_tester_agent import AcceptanceTesterAgent, AcceptanceResult


def _make_agent(llm_response: str = "") -> AcceptanceTesterAgent:
    router = MagicMock()
    router.get_model.return_value = MagicMock(name="test-model")
    router.generate = AsyncMock(return_value=llm_response)
    return AcceptanceTesterAgent(router)


def _ok_response(passed: bool, detail: str = "") -> str:
    import json
    items = [{"criterion": "c", "passed": passed, "detail": detail}]
    return json.dumps({"results": items})


class TestAcceptanceResult:

    def test_dataclass_fields(self):
        r = AcceptanceResult(criterion="c1", passed=True, detail="looks good")
        assert r.criterion == "c1"
        assert r.passed is True
        assert r.detail == "looks good"

    def test_default_detail_is_empty(self):
        r = AcceptanceResult(criterion="c1", passed=False)
        assert r.detail == ""


class TestAcceptanceTesterAgent:

    @pytest.mark.asyncio
    async def test_returns_one_result_per_criterion(self, tmp_path):
        import json
        criteria = ["App shows score", "Player can move"]
        response = json.dumps({"results": [
            {"criterion": "App shows score", "passed": True, "detail": "Score visible"},
            {"criterion": "Player can move", "passed": False, "detail": "No movement observed"},
        ]})
        agent = _make_agent(response)
        results = await agent.run_tests(criteria, tmp_path, screenshot_path=None)
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_result_types_are_acceptance_result(self, tmp_path):
        import json
        criteria = ["command exits 0: python app.py"]
        response = json.dumps({"results": [
            {"criterion": "command exits 0: python app.py", "passed": True, "detail": ""}
        ]})
        agent = _make_agent(response)
        results = await agent.run_tests(criteria, tmp_path, screenshot_path=None)
        assert all(isinstance(r, AcceptanceResult) for r in results)

    @pytest.mark.asyncio
    async def test_passed_flag_propagated(self, tmp_path):
        import json
        criteria = ["App shows score"]
        response = json.dumps({"results": [
            {"criterion": "App shows score", "passed": False, "detail": "No score on screen"}
        ]})
        agent = _make_agent(response)
        results = await agent.run_tests(criteria, tmp_path, screenshot_path=None)
        assert results[0].passed is False
        assert "No score on screen" in results[0].detail

    @pytest.mark.asyncio
    async def test_screenshot_path_included_in_prompt_when_provided(self, tmp_path):
        import json
        criteria = ["App renders a game board"]
        shot = str(tmp_path / "shot.png")
        response = json.dumps({"results": [{"criterion": criteria[0], "passed": True, "detail": ""}]})
        agent = _make_agent(response)
        await agent.run_tests(criteria, tmp_path, screenshot_path=shot)
        prompt_arg = agent.model_router.generate.call_args[0][0]
        assert "shot.png" in prompt_arg

    @pytest.mark.asyncio
    async def test_returns_failed_results_on_llm_error(self, tmp_path):
        router = MagicMock()
        router.get_model.return_value = MagicMock()
        router.generate = AsyncMock(side_effect=RuntimeError("LLM timeout"))
        agent = AcceptanceTesterAgent(router)
        criteria = ["App shows score", "Player can move"]
        results = await agent.run_tests(criteria, tmp_path, screenshot_path=None)
        assert len(results) == len(criteria)
        assert all(not r.passed for r in results)

    @pytest.mark.asyncio
    async def test_returns_empty_list_for_empty_criteria(self, tmp_path):
        agent = _make_agent("")
        results = await agent.run_tests([], tmp_path, screenshot_path=None)
        assert results == []

    @pytest.mark.asyncio
    async def test_detail_always_present_as_string(self, tmp_path):
        import json
        criteria = ["App shows score"]
        response = json.dumps({"results": [
            {"criterion": "App shows score", "passed": True}  # no detail key
        ]})
        agent = _make_agent(response)
        results = await agent.run_tests(criteria, tmp_path, screenshot_path=None)
        assert isinstance(results[0].detail, str)

    @pytest.mark.asyncio
    async def test_returns_failed_results_when_no_model(self, tmp_path):
        router = MagicMock()
        router.get_model.return_value = None
        agent = AcceptanceTesterAgent(router)
        results = await agent.run_tests(["criterion"], tmp_path, screenshot_path=None)
        assert len(results) == 1
        assert results[0].passed is False
