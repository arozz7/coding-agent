"""Integration tests for session continuity.

Validates that:
  - Two tasks sent with the same session_id both complete successfully.
  - The second call's run_task receives the same session_id.
  - Session listing and retrieval endpoints work.
"""

import asyncio

import pytest

from tests.integration.conftest import poll_until_done


class TestSessionContinuity:
    @pytest.mark.asyncio
    async def test_same_session_id_reused(self, client):
        ac, store, orch = client
        session_id = "discord_channel_42"

        # First message
        r1 = await ac.post(
            "/task/start",
            json={"task": "Who won the World Series?", "session_id": session_id},
        )
        assert r1.status_code == 200
        assert r1.json()["session_id"] == session_id

        job1 = await poll_until_done(ac, r1.json()["job_id"])
        assert job1["status"] == "done"

        # Second message — same session
        r2 = await ac.post(
            "/task/start",
            json={"task": "Tell me more about that.", "session_id": session_id},
        )
        assert r2.status_code == 200
        assert r2.json()["session_id"] == session_id

        job2 = await poll_until_done(ac, r2.json()["job_id"])
        assert job2["status"] == "done"

    @pytest.mark.asyncio
    async def test_run_task_called_with_session_id(self, client):
        ac, store, orch = client
        session_id = "sticky_session_99"

        r = await ac.post(
            "/task/start",
            json={"task": "What is Python?", "session_id": session_id},
        )
        await poll_until_done(ac, r.json()["job_id"])

        # run_task must have been called with the correct session_id
        call_kwargs = orch.run_task.call_args
        assert call_kwargs is not None
        # keyword arg or positional
        args, kwargs = call_kwargs
        passed_session = kwargs.get("session_id") or (args[1] if len(args) > 1 else None)
        assert passed_session == session_id

    @pytest.mark.asyncio
    async def test_auto_generated_session_id_when_not_provided(self, client):
        ac, _, _ = client
        # When no session_id is provided, the API generates one automatically
        r = await ac.post("/task/start", json={"task": "task one"})
        assert r.status_code == 200
        session_id = r.json()["session_id"]
        assert session_id  # non-empty
        assert isinstance(session_id, str)
        assert "session_" in session_id  # matches generated format

    @pytest.mark.asyncio
    async def test_include_history_flag_passed(self, client):
        ac, _, orch = client

        r = await ac.post(
            "/task/start",
            json={"task": "question", "session_id": "sess_h", "include_history": True},
        )
        await poll_until_done(ac, r.json()["job_id"])

        _, kwargs = orch.run_task.call_args
        assert kwargs.get("include_history") is True


class TestSessionEndpoints:
    @pytest.mark.asyncio
    async def test_list_sessions(self, client):
        ac, _, orch = client
        orch.list_sessions.return_value = [
            {"session_id": "s1", "message_count": 3, "status": "active", "created_at": "2026-01-01"}
        ]
        resp = await ac.get("/sessions")
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "s1"

    @pytest.mark.asyncio
    async def test_get_session_history(self, client):
        ac, _, orch = client
        orch.get_session_history.return_value = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        resp = await ac.get("/sessions/my_session")
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_id"] == "my_session"
        assert len(body["history"]) == 2

    @pytest.mark.asyncio
    async def test_delete_session(self, client):
        ac, _, orch = client
        orch.session_memory.delete_session.return_value = True
        resp = await ac.delete("/sessions/my_session")
        assert resp.status_code == 200
        assert resp.json()["success"] is True

    @pytest.mark.asyncio
    async def test_delete_nonexistent_session_404(self, client):
        ac, _, orch = client
        orch.session_memory.delete_session.return_value = False
        resp = await ac.delete("/sessions/does_not_exist")
        assert resp.status_code == 404
