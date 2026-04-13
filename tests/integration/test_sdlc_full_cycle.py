"""Integration tests — full SDLC pipeline (Plan→Build→Test→Debug→Run→Verify).

Strategy:
  - Mock the LLM (agents) only; everything else is real:
      orchestrator routing, SDLCWorkflow sequencing, ShellTool (mocked for
      safety), BrowserTool (mocked — no real Playwright needed in CI).
  - Tests verify that the *wiring* is correct, not that the LLM is smart.
  - A mock LLM that returns structured responses lets us control each phase
    outcome deterministically.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from agent.sdlc_workflow import MAX_DEBUG_RETRIES, SDLCWorkflow


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_agent_mock(response: str = "Mock response", files: list[str] | None = None) -> MagicMock:
    """Return an agent mock whose .run() resolves immediately."""
    agent = MagicMock()
    agent.run = AsyncMock(
        return_value={
            "success": True,
            "response": response,
            "files_created": files or [],
        }
    )
    return agent


def _make_orchestrator(tmp_path: Path) -> MagicMock:
    """Build a minimal orchestrator mock wired to SDLCWorkflow."""
    orch = MagicMock()
    orch.workspace_path = str(tmp_path)

    # Agents
    orch.plan_agent = _make_agent_mock(response="## Plan\n1. Create app.py\n2. Write tests")
    orch.developer_agent = _make_agent_mock(
        response="Created app.py", files=["app.py"]
    )
    orch.tester_agent = _make_agent_mock(
        response="Created test_app.py", files=["test_app.py"]
    )

    # Shell tool: tests pass on first run
    orch.shell_tool = MagicMock()
    orch.shell_tool.run = MagicMock(
        return_value={"returncode": 0, "stdout": "1 passed", "stderr": ""}
    )

    # Browser tool
    orch.browser_tool = MagicMock()
    orch.browser_tool.wait_for_server = AsyncMock(return_value=True)
    orch.browser_tool.screenshot = AsyncMock(
        return_value={"success": True, "path": str(tmp_path / ".screenshots" / "shot.png")}
    )

    # Context helpers
    orch._build_enriched_context = AsyncMock(return_value="")
    orch._build_context_from_events = MagicMock(return_value="")
    orch._create_session_executor = MagicMock(return_value=MagicMock())
    orch.model_router = MagicMock()

    return orch


# ---------------------------------------------------------------------------
# Test cases
# ---------------------------------------------------------------------------

class TestSDLCFullPipeline:

    @pytest.mark.asyncio
    async def test_happy_path_all_phases_complete(self, tmp_path: Path):
        """All phases run in order and result is success with screenshot."""
        orch = _make_orchestrator(tmp_path)
        workflow = SDLCWorkflow(orch)

        # Create a fake screenshot file so the attachment check passes
        screenshots_dir = tmp_path / ".screenshots"
        screenshots_dir.mkdir()
        fake_shot = screenshots_dir / "job123_9999.png"
        fake_shot.write_bytes(b"\x89PNG")

        orch.browser_tool.screenshot = AsyncMock(
            return_value={"success": True, "path": str(fake_shot)}
        )

        # Patch _run_phase to avoid spawning a real subprocess
        with patch.object(workflow, "_run_phase", new=AsyncMock(return_value={
            "success": True, "port": 9876, "process": MagicMock(), "command": "python app.py"
        })):
            result = await workflow.run("build a full Flask hello-world app", "sess_1", job_id="job123")

        assert result["success"] is True
        inner = result["result"]
        assert inner["task_type"] == "sdlc"
        assert inner["phase"] == "complete"
        assert "app.py" in inner["files_created"]
        assert inner["screenshot_path"] is not None

    @pytest.mark.asyncio
    async def test_plan_phase_called_first(self, tmp_path: Path):
        """PlanAgent must be called before DeveloperAgent."""
        orch = _make_orchestrator(tmp_path)
        call_order: list[str] = []

        async def plan_run(*a, **kw):
            call_order.append("plan")
            return {"success": True, "response": "plan text", "files_created": []}

        async def dev_run(*a, **kw):
            call_order.append("develop")
            return {"success": True, "response": "built", "files_created": ["app.py"]}

        orch.plan_agent.run = plan_run
        orch.developer_agent.run = dev_run
        workflow = SDLCWorkflow(orch)

        with patch.object(workflow, "_run_phase", new=AsyncMock(return_value={
            "success": False, "port": None, "process": None
        })):
            await workflow.run("build a complete app", "sess_order")

        assert call_order.index("plan") < call_order.index("develop")

    @pytest.mark.asyncio
    async def test_tests_pass_skips_debug_loop(self, tmp_path: Path):
        """When pytest passes on first run the debug loop is never entered."""
        orch = _make_orchestrator(tmp_path)
        orch.shell_tool.run = MagicMock(
            return_value={"returncode": 0, "stdout": "2 passed", "stderr": ""}
        )
        workflow = SDLCWorkflow(orch)

        debug_called = False

        async def _debug_loop_spy(*a, **kw):
            nonlocal debug_called
            debug_called = True
            return {"resolved": True, "files_created": [], "attempts": 0}

        workflow._debug_loop = _debug_loop_spy

        with patch.object(workflow, "_run_phase", new=AsyncMock(return_value={
            "success": False, "port": None, "process": None
        })):
            await workflow.run("build a complete app", "sess_skip_debug")

        assert not debug_called

    @pytest.mark.asyncio
    async def test_failing_tests_trigger_debug_loop(self, tmp_path: Path):
        """When pytest fails the debug loop is entered."""
        orch = _make_orchestrator(tmp_path)
        # First call: fail; second call: pass (simulates one fix iteration)
        orch.shell_tool.run = MagicMock(
            side_effect=[
                {"returncode": 1, "stdout": "FAILED test_app.py", "stderr": "AssertionError"},
                {"returncode": 0, "stdout": "1 passed", "stderr": ""},
                {"returncode": 0, "stdout": "1 passed", "stderr": ""},
            ]
        )
        workflow = SDLCWorkflow(orch)

        with patch.object(workflow, "_run_phase", new=AsyncMock(return_value={
            "success": False, "port": None, "process": None
        })):
            result = await workflow.run("build a complete app", "sess_debug")

        # DeveloperAgent called once for build + once for the fix
        assert orch.developer_agent.run.call_count >= 2
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_debug_retries_exhausted_returns_partial_result(self, tmp_path: Path):
        """After MAX_DEBUG_RETRIES failures a clear message is returned (not an exception)."""
        orch = _make_orchestrator(tmp_path)
        # Always failing pytest
        orch.shell_tool.run = MagicMock(
            return_value={"returncode": 1, "stdout": "FAILED", "stderr": "AssertionError"}
        )
        workflow = SDLCWorkflow(orch)

        result = await workflow.run("build a complete app", "sess_exhaust")

        assert result["success"] is True
        inner = result["result"]
        assert inner["phase"] == "debug_exhausted"
        assert "debug_exhausted" in inner["phase"]
        assert MAX_DEBUG_RETRIES.__str__() in inner["response"]

    @pytest.mark.asyncio
    async def test_run_phase_skipped_when_no_entrypoint(self, tmp_path: Path):
        """If _run_phase returns no process the verify phase is skipped gracefully."""
        orch = _make_orchestrator(tmp_path)
        workflow = SDLCWorkflow(orch)

        with patch.object(workflow, "_run_phase", new=AsyncMock(return_value={
            "success": False, "port": None, "process": None, "error": "No entrypoint"
        })):
            result = await workflow.run("build a complete app", "sess_no_run")

        assert result["success"] is True
        assert result["result"]["screenshot_path"] is None

    @pytest.mark.asyncio
    async def test_phase_callback_called_in_order(self, tmp_path: Path):
        """on_phase callback fires in the expected phase sequence."""
        orch = _make_orchestrator(tmp_path)
        workflow = SDLCWorkflow(orch)
        phases_emitted: list[str] = []

        with patch.object(workflow, "_run_phase", new=AsyncMock(return_value={
            "success": False, "port": None, "process": None
        })):
            await workflow.run(
                "build a complete app",
                "sess_phases",
                on_phase=phases_emitted.append,
            )

        assert "sdlc:planning" in phases_emitted
        assert "sdlc:building" in phases_emitted
        assert "sdlc:testing" in phases_emitted
        assert "complete" in phases_emitted

        plan_idx = phases_emitted.index("sdlc:planning")
        build_idx = phases_emitted.index("sdlc:building")
        test_idx = phases_emitted.index("sdlc:testing")
        assert plan_idx < build_idx < test_idx
