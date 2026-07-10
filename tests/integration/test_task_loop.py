"""Integration tests for the task loop (PlannerAgent + TaskLoop orchestration)."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from agent.agents.planner_agent import PlannerAgent, PlanResult, VALID_AGENT_TYPES
from agent.agents.verifier_agent import VerifierResult
from agent.orchestration.criterion_evaluator import CriterionResult
from agent.orchestration.task_loop import TaskLoop, TaskLoopDeps


def _repeat_last(values):
    """AsyncMock side_effect helper: yield each value in order, then repeat the last forever."""
    def _side_effect(*args, **kwargs):
        idx = _side_effect.calls
        _side_effect.calls += 1
        return values[min(idx, len(values) - 1)]
    _side_effect.calls = 0
    return _side_effect


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

    @pytest.mark.asyncio
    async def test_develop_plan_capped_regardless_of_llm_output(self):
        """Small local models don't reliably respect a '3-6 tasks' prompt
        instruction. Truncate at the boundary rather than trusting the model,
        so an over-long plan can't burn cycles across many small-model calls
        for what should be one narrow deliverable.
        """
        raw = "[" + ",".join(
            f'{{"description": "Task {i}", "agent_type": "develop"}}' for i in range(12)
        ) + "]"
        planner = self._make_planner(raw)
        tasks = await planner.plan("build a feature", task_type="develop")
        assert len(tasks) <= 6


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

def _build_task_loop(
    plan_specs,
    agent_response="Task done.",
    job_id=None,
    objective_resolver=None,
    completion_criteria=None,
    acceptance_criteria=None,
    verifier_results=None,
    evaluate_criteria_results=None,
):
    """Build a TaskLoop wired with mocked deps and a real TaskStore.

    verifier_results: optional list of VerifierResult to return from
      successive run_verification() calls (last value repeats once
      exhausted). Defaults to always-passing.
    evaluate_criteria_results: optional list of CriterionResult lists to
      return from successive evaluate_criteria() calls.
    """
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
        return_value=PlanResult(
            tasks=list(plan_specs),
            completion_criteria=completion_criteria or [],
            acceptance_criteria=acceptance_criteria or [],
        )
    )

    plan_reviewer_agent = MagicMock()
    plan_reviewer_agent.review = AsyncMock(side_effect=lambda specs, objective: specs)

    verifier_coordinator = MagicMock()
    if verifier_results is not None:
        verifier_coordinator.run_verification = AsyncMock(side_effect=_repeat_last(verifier_results))
    else:
        verifier_coordinator.run_verification = AsyncMock(
            return_value=VerifierResult(score=8, passed=True)
        )
    if evaluate_criteria_results is not None:
        verifier_coordinator.evaluate_criteria = AsyncMock(side_effect=_repeat_last(evaluate_criteria_results))
    else:
        verifier_coordinator.evaluate_criteria = AsyncMock(return_value=[])
    verifier_coordinator.make_fix_specs = MagicMock(return_value=[
        {"description": "Fix gap", "agent_type": "develop"}
    ])

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


class TestTaskLoop:
    """Test TaskLoop.run without a real LLM, DB, or orchestrator."""

    _make_task_loop = staticmethod(_build_task_loop)

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


class TestRunLedgerWiring:

    _make_task_loop = staticmethod(_build_task_loop)

    @pytest.mark.asyncio
    async def test_records_run_outcome_for_develop_objectives(self):
        specs = [{"description": "Task 1", "agent_type": "develop"}]
        ledger = MagicMock()
        loop, _ = self._make_task_loop(specs, verifier_results=[VerifierResult(score=8, passed=True)])
        loop._d = loop._d.__class__(**{**loop._d.__dict__, "run_ledger": ledger})

        await loop.run("build the app", "develop", "sess", job_id="job1")

        ledger.record.assert_called_once()
        _, kwargs = ledger.record.call_args
        assert kwargs["tasks_completed"] == 1
        assert kwargs["verifier_score"] == 8

    @pytest.mark.asyncio
    async def test_skipped_when_no_ledger_configured(self):
        specs = [{"description": "Task 1", "agent_type": "develop"}]
        loop, _ = self._make_task_loop(specs)
        # Default deps have run_ledger=None — should not raise.
        result = await loop.run("build the app", "develop", "sess", job_id="job1")
        assert result["success"] is True

    @pytest.mark.asyncio
    async def test_not_recorded_for_research_objectives(self):
        specs = [{"description": "Task 1", "agent_type": "research"}]
        ledger = MagicMock()
        loop, _ = self._make_task_loop(specs)
        loop._d = loop._d.__class__(**{**loop._d.__dict__, "run_ledger": ledger})

        await loop.run("research something", "research", "sess", job_id="job1")

        ledger.record.assert_not_called()


# ---------------------------------------------------------------------------
# Verifier quality gate: criteria passing must not declare victory alone
# ---------------------------------------------------------------------------
#
# Regression coverage for the bug seen in production logs: auto-checkable
# criteria (file exists / file contains / command exits 0) were satisfied —
# sometimes via literal string-insertion fix tasks — while the holistic
# verifier scored the same output 0-2/10. TaskLoop reported success anyway.

class TestVerifierQualityGate:

    _make_task_loop = staticmethod(_build_task_loop)

    @pytest.mark.asyncio
    async def test_criteria_pass_and_verifier_passes_reports_done_cleanly(self):
        specs = [{"description": "Task 1", "agent_type": "develop"}]
        loop, _ = self._make_task_loop(
            specs,
            completion_criteria=["file exists: index.js"],
            evaluate_criteria_results=[[CriterionResult(criterion="file exists: index.js", passed=True)]],
            verifier_results=[VerifierResult(score=8, passed=True)],
        )
        result = await loop.run("build the app", "develop", "sess", job_id="job1")

        assert "needs review" not in result["job_summary"].lower()
        assert result["job_summary"].startswith("**1/1 tasks completed**")

    @pytest.mark.asyncio
    async def test_criteria_pass_but_low_verifier_score_injects_fix_and_flags_review(self):
        specs = [{"description": "Task 1", "agent_type": "develop"}]
        loop, _ = self._make_task_loop(
            specs,
            completion_criteria=["file exists: index.js"],
            evaluate_criteria_results=[[CriterionResult(criterion="file exists: index.js", passed=True)]],
            # Verifier never passes — stagnation eventually stops the gate loop.
            verifier_results=[
                VerifierResult(score=1, passed=False, gaps=["stub output"]),
                VerifierResult(score=1, passed=False, gaps=["stub output"]),
            ],
        )
        result = await loop.run("build the app", "develop", "sess", job_id="job1")

        # run_agent_fn was called for the original task plus at least one
        # injected verifier-gate fix task.
        assert loop._d.run_agent_fn.await_count >= 2
        assert "needs review" in result["job_summary"].lower()
        assert "1/10" in result["job_summary"]

    @pytest.mark.asyncio
    async def test_quality_gate_exhaustion_does_not_reinject_but_lets_acceptance_loop_run(self):
        """Regression test for logs/api-20260710-100353.log, refined after
        advisor review found the first version of this fix wrong: an early
        draft made a stagnation stop `break` the whole run, but the real log
        showed the acceptance-criterion loop converging 3/6 -> 6/6 criteria
        over several rounds *starting right where stagnation first
        triggered* — a hard break would have denied it that turn entirely.
        The correct fix is narrower: once the holistic quality gate
        stagnates, stop re-injecting ITS OWN fix tasks (that was the actual
        waste — repeated "stopping" messages that didn't stop anything) but
        let the acceptance loop keep running on its own budget, since it was
        the mechanism actually making progress.
        """
        specs = [{"description": "Task 1", "agent_type": "develop"}]
        loop, _ = self._make_task_loop(
            specs,
            completion_criteria=["file exists: index.js"],
            acceptance_criteria=["command exits 0: npm run build"],
            evaluate_criteria_results=[[CriterionResult(criterion="file exists: index.js", passed=True)]],
            # score 3 then a 2-point drop to 1 — triggers check_stagnation's
            # "dropped >= 2 pts" stop condition on the second gate pass.
            verifier_results=[
                VerifierResult(score=3, passed=False, gaps=["gap"]),
                VerifierResult(score=1, passed=False, gaps=["gap"]),
            ],
        )

        result = await loop.run("build the app", "develop", "sess", job_id="job1")

        gate_lines = [s for s in result["job_summary"].splitlines() if "Verifier gate" in s]
        assert len(gate_lines) == 1, "gate must stop re-injecting its own fix after stagnating once"
        assert "acceptance" in result["job_summary"].lower(), "acceptance loop must still get to run"
        assert "needs review" in result["job_summary"].lower()

    @pytest.mark.asyncio
    async def test_verifier_round_budget_exhaustion_without_stagnation_still_lets_acceptance_run(self):
        """The other path into the quality-gate exhaustion flag: scores that
        oscillate without ever tripping check_stagnation's drop/plateau
        conditions still exhaust the verifier-round budget eventually. Must
        behave the same as the stagnation-triggered path — stop
        re-injecting once the round budget runs out, but still let the
        acceptance loop run — not just the narrower stagnation-drop case.
        """
        specs = [{"description": "Task 1", "agent_type": "develop"}]
        # Bounces 3<->4 forever: no 2pt drop, no same-score plateau at >=5 —
        # check_stagnation never returns stop=True for this sequence. Only
        # the round-budget check (_verifier_rounds < _MAX_VERIFIER_ROUNDS)
        # ends the re-injection.
        oscillating = [VerifierResult(score=s, passed=False, gaps=["gap"]) for s in [3, 4, 3, 4, 3, 4, 3, 4]]
        loop, _ = self._make_task_loop(
            specs,
            completion_criteria=["file exists: index.js"],
            acceptance_criteria=["command exits 0: npm run build"],
            evaluate_criteria_results=[[CriterionResult(criterion="file exists: index.js", passed=True)]],
            verifier_results=oscillating,
        )

        result = await loop.run("build the app", "develop", "sess", job_id="job1")

        gate_lines = [s for s in result["job_summary"].splitlines() if "Verifier gate" in s]
        # Bounded by _MAX_VERIFIER_ROUNDS (default 6) — must stop growing,
        # not keep injecting a fix on every one of the 8 provided scores.
        assert 0 < len(gate_lines) < 8
        assert "acceptance" in result["job_summary"].lower(), "acceptance loop must still get to run"
        assert "needs review" in result["job_summary"].lower()

    @pytest.mark.asyncio
    async def test_completed_count_excludes_status_lines_not_real_tasks(self):
        """Regression test for the '9/7 tasks completed' miscount: the header
        must reflect real executed tasks, not every checkmarked status line
        (criteria-satisfied, acceptance-satisfied) appended to the summary.
        """
        specs = [
            {"description": "Task 1", "agent_type": "develop"},
            {"description": "Task 2", "agent_type": "develop"},
        ]
        loop, _ = self._make_task_loop(
            specs,
            completion_criteria=["file exists: index.js"],
            evaluate_criteria_results=[[CriterionResult(criterion="file exists: index.js", passed=True)]],
            verifier_results=[VerifierResult(score=8, passed=True)],
        )
        result = await loop.run("build the app", "develop", "sess", job_id="job1")

        # 2 real tasks executed; the criteria-satisfied line is a status
        # line, not a third completed task.
        assert result["job_summary"].startswith("**2/2 tasks completed**")
