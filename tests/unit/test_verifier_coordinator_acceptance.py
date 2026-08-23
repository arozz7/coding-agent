"""Unit tests for VerifierCoordinator.run_acceptance_tests()'s no-entry-point
fallback path — regression coverage for the fix that stopped it from
silently skipping acceptance testing on every static single-file deliverable.
"""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from agent.orchestration.verifier_coordinator import VerifierCoordinator


def _make_coordinator() -> VerifierCoordinator:
    router = MagicMock()
    router.get_model.return_value = MagicMock(context_window=32_000)
    verifier = MagicMock()
    return VerifierCoordinator(verifier_agent=verifier, model_router=router)


class TestRunAcceptanceTestsNoEntryPoint:
    @pytest.mark.asyncio
    async def test_static_fallback_actually_evaluates_screenshot(self, tmp_path):
        """When app_probe.launch() still can't start a server (e.g. the
        static-server fallback itself failed to bind/become ready) but an
        HTML file exists, the coordinator must still take a file:// screenshot
        AND run real acceptance evaluation against it -- not discard the
        opportunity with an early `return []` the way it used to."""
        (tmp_path / "index.html").write_text("<html></html>")
        coordinator = _make_coordinator()

        app_probe = MagicMock()
        app_probe.launch = AsyncMock(return_value=None)
        app_probe.screenshot_file = AsyncMock(return_value=str(tmp_path / ".screenshots" / "static_1.png"))

        acceptance_tester = MagicMock()
        expected_results = [MagicMock(passed=True)]
        acceptance_tester.run_tests = AsyncMock(return_value=expected_results)

        results = await coordinator.run_acceptance_tests(
            criteria=["the game loads without errors"],
            workspace=tmp_path,
            app_probe=app_probe,
            acceptance_tester=acceptance_tester,
        )

        acceptance_tester.run_tests.assert_called_once()
        assert results == expected_results
        assert coordinator.last_screenshot_path == str(tmp_path / ".screenshots" / "static_1.png")

    @pytest.mark.asyncio
    async def test_no_html_files_returns_empty_without_calling_acceptance_tester(self, tmp_path):
        """Regression guard: a workspace with genuinely nothing to screenshot
        (no HTML at all) still returns [] and never calls run_tests()."""
        (tmp_path / "README.md").write_text("# nothing runnable here")
        coordinator = _make_coordinator()

        app_probe = MagicMock()
        app_probe.launch = AsyncMock(return_value=None)
        app_probe.screenshot_file = AsyncMock()

        acceptance_tester = MagicMock()
        acceptance_tester.run_tests = AsyncMock()

        results = await coordinator.run_acceptance_tests(
            criteria=["some criterion"],
            workspace=tmp_path,
            app_probe=app_probe,
            acceptance_tester=acceptance_tester,
        )

        assert results == []
        acceptance_tester.run_tests.assert_not_called()
        app_probe.screenshot_file.assert_not_called()

    @pytest.mark.asyncio
    async def test_static_screenshot_failure_returns_empty(self, tmp_path):
        """HTML exists but the file:// screenshot itself fails -- still no
        evidence to evaluate, so this must fall back to [] rather than call
        run_tests() with a None screenshot_path."""
        (tmp_path / "index.html").write_text("<html></html>")
        coordinator = _make_coordinator()

        app_probe = MagicMock()
        app_probe.launch = AsyncMock(return_value=None)
        app_probe.screenshot_file = AsyncMock(return_value=None)

        acceptance_tester = MagicMock()
        acceptance_tester.run_tests = AsyncMock()

        results = await coordinator.run_acceptance_tests(
            criteria=["some criterion"],
            workspace=tmp_path,
            app_probe=app_probe,
            acceptance_tester=acceptance_tester,
        )

        assert results == []
        acceptance_tester.run_tests.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_criteria_short_circuits_before_launch(self, tmp_path):
        coordinator = _make_coordinator()
        app_probe = MagicMock()
        app_probe.launch = AsyncMock()
        acceptance_tester = MagicMock()

        results = await coordinator.run_acceptance_tests(
            criteria=[],
            workspace=tmp_path,
            app_probe=app_probe,
            acceptance_tester=acceptance_tester,
        )

        assert results == []
        app_probe.launch.assert_not_called()
