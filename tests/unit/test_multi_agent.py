"""Unit tests for multi-agent workflow."""
import pytest
from unittest.mock import Mock, AsyncMock, patch
from typing import List

from agent.multi_agent import (
    MultiAgentOrchestrator,
    MultiAgentState,
    TaskStatus,
    AgentConfig,
)
from agent.multi_agent.workflow import PlannerNode, ExecutorNode, ReviewerNode


class TestTaskStatus:
    def test_task_status_values(self):
        assert TaskStatus.PENDING.value == "pending"
        assert TaskStatus.PLANNED.value == "planned"
        assert TaskStatus.EXECUTING.value == "executing"
        assert TaskStatus.REVIEWING.value == "reviewing"
        assert TaskStatus.COMPLETED.value == "completed"
        assert TaskStatus.FAILED.value == "failed"


class TestMultiAgentState:
    def test_state_creation(self):
        state: MultiAgentState = {
            "task": "test task",
            "plan": None,
            "execution_results": [],
            "review_result": None,
            "status": TaskStatus.PENDING,
            "iterations": 0,
            "final_response": None,
            "session_id": "test_session",
        }
        assert state["task"] == "test task"
        assert state["status"] == TaskStatus.PENDING


class TestPlannerNode:
    @pytest.mark.asyncio
    async def test_planner_creates_plan(self):
        mock_router = Mock()
        mock_model = Mock()
        mock_model.name = "test_model"
        mock_router.get_model.return_value = mock_model
        mock_router.generate = AsyncMock(return_value="Step 1: Do something")
        
        mock_memory = Mock()
        
        config = AgentConfig(
            workspace_path="./workspace",
            model_router=mock_router,
            session_memory=mock_memory,
        )
        
        planner = PlannerNode(config)
        
        state: MultiAgentState = {
            "task": "test task",
            "plan": None,
            "execution_results": [],
            "review_result": None,
            "status": TaskStatus.PENDING,
            "iterations": 0,
            "final_response": None,
            "session_id": "test",
        }
        
        result = await planner.execute(state)
        
        assert result["plan"] is not None
        assert result["status"] == TaskStatus.PLANNED
        mock_router.generate.assert_called_once()

    @pytest.mark.asyncio
    async def test_planner_no_model(self):
        mock_router = Mock()
        mock_router.get_model.return_value = None
        mock_memory = Mock()
        
        config = AgentConfig(
            workspace_path="./workspace",
            model_router=mock_router,
            session_memory=mock_memory,
        )
        
        planner = PlannerNode(config)
        
        state: MultiAgentState = {
            "task": "test task",
            "plan": None,
            "execution_results": [],
            "review_result": None,
            "status": TaskStatus.PENDING,
            "iterations": 0,
            "final_response": None,
            "session_id": "test",
        }
        
        result = await planner.execute(state)
        
        assert result["status"] == TaskStatus.FAILED
        assert "No coding model configured" in result["final_response"]


class TestExecutorNode:
    @pytest.mark.asyncio
    async def test_executor_runs_task(self):
        mock_router = Mock()
        mock_model = Mock()
        mock_model.name = "test_model"
        mock_router.get_model.return_value = mock_model
        mock_router.generate = AsyncMock(return_value="Executed successfully")
        
        mock_memory = Mock()
        
        config = AgentConfig(
            workspace_path="./workspace",
            model_router=mock_router,
            session_memory=mock_memory,
        )
        
        executor = ExecutorNode(config)
        
        state: MultiAgentState = {
            "task": "test task",
            "plan": "Step 1: Do it",
            "execution_results": [],
            "review_result": None,
            "status": TaskStatus.PENDING,
            "iterations": 0,
            "final_response": None,
            "session_id": "test",
        }
        
        result = await executor.execute(state)
        
        assert len(result["execution_results"]) == 1
        assert result["status"] == TaskStatus.REVIEWING


class TestReviewerNode:
    @pytest.mark.asyncio
    async def test_reviewer_approves(self):
        mock_router = Mock()
        mock_model = Mock()
        mock_model.name = "test_model"
        mock_router.get_model.return_value = mock_model
        mock_router.generate = AsyncMock(return_value="YES")
        
        mock_memory = Mock()
        
        config = AgentConfig(
            workspace_path="./workspace",
            model_router=mock_router,
            session_memory=mock_memory,
            max_iterations=3,
        )
        
        reviewer = ReviewerNode(config)
        
        state: MultiAgentState = {
            "task": "test task",
            "plan": "Step 1: Do it",
            "execution_results": [{"iteration": 1, "result": "Done"}],
            "review_result": None,
            "status": TaskStatus.REVIEWING,
            "iterations": 0,
            "final_response": None,
            "session_id": "test",
        }
        
        result = await reviewer.execute(state)
        
        assert result["status"] == TaskStatus.COMPLETED
        assert result["final_response"] == "Done"
        assert result["iterations"] == 1

    @pytest.mark.asyncio
    async def test_reviewer_rejects(self):
        mock_router = Mock()
        mock_model = Mock()
        mock_model.name = "test_model"
        mock_router.get_model.return_value = mock_model
        mock_router.generate = AsyncMock(return_value="NO - needs more work")
        
        mock_memory = Mock()
        
        config = AgentConfig(
            workspace_path="./workspace",
            model_router=mock_router,
            session_memory=mock_memory,
            max_iterations=3,
        )
        
        reviewer = ReviewerNode(config)
        
        state: MultiAgentState = {
            "task": "test task",
            "plan": "Step 1: Do it",
            "execution_results": [{"iteration": 1, "result": "Done"}],
            "review_result": None,
            "status": TaskStatus.REVIEWING,
            "iterations": 0,
            "final_response": None,
            "session_id": "test",
        }
        
        result = await reviewer.execute(state)
        
        assert result["status"] == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_reviewer_max_iterations(self):
        mock_router = Mock()
        mock_model = Mock()
        mock_model.name = "test_model"
        mock_router.get_model.return_value = mock_model
        mock_router.generate = AsyncMock(return_value="NO - needs more work")
        
        mock_memory = Mock()
        
        config = AgentConfig(
            workspace_path="./workspace",
            model_router=mock_router,
            session_memory=mock_memory,
            max_iterations=2,
        )
        
        reviewer = ReviewerNode(config)
        
        state: MultiAgentState = {
            "task": "test task",
            "plan": "Step 1: Do it",
            "execution_results": [
                {"iteration": 1, "result": "Done"},
                {"iteration": 2, "result": "Done again"},
            ],
            "review_result": None,
            "status": TaskStatus.REVIEWING,
            "iterations": 2,
            "final_response": None,
            "session_id": "test",
        }
        
        result = await reviewer.execute(state)
        
        assert result["status"] == TaskStatus.COMPLETED
        assert "Max iterations reached" in result["final_response"]


class TestMultiAgentOrchestrator:
    def test_orchestrator_initialization(self):
        mock_router = Mock()
        mock_session_memory = Mock()
        
        with patch("agent.multi_agent.workflow.SessionMemory") as mock_session:
            mock_session.return_value = mock_session_memory
            
            orchestrator = MultiAgentOrchestrator(
                workspace_path="./workspace",
                model_router=mock_router,
                session_db_path=":memory:",
                max_iterations=3,
            )
            
            assert orchestrator.workspace_path == "./workspace"
            assert orchestrator.model_router == mock_router

    @pytest.mark.asyncio
    async def test_run_task_calls_workflow(self):
        mock_router = Mock()
        mock_session_memory = Mock()
        
        with patch("agent.multi_agent.workflow.SessionMemory") as mock_session:
            mock_session.return_value = mock_session_memory
            
            orchestrator = MultiAgentOrchestrator(
                workspace_path="./workspace",
                model_router=mock_router,
                session_db_path=":memory:",
            )
            
            with patch.object(orchestrator.workflow, "run") as mock_run:
                mock_run.return_value = {
                    "success": True,
                    "session_id": "test",
                    "result": {"response": "Done"},
                }
                
                result = await orchestrator.run_task("test task", "test_session")
                
                assert result["success"] == True
                mock_run.assert_called_once_with("test task", "test_session")