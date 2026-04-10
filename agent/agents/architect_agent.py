from typing import Dict, Any
from agent.agents.base_agent import AgentRole


class ArchitectRole(AgentRole):
    def __init__(self):
        super().__init__(
            name="architect",
            description="Designs system architecture and provides high-level design recommendations",
        )
    
    def get_system_prompt(self) -> str:
        return """You are an expert software architect. Your role is to:
- Analyze requirements and design scalable, maintainable systems
- Choose appropriate patterns and technologies
- Consider trade-offs and document decisions
- Provide clear architectural guidance

Focus on:
- Clean architecture and separation of concerns
- SOLID principles
- API design
- Data modeling
- Security considerations"""
    
    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        task = context.get("task", "")
        
        prompt = f"""{self.get_system_prompt()}

Task: {task}

Provide a detailed architectural design with:
1. High-level components and their responsibilities
2. Data flow between components
3. Technology recommendations
4. Key design patterns to use
5. Potential challenges and mitigation strategies"""
        
        model = context.get("model_router").get_model("coding")
        if not model:
            return {"success": False, "error": "No coding model configured"}
        
        response = await context.get("model_router").generate(prompt, model)
        
        return {
            "success": True,
            "role": self.name,
            "response": response,
            "task": task,
        }


class ArchitectAgent:
    def __init__(self, model_router, tools=None):
        from agent.agents.base_agent import BaseAgent
        self.base = BaseAgent(ArchitectRole(), model_router, tools)
    
    async def run(self, task: str, context: Dict[str, Any] = None):
        return await self.base.run(task, context)