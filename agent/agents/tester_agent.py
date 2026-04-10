from typing import Dict, Any
from agent.agents.base_agent import AgentRole


class TesterRole(AgentRole):
    def __init__(self):
        super().__init__(
            name="tester",
            description="Generates comprehensive tests for code functionality",
        )
    
    def get_system_prompt(self) -> str:
        return """You are an expert test engineer. Your role is to:
- Write comprehensive test suites
- Cover edge cases and boundary conditions
- Use appropriate testing frameworks
- Create meaningful test assertions
- Follow testing best practices

Focus on:
- High code coverage
- Unit tests and integration tests
- Test-driven development
- Mocking and stubbing
- Test maintainability"""
    
    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        task = context.get("task", "")
        code = context.get("code", "")
        language = context.get("language", "python")
        
        framework_hint = {
            "python": "pytest",
            "javascript": "jest",
            "typescript": "jest",
            "java": "junit",
            "go": "testing",
            "rust": "#[test]",
        }.get(language, "pytest")
        
        prompt = f"""{self.get_system_prompt()}

Original Task: {task}

Code to Test:
```
{code}
```

Write comprehensive tests using {framework_hint} with:
1. Unit tests for all functions/methods
2. Edge case coverage
3. Proper assertions
4. Fixtures and mocks where needed
5. Test documentation

Ensure tests are runnable and will pass with the provided code."""
        
        model = context.get("model_router").get_model("coding")
        if not model:
            return {"success": False, "error": "No coding model configured"}
        
        response = await context.get("model_router").generate(prompt, model)
        
        return {
            "success": True,
            "role": self.name,
            "response": response,
            "task": task,
            "language": language,
            "tests_generated": True,
        }


class TesterAgent:
    def __init__(self, model_router, tools=None):
        from agent.agents.base_agent import BaseAgent
        self.base = BaseAgent(TesterRole(), model_router, tools)
    
    async def run(self, task: str, context: Dict[str, Any] = None):
        return await self.base.run(task, context)