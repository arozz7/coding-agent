"""Unit tests for VerifierCoordinator.make_fix_specs() and _detect_fix_phase()."""
import pytest
from unittest.mock import MagicMock

from agent.orchestration.verifier_coordinator import VerifierCoordinator
from agent.agents.verifier_agent import VerifierResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_coordinator() -> VerifierCoordinator:
    router = MagicMock()
    router.get_model.return_value = MagicMock(context_window=32_000)
    verifier = MagicMock()
    return VerifierCoordinator(verifier_agent=verifier, model_router=router)


def _result(score: int, gaps: list[str] | None = None, test_output: str = "") -> VerifierResult:
    return VerifierResult(
        score=score,
        passed=score >= 7,
        gaps=gaps or [],
        feedback="",
        task_type="code",
        test_output=test_output,
    )


# ---------------------------------------------------------------------------
# _detect_fix_phase
# ---------------------------------------------------------------------------

class TestDetectFixPhase:
    def test_truncation_signal_wins_over_round_number(self):
        phase, instruction = VerifierCoordinator._detect_fix_phase(
            "[truncated] FAIL: src/main.py appears incomplete", "", round_num=5
        )
        assert phase == "file-incomplete"
        assert "truncat" in instruction.lower() or "split" in instruction.lower()

    def test_syntax_signal_detected(self):
        phase, _ = VerifierCoordinator._detect_fix_phase(
            "[syntax check] ERRORS:\n  src/app.js: SyntaxError: Unexpected token", "", 3
        )
        assert phase == "syntax"

    def test_runtime_signal_detected(self):
        phase, _ = VerifierCoordinator._detect_fix_phase(
            "Error: Cannot find module './utils'", "", 3
        )
        assert phase == "runtime"

    def test_test_failure_signal_detected(self):
        phase, _ = VerifierCoordinator._detect_fix_phase(
            "FAILED tests/test_engine.py::test_init - AssertionError", "", 3
        )
        assert phase == "test-failures"

    def test_fallback_round_1_syntax(self):
        phase, _ = VerifierCoordinator._detect_fix_phase("", "", round_num=1)
        assert phase == "syntax"

    def test_fallback_round_2_runtime(self):
        phase, _ = VerifierCoordinator._detect_fix_phase("", "", round_num=2)
        assert phase == "runtime"

    def test_fallback_round_3_plus_functionality(self):
        phase, _ = VerifierCoordinator._detect_fix_phase("", "", round_num=4)
        assert phase == "functionality"

    def test_truncation_takes_priority_over_syntax(self):
        phase, _ = VerifierCoordinator._detect_fix_phase(
            "[truncated] FAIL: src/app.js incomplete\n[syntax check] SyntaxError: Unexpected token",
            "",
            round_num=1,
        )
        assert phase == "file-incomplete"


# ---------------------------------------------------------------------------
# make_fix_specs — regression feedback
# ---------------------------------------------------------------------------

class TestMakeFixSpecsRegression:
    def test_regression_section_appears_on_score_drop(self):
        coord = _make_coordinator()
        specs = coord.make_fix_specs(
            objective="build a CLI tool",
            task_type="develop",
            vresult=_result(score=2, gaps=["missing output"]),
            round_num=3,
            files_changed_this_round=["src/cli.py", "src/utils.py"],
            prev_score=5,
        )
        desc = specs[0]["description"]
        assert "REGRESSION" in desc
        assert "5/10" in desc
        assert "2/10" in desc
        assert "src/cli.py" in desc

    def test_no_regression_section_when_score_improves(self):
        coord = _make_coordinator()
        specs = coord.make_fix_specs(
            objective="build a CLI tool",
            task_type="develop",
            vresult=_result(score=6, gaps=["missing tests"]),
            round_num=3,
            files_changed_this_round=["src/cli.py"],
            prev_score=4,
        )
        desc = specs[0]["description"]
        assert "REGRESSION" not in desc

    def test_no_regression_section_on_first_round(self):
        coord = _make_coordinator()
        specs = coord.make_fix_specs(
            objective="build a CLI tool",
            task_type="develop",
            vresult=_result(score=2, gaps=["missing output"]),
            round_num=1,
            files_changed_this_round=["src/main.py"],
            prev_score=-1,  # sentinel: no previous round
        )
        desc = specs[0]["description"]
        assert "REGRESSION" not in desc

    def test_regression_section_absent_when_no_files_changed(self):
        coord = _make_coordinator()
        specs = coord.make_fix_specs(
            objective="build a CLI tool",
            task_type="develop",
            vresult=_result(score=1, gaps=["broken"]),
            round_num=2,
            files_changed_this_round=[],
            prev_score=4,
        )
        desc = specs[0]["description"]
        assert "REGRESSION" not in desc


# ---------------------------------------------------------------------------
# make_fix_specs — chunking directive
# ---------------------------------------------------------------------------

class TestMakeFixSpecsChunking:
    def test_chunking_directive_on_truncation_in_test_output(self):
        coord = _make_coordinator()
        specs = coord.make_fix_specs(
            objective="build a game",
            task_type="develop",
            vresult=_result(
                score=0,
                gaps=["UI not rendering"],
                test_output="[truncated] FAIL: public/index.html appears incomplete",
            ),
            round_num=3,
        )
        desc = specs[0]["description"]
        assert "TRUNCATION" in desc or "truncat" in desc.lower()
        assert "150" in desc or "module" in desc.lower()

    def test_chunking_directive_on_truncation_in_gaps(self):
        coord = _make_coordinator()
        specs = coord.make_fix_specs(
            objective="build a Rust CLI",
            task_type="develop",
            vresult=_result(
                score=1,
                gaps=["src/main.rs appears truncated and incomplete"],
                test_output="",
            ),
            round_num=4,
        )
        desc = specs[0]["description"]
        assert "TRUNCATION" in desc or "truncat" in desc.lower()

    def test_no_chunking_directive_without_truncation_signal(self):
        coord = _make_coordinator()
        specs = coord.make_fix_specs(
            objective="build a CLI tool",
            task_type="develop",
            vresult=_result(score=4, gaps=["missing error handling"], test_output="5 passed in 1.2s"),
            round_num=3,
        )
        desc = specs[0]["description"]
        assert "TRUNCATION" not in desc


# ---------------------------------------------------------------------------
# make_fix_specs — phase label in description
# ---------------------------------------------------------------------------

class TestMakeFixSpecsPhaseLabel:
    def test_phase_label_included(self):
        coord = _make_coordinator()
        specs = coord.make_fix_specs(
            "build X", "develop", _result(3, ["a"]), round_num=1
        )
        assert "[Fix round 1 — syntax]" in specs[0]["description"]

    def test_truncation_phase_label(self):
        coord = _make_coordinator()
        specs = coord.make_fix_specs(
            "build X", "develop",
            _result(0, ["broken"], test_output="[truncated] FAIL: src/app.js incomplete"),
            round_num=4,
        )
        assert "file-incomplete" in specs[0]["description"]
