"""Task execution context + dependency bundle for TaskLoop.

Extracted from task_loop.py: _TaskExecCtx abstracts the job_id vs.
no-persistence differences in task-list bookkeeping, and TaskLoopDeps is
the plain dependency bundle TaskLoop is constructed with. Neither has any
coupling to the loop's control flow itself.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Optional

if TYPE_CHECKING:
    from agent.agents.acceptance_tester_agent import AcceptanceTesterAgent
    from agent.agents.planner_agent import PlannerAgent
    from agent.agents.plan_reviewer_agent import PlanReviewerAgent
    from agent.memory import SessionMemory
    from agent.memory.memory_wiki import MemoryWiki
    from agent.orchestration import VerifierCoordinator
    from agent.orchestration import CriterionScoreStore
    from agent.orchestration.context_builder import ContextBuilder
    from agent.orchestration.objective_resolver import ObjectiveResolver
    from agent.orchestration.run_ledger import RunLedger
    from agent.skills.skill_executor import SkillExecutor


# ---------------------------------------------------------------------------
# Task execution context — abstracts job_id vs no-persistence differences
# ---------------------------------------------------------------------------

class _TaskExecCtx:
    """Wraps task-list operations so loop body is mode-agnostic."""

    def __init__(self, job_id: Optional[str], task_specs: list[dict], task_store: Any) -> None:
        self._job_id = job_id
        self._task_specs = task_specs
        self._task_store = task_store
        self._ptr = 0          # used only in no-persistence mode
        self.total = len(task_specs)

    @property
    def job_id(self) -> Optional[str]:
        return self._job_id

    def fetch_next(self) -> Optional[dict]:
        """Return next pending task dict (id, sequence, description, agent_type) or None."""
        if self._job_id:
            obj = self._task_store.get_next_pending(self._job_id)
            if obj is None:
                return None
            return {
                "task_id": obj.task_id,
                "sequence": obj.sequence,
                "description": obj.description,
                "agent_type": obj.agent_type,
            }
        if self._ptr >= len(self._task_specs):
            return None
        spec = self._task_specs[self._ptr]
        self._ptr += 1
        return {
            "task_id": None,
            "sequence": self._ptr,
            "description": spec["description"],
            "agent_type": spec.get("agent_type", "develop"),
        }

    def add_task(self, description: str, agent_type: str) -> int:
        """Append a new task; return its sequence number."""
        if self._job_id:
            new = self._task_store.create_task(
                job_id=self._job_id, description=description, agent_type=agent_type
            )
            self.total = max(self.total, new.sequence)
            return new.sequence
        self._task_specs.append({"description": description, "agent_type": agent_type})
        self.total = len(self._task_specs)
        return self.total

    def add_tasks(self, specs: list[dict]) -> None:
        for spec in specs:
            self.add_task(spec["description"], spec.get("agent_type", "develop"))

    def mark_running(self, task_id: Optional[str]) -> None:
        if self._job_id and task_id:
            self._task_store.update_task(task_id, "running")

    def mark_done(self, task_id: Optional[str], status: str, summary: str = "") -> None:
        if self._job_id and task_id:
            self._task_store.update_task(task_id, status, summary)

    def persist_tasks(self, task_specs: list[dict]) -> None:
        """Write initial task list to the store (called once at loop start)."""
        if self._job_id:
            self._task_store.create_tasks(self._job_id, task_specs)


# ---------------------------------------------------------------------------
# Dependencies bundle
# ---------------------------------------------------------------------------

@dataclass
class TaskLoopDeps:
    tool_executor: Any
    context_builder: "ContextBuilder"
    planner_agent: "PlannerAgent"
    plan_reviewer_agent: "PlanReviewerAgent"
    task_store: Any
    verifier_coordinator: "VerifierCoordinator"
    criterion_score_store: "CriterionScoreStore"
    session_memory: "SessionMemory"
    skill_executor: "SkillExecutor"
    memory_wiki: "MemoryWiki"
    acceptance_tester_agent: "AcceptanceTesterAgent"
    run_agent_fn: Callable    # orchestrator._run_specialized_agent
    drain_switch_fn: Callable # orchestrator._drain_switch_notices
    objective_resolver: Optional["ObjectiveResolver"] = None
    run_ledger: Optional["RunLedger"] = None
