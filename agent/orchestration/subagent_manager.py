"""subagent_manager — subagent lifecycle (spawn / parallel / query).

Extracted from AgentOrchestrator so the orchestrator is not responsible for
managing isolated subagent contexts.
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from agent.workspace_context import get_workspace

if TYPE_CHECKING:
    from agent.memory import CodebaseMemory, SessionMemory
    from agent.orchestration.context_builder import ContextBuilder

logger = structlog.get_logger()


class SubagentManager:
    """Manages isolated subagent execution contexts."""

    def __init__(
        self,
        agents: dict[str, Any],
        context_builder: "ContextBuilder",
        codebase_memory: "CodebaseMemory",
        session_memory: "SessionMemory",
        EventEmittingExecutor: type,
        tool_executor: Any,
    ) -> None:
        self._agents = agents
        self._context_builder = context_builder
        self._codebase_memory = codebase_memory
        self._session_memory = session_memory
        self._EventEmittingExecutor = EventEmittingExecutor
        self._tool_executor = tool_executor
        self._sessions: dict[str, dict] = {}
        self.logger = logger.bind(component="subagent_manager")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def spawn(
        self,
        task: str,
        role: str = "developer",
        parent_session_id: str | None = None,
        context_limits: dict | None = None,
    ) -> dict:
        """Spawn a subagent with isolated context for a single task."""
        subagent_id = f"subagent_{uuid.uuid4().hex[:8]}"
        self.logger.info("spawning_subagent", subagent_id=subagent_id, role=role, task=task[:100])

        _ws_now = get_workspace()
        self._session_memory.get_or_create_session(subagent_id, _ws_now)
        enriched_context = await self._context_builder.build(task)

        isolated_context = {
            "session_id": subagent_id,
            "parent_session_id": parent_session_id,
            "workspace_path": _ws_now,
            "model_router": self._context_builder.model_router,
            "tool_executor": self._create_executor(subagent_id),
            "enriched_context": enriched_context,
            "context_limits": context_limits or {},
            "is_subagent": True,
        }

        agent = self._select_agent(role)
        try:
            result = await agent.run(task, isolated_context)
            self._sessions[subagent_id] = {
                "id": subagent_id,
                "role": role,
                "task": task,
                "parent_session_id": parent_session_id,
                "result": result,
                "status": "completed" if result.get("success") else "failed",
            }

            if parent_session_id:
                self._session_memory.save_message(
                    parent_session_id,
                    "subagent",
                    f"[{role}] {task[:50]}... -> {result.get('response', '')[:200]}",
                )

            self._merge_rag(result.get("files_created", []), result.get("success"), _ws_now)
            self.logger.info("subagent_completed", subagent_id=subagent_id, status=self._sessions[subagent_id]["status"])
            return {"success": True, "subagent_id": subagent_id, "role": role, "result": result}

        except Exception as exc:
            self.logger.error("subagent_failed", subagent_id=subagent_id, error=str(exc))
            return {"success": False, "subagent_id": subagent_id, "error": str(exc)}

    async def spawn_multiple(
        self,
        tasks: list[str],
        roles: list[str] | None = None,
        parent_session_id: str | None = None,
    ) -> list[dict]:
        """Spawn multiple subagents in parallel."""
        if roles is None:
            roles = ["developer"] * len(tasks)

        results = await asyncio.gather(
            *[self.spawn(task, role, parent_session_id) for task, role in zip(tasks, roles)],
            return_exceptions=True,
        )
        return [
            r if not isinstance(r, Exception) else {"success": False, "error": str(r), "task": tasks[i]}
            for i, r in enumerate(results)
        ]

    def get_result(self, subagent_id: str) -> dict:
        return self._sessions.get(subagent_id, {"error": "Subagent not found"})

    def list_all(self) -> list[dict]:
        return [
            {"id": sa["id"], "role": sa["role"], "task": sa["task"][:50], "status": sa["status"]}
            for sa in self._sessions.values()
        ]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _select_agent(self, role: str) -> Any:
        role_map = {
            "tester": "tester",
            "reviewer": "reviewer",
            "architect": "architect",
            "researcher": "research",
            "chat": "chat",
        }
        return self._agents[role_map.get(role, "developer")]

    def _create_executor(self, session_id: str) -> Any:
        return self._EventEmittingExecutor(
            self._tool_executor,
            self._session_memory,
            session_id,
        )

    def _merge_rag(self, files_created: list[str], success: bool | None, ws: str) -> None:
        if not files_created or not success:
            return
        project_id = Path(ws).name
        for rel_path in files_created:
            abs_path = Path(ws) / rel_path
            if abs_path.exists() and abs_path.is_file():
                try:
                    self._codebase_memory.index_files([str(abs_path)], project_id)
                except Exception as exc:
                    self.logger.warning("subagent_rag_merge_failed", file=rel_path, error=str(exc))
