from typing import Dict, Any, List
import re
from agent.agents.base_agent import AgentRole


class TesterRole(AgentRole):
    def __init__(self, file_system_tool=None, pytest_tool=None):
        super().__init__(
            name="tester",
            description="Generates comprehensive tests for code functionality",
        )
        self.file_system_tool = file_system_tool
        self.pytest_tool = pytest_tool
    
    def get_system_prompt(self) -> str:
        return """You are an expert test engineer. Your role is to:
- Write comprehensive test suites
- Cover edge cases and boundary conditions
- Use appropriate testing frameworks
- Create meaningful test assertions
- Follow testing best practices
- RUN the tests after writing them

IMPORTANT - You CAN execute tests:
1. Run `pytest` or `npm test` to verify tests pass
2. Fix any failing tests
3. Report test results

When generating tests, write actual test files:
FILE: tests/test_filename.py
```python
import pytest

def test_something():
    assert True
```

Focus on:
- High code coverage
- Unit tests and integration tests
- Test-driven development
- Mocking and stubbing
- Test maintainability"""

    def _extract_file_writes(self, response: str) -> List[tuple]:
        pattern = r'FILE:\s*(.+?)\n```\w*\n(.*?)```'
        matches = re.findall(pattern, response, re.DOTALL)
        return [(path.strip(), content.strip()) for path, content in matches]

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

Write the test file:
FILE: tests/test_<module>.py
```{language}
# test code here
```

Run the tests if pytest tool is available."""
        model_router = context.get("model_router")
        
        if not model_router:
            return {"success": False, "error": "model_router not available"}
        
        model = model_router.get_model("coding")
        if not model:
            return {"success": False, "error": "No coding model configured"}
        
        response = await model_router.generate(prompt, model)
        
        files_created = []
        test_output = None
        
        if self.file_system_tool:
            file_writes = self._extract_file_writes(response)
            for file_path, content in file_writes:
                try:
                    self.file_system_tool.write_file(file_path, content)
                    files_created.append(file_path)
                    self.logger.info("test_file_written", path=file_path)
                except Exception as e:
                    self.logger.error("test_file_write_failed", path=file_path, error=str(e))
        
        if self.pytest_tool and files_created:
            try:
                test_result = self.pytest_tool.run(path=files_created[0])
                test_output = test_result
                self.logger.info("tests_run", files=files_created, result=test_result)
            except Exception as e:
                self.logger.error("test_run_failed", error=str(e))
        
        return {
            "success": True,
            "role": self.name,
            "response": response,
            "task": task,
            "language": language,
            "tests_generated": True,
            "files_created": files_created,
            "test_output": test_output,
        }


class TesterAgent:
    def __init__(self, model_router, tools=None, file_system_tool=None, pytest_tool=None):
        from agent.agents.base_agent import BaseAgent
        role = TesterRole(file_system_tool, pytest_tool)
        self.base = BaseAgent(role, model_router, tools)
        self.file_system_tool = file_system_tool
        self.pytest_tool = pytest_tool
    
    async def run(self, task: str, context: Dict[str, Any] = None):
        if context is None:
            context = {}
        if self.file_system_tool and "file_system_tool" not in context:
            context["file_system_tool"] = self.file_system_tool
        if self.pytest_tool and "pytest_tool" not in context:
            context["pytest_tool"] = self.pytest_tool
        return await self.base.run(task, context)