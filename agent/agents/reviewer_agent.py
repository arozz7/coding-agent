from typing import Dict, Any
from agent.agents.base_agent import AgentRole


class ReviewerRole(AgentRole):
    def __init__(self):
        super().__init__(
            name="reviewer",
            description="Reviews code for quality, security, and best practices",
        )
    
    def get_system_prompt(self) -> str:
        return """You are an expert code reviewer. Your role is to:
- Review code for quality and correctness
- Identify potential bugs and security issues
- Ensure adherence to best practices
- Suggest improvements
- Verify test coverage

Focus on:
- Code smells and anti-patterns
- Security vulnerabilities
- Performance issues
- Maintainability
- Test quality"""
    
    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        task = context.get("task", "")
        code = context.get("code", "")
        
        prompt = f"""{self.get_system_prompt()}

Original Task: {task}

Code to Review:
```
{code}
```

Provide a detailed review with:
1. Issues found (severity: critical/high/medium/low)
2. Specific line numbers and suggestions
3. Security concerns
4. Performance recommendations
5. Overall code quality score (1-10)

Format as a structured review."""
        
        model = context.get("model_router").get_model("coding")
        if not model:
            return {"success": False, "error": "No coding model configured"}
        
        response = await context.get("model_router").generate(prompt, model)
        
        return {
            "success": True,
            "role": self.name,
            "response": response,
            "task": task,
            "issues_found": context.get("issues_found", 0),
        }


class ReviewerAgent:
    def __init__(self, model_router, tools=None):
        from agent.agents.base_agent import BaseAgent
        self.base = BaseAgent(ReviewerRole(), model_router, tools)
    
    async def run(self, task: str, context: Dict[str, Any] = None):
        return await self.base.run(task, context)