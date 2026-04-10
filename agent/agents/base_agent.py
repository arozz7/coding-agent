from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any
import structlog

logger = structlog.get_logger()


class AgentRole(ABC):
    def __init__(self, name: str, description: str):
        self.name = name
        self.description = description
        self.logger = logger.bind(component=f"agent_role:{name}")
    
    @abstractmethod
    async def execute(self, context: dict) -> dict:
        pass
    
    @abstractmethod
    def get_system_prompt(self) -> str:
        pass


class BaseAgent:
    def __init__(
        self,
        role: AgentRole,
        model_router,
        tools: Optional[List[Any]] = None,
    ):
        self.role = role
        self.model_router = model_router
        self.tools = tools or []
        self.logger = logger.bind(component=f"base_agent:{role.name}")
    
    async def run(self, task: str, context: Optional[dict] = None) -> dict:
        self.logger.info("agent_starting", role=self.role.name, task=task[:100])
        
        full_context = context or {}
        full_context["task"] = task
        full_context["role"] = self.role.name
        
        try:
            result = await self.role.execute(full_context)
            self.logger.info("agent_completed", role=self.role.name, success=result.get("success"))
            return result
        except Exception as e:
            self.logger.error("agent_failed", role=self.role.name, error=str(e))
            return {"success": False, "error": str(e)}
    
    def add_tool(self, tool: Any) -> None:
        self.tools.append(tool)
        self.logger.debug("tool_added", tool=type(tool).__name__)
    
    def remove_tool(self, tool_name: str) -> None:
        self.tools = [t for t in self.tools if type(t).__name__ != tool_name]
        self.logger.debug("tool_removed", tool=tool_name)