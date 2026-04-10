from typing import Dict, Any, Optional
from agent.agents.base_agent import AgentRole


class DeveloperRole(AgentRole):
    def __init__(self):
        super().__init__(
            name="developer",
            description="Implements code based on specifications and requirements",
        )
    
    def get_system_prompt(self) -> str:
        return """You are an expert software developer. Your role is to:
- Write clean, efficient, and maintainable code
- Follow best practices and coding standards
- Create comprehensive tests
- Document your code
- Refactor for clarity and performance

Focus on:
- Code correctness and edge cases
- Proper error handling
- Security best practices
- Performance optimization
- Readable and self-documenting code"""
    
    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        task = context.get("task", "")
        architecture = context.get("architecture", "")
        
        prompt = f"""{self.get_system_prompt()}

Task: {task}

{architecture if architecture else ''}

Implement the solution with:
1. Complete, working code
2. Appropriate error handling
3. Basic tests
4. Clear documentation in comments

Use best practices for the language/framework specified."""
        
        model = context.get("model_router").get_model("coding")
        if not model:
            return {"success": False, "error": "No coding model configured"}
        
        response = await context.get("model_router").generate(prompt, model)
        
        return {
            "success": True,
            "role": self.name,
            "response": response,
            "task": task,
            "files_created": context.get("files_created", []),
        }


class DeveloperAgent:
    def __init__(self, model_router, tools=None):
        from agent.agents.base_agent import BaseAgent
        self.base = BaseAgent(DeveloperRole(), model_router, tools)
    
    async def run(self, task: str, context: Dict[str, Any] = None):
        return await self.base.run(task, context)