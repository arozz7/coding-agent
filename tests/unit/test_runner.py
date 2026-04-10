"""Unit tests for test runner tool."""
import pytest
import tempfile
import os
from pathlib import Path
from unittest.mock import Mock, patch


class TestPytestTool:
    def test_initialization(self):
        from agent.tools import PytestTool

        with tempfile.TemporaryDirectory() as tmpdir:
            tool = PytestTool(tmpdir)
            assert tool.project_root == Path(tmpdir).resolve()

    def test_run_default_path(self):
        from agent.tools import PytestTool

        with tempfile.TemporaryDirectory() as tmpdir:
            tool = PytestTool(tmpdir)
            result = tool.run(path="tests/unit", verbose=True)
            output = result.get("output", "") + result.get("errors_output", "")
            assert "passed" in output or "failed" in output or result["success"] == False

    def test_run_specific_file(self):
        from agent.tools import PytestTool

        with tempfile.TemporaryDirectory() as tmpdir:
            tool = PytestTool(tmpdir)
            result = tool.run(path="tests/unit/test_runner.py")
            output = result.get("output", "") + result.get("errors_output", "")
            assert "passed" in output or "failed" in output or result["success"] == False

    def test_list_tests(self):
        from agent.tools import PytestTool

        with tempfile.TemporaryDirectory() as tmpdir:
            tool = PytestTool(tmpdir)
            result = tool.list_tests()
            assert "success" in result

    @patch("subprocess.run")
    def test_run_with_marker(self, mock_run):
        from agent.tools import PytestTool

        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = "1 passed"
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        with tempfile.TemporaryDirectory() as tmpdir:
            tool = PytestTool(tmpdir)
            result = tool.run_by_marker("unit")
            assert result["success"] == True
            mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_pytest_not_found(self, mock_run):
        from agent.tools import PytestTool

        mock_run.side_effect = FileNotFoundError()

        with tempfile.TemporaryDirectory() as tmpdir:
            tool = PytestTool(tmpdir)
            result = tool.run()
            assert result["success"] == False
            assert "pytest is not installed" in result["error"]

    @patch("subprocess.run")
    def test_parse_summary_with_failures(self, mock_run):
        from agent.tools import PytestTool

        mock_result = Mock()
        mock_result.returncode = 1
        mock_result.stdout = "5 passed, 2 failed"
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        with tempfile.TemporaryDirectory() as tmpdir:
            tool = PytestTool(tmpdir)
            result = tool.run()
            assert result["passed"] == 5
            assert result["failed"] == 2

    @patch("subprocess.run")
    def test_parse_summary_with_skipped(self, mock_run):
        from agent.tools import PytestTool

        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = "10 passed, 3 skipped"
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        with tempfile.TemporaryDirectory() as tmpdir:
            tool = PytestTool(tmpdir)
            result = tool.run()
            assert result["passed"] == 10
            assert result["skipped"] == 3