"""Integration tests simulating the full Discord bot interaction pattern.

The Discord bot:
  1. Receives a user message
  2. POSTs to POST /task/start  →  gets job_id immediately
  3. Polls GET /task/{job_id}   →  waits for status="done"
  4. GETs /task/{job_id}/result →  posts the response to Discord

These tests exercise that entire cycle without a real LLM or real Discord.
"""

import asyncio

import pytest

from tests.integration.conftest import make_mock_orchestrator, poll_until_done


class TestStartPollCycle:
    """Core Discord interaction: start → poll → result."""

    @pytest.mark.asyncio
    async def test_start_returns_job_id(self, client):
        ac, store, orch = client
        resp = await ac.post(
            "/task/start",
            json={"task": "hello, how are you?", "session_id": "discord_chan_1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "job_id" in data
        assert "session_id" in data
        assert data["task_type"] in ("chat", "develop", "research", "review", "test", "architect")

    @pytest.mark.asyncio
    async def test_full_poll_cycle_chat(self, client):
        ac, store, orch = client
        # POST
        start = await ac.post(
            "/task/start",
            json={"task": "What is the capital of France?", "session_id": "disc_1"},
        )
        assert start.status_code == 200
        job_id = start.json()["job_id"]

        # Poll
        job = await poll_until_done(ac, job_id)
        assert job["status"] == "done"
        assert job["phase"] == "complete"

        # Full result
        result_resp = await ac.get(f"/task/{job_id}/result")
        assert result_resp.status_code == 200
        result = result_resp.json()
        assert result["status"] == "done"
        assert result["result"] == "Mock response"

    @pytest.mark.asyncio
    async def test_task_type_detection_develop(self, client):
        ac, store, orch = client
        orch._detect_task_type_keyword.return_value = "develop"

        start = await ac.post(
            "/task/start",
            json={"task": "implement a login endpoint"},
        )
        assert start.status_code == 200
        assert start.json()["task_type"] == "develop"

    @pytest.mark.asyncio
    async def test_task_type_detection_research(self, client):
        ac, store, orch = client
        orch._detect_task_type_keyword.return_value = "research"

        start = await ac.post(
            "/task/start",
            json={"task": "how does the auth middleware work?"},
        )
        assert start.status_code == 200
        assert start.json()["task_type"] == "research"

    @pytest.mark.asyncio
    async def test_job_status_endpoint_404_for_unknown(self, client):
        ac, _, _ = client
        resp = await ac.get("/task/nonexistent_job_id")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_result_endpoint_not_done_returns_status(self, client):
        ac, store, orch = client
        # Orchestrator never completes (simulate slow job)
        event = asyncio.Event()

        async def _slow(*args, **kwargs):
            await event.wait()
            return {"success": True, "session_id": "s", "result": {"response": "done", "task_type": "chat", "files_created": []}}

        orch.run_task = _slow

        start = await ac.post("/task/start", json={"task": "slow task"})
        job_id = start.json()["job_id"]

        # Give the background task a moment to start running
        await asyncio.sleep(0.05)

        result_resp = await ac.get(f"/task/{job_id}/result")
        assert result_resp.status_code == 200
        body = result_resp.json()
        assert body["status"] in ("pending", "running")
        assert body["result"] is None

        # Unblock
        event.set()

    @pytest.mark.asyncio
    async def test_cancel_pending_job(self, client):
        ac, store, orch = client
        event = asyncio.Event()

        async def _blocked(*args, **kwargs):
            await event.wait()
            return {"success": True, "session_id": "s", "result": {"response": "r", "task_type": "chat", "files_created": []}}

        orch.run_task = _blocked

        start = await ac.post("/task/start", json={"task": "block me"})
        job_id = start.json()["job_id"]

        # Cancel before completing
        cancel_resp = await ac.delete(f"/task/{job_id}")
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["cancelled"] is True

        event.set()

    @pytest.mark.asyncio
    async def test_list_jobs(self, client):
        ac, _, _ = client
        # Create two jobs
        for task in ("task one", "task two"):
            await ac.post("/task/start", json={"task": task})

        await asyncio.sleep(0.1)

        resp = await ac.get("/jobs")
        assert resp.status_code == 200
        jobs = resp.json()["jobs"]
        assert len(jobs) >= 2


class TestModelSwitch:
    """Simulates !model command from Discord bot."""

    @pytest.mark.asyncio
    async def test_switch_model(self, client):
        ac, _, orch = client
        resp = await ac.post("/models/active", json={"model": "test-model"})
        assert resp.status_code == 200
        assert resp.json()["active_model"] == "test-model"

    @pytest.mark.asyncio
    async def test_switch_unknown_model_returns_404(self, client):
        ac, _, orch = client
        orch.model_router.set_active_model.side_effect = ValueError("Unknown model 'nope'. Available: []")
        resp = await ac.post("/models/active", json={"model": "nope"})
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_clear_active_model(self, client):
        ac, _, orch = client
        resp = await ac.post("/models/active", json={"model": None})
        assert resp.status_code == 200
        assert "Reverted" in resp.json()["message"]

    @pytest.mark.asyncio
    async def test_list_models(self, client):
        ac, _, orch = client
        resp = await ac.get("/models")
        assert resp.status_code == 200
        body = resp.json()
        assert "models" in body
        assert "active_model" in body


class TestHealthEndpoints:
    @pytest.mark.asyncio
    async def test_root(self, client):
        ac, _, _ = client
        resp = await ac.get("/")
        assert resp.status_code == 200
        assert resp.json()["status"] == "running"

    @pytest.mark.asyncio
    async def test_health(self, client):
        ac, _, _ = client
        resp = await ac.get("/health")
        assert resp.status_code == 200
        assert resp.json()["agent_ready"] is True

    @pytest.mark.asyncio
    async def test_readiness(self, client):
        ac, _, orch = client
        resp = await ac.get("/ready")
        assert resp.status_code == 200
        body = resp.json()
        assert "ready" in body
