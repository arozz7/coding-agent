"""HTTP client for the agent REST API, plus backoff/retry helpers."""
from __future__ import annotations

import asyncio
import os
from typing import Optional

import httpx

API_URL = os.getenv("AGENT_API_URL", "http://127.0.0.1:5005")
POLL_INTERVAL = int(os.getenv("BOT_POLL_INTERVAL", "5"))  # seconds

# ---------------------------------------------------------------------------
# Retry helpers
# ---------------------------------------------------------------------------

_BACKOFF_STEPS = [2, 5, 15, 30, 60, 120, 300]


def _backoff(attempt: int) -> float:
    return float(_BACKOFF_STEPS[min(attempt, len(_BACKOFF_STEPS) - 1)])


_RETRIABLE_STATUSES = {429, 502, 503, 504}


async def _http_retry(coro_factory, label: str = "request"):
    """Call ``coro_factory()`` repeatedly until it succeeds.

    Only non-retriable ``httpx.HTTPStatusError`` (e.g. 404, 403) propagates
    immediately.  All transient errors use the exponential-hold backoff curve.
    """
    attempt = 0
    while True:
        try:
            return await coro_factory()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in _RETRIABLE_STATUSES:
                raise
            delay = _backoff(attempt)
            print(
                f"[bot] {label}: HTTP {exc.response.status_code} — "
                f"retry in {delay:.0f}s (attempt {attempt + 1})"
            )
        except (httpx.ConnectError, httpx.TimeoutException, httpx.RemoteProtocolError) as exc:
            delay = _backoff(attempt)
            print(
                f"[bot] {label}: {type(exc).__name__} — "
                f"retry in {delay:.0f}s (attempt {attempt + 1})"
            )
        except Exception:
            raise

        await asyncio.sleep(delay)
        attempt += 1


# ---------------------------------------------------------------------------
# Async API client
# ---------------------------------------------------------------------------

class AgentClient:
    """Thin async wrapper around the agent REST API."""

    def __init__(self, api_url: str = API_URL):
        self.api_url = api_url

    async def _get(self, endpoint: str, **params) -> dict:
        url = f"{self.api_url}{endpoint}"
        return await _http_retry(
            lambda: self._raw_get(url, params or None),
            label=f"GET {endpoint}",
        )

    async def _raw_get(self, url: str, params) -> dict:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(url, params=params)
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, body: dict, timeout: float = 30.0) -> dict:
        url = f"{self.api_url}{path}"
        return await _http_retry(
            lambda: self._raw_post(url, body, timeout),
            label=f"POST {path}",
        )

    async def _raw_post(self, url: str, body: dict, timeout: float) -> dict:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(url, json=body)
            r.raise_for_status()
            return r.json()

    async def _delete(self, path: str) -> dict:
        url = f"{self.api_url}{path}"
        return await _http_retry(
            lambda: self._raw_delete(url),
            label=f"DELETE {path}",
        )

    async def _raw_delete(self, url: str) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.delete(url)
            r.raise_for_status()
            return r.json()

    async def start_task(
        self,
        task: str,
        session_id: Optional[str] = None,
        force_task_type: Optional[str] = None,
    ) -> dict:
        payload: dict = {"task": task}
        if session_id:
            payload["session_id"] = session_id
        if force_task_type:
            payload["force_task_type"] = force_task_type
        return await self._post("/task/start", payload)

    async def get_job(self, job_id: str) -> dict:
        return await self._get(f"/task/{job_id}")

    async def get_job_result(self, job_id: str) -> dict:
        return await self._get(f"/task/{job_id}/result")

    async def cancel_job(self, job_id: str) -> dict:
        return await self._delete(f"/task/{job_id}")

    async def get_job_tasks(self, job_id: str) -> dict:
        return await self._get(f"/task/{job_id}/tasks")

    async def get_file(self, path: str) -> dict:
        return await self._get("/workspace/file", path=path)

    async def set_project(self, name: str) -> dict:
        return await self._post("/workspace/project", {"name": name})

    async def preview_delete_project(self, name: str) -> dict:
        return await self._get(f"/projects/{name}/delete-preview")

    async def delete_project(self, name: str) -> dict:
        return await self._delete(f"/projects/{name}")

    async def get_session_history(self, session_id: str) -> dict:
        return await self._get(f"/sessions/{session_id}")

    async def list_sessions(self) -> dict:
        return await self._get("/sessions")

    async def delete_session(self, session_id: str) -> dict:
        return await self._delete(f"/sessions/{session_id}")

    async def restart(self) -> dict:
        return await self._post("/restart", {}, timeout=10.0)

    async def get_recent_jobs(self, limit: int = 10) -> dict:
        return await self._get("/jobs", limit=limit)

    async def wait_until_reachable(self) -> None:
        """Poll /health until the API responds. Never gives up."""
        attempt = 0
        while True:
            try:
                async with httpx.AsyncClient(timeout=5.0) as c:
                    r = await c.get(f"{self.api_url}/health")
                    r.raise_for_status()
                print(f"[bot] API is reachable at {self.api_url}")
                return
            except Exception as exc:
                delay = _backoff(attempt)
                print(
                    f"[bot] API not reachable ({type(exc).__name__}) — "
                    f"retrying in {delay:.0f}s (attempt {attempt + 1})"
                )
                await asyncio.sleep(delay)
                attempt += 1
