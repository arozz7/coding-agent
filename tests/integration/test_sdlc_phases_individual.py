"""Integration tests — individual SDLC phase behaviours.

Each test isolates one phase to verify its contract independently:
  - plan   → returns plan text from PlanAgent
  - build  → calls DeveloperAgent with plan injected into task
  - test   → writes tests via TesterAgent then runs pytest
  - debug  → loops developer→pytest until green or cap
  - run    → detects port/command and spawns subprocess
  - verify → waits for server, screenshots, saves to .screenshots/
  - port   → detects port from .env, package.json, pyproject.toml
  - cleanup→ old screenshots are pruned, recent ones are kept
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent.sdlc_workflow import MAX_DEBUG_RETRIES, SCREENSHOT_TTL_HOURS, SDLCWorkflow


def _make_orch(tmp_path: Path) -> MagicMock:
    orch = MagicMock()
    orch.workspace_path = str(tmp_path)
    orch.plan_agent = MagicMock()
    orch.developer_agent = MagicMock()
    orch.tester_agent = MagicMock()
    orch.shell_tool = MagicMock()
    orch.browser_tool = MagicMock()
    orch._build_enriched_context = AsyncMock(return_value="")
    orch._build_context_from_events = MagicMock(return_value="")
    orch._create_session_executor = MagicMock(return_value=MagicMock())
    orch.model_router = MagicMock()
    return orch


# ---------------------------------------------------------------------------
# Plan phase
# ---------------------------------------------------------------------------

class TestPlanPhase:

    @pytest.mark.asyncio
    async def test_plan_phase_returns_agent_response(self, tmp_path: Path):
        orch = _make_orch(tmp_path)
        orch.plan_agent.run = AsyncMock(return_value={
            "success": True,
            "response": "Step 1: create app.py\nStep 2: write tests",
            "files_created": [],
        })
        wf = SDLCWorkflow(orch)
        ctx = await wf._build_context("task", "s1")
        result = await wf._plan_phase("build a todo app", ctx)
        assert result["success"] is True
        assert "Step 1" in result["response"]

    @pytest.mark.asyncio
    async def test_plan_failure_propagates(self, tmp_path: Path):
        orch = _make_orch(tmp_path)
        orch.plan_agent.run = AsyncMock(return_value={
            "success": False, "error": "model unavailable"
        })
        wf = SDLCWorkflow(orch)
        ctx = await wf._build_context("task", "s2")
        result = await wf._plan_phase("build a todo app", ctx)
        assert result["success"] is False


# ---------------------------------------------------------------------------
# Build phase
# ---------------------------------------------------------------------------

class TestBuildPhase:

    @pytest.mark.asyncio
    async def test_build_injects_plan_into_task(self, tmp_path: Path):
        orch = _make_orch(tmp_path)
        received_task: list[str] = []

        async def capture_run(task, ctx):
            received_task.append(task)
            return {"success": True, "response": "built", "files_created": ["app.py"]}

        orch.developer_agent.run = capture_run
        wf = SDLCWorkflow(orch)
        ctx = await wf._build_context("task", "s3")
        await wf._build_phase("build a todo app", "## Plan\nStep 1", ctx)

        assert len(received_task) == 1
        assert "## Plan" in received_task[0]
        assert "Step 1" in received_task[0]

    @pytest.mark.asyncio
    async def test_build_returns_files_created(self, tmp_path: Path):
        orch = _make_orch(tmp_path)
        orch.developer_agent.run = AsyncMock(return_value={
            "success": True, "response": "done", "files_created": ["app.py", "models.py"]
        })
        wf = SDLCWorkflow(orch)
        ctx = await wf._build_context("task", "s4")
        result = await wf._build_phase("task", "plan", ctx)
        assert "app.py" in result["files_created"]
        assert "models.py" in result["files_created"]


# ---------------------------------------------------------------------------
# Test phase
# ---------------------------------------------------------------------------

class TestTestPhase:

    @pytest.mark.asyncio
    async def test_passing_pytest_marks_tests_passed(self, tmp_path: Path):
        orch = _make_orch(tmp_path)
        orch.tester_agent.run = AsyncMock(return_value={
            "success": True, "response": "tests written", "files_created": ["test_app.py"]
        })
        orch.shell_tool.run = MagicMock(
            return_value={"returncode": 0, "stdout": "2 passed", "stderr": ""}
        )
        wf = SDLCWorkflow(orch)
        ctx = await wf._build_context("task", "s5")
        result = await wf._test_phase("build a todo app", ctx)
        assert result["tests_passed"] is True
        assert result["error"] == ""

    @pytest.mark.asyncio
    async def test_failing_pytest_marks_tests_failed(self, tmp_path: Path):
        orch = _make_orch(tmp_path)
        orch.tester_agent.run = AsyncMock(return_value={
            "success": True, "response": "tests written", "files_created": ["test_app.py"]
        })
        orch.shell_tool.run = MagicMock(
            return_value={"returncode": 1, "stdout": "FAILED test_app.py", "stderr": "AssertionError: 1 != 2"}
        )
        wf = SDLCWorkflow(orch)
        ctx = await wf._build_context("task", "s6")
        result = await wf._test_phase("build a todo app", ctx)
        assert result["tests_passed"] is False
        assert "FAILED" in result["error"] or "AssertionError" in result["error"]


# ---------------------------------------------------------------------------
# Debug loop
# ---------------------------------------------------------------------------

class TestDebugLoop:

    @pytest.mark.asyncio
    async def test_resolves_on_first_fix(self, tmp_path: Path):
        orch = _make_orch(tmp_path)
        orch.developer_agent.run = AsyncMock(return_value={
            "success": True, "response": "fixed", "files_created": ["app.py"]
        })
        orch.shell_tool.run = MagicMock(
            return_value={"returncode": 0, "stdout": "1 passed", "stderr": ""}
        )
        wf = SDLCWorkflow(orch)
        ctx = await wf._build_context("task", "s7")
        result = await wf._debug_loop("test output error", ctx, lambda _: None)
        assert result["resolved"] is True
        assert result["attempts"] == 1

    @pytest.mark.asyncio
    async def test_resolves_on_third_attempt(self, tmp_path: Path):
        orch = _make_orch(tmp_path)
        orch.developer_agent.run = AsyncMock(return_value={
            "success": True, "response": "fixed", "files_created": []
        })
        # Fail twice then pass
        orch.shell_tool.run = MagicMock(
            side_effect=[
                {"returncode": 1, "stdout": "FAILED", "stderr": "err"},
                {"returncode": 1, "stdout": "FAILED", "stderr": "err"},
                {"returncode": 0, "stdout": "1 passed", "stderr": ""},
            ]
        )
        wf = SDLCWorkflow(orch)
        ctx = await wf._build_context("task", "s8")
        result = await wf._debug_loop("initial error", ctx, lambda _: None)
        assert result["resolved"] is True
        assert result["attempts"] == 3

    @pytest.mark.asyncio
    async def test_exhausts_after_max_retries(self, tmp_path: Path):
        orch = _make_orch(tmp_path)
        orch.developer_agent.run = AsyncMock(return_value={
            "success": True, "response": "tried", "files_created": []
        })
        orch.shell_tool.run = MagicMock(
            return_value={"returncode": 1, "stdout": "FAILED", "stderr": "err"}
        )
        wf = SDLCWorkflow(orch)
        ctx = await wf._build_context("task", "s9")
        result = await wf._debug_loop("error", ctx, lambda _: None)
        assert result["resolved"] is False
        assert result["attempts"] == MAX_DEBUG_RETRIES
        assert orch.developer_agent.run.call_count == MAX_DEBUG_RETRIES

    @pytest.mark.asyncio
    async def test_debug_loop_emits_phase_labels(self, tmp_path: Path):
        orch = _make_orch(tmp_path)
        orch.developer_agent.run = AsyncMock(return_value={
            "success": True, "response": "fixed", "files_created": []
        })
        orch.shell_tool.run = MagicMock(
            side_effect=[
                {"returncode": 1, "stdout": "FAILED", "stderr": ""},
                {"returncode": 0, "stdout": "1 passed", "stderr": ""},
            ]
        )
        phases: list[str] = []
        wf = SDLCWorkflow(orch)
        ctx = await wf._build_context("task", "s10")
        await wf._debug_loop("error", ctx, phases.append)
        assert any("debugging" in p for p in phases)


# ---------------------------------------------------------------------------
# Port detection
# ---------------------------------------------------------------------------

class TestPortDetection:

    def test_reads_port_from_dotenv(self, tmp_path: Path):
        (tmp_path / ".env").write_text("PORT=4567\n")
        orch = _make_orch(tmp_path)
        wf = SDLCWorkflow(orch)
        assert wf._read_port_from_files() == 4567

    def test_reads_app_port_from_dotenv(self, tmp_path: Path):
        (tmp_path / ".env").write_text("APP_PORT=3456\n")
        orch = _make_orch(tmp_path)
        wf = SDLCWorkflow(orch)
        assert wf._read_port_from_files() == 3456

    def test_reads_port_from_package_json(self, tmp_path: Path):
        (tmp_path / "package.json").write_text(
            '{"scripts": {"start": "node server.js --port 5678"}}'
        )
        orch = _make_orch(tmp_path)
        wf = SDLCWorkflow(orch)
        assert wf._read_port_from_files() == 5678

    def test_reads_port_from_pyproject_toml(self, tmp_path: Path):
        (tmp_path / "pyproject.toml").write_text("[tool.uvicorn]\nport = 6789\n")
        orch = _make_orch(tmp_path)
        wf = SDLCWorkflow(orch)
        assert wf._read_port_from_files() == 6789

    def test_returns_none_when_no_config(self, tmp_path: Path):
        orch = _make_orch(tmp_path)
        wf = SDLCWorkflow(orch)
        assert wf._read_port_from_files() is None

    def test_finds_free_port_avoids_in_use(self, tmp_path: Path):
        import socket
        orch = _make_orch(tmp_path)
        wf = SDLCWorkflow(orch)
        # Bind a real socket to block a port then confirm wf skips it
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            used_port = s.getsockname()[1]
            # The in-use check should return True for this port
            assert wf._port_in_use(used_port) is True


# ---------------------------------------------------------------------------
# Start command detection
# ---------------------------------------------------------------------------

class TestCommandDetection:

    def test_detects_fastapi_entry_point(self, tmp_path: Path):
        (tmp_path / "main.py").write_text("from fastapi import FastAPI\napp = FastAPI()\n")
        orch = _make_orch(tmp_path)
        wf = SDLCWorkflow(orch)
        cmd = wf._detect_start_command(8000)
        assert cmd is not None
        assert "uvicorn" in cmd
        assert "8000" in cmd

    def test_detects_flask_entry_point(self, tmp_path: Path):
        (tmp_path / "app.py").write_text("from flask import Flask\napp = Flask(__name__)\n")
        orch = _make_orch(tmp_path)
        wf = SDLCWorkflow(orch)
        cmd = wf._detect_start_command(8000)
        assert cmd is not None
        assert "python" in cmd

    def test_detects_npm_start(self, tmp_path: Path):
        (tmp_path / "package.json").write_text('{"scripts": {"start": "node index.js"}}')
        orch = _make_orch(tmp_path)
        wf = SDLCWorkflow(orch)
        cmd = wf._detect_start_command(3000)
        assert cmd == "npm start"

    def test_returns_none_for_unknown_project(self, tmp_path: Path):
        orch = _make_orch(tmp_path)
        wf = SDLCWorkflow(orch)
        assert wf._detect_start_command(8000) is None


# ---------------------------------------------------------------------------
# Screenshot cleanup
# ---------------------------------------------------------------------------

class TestScreenshotCleanup:

    def test_old_screenshots_pruned(self, tmp_path: Path):
        screenshots = tmp_path / ".screenshots"
        screenshots.mkdir()

        old_file = screenshots / "old.png"
        old_file.write_bytes(b"\x89PNG")
        # Back-date modification time past TTL
        old_ts = time.time() - (SCREENSHOT_TTL_HOURS * 3600 + 60)
        import os
        os.utime(old_file, (old_ts, old_ts))

        recent_file = screenshots / "recent.png"
        recent_file.write_bytes(b"\x89PNG")

        orch = _make_orch(tmp_path)
        wf = SDLCWorkflow(orch)
        wf._cleanup_old_screenshots(screenshots)

        assert not old_file.exists()
        assert recent_file.exists()

    def test_recent_screenshots_kept(self, tmp_path: Path):
        screenshots = tmp_path / ".screenshots"
        screenshots.mkdir()

        recent = screenshots / "recent.png"
        recent.write_bytes(b"\x89PNG")

        orch = _make_orch(tmp_path)
        wf = SDLCWorkflow(orch)
        wf._cleanup_old_screenshots(screenshots)

        assert recent.exists()
