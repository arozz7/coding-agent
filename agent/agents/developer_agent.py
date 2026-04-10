from typing import Dict, Any, Optional, List
import re
from agent.agents.base_agent import AgentRole


class DeveloperRole(AgentRole):
    def __init__(self, file_system_tool=None, shell_tool=None, browser_tool=None):
        super().__init__(
            name="developer",
            description="Implements code based on specifications and requirements",
        )
        self.file_system_tool = file_system_tool
        self.shell_tool = shell_tool
        self.browser_tool = browser_tool
    
    def get_system_prompt(self) -> str:
        return """You are an expert software developer. Your role is to:
- Write clean, efficient, and maintainable code
- Follow best practices and coding standards
- Create comprehensive tests
- Document your code
- Refactor for clarity and performance
- EXECUTE commands when asked to run, test, or verify code
- Take screenshots of running applications using screenshot()

IMPORTANT - You HAVE capabilities to execute:
1. File system - write files using FILE: path\n```language\ncode\n```
2. Shell commands - run `npm install`, `npm run start`, `npm run build`, etc. using backticks
3. Screenshot - call screenshot() to capture browser screenshot of running app
4. When user asks to run/test/verify the project, DO run the commands and report results

When you generate code, DO NOT just describe it - write the actual code files.
Use markdown code blocks with language identifiers (e.g., ```python, ```typescript).

For file writing instructions in your response:
- Write filename in the first line like: FILE: 
- Follow with the complete file content in a code block

For running commands, simply include the command in backticks:
`npm install`
`npm run start`

For taking screenshots, the browser tool will automatically capture after running:
`screenshot` or `npm run start && screenshot`

For example:
FILE: src/main.py
```python
def main():
    print("Hello, World!")
```

Focus on:
- Code correctness and edge cases
- Proper error handling
- Security best practices
- Performance optimization
- Readable and self-documenting code"""

    def _extract_file_writes(self, response: str) -> List[tuple]:
        pattern = r'FILE:\s*(.+?)\n```\w*\n(.*?)```'
        matches = re.findall(pattern, response, re.DOTALL)
        return [(path.strip(), content.strip()) for path, content in matches]

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        task = context.get("task", "")
        architecture = context.get("architecture", "")
        files_created = []
        model_router = context.get("model_router")
        
        if not model_router:
            return {"success": False, "error": "model_router not available"}
        
        prompt = f"""{self.get_system_prompt()}

Task: {task}

{architecture if architecture else ''}

Implement the solution with:
1. Complete, working code
2. Appropriate error handling
3. Basic tests
4. Clear documentation in comments

Write actual files using the format:
FILE: 
```<language>
# file content here
```
"""
        
        model = model_router.get_model("coding")
        if not model:
            return {"success": False, "error": "No coding model configured"}
        
        response = await model_router.generate(prompt, model)
        
        if self.file_system_tool:
            file_writes = self._extract_file_writes(response)
            for file_path, content in file_writes:
                try:
                    self.file_system_tool.write_file(file_path, content)
                    files_created.append(file_path)
                    self.logger.info("file_written", path=file_path, size=len(content))
                except Exception as e:
                    self.logger.error("file_write_failed", path=file_path, error=str(e))
        
        shell_output = None
        if self.shell_tool:
            cmd_matches = re.findall(r'`([^`]+)`', response)
            for cmd in cmd_matches:
                try:
                    shell_output = self.shell_tool.run(cmd)
                    self.logger.info("shell_output", cmd=cmd, result=shell_output)
                except Exception as e:
                    self.logger.error("shell_failed", cmd=cmd, error=str(e))
        
        # Append shell output to response for user visibility (last output)
        if shell_output:
            response += f"\n\n**Shell Output:**\n```\n{shell_output.get('stdout', '')}{shell_output.get('stderr', '')}\n```"
            if not shell_output.get("success"):
                response += f"\n⚠️ Exit code: {shell_output.get('returncode')}"
        
        screenshot_path = None
        if self.browser_tool:
            if re.search(r'screenshot', task, re.IGNORECASE) or re.search(r'run_and_screenshot|capture', response, re.IGNORECASE):
                try:
                    result = await self.browser_tool.run_and_screenshot()
                    if result.get("success"):
                        screenshot_path = result.get("path")
                        response += f"\n\n📸 Screenshot captured: {screenshot_path}"
                except Exception as e:
                    self.logger.error("screenshot_failed", error=str(e))
        
        return {
            "success": True,
            "role": self.name,
            "response": response,
            "task": task,
            "files_created": files_created,
            "shell_output": shell_output,
            "screenshot": screenshot_path,
        }


class DeveloperAgent:
    def __init__(self, model_router, tools=None, file_system_tool=None, shell_tool=None, browser_tool=None):
        from agent.agents.base_agent import BaseAgent
        role = DeveloperRole(file_system_tool, shell_tool, browser_tool)
        self.base = BaseAgent(role, model_router, tools)
        self.file_system_tool = file_system_tool
        self.shell_tool = shell_tool
        self.browser_tool = browser_tool
    
    async def run(self, task: str, context: Dict[str, Any] = None):
        if context is None:
            context = {}
        if self.file_system_tool and "file_system_tool" not in context:
            context["file_system_tool"] = self.file_system_tool
        if self.browser_tool and "browser_tool" not in context:
            context["browser_tool"] = self.browser_tool
        return await self.base.run(task, context)