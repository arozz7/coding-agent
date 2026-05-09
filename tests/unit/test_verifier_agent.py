"""Unit tests for VerifierAgent — all LLM calls are mocked."""
import pytest
import tempfile
from pathlib import Path
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
        assert set(d.keys()) == {"score", "passed", "gaps", "feedback", "task_type", "test_output"}
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
        # New two-call format: Call 1 uses coverage (0-5) + depth (0-5)
        agent = _make_agent('{"coverage": 4, "depth": 4, "gaps": [], "feedback": "Comprehensive."}')
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
        # Malformed JSON → coverage=2, depth=2 → score=4 (0-5 defaults)
        agent = _make_agent("I cannot evaluate this.")
        result = await agent.verify_research("objective", "response", [])
        assert result.score == 4
        assert result.passed is False  # 4 < 7

    @pytest.mark.asyncio
    async def test_extra_fields_in_json_ignored(self):
        # New format: coverage + depth; extra keys are ignored
        agent = _make_agent(
            '{"coverage": 5, "depth": 4, "gaps": [], "feedback": "Good.", "unexpected_key": "value"}'
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


# ---------------------------------------------------------------------------
# Truncation detection
# ---------------------------------------------------------------------------

class TestDetectTruncatedFiles:
    def _agent(self) -> VerifierAgent:
        router = MagicMock()
        return VerifierAgent(model_router=router)

    def _write(self, ws: Path, rel: str, content: str) -> None:
        p = ws / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")

    def test_js_truncated_on_open_brace(self):
        agent = self._agent()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            self._write(ws, "src/app.js", "function hello() {\n  console.log('hi');\nfunction broken() {")
            truncated = agent._detect_truncated_files(ws)
            assert "src/app.js" in truncated

    def test_js_complete_not_flagged(self):
        agent = self._agent()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            self._write(ws, "src/app.js", "function hello() {\n  return 1;\n}\n")
            truncated = agent._detect_truncated_files(ws)
            assert truncated == []

    def test_html_truncated_missing_closing_tag(self):
        agent = self._agent()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            self._write(ws, "public/index.html", "<html>\n<body>\n<p>Hello</p>\n")
            truncated = agent._detect_truncated_files(ws)
            assert "public/index.html" in truncated

    def test_html_complete_not_flagged(self):
        agent = self._agent()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            self._write(ws, "index.html", "<html>\n<body>\n<p>Hello</p>\n</body>\n</html>\n")
            truncated = agent._detect_truncated_files(ws)
            assert truncated == []

    def test_python_truncated_on_def_header(self):
        agent = self._agent()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            self._write(ws, "app.py", "class Foo:\n    def bar(self):\n        pass\n\ndef incomplete_func:")
            truncated = agent._detect_truncated_files(ws)
            assert "app.py" in truncated

    def test_node_modules_ignored(self):
        agent = self._agent()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            self._write(ws, "node_modules/pkg/index.js", "function x() {")
            truncated = agent._detect_truncated_files(ws)
            assert truncated == []

    def test_json_truncated_without_closing_brace(self):
        agent = self._agent()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            self._write(ws, "config.json", '{\n  "name": "test",\n  "version": "1.0"')
            truncated = agent._detect_truncated_files(ws)
            assert "config.json" in truncated
