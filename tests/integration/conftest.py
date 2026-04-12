"""Shared fixtures for integration tests.

Strategy:
  - Patch `local_coding_agent.create_agent` so the startup event never hits
    real Ollama / model configs.
  - Patch `api.main._job_store` with a fresh SQLite store rooted in tmp_path
    so tests are isolated and never touch data/jobs.db.
  - Expose a pre-configured AsyncClient via `client` fixture.
"""

import asyncio
from typing import AsyncGenerator, Tuple
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from api.job_store import JobStore


def make_mock_orchestrator(task_type: str = "chat", response: str = "Mock response"):
    """Build a realistic mock AgentOrchestrator for the given task_type."""
    orch = MagicMock()
    orch._detect_task_type_keyword.return_value = task_type
    orch.run_task = AsyncMock(
        return_value={
            "success": True,
            "session_id": "test_session",
            "result": {
                "response": response,
                "task_type": task_type,
                "files_created": [],
            },
        }
    )
    orch.run_stream = AsyncMock(return_value=iter([]))
    orch.list_sessions.return_value = []
    orch.get_session_history.return_value = []
    orch.list_subagents.return_value = []
    orch.index_workspace.return_value = {"indexed": 0}

    # Model router
    mock_config = MagicMock()
    mock_config.name = "test-model"
    mock_config.type = "local"
    mock_config.endpoint = "http://localhost:11434"
    mock_config.is_coding_optimized = True
    mock_config.context_window = 4096

    router = MagicMock()
    router.get_active_model_name.return_value = "test-model"
    router.get_model.return_value = mock_config
    router.configs = [mock_config]
    router.set_active_model.return_value = mock_config
    router.get_cost_summary.return_value = {}
    router.get_healthy_models.return_value = ["test-model"]
    router.health_check = AsyncMock(return_value=True)
    orch.model_router = router

    # session_memory (used by DELETE /sessions/{id})
    orch.session_memory = MagicMock()
    orch.session_memory.delete_session.return_value = True

    # codebase_memory
    orch.codebase_memory = MagicMock()
    orch.codebase_memory.search_files.return_value = []
    orch.codebase_memory.get_stats.return_value = {}

    # memory_wiki
    orch.memory_wiki = MagicMock()
    orch.memory_wiki.get_statistics.return_value = {}
    orch.memory_wiki.lint.return_value = []

    # skill_manager
    orch.skill_manager = MagicMock()
    orch.skill_manager.list_skills.return_value = []
    orch.skill_manager.fetch_remote = MagicMock(return_value={"fetched": 0})

    return orch


@pytest_asyncio.fixture
async def test_store(tmp_path) -> JobStore:
    """A fresh JobStore backed by a temp SQLite file."""
    store = JobStore(str(tmp_path / "test_jobs.db"))
    store.load()
    return store


@pytest_asyncio.fixture
async def client(test_store) -> AsyncGenerator[Tuple[AsyncClient, JobStore, MagicMock], None]:
    """ASGI test client with mocked agent and isolated job store.

    httpx ASGITransport does not trigger ASGI lifespan events, so we
    patch the module-level globals directly instead of relying on the
    startup handler to call create_agent.

    Yields: (AsyncClient, JobStore, mock_orchestrator)
    """
    from api.main import app

    mock_orch = make_mock_orchestrator()

    with (
        patch("api.main._orchestrator", mock_orch),
        patch("api.main._job_store", test_store),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as ac:
            yield ac, test_store, mock_orch


async def poll_until_done(
    ac: AsyncClient,
    job_id: str,
    timeout: float = 5.0,
    interval: float = 0.05,
) -> dict:
    """Poll GET /task/{job_id} until status is terminal. Returns final job dict."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = await ac.get(f"/task/{job_id}")
        assert resp.status_code == 200, f"poll failed: {resp.text}"
        job = resp.json()
        if job["status"] in ("done", "failed", "cancelled"):
            return job
        await asyncio.sleep(interval)
    raise TimeoutError(f"Job {job_id!r} did not finish within {timeout}s")
