"""Integration tests for the task loop (PlannerAgent + TaskLoop orchestration)."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from agent.agents.planner_agent import PlannerAgent, PlanResult, VALID_AGENT_TYPES
from agent.agents.verifier_agent import VerifierResult
from agent.orchestration.task_loop import TaskLoop, TaskLoopDeps


# ---------------------------------------------------------------------------
# PlannerAgent unit tests
# ---------------------------------------------------------------------------

class TestPlannerAgentParsing:
    """Test JSON parsing and validation without hitting an LLM."""

    def _make_planner(self, response_text: str) -> PlannerAgent:
        model_router = MagicMock()
        model_router.get_model.return_value = MagicMock(name="test-model")
        model_router.generate = AsyncMock(return_value=response_text)
        return PlannerAgent(model_router)

    @pytest.mark.asyncio
    async def test_valid_json_array(self):
        raw = """
[
  {"description": "Check package.json", "agent_type": "develop"},
  {"description": "Run npm start", "agent_type": "develop"},
  {"description": "Fix the error", "agent_type": "develop"}
]
"""
        planner = self._make_planner(raw)
        tasks = await planner.plan("run and debug the game")
        assert len(tasks) == 3
        assert tasks[0]["description"] == "Check package.json"
        assert tasks[0]["agent_type"] == "develop"

    @pytest.mark.asyncio
    async def test_json_in_prose(self):
        raw = """Sure, here is the plan:
[
  {"description": "Search for SQLite docs", "agent_type": "research"},
  {"description": "Synthesize findings", "agent_type": "research"}
]
That should cover it!"""
        planner = self._make_planner(raw)
        tasks = await planner.plan("research SQLite FTS5")
        assert len(tasks) == 2
        assert tasks[0]["agent_type"] == "research"

    @pytest.mark.asyncio
    async def test_invalid_agent_type_coerced_to_develop(self):
        raw = '[{"description": "Do stuff", "agent_type": "robot"}]'
        planner = self._make_planner(raw)
        tasks = await planner.plan("objective")
        assert tasks[0]["agent_type"] == "develop"

    @pytest.mark.asyncio
    async def test_all_valid_agent_types_accepted(self):
        for agent_type in VALID_AGENT_TYPES:
            raw = f'[{{"description": "task", "agent_type": "{agent_type}"}}]'
            planner = self._make_planner(raw)
            tasks = await planner.plan("objective")
            assert tasks[0]["agent_type"] == agent_type

    @pytest.mark.asyncio
    async def test_empty_description_skipped(self):
        raw = '[{"description": "", "agent_type": "develop"}, {"description": "Valid task", "agent_type": "develop"}]'
        planner = self._make_planner(raw)
        tasks = await planner.plan("objective")
        assert len(tasks) == 1
        assert tasks[0]["description"] == "Valid task"

    @pytest.mark.asyncio
    async def test_invalid_json_falls_back(self):
        planner = self._make_planner("not valid json at all")
        tasks = await planner.plan("objective", task_type="develop")
        assert len(tasks) == 1
        assert tasks[0]["agent_type"] == "develop"
        assert "objective" in tasks[0]["description"]

    @pytest.mark.asyncio
    async def test_no_model_falls_back(self):
        model_router = MagicMock()
        model_router.get_model.return_value = None
        planner = PlannerAgent(model_router)
        tasks = await planner.plan("research the codebase", task_type="research")
        assert len(tasks) == 1
        assert tasks[0]["agent_type"] == "research"

    @pytest.mark.asyncio
    async def test_llm_exception_falls_back(self):
        model_router = MagicMock()
        model_router.get_model.return_value = MagicMock()
        model_router.generate = AsyncMock(side_effect=RuntimeError("LLM error"))
        planner = PlannerAgent(model_router)
        tasks = await planner.plan("objective")
        assert len(tasks) >= 1


class TestPlannerAgentStrategy:
    def _make_planner(self) -> PlannerAgent:
        model_router = MagicMock()
        model_router.get_model.return_value = None  # force fallback
        return PlannerAgent(model_router)

    def test_develop_strategy_hint_contains_run(self):
        planner = self._make_planner()
        hint = planner._strategy_hint("develop")
        assert "run" in hint.lower()

    def test_research_strategy_hint_contains_synthesize(self):
        planner = self._make_planner()
        hint = planner._strategy_hint("research")
        assert "synth" in hint.lower()

    def test_fallback_research_task_type(self):
        planner = self._make_planner()
        tasks = planner._fallback_plan("find info", "research")
        assert tasks[0]["agent_type"] == "research"

    def test_fallback_develop_task_type(self):
        planner = self._make_planner()
        tasks = planner._fallback_plan("fix bug", "develop")
        assert tasks[0]["agent_type"] == "develop"


# ---------------------------------------------------------------------------
# Task loop integration: agent/orchestration/task_loop.TaskLoop
# ---------------------------------------------------------------------------
#
# Phase-33 extracted the loop that used to live in
# AgentOrchestrator._run_task_loop into its own TaskLoop class driven by a
# TaskLoopDeps bundle (see agent/orchestration/task_loop.py). These tests
# build that bundle directly with mocked collaborators instead of mocking
# the whole orchestrator, matching the new architecture.

class TestTaskLoop:
    """Test TaskLoop.run without a real LLM, DB, or orchestrator."""

    def _make_task_loop(self, plan_specs, agent_response="Task done.", job_id=None, objective_resolver=None):
        """Build a TaskLoop wired with mocked deps and a real TaskStore."""
        import tempfile
        import os
        from api.task_store import TaskStore

        tmp_fd, tmp_path_db = tempfile.mkstemp(suffix=".db")
        os.close(tmp_fd)
        task_store = TaskStore(db_path=tmp_path_db)

        tool_executor = MagicMock()
        tool_executor.workspace_path = "."
        tool_executor.execute = AsyncMock(return_value="")

        context_builder = MagicMock()
        context_builder.build_planning_context = AsyncMock(return_value="")
        context_builder.char_budget = MagicMock(return_value=5000)
        context_builder.model_router = MagicMock()

        planner_agent = MagicMock()
        planner_agent.plan_with_criteria = AsyncMock(
            return_value=PlanResult(tasks=list(plan_specs))
        )

        plan_reviewer_agent = MagicMock()
        plan_reviewer_agent.review = AsyncMock(side_effect=lambda specs, objective: specs)

        verifier_coordinator = MagicMock()
        verifier_coordinator.run_verification = AsyncMock(
            return_value=VerifierResult(score=8, passed=True)
        )

        session_memory = MagicMock()
        skill_executor = MagicMock()
        skill_executor.execute_post = AsyncMock(return_value={})
        memory_wiki = MagicMock()
        acceptance_tester_agent = MagicMock()

        run_agent_fn = AsyncMock(return_value={
            "success": True,
            "response": agent_response,
            "files_created": [],
            "new_tasks": [],
        })
        drain_switch_fn = MagicMock(return_value=[])

        deps = TaskLoopDeps(
            tool_executor=tool_executor,
            context_builder=context_builder,
            planner_agent=planner_agent,
            plan_reviewer_agent=plan_reviewer_agent,
            task_store=task_store,
            verifier_coordinator=verifier_coordinator,
            criterion_score_store=MagicMock(),
            session_memory=session_memory,
            skill_executor=skill_executor,
            memory_wiki=memory_wiki,
            acceptance_tester_agent=acceptance_tester_agent,
            run_agent_fn=run_agent_fn,
            drain_switch_fn=drain_switch_fn,
            objective_resolver=objective_resolver,
        )
        return TaskLoop(deps), task_store

    @pytest.mark.asyncio
    async def test_loop_executes_all_tasks(self):
        specs = [
            {"description": "Task 1", "agent_type": "develop"},
            {"description": "Task 2", "agent_type": "develop"},
            {"description": "Task 3", "agent_type": "develop"},
        ]
        loop, _ = self._make_task_loop(specs)
        result = await loop.run("objective", "develop", "session1")

        assert result["success"] is True
        assert loop._d.run_agent_fn.call_count == 3

    @pytest.mark.asyncio
    async def test_loop_combines_responses(self):
        specs = [
            {"description": "Step 1", "agent_type": "develop"},
            {"description": "Step 2", "agent_type": "develop"},
        ]
        loop, _ = self._make_task_loop(specs, agent_response="output here")
        result = await loop.run("objective", "develop", "session1")

        assert "Step 1" in result["response"]
        assert "Step 2" in result["response"]
        assert "output here" in result["response"]

    @pytest.mark.asyncio
    async def test_loop_stores_tasks_when_job_id_given(self):
        specs = [
            {"description": "T1", "agent_type": "develop"},
            {"description": "T2", "agent_type": "research"},
        ]
        loop, task_store = self._make_task_loop(specs)

        job_id = "test-job-123"
        await loop.run("objective", "develop", "sess", job_id=job_id)

        tasks = task_store.list_tasks(job_id)
        assert len(tasks) == 2
        assert all(t.status == "done" for t in tasks)

    @pytest.mark.asyncio
    async def test_loop_handles_failed_task_and_continues(self):
        specs = [
            {"description": "Will fail", "agent_type": "develop"},
            {"description": "Will succeed", "agent_type": "develop"},
        ]
        loop, _ = self._make_task_loop(specs)

        call_count = [0]

        async def mock_agent(task, agent_type, session_id, on_phase=None, job_id=None, _direct=False, extra_context=""):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"success": False, "error": "npm not found"}
            return {"success": True, "response": "done", "files_created": [], "new_tasks": []}

        loop._d.run_agent_fn = mock_agent
        result = await loop.run("objective", "develop", "sess")

        # Both tasks were attempted
        assert call_count[0] == 2
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_loop_appends_new_tasks(self):
        specs = [{"description": "Initial task", "agent_type": "develop"}]
        loop, _ = self._make_task_loop(specs)

        call_count = [0]

        async def mock_agent(task, agent_type, session_id, on_phase=None, job_id=None, _direct=False, extra_context=""):
            call_count[0] += 1
            new_tasks = []
            if call_count[0] == 1:
                # First task spawns a follow-up
                new_tasks = [{"description": "Follow-up task", "agent_type": "develop"}]
            return {
                "success": True,
                "response": f"done {call_count[0]}",
                "files_created": [],
                "new_tasks": new_tasks,
            }

        loop._d.run_agent_fn = mock_agent
        result = await loop.run("objective", "develop", "sess")

        # Both the original and the dynamically-added task ran
        assert call_count[0] == 2

    @pytest.mark.asyncio
    async def test_loop_deduplicates_files(self):
        specs = [
            {"description": "T1", "agent_type": "develop"},
            {"description": "T2", "agent_type": "develop"},
        ]
        loop, _ = self._make_task_loop(specs)
        loop._d.run_agent_fn = AsyncMock(return_value={
            "success": True,
            "response": "done",
            "files_created": ["src/app.js"],
            "new_tasks": [],
        })
        result = await loop.run("objective", "develop", "sess")
        assert result["files_created"].count("src/app.js") == 1

    @pytest.mark.asyncio
    async def test_phase_callback_called(self):
        specs = [
            {"description": "Step 1", "agent_type": "develop"},
            {"description": "Step 2", "agent_type": "develop"},
        ]
        loop, _ = self._make_task_loop(specs)
        phases_emitted = []
        await loop.run(
            "objective", "develop", "sess",
            on_phase=lambda p: phases_emitted.append(p)
        )
        task_phases = [p for p in phases_emitted if p.startswith("task:")]
        assert len(task_phases) >= 2

    @pytest.mark.asyncio
    async def test_objective_resolved_before_planning(self):
        """A vague objective is rewritten by objective_resolver before the
        planner ever sees it — the planner must receive the resolved text.
        """
        from agent.orchestration.objective_resolver import ResolvedObjective

        specs = [{"description": "Task 1", "agent_type": "develop"}]
        resolver = MagicMock()
        resolver.resolve = AsyncMock(return_value=ResolvedObjective(
            objective="Initialize SQLite schema in src-tauri/db/ using sqlx",
            context_excerpt="### NEXT_STEPS.md\n- [ ] Initialize SQLite schema",
        ))
        loop, _ = self._make_task_loop(specs, objective_resolver=resolver)

        await loop.run("continue with the next steps", "develop", "sess")

        resolver.resolve.assert_awaited_once()
        planned_objective = loop._d.planner_agent.plan_with_criteria.call_args[0][0]
        assert planned_objective == "Initialize SQLite schema in src-tauri/db/ using sqlx"

    @pytest.mark.asyncio
    async def test_objective_unresolved_when_no_resolver_configured(self):
        """No objective_resolver wired (e.g. older TaskLoopDeps) — planning
        proceeds against the original objective unchanged.
        """
        specs = [{"description": "Task 1", "agent_type": "develop"}]
        loop, _ = self._make_task_loop(specs, objective_resolver=None)

        await loop.run("continue with the next steps", "develop", "sess")

        planned_objective = loop._d.planner_agent.plan_with_criteria.call_args[0][0]
        assert planned_objective == "continue with the next steps"

    @pytest.mark.asyncio
    async def test_objective_resolver_skipped_for_non_develop_task_types(self):
        specs = [{"description": "Task 1", "agent_type": "research"}]
        resolver = MagicMock()
        resolver.resolve = AsyncMock()
        loop, _ = self._make_task_loop(specs, objective_resolver=resolver)

        await loop.run("continue researching", "research", "sess")

        resolver.resolve.assert_not_awaited()
