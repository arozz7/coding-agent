from typing import Dict, Any, List, Optional
from agent.agents.base_agent import AgentRole


class ArchitectRole(AgentRole):
    def __init__(self, file_system_tool=None, code_analyzer=None):
        super().__init__(
            name="architect",
            description="Designs system architecture and provides high-level design recommendations",
        )
        self.file_system_tool = file_system_tool
        self.code_analyzer = code_analyzer
    
    def get_system_prompt(self) -> str:
        return """You are an expert architecture assistant. You help users
with system design tasks by analyzing requirements, designing architecture,
and writing ADRs (Architecture Decision Records).

Available tools:
- read: Extensively examine codebase files and workspace
- write: Create or overwrite Architecture Documentation using the FILE: syntax

Guidelines:
- Use the exact FILE: formatting block to write ADRs:
  FILE: docs/adr/ADR-XXX.md
  ```markdown
  content
  ```
- Focus on clean architecture, SOLID principles, and data modeling
- Be concise in your responses"""

    def _extract_file_writes(self, response: str) -> List[tuple]:
        import re
        pattern = r'FILE:\s*(.+?)\n```markdown\n(.*?)```'
        matches = re.findall(pattern, response, re.DOTALL)
        return [(path.strip(), content.strip()) for path, content in matches]
    
    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        task = context.get("task", "")
        tool_executor = context.get("tool_executor")

        workspace_context = ""
        if tool_executor:
            try:
                listing = await tool_executor.execute("file_list", {"path": ""})
                workspace_context = f"\n\nWorkspace:\n{listing}"
            except Exception as e:
                self.logger.warning("workspace_list_failed", error=str(e))

        enriched_context = context.get("enriched_context", "")
        prompt = f"""Task: {task}
{workspace_context}{enriched_context}

Provide a detailed architectural design with:
1. High-level components and their responsibilities
2. Data flow between components
3. Technology recommendations
4. Key design patterns to use
5. Potential challenges and mitigation strategies

If creating documentation, write the file:
FILE: docs/adr/ADR-XXX.md
```markdown
# ADR: Title
...
```"""
        model_router = context.get("model_router")

        if not model_router:
            return {"success": False, "error": "model_router not available"}

        model = model_router.get_model("coding")
        if not model:
            return {"success": False, "error": "No coding model configured"}

        response = await model_router.generate(prompt, model, system_prompt=self.get_system_prompt())

        files_created = []
        if tool_executor:
            file_writes = self._extract_file_writes(response)
            for file_path, content in file_writes:
                try:
                    await tool_executor.execute("file_write", {"path": file_path, "content": content})
                    files_created.append(file_path)
                except Exception as e:
                    self.logger.error("adr_write_failed", path=file_path, error=str(e))

        return {
            "success": True,
            "role": self.name,
            "response": response,
            "task": task,
            "files_created": files_created,
        }


class ArchitectAgent:
    def __init__(self, model_router, tools=None, file_system_tool=None, code_analyzer=None):
        from agent.agents.base_agent import BaseAgent
        role = ArchitectRole(file_system_tool, code_analyzer)
        self.base = BaseAgent(role, model_router, tools)

    async def run(self, task: str, context: Dict[str, Any] = None):
        if context is None:
            context = {}
        return await self.base.run(task, context)