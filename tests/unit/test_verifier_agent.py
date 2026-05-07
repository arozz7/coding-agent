"""Unit tests for VerifierAgent — all LLM calls are mocked."""
import pytest
from unittest.mock import AsyncMock, MagicMock

from agent.agents.verifier_agent import VerifierAgent, VerifierResult, PASS_THRESHOLD


# ---------------------------------------------------------------------------
# VerifierResult
# ---------------------------------------------------------------------------

class TestVerifierResult:
    def test_passed_at_threshold(self):
        r = VerifierResult(score=PASS_THRESHOLD, passed=True)
        assert r.passed is True

    def test_fails_below_threshold(self):
        r = VerifierResult(score=PASS_THRESHOLD - 1, passed=False)
        assert r.passed is False

    def test_to_dict_has_all_fields(self):
        r = VerifierResult(score=8, passed=True, gaps=["g1"], feedback="ok", task_type="research")
        d = r.to_dict()
        assert set(d.keys()) == {"score", "passed", "gaps", "feedback", "task_type"}
        assert d["score"] == 8
        assert d["gaps"] == ["g1"]

    def test_score_clamping_in_parse(self):
        """_parse_result must clamp out-of-range scores."""
        agent = _make_agent('{"score": 99, "gaps": [], "feedback": ""}')
        result = agent._parse_result({"score": 99, "gaps": [], "feedback": ""}, "research")
        assert result.score == 10

    def test_default_score_on_empty_raw(self):
        agent = _make_agent("")
        result = agent._parse_result({}, "code")
        assert result.score == 5
        assert result.passed is False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(json_response: str) -> VerifierAgent:
    router = MagicMock()
    router.get_model.return_value = MagicMock()
    router.generate = AsyncMock(return_value=json_response)
    return VerifierAgent(model_router=router)


# ---------------------------------------------------------------------------
# Research verifier
# ---------------------------------------------------------------------------

class TestVerifierAgentResearch:
    @pytest.mark.asyncio
    async def test_pass_on_high_score(self):
        agent = _make_agent('{"score": 8, "gaps": [], "feedback": "Comprehensive."}')
        result = await agent.verify_research("do deep research on X", "full report here", [])
        assert result.passed is True
        assert result.score == 8
        assert result.task_type == "research"

    @pytest.mark.asyncio
    async def test_fail_on_low_score(self):
        agent = _make_agent(
            '{"score": 5, "gaps": ["missing scheduling section", "no email integration"], '
            '"feedback": "Coverage is incomplete."}'
        )
        result = await agent.verify_research("build agent research", "partial report", [])
        assert result.passed is False
        assert "missing scheduling section" in result.gaps

    @pytest.mark.asyncio
    async def test_files_requested_but_not_created(self):
        agent = _make_agent(
            '{"score": 4, "gaps": ["requested markdown files were not created"], '
            '"feedback": "Files missing."}'
        )
        result = await agent.verify_research(
            "research X and capture to markdown files",
            "report text",
            files_created=[],
        )
        assert result.score == 4
        assert any("file" in g.lower() for g in result.gaps)

    @pytest.mark.asyncio
    async def test_malformed_json_returns_default(self):
        agent = _make_agent("I cannot evaluate this.")
        result = await agent.verify_research("objective", "response", [])
        assert result.score == 5
        assert result.passed is False  # 5 < 7

    @pytest.mark.asyncio
    async def test_extra_fields_in_json_ignored(self):
        agent = _make_agent(
            '{"score": 9, "gaps": [], "feedback": "Good.", "unexpected_key": "value"}'
        )
        result = await agent.verify_research("obj", "resp", [])
        assert result.score == 9
        assert result.passed is True


# ---------------------------------------------------------------------------
# Code verifier
# ---------------------------------------------------------------------------

class TestVerifierAgentCode:
    @pytest.mark.asyncio
    async def test_pass_without_tool_executor(self):
        agent = _make_agent('{"score": 8, "gaps": [], "feedback": "Solid implementation."}')
        result = await agent.verify_code("implement feature X", "code here", ["main.py"])
        assert result.passed is True
        assert result.task_type == "code"

    @pytest.mark.asyncio
    async def test_fail_missing_requirements(self):
        agent = _make_agent(
            '{"score": 5, "gaps": ["error handling missing", "tests not written"], '
            '"feedback": "Incomplete."}'
        )
        result = await agent.verify_code("implement X with tests", "partial code", [])
        assert result.passed is False
        assert len(result.gaps) == 2

    @pytest.mark.asyncio
    async def test_tool_executor_runs_tests_and_includes_output(self):
        router = MagicMock()
        router.get_model.return_value = MagicMock()
        prompts_seen: list = []

        async def capture_generate(prompt, model, **kwargs):
            prompts_seen.append(prompt)
            return '{"score": 8, "gaps": [], "feedback": "ok"}'

        router.generate = capture_generate
        tool_executor = MagicMock()
        tool_executor.execute = AsyncMock(return_value="5 passed in 0.3s")

        agent = VerifierAgent(model_router=router)
        result = await agent.verify_code("objective", "response", [], tool_executor=tool_executor)

        tool_executor.execute.assert_called_once()
        assert "5 passed" in prompts_seen[0]
        assert result.passed is True

    @pytest.mark.asyncio
    async def test_failing_tests_context_reaches_llm(self):
        router = MagicMock()
        router.get_model.return_value = MagicMock()
        prompts_seen: list = []

        async def capture_generate(prompt, model, **kwargs):
            prompts_seen.append(prompt)
            return '{"score": 4, "gaps": ["tests failing"], "feedback": "Tests broken."}'

        router.generate = capture_generate
        tool_executor = MagicMock()
        tool_executor.execute = AsyncMock(return_value="FAILED 3 errors in test_main.py")

        agent = VerifierAgent(model_router=router)
        result = await agent.verify_code("obj", "resp", [], tool_executor=tool_executor)

        assert "FAILED" in prompts_seen[0]
        assert result.score == 4
        assert result.passed is False

    @pytest.mark.asyncio
    async def test_tool_executor_exception_is_handled(self):
        router = MagicMock()
        router.get_model.return_value = MagicMock()
        router.generate = AsyncMock(return_value='{"score": 7, "gaps": [], "feedback": "ok"}')

        tool_executor = MagicMock()
        tool_executor.execute = AsyncMock(side_effect=RuntimeError("shell unavailable"))

        agent = VerifierAgent(model_router=router)
        result = await agent.verify_code("obj", "resp", [], tool_executor=tool_executor)
        # Should not raise — falls back gracefully
        assert result.score == 7
