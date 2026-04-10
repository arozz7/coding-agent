from typing import TypedDict, Annotated, List, Optional
from dataclasses import dataclass
from enum import Enum
import structlog

from llm import ModelRouter, ModelConfig
from agent.tools import FileSystemTool, GitTool, PytestTool, CodeAnalyzer
from agent.memory import SessionMemory

logger = structlog.get_logger()


class TaskStatus(Enum):
    PENDING = "pending"
    PLANNED = "planned"
    EXECUTING = "executing"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    FAILED = "failed"


class MultiAgentState(TypedDict):
    task: str
    plan: Optional[str]
    execution_results: List[dict]
    review_result: Optional[str]
    status: TaskStatus
    iterations: int
    final_response: Optional[str]
    session_id: str


@dataclass
class AgentConfig:
    workspace_path: str
    model_router: ModelRouter
    session_memory: SessionMemory
    max_iterations: int = 3


class PlannerNode:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.logger = logger.bind(component="planner")

    async def execute(self, state: MultiAgentState) -> MultiAgentState:
        task = state["task"]
        self.logger.info("planning", task=task)
        
        model = self.config.model_router.get_model("coding")
        if not model:
            state["status"] = TaskStatus.FAILED
            state["final_response"] = "No coding model configured"
            return state
        
        prompt = f"""Analyze this task and create a step-by-step execution plan:

Task: {task}

Provide a clear, numbered plan with specific steps. Each step should be actionable."""

        try:
            response = await self.config.model_router.generate(prompt, model)
            state["plan"] = response
            state["status"] = TaskStatus.PLANNED
            self.logger.info("plan_created", plan_length=len(response))
        except Exception as e:
            self.logger.error("planning_failed", error=str(e))
            state["status"] = TaskStatus.FAILED
            state["final_response"] = f"Planning failed: {e}"
        
        return state


class ExecutorNode:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.fs_tool = FileSystemTool(config.workspace_path)
        self.pytest_tool = PytestTool(config.workspace_path)
        self.code_analyzer = CodeAnalyzer()
        self.logger = logger.bind(component="executor")

    async def execute(self, state: MultiAgentState) -> MultiAgentState:
        task = state["task"]
        plan = state.get("plan", "")
        state["status"] = TaskStatus.EXECUTING
        self.logger.info("executing", task=task)
        
        model = self.config.model_router.get_model("coding")
        if not model:
            state["status"] = TaskStatus.FAILED
            state["final_response"] = "No coding model configured"
            return state
        
        prompt = f"""Task: {task}

Plan:
{plan}

Execute this plan. If you need to write code, create files in the workspace. 
Report what you did and any results."""

        try:
            response = await self.config.model_router.generate(prompt, model)
            state["execution_results"].append({
                "iteration": state["iterations"],
                "result": response,
            })
            state["status"] = TaskStatus.REVIEWING
            self.logger.info("execution_completed")
        except Exception as e:
            self.logger.error("execution_failed", error=str(e))
            state["status"] = TaskStatus.FAILED
            state["final_response"] = f"Execution failed: {e}"
        
        return state


class ReviewerNode:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.logger = logger.bind(component="reviewer")

    async def execute(self, state: MultiAgentState) -> MultiAgentState:
        task = state["task"]
        plan = state.get("plan", "")
        results = state.get("execution_results", [])
        
        if not results:
            state["status"] = TaskStatus.FAILED
            state["final_response"] = "No execution results to review"
            return state
        
        last_result = results[-1]["result"]
        state["iterations"] += 1
        
        self.logger.info("reviewing", iteration=state["iterations"])
        
        model = self.config.model_router.get_model("coding")
        if not model:
            state["status"] = TaskStatus.FAILED
            state["final_response"] = "No coding model configured"
            return state
        
        prompt = f"""Review this execution result:

Original Task: {task}

Plan:
{plan}

Execution Result:
{last_result}

Is the task complete? Answer YES or NO. If NO, explain what needs to be fixed."""

        try:
            response = await self.config.model_router.generate(prompt, model)
            state["review_result"] = response
            
            if "YES" in response.upper() and len(response) < 100:
                state["status"] = TaskStatus.COMPLETED
                state["final_response"] = last_result
            elif state["iterations"] >= self.config.max_iterations:
                state["status"] = TaskStatus.COMPLETED
                state["final_response"] = f"Max iterations reached. Last result:\n{last_result}"
            else:
                state["status"] = TaskStatus.PENDING
                
        except Exception as e:
            self.logger.error("review_failed", error=str(e))
            state["status"] = TaskStatus.FAILED
            state["final_response"] = f"Review failed: {e}"
        
        return state


class MultiAgentWorkflow:
    def __init__(self, config: AgentConfig):
        self.config = config
        self.planner = PlannerNode(config)
        self.executor = ExecutorNode(config)
        self.reviewer = ReviewerNode(config)
        self.logger = logger.bind(component="multi_agent_workflow")
        
        from langgraph.graph import StateGraph, END
        from langgraph.checkpoint.memory import MemorySaver
        
        self.graph = StateGraph(MultiAgentState)
        
        self.graph.add_node("planner", self._planner_wrapper)
        self.graph.add_node("executor", self._executor_wrapper)
        self.graph.add_node("reviewer", self._reviewer_wrapper)
        
        self.graph.set_entry_point("planner")
        
        self.graph.add_edge("planner", "executor")
        self.graph.add_edge("executor", "reviewer")
        
        self.graph.add_conditional_edges(
            "reviewer",
            self._should_continue,
            {
                "continue": "executor",
                "end": END,
            }
        )
        
        checkpointer = MemorySaver()
        self.compiled = self.graph.compile(checkpointer=checkpointer)

    async def _planner_wrapper(self, state: MultiAgentState) -> MultiAgentState:
        return await self.planner.execute(state)

    async def _executor_wrapper(self, state: MultiAgentState) -> MultiAgentState:
        return await self.executor.execute(state)

    async def _reviewer_wrapper(self, state: MultiAgentState) -> MultiAgentState:
        return await self.reviewer.execute(state)

    def _should_continue(self, state: MultiAgentState) -> str:
        if state["status"] == TaskStatus.COMPLETED:
            return "end"
        return "continue"

    async def run(self, task: str, session_id: str) -> dict:
        self.config.session_memory.get_or_create_session(session_id, self.config.workspace_path)
        self.config.session_memory.save_message(session_id, "user", task)
        
        initial_state: MultiAgentState = {
            "task": task,
            "plan": None,
            "execution_results": [],
            "review_result": None,
            "status": TaskStatus.PENDING,
            "iterations": 0,
            "final_response": None,
            "session_id": session_id,
        }
        
        try:
            config = {"configurable": {"thread_id": session_id}}
            
            final_state = None
            async for event in self.compiled.astream(initial_state, config):
                for node_name, node_state in event.items():
                    final_state = node_state
            
            if final_state and final_state.get("final_response"):
                self.config.session_memory.save_message(
                    session_id, "assistant", final_state["final_response"]
                )
                
                return {
                    "success": final_state["status"] == TaskStatus.COMPLETED,
                    "session_id": session_id,
                    "result": {
                        "response": final_state["final_response"],
                        "plan": final_state.get("plan"),
                        "iterations": final_state.get("iterations", 0),
                    },
                }
            
            return {
                "success": False,
                "session_id": session_id,
                "error": "No response generated",
            }
            
        except Exception as e:
            self.logger.error("workflow_failed", error=str(e))
            return {
                "success": False,
                "session_id": session_id,
                "error": str(e),
            }


class MultiAgentOrchestrator:
    def __init__(
        self,
        workspace_path: str,
        model_router: ModelRouter,
        session_db_path: str = "data/memory.db",
        max_iterations: int = 3,
    ):
        self.workspace_path = workspace_path
        self.model_router = model_router
        self.session_memory = SessionMemory(session_db_path)
        
        config = AgentConfig(
            workspace_path=workspace_path,
            model_router=model_router,
            session_memory=self.session_memory,
            max_iterations=max_iterations,
        )
        
        self.workflow = MultiAgentWorkflow(config)
        self.logger = logger.bind(component="multi_agent_orchestrator")

    async def run_task(self, task: str, session_id: Optional[str] = None) -> dict:
        from datetime import datetime
        
        if not session_id:
            session_id = f"multi_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        
        return await self.workflow.run(task, session_id)

    def get_session_history(self, session_id: str) -> List[dict]:
        return self.session_memory.get_conversation_history(session_id)

    def list_sessions(self, limit: int = 20) -> List[dict]:
        return self.session_memory.list_sessions(limit)