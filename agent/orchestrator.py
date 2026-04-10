from typing import TypedDict, Annotated, List, Optional
from dataclasses import dataclass
from datetime import datetime
import structlog

from llm import ModelRouter
from agent.memory import SessionMemory, CodebaseMemory
from agent.tools import FileSystemTool, PytestTool, CodeAnalyzer
from observability.logging import AgentLogger

logger = structlog.get_logger()


class AgentState(TypedDict):
    task: str
    response: str
    session_id: str


class AgentOrchestrator:
    def __init__(
        self,
        workspace_path: str,
        model_router: ModelRouter,
        session_db_path: str = "data/memory.db",
        chroma_path: str = "data/chroma_db",
    ):
        self.workspace_path = workspace_path
        self.model_router = model_router
        self.session_memory = SessionMemory(session_db_path)
        self.codebase_memory = CodebaseMemory(chroma_path)
        self.fs_tool = FileSystemTool(workspace_path)
        self.pytest_tool = PytestTool(workspace_path)
        self.code_analyzer = CodeAnalyzer()
        self.logger = logger.bind(component="agent_orchestrator")
        self.agent_logger = AgentLogger("orchestrator")

    def _build_context(self, session_id: str, include_history: bool = True) -> str:
        if not include_history:
            return ""
        
        history = self.session_memory.get_conversation_history(session_id, max_messages=10)
        
        if not history:
            return ""
        
        context_lines = ["\n\nPrevious conversation:\n"]
        for msg in history[-6:]:
            role = msg["role"].capitalize()
            content = msg["content"]
            context_lines.append(f"{role}: {content[:500]}")
        
        return "\n".join(context_lines)

    async def run_task(
        self, task: str, session_id: Optional[str] = None, include_history: bool = True
    ) -> dict:
        if not session_id:
            session_id = f"session_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

        self.session_memory.get_or_create_session(session_id, self.workspace_path)
        self.session_memory.save_message(session_id, "user", task)

        config = self.model_router.get_model("coding")
        if not config:
            return {
                "success": False,
                "session_id": session_id,
                "error": "No coding model configured",
            }

        self.agent_logger.log_task_start("agent", {"task": task})

        context = self._build_context(session_id, include_history)
        
        prompt = f"""You are a helpful coding assistant. Respond to the following request:

{task}{context}

Provide a clear, concise response. If writing code, use markdown code blocks."""

        try:
            response = await self.model_router.generate(prompt, config)
            
            self.session_memory.save_message(
                session_id,
                "assistant",
                response,
                model_name=config.name,
            )

            self.agent_logger.log_task_complete(
                "agent", 0, {"response_length": len(response)}
            )

            return {
                "success": True,
                "session_id": session_id,
                "result": {
                    "response": response,
                    "task": task,
                },
            }

        except Exception as ex:
            self.logger.error("task_failed", error=str(ex))
            self.session_memory.update_task_status(
                session_id, task, "failed", {"error": str(ex)}
            )
            return {
                "success": False,
                "session_id": session_id,
                "error": str(ex),
            }

    async def run_stream(
        self, task: str, session_id: Optional[str] = None, include_history: bool = True
    ):
        if not session_id:
            session_id = f"session_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

        self.session_memory.get_or_create_session(session_id, self.workspace_path)
        self.session_memory.save_message(session_id, "user", task)

        config = self.model_router.get_model("coding")
        if not config:
            raise ValueError("No coding model configured")

        context = self._build_context(session_id, include_history)
        
        prompt = f"""You are a helpful coding assistant. Respond to the following request:

{task}{context}"""

        full_response = ""
        async for chunk in self.model_router.generate_stream(prompt, config):
            full_response += chunk
            yield {"chunk": chunk, "full_response": full_response}

        self.session_memory.save_message(
            session_id, "assistant", full_response, model_name=config.name
        )

    def get_session_history(self, session_id: str) -> List[dict]:
        return self.session_memory.get_conversation_history(session_id)

    def list_sessions(self, limit: int = 20) -> List[dict]:
        return self.session_memory.list_sessions(limit)

    def get_session_info(self, session_id: str) -> dict:
        return self.session_memory.get_session_summary(session_id)
