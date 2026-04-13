"""Integration tests for the task loop (PlannerAgent + orchestrator routing)."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from agent.agents.planner_agent import PlannerAgent, VALID_AGENT_TYPES


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
# Task loop integration: orchestrator._run_task_loop()
# ---------------------------------------------------------------------------

class TestTaskLoop:
    """Test the orchestrator task loop without a real LLM or DB."""

    def _make_orchestrator(self, plan_specs, agent_response="Task done."):
        """Build a minimal orchestrator mock for loop testing."""
        from agent.orchestrator import AgentOrchestrator

        orch = MagicMock(spec=AgentOrchestrator)

        # Planner returns the given specs
        planner = MagicMock()
        planner.plan = AsyncMock(return_value=plan_specs)
        orch.planner_agent = planner

        # task_store: fully functional using real TaskStore with a temp DB
        import tempfile
        from api.task_store import TaskStore
        tmp_fd, tmp_path_db = tempfile.mkstemp(suffix=".db")
        import os; os.close(tmp_fd)
        orch.task_store = TaskStore(db_path=tmp_path_db)

        # _build_enriched_context returns empty string
        orch._build_enriched_context = AsyncMock(return_value="")
        orch._build_context_from_events = MagicMock(return_value="")

        # _run_specialized_agent: called with _direct=True from the loop
        orch._run_specialized_agent = AsyncMock(return_value={
            "success": True,
            "response": agent_response,
            "files_created": [],
            "new_tasks": [],
        })

        # logger
        orch.logger = MagicMock()

        # Bind the real method
        orch._run_task_loop = AgentOrchestrator._run_task_loop.__get__(orch)

        return orch

    @pytest.mark.asyncio
    async def test_loop_executes_all_tasks(self):
        specs = [
            {"description": "Task 1", "agent_type": "develop"},
            {"description": "Task 2", "agent_type": "develop"},
            {"description": "Task 3", "agent_type": "develop"},
        ]
        orch = self._make_orchestrator(specs)
        result = await orch._run_task_loop("objective", "develop", "session1")

        assert result["success"] is True
        assert orch._run_specialized_agent.call_count == 3

    @pytest.mark.asyncio
    async def test_loop_combines_responses(self):
        specs = [
            {"description": "Step 1", "agent_type": "develop"},
            {"description": "Step 2", "agent_type": "develop"},
        ]
        orch = self._make_orchestrator(specs, agent_response="output here")
        result = await orch._run_task_loop("objective", "develop", "session1")

        assert "Step 1" in result["response"]
        assert "Step 2" in result["response"]
        assert "output here" in result["response"]

    @pytest.mark.asyncio
    async def test_loop_stores_tasks_when_job_id_given(self):
        import tempfile, os
        from api.task_store import TaskStore

        tmp_fd, tmp_path_db = tempfile.mkstemp(suffix=".db")
        os.close(tmp_fd)
        task_store = TaskStore(db_path=tmp_path_db)

        specs = [
            {"description": "T1", "agent_type": "develop"},
            {"description": "T2", "agent_type": "research"},
        ]
        orch = self._make_orchestrator(specs)
        orch.task_store = task_store

        job_id = "test-job-123"
        await orch._run_task_loop("objective", "develop", "sess", job_id=job_id)

        tasks = task_store.list_tasks(job_id)
        assert len(tasks) == 2
        assert all(t.status == "done" for t in tasks)

    @pytest.mark.asyncio
    async def test_loop_handles_failed_task_and_continues(self):
        specs = [
            {"description": "Will fail", "agent_type": "develop"},
            {"description": "Will succeed", "agent_type": "develop"},
        ]
        orch = self._make_orchestrator(specs)

        call_count = [0]

        async def mock_agent(task, agent_type, session_id, on_phase=None, job_id=None, _direct=False):
            call_count[0] += 1
            if call_count[0] == 1:
                return {"success": False, "error": "npm not found"}
            return {"success": True, "response": "done", "files_created": [], "new_tasks": []}

        orch._run_specialized_agent = mock_agent
        result = await orch._run_task_loop("objective", "develop", "sess")

        # Both tasks were attempted
        assert call_count[0] == 2
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_loop_appends_new_tasks(self):
        specs = [{"description": "Initial task", "agent_type": "develop"}]
        orch = self._make_orchestrator(specs)

        call_count = [0]

        async def mock_agent(task, agent_type, session_id, on_phase=None, job_id=None, _direct=False):
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

        orch._run_specialized_agent = mock_agent
        result = await orch._run_task_loop("objective", "develop", "sess")

        # Both the original and the dynamically-added task ran
        assert call_count[0] == 2

    @pytest.mark.asyncio
    async def test_loop_deduplicates_files(self):
        specs = [
            {"description": "T1", "agent_type": "develop"},
            {"description": "T2", "agent_type": "develop"},
        ]
        orch = self._make_orchestrator(specs)
        orch._run_specialized_agent = AsyncMock(return_value={
            "success": True,
            "response": "done",
            "files_created": ["src/app.js"],
            "new_tasks": [],
        })
        result = await orch._run_task_loop("objective", "develop", "sess")
        assert result["files_created"].count("src/app.js") == 1

    @pytest.mark.asyncio
    async def test_phase_callback_called(self):
        specs = [
            {"description": "Step 1", "agent_type": "develop"},
            {"description": "Step 2", "agent_type": "develop"},
        ]
        orch = self._make_orchestrator(specs)
        phases_emitted = []
        await orch._run_task_loop(
            "objective", "develop", "sess",
            on_phase=lambda p: phases_emitted.append(p)
        )
        task_phases = [p for p in phases_emitted if p.startswith("task:")]
        assert len(task_phases) >= 2
