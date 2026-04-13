"""Integration tests — Discord bot simulation for the SDLC pipeline.

Simulates the full Discord interaction cycle for SDLC tasks:
  1. User sends  !ask build a complete Flask app
  2. Bot POSTs   /task/start  →  gets job_id, task_type="sdlc"
  3. Bot polls   /task/{job_id} watching for sdlc:* phase transitions
  4. When done   job contains screenshot_path and full response
  5. Bot sends   screenshot as attachment + summary in channel

Uses the same mock orchestrator pattern as conftest.py but with SDLC-specific
return values so no real LLM, Playwright, or subprocess is needed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.integration.conftest import make_mock_orchestrator, poll_until_done


# ---------------------------------------------------------------------------
# SDLC-specific orchestrator mock
# ---------------------------------------------------------------------------

def make_sdlc_orchestrator(
    screenshot_path: str | None = None,
    phase_exhausted: bool = False,
) -> MagicMock:
    """Extend the base mock with SDLC result shape."""
    orch = make_mock_orchestrator(task_type="sdlc", response="**SDLC complete** — all phases passed.")

    inner = {
        "response": "**SDLC complete** — plan, build, test, run, and verify all passed.\n\n"
                    "**Files created:**\n  - `app.py`\n  - `test_app.py`",
        "task_type": "sdlc",
        "files_created": ["app.py", "test_app.py"],
        "phase": "debug_exhausted" if phase_exhausted else "complete",
        "screenshot_path": screenshot_path,
    }

    orch.run_task = AsyncMock(
        return_value={
            "success": True,
            "session_id": "test_session",
            "result": inner,
        }
    )
    orch._detect_task_type_keyword.return_value = "sdlc"
    return orch


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestSDLCDiscordFlow:

    @pytest.mark.asyncio
    async def test_sdlc_task_type_detected(self, client):
        ac, store, orch = client
        orch._detect_task_type_keyword.return_value = "sdlc"

        resp = await ac.post(
            "/task/start",
            json={"task": "build a complete Flask todo app", "session_id": "disc_sdlc_1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["task_type"] == "sdlc"
        assert "job_id" in data

    @pytest.mark.asyncio
    async def test_sdlc_job_completes_with_files(self, client):
        ac, store, orch = client
        orch._detect_task_type_keyword.return_value = "sdlc"
        orch.run_task = AsyncMock(return_value={
            "success": True,
            "session_id": "test_session",
            "result": {
                "response": "SDLC complete",
                "task_type": "sdlc",
                "files_created": ["app.py", "test_app.py"],
                "phase": "complete",
                "screenshot_path": None,
            },
        })

        start = await ac.post(
            "/task/start",
            json={"task": "build a complete Flask app", "session_id": "disc_sdlc_2"},
        )
        job_id = start.json()["job_id"]
        job = await poll_until_done(ac, job_id)

        assert job["status"] == "done"
        assert "app.py" in job["files_created"]
        assert "test_app.py" in job["files_created"]

    @pytest.mark.asyncio
    async def test_sdlc_screenshot_path_stored_in_job(self, client, tmp_path):
        """screenshot_path from the SDLC result is persisted in the job record."""
        ac, store, orch = client
        fake_shot = str(tmp_path / ".screenshots" / "job_abc_123.png")

        orch._detect_task_type_keyword.return_value = "sdlc"
        orch.run_task = AsyncMock(return_value={
            "success": True,
            "session_id": "test_session",
            "result": {
                "response": "SDLC complete",
                "task_type": "sdlc",
                "files_created": ["app.py"],
                "phase": "complete",
                "screenshot_path": fake_shot,
            },
        })

        start = await ac.post(
            "/task/start",
            json={"task": "build a complete app", "session_id": "disc_sdlc_3"},
        )
        job_id = start.json()["job_id"]
        job = await poll_until_done(ac, job_id)

        assert job["status"] == "done"
        assert job.get("screenshot_path") == fake_shot

    @pytest.mark.asyncio
    async def test_sdlc_debug_exhausted_still_returns_done(self, client):
        """Even when retries are exhausted the job finishes as 'done', not 'failed'."""
        ac, store, orch = client
        orch._detect_task_type_keyword.return_value = "sdlc"
        orch.run_task = AsyncMock(return_value={
            "success": True,
            "session_id": "test_session",
            "result": {
                "response": "Could not auto-fix after 5 attempts.",
                "task_type": "sdlc",
                "files_created": ["app.py"],
                "phase": "debug_exhausted",
                "screenshot_path": None,
            },
        })

        start = await ac.post(
            "/task/start",
            json={"task": "build a complete app", "session_id": "disc_sdlc_4"},
        )
        job_id = start.json()["job_id"]
        job = await poll_until_done(ac, job_id)

        assert job["status"] == "done"

    @pytest.mark.asyncio
    async def test_sdlc_phase_updates_visible_during_run(self, client):
        """Phase label transitions from sdlc:planning → sdlc:building etc. are stored."""
        ac, store, orch = client
        phase_sequence = [
            "sdlc:planning", "sdlc:building", "sdlc:testing", "sdlc:running",
            "sdlc:verifying", "complete"
        ]
        phase_iter = iter(phase_sequence)

        # Track on_phase calls
        seen_phases: list[str] = []

        async def run_task_with_phases(task, session_id, include_history, on_phase, job_id=None):
            for phase in phase_sequence:
                seen_phases.append(phase)
                if on_phase:
                    on_phase(phase)
                await asyncio.sleep(0)
            return {
                "success": True,
                "session_id": session_id,
                "result": {
                    "response": "done",
                    "task_type": "sdlc",
                    "files_created": [],
                    "phase": "complete",
                    "screenshot_path": None,
                },
            }

        orch._detect_task_type_keyword.return_value = "sdlc"
        orch.run_task = run_task_with_phases

        start = await ac.post(
            "/task/start",
            json={"task": "build a complete app", "session_id": "disc_sdlc_5"},
        )
        job_id = start.json()["job_id"]
        await poll_until_done(ac, job_id)

        assert "sdlc:planning" in seen_phases
        assert "sdlc:building" in seen_phases
        assert "complete" in seen_phases

    @pytest.mark.asyncio
    async def test_result_endpoint_returns_sdlc_response(self, client):
        """GET /task/{job_id}/result returns the full SDLC prose response."""
        ac, store, orch = client
        orch._detect_task_type_keyword.return_value = "sdlc"
        orch.run_task = AsyncMock(return_value={
            "success": True,
            "session_id": "test_session",
            "result": {
                "response": "**SDLC complete** — plan, build, test, run, verify.",
                "task_type": "sdlc",
                "files_created": ["app.py"],
                "phase": "complete",
                "screenshot_path": None,
            },
        })

        start = await ac.post(
            "/task/start",
            json={"task": "build a complete app", "session_id": "disc_sdlc_6"},
        )
        job_id = start.json()["job_id"]
        await poll_until_done(ac, job_id)

        result_resp = await ac.get(f"/task/{job_id}/result")
        assert result_resp.status_code == 200
        body = result_resp.json()
        assert body["status"] == "done"
        assert "SDLC complete" in body["result"]

    @pytest.mark.asyncio
    async def test_failed_sdlc_stores_error(self, client):
        """A hard failure in the SDLC workflow is stored as a failed job."""
        ac, store, orch = client
        orch._detect_task_type_keyword.return_value = "sdlc"
        orch.run_task = AsyncMock(return_value={
            "success": False,
            "session_id": "test_session",
            "error": "Build phase failed: model unavailable",
        })

        start = await ac.post(
            "/task/start",
            json={"task": "build a complete app", "session_id": "disc_sdlc_7"},
        )
        job_id = start.json()["job_id"]
        job = await poll_until_done(ac, job_id)

        assert job["status"] == "failed"
        assert job.get("error") is not None
