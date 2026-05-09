"""Unit tests for VerifierCoordinator criterion evaluation (Phase 28)."""
import pytest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from agent.orchestration.verifier_coordinator import CriterionResult, VerifierCoordinator


def _make_coordinator(llm_response: str = '{"passed": true}') -> VerifierCoordinator:
    verifier_agent = MagicMock()
    router = MagicMock()
    router.get_model.return_value = MagicMock()
    router.generate = AsyncMock(return_value=llm_response)

    coord = VerifierCoordinator(verifier_agent=verifier_agent, model_router=router)
    return coord


async def _noop_shell(cmd: str) -> str:
    return ""


class TestCriterionResult:
    def test_dataclass_fields(self):
        r = CriterionResult(criterion="file exists: app.py", passed=True, detail="found: app.py")
        assert r.criterion == "file exists: app.py"
        assert r.passed is True
        assert r.detail == "found: app.py"

    def test_default_detail_empty(self):
        r = CriterionResult(criterion="c", passed=False)
        assert r.detail == ""


class TestEvaluateCriteriaFileExists:
    @pytest.mark.asyncio
    async def test_file_exists_passes_when_present(self):
        coord = _make_coordinator()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "app.py").write_text("x", encoding="utf-8")
            results = await coord.evaluate_criteria(["file exists: app.py"], ws, _noop_shell)
        assert results[0].passed is True

    @pytest.mark.asyncio
    async def test_file_exists_fails_when_missing(self):
        coord = _make_coordinator()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            results = await coord.evaluate_criteria(["file exists: missing.py"], ws, _noop_shell)
        assert results[0].passed is False
        assert "missing" in results[0].detail


class TestEvaluateCriteriaFileContains:
    @pytest.mark.asyncio
    async def test_file_contains_passes_when_substring_present(self):
        coord = _make_coordinator()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "README.md").write_text("# Usage\nSee docs.\n", encoding="utf-8")
            results = await coord.evaluate_criteria(["file contains: README.md:# Usage"], ws, _noop_shell)
        assert results[0].passed is True

    @pytest.mark.asyncio
    async def test_file_contains_fails_when_substring_absent(self):
        coord = _make_coordinator()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            (ws / "README.md").write_text("nothing here\n", encoding="utf-8")
            results = await coord.evaluate_criteria(["file contains: README.md:# Usage"], ws, _noop_shell)
        assert results[0].passed is False

    @pytest.mark.asyncio
    async def test_file_contains_fails_when_file_missing(self):
        coord = _make_coordinator()
        with tempfile.TemporaryDirectory() as d:
            ws = Path(d)
            results = await coord.evaluate_criteria(["file contains: ghost.md:something"], ws, _noop_shell)
        assert results[0].passed is False


class TestEvaluateCriteriaCommandExits0:
    @pytest.mark.asyncio
    async def test_command_exits_0_passes(self):
        coord = _make_coordinator()

        async def _shell_ok(cmd: str) -> str:
            return "output\n__EXIT__0"

        with tempfile.TemporaryDirectory() as d:
            results = await coord.evaluate_criteria(["command exits 0: echo hi"], Path(d), _shell_ok)
        assert results[0].passed is True

    @pytest.mark.asyncio
    async def test_command_exits_nonzero_fails(self):
        coord = _make_coordinator()

        async def _shell_fail(cmd: str) -> str:
            return "error output\n__EXIT__1"

        with tempfile.TemporaryDirectory() as d:
            results = await coord.evaluate_criteria(["command exits 0: npm test"], Path(d), _shell_fail)
        assert results[0].passed is False

    @pytest.mark.asyncio
    async def test_server_commands_skipped(self):
        """Long-running server commands must not block the loop."""
        coord = _make_coordinator()
        blocking_calls: list = []

        async def _shell_track(cmd: str) -> str:
            blocking_calls.append(cmd)
            return "__EXIT__0"

        with tempfile.TemporaryDirectory() as d:
            results = await coord.evaluate_criteria(["command exits 0: npm start"], Path(d), _shell_track)
        assert results[0].passed is True  # skipped = passes
        assert not blocking_calls  # shell was never called


class TestEvaluateCriteriaLlmFallback:
    @pytest.mark.asyncio
    async def test_plain_english_criterion_uses_llm(self):
        coord = _make_coordinator('{"passed": true, "detail": "looks good"}')
        with tempfile.TemporaryDirectory() as d:
            results = await coord.evaluate_criteria(
                ["The app renders a score counter on screen"], Path(d), _noop_shell, combined_response="score: 0"
            )
        assert results[0].passed is True

    @pytest.mark.asyncio
    async def test_llm_fail_response(self):
        coord = _make_coordinator('{"passed": false, "detail": "no score counter visible"}')
        with tempfile.TemporaryDirectory() as d:
            results = await coord.evaluate_criteria(
                ["The app renders a score counter"], Path(d), _noop_shell
            )
        assert results[0].passed is False
        assert "score counter" in results[0].detail


class TestMakeTargetedFixSpec:
    def test_returns_dict_with_description_and_agent_type(self):
        coord = _make_coordinator()
        failing = CriterionResult(criterion="command exits 0: npm test", passed=False, detail="exit 1")
        spec = coord.make_targeted_fix_spec(failing, "build a game", round_num=1)
        assert "description" in spec
        assert spec["agent_type"] == "develop"
        assert "npm test" in spec["description"]

    def test_detail_in_description(self):
        coord = _make_coordinator()
        failing = CriterionResult(criterion="file exists: src/app.js", passed=False, detail="missing: src/app.js")
        spec = coord.make_targeted_fix_spec(failing, "build app", round_num=2)
        assert "missing" in spec["description"]

    def test_round_num_in_description(self):
        coord = _make_coordinator()
        failing = CriterionResult(criterion="c", passed=False)
        spec = coord.make_targeted_fix_spec(failing, "obj", round_num=3)
        assert "3" in spec["description"]
