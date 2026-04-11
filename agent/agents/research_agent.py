import re
from pathlib import Path
from typing import Dict, Any, List, Optional
from agent.agents.base_agent import AgentRole


class ResearchRole(AgentRole):
    def __init__(self, file_system_tool=None, code_analyzer=None):
        super().__init__(
            name="researcher",
            description="Investigates the codebase, reads files, and synthesises findings — never writes new code",
        )
        self.file_system_tool = file_system_tool
        self.code_analyzer = code_analyzer

    def get_system_prompt(self) -> str:
        return """You are an expert codebase research analyst. Your role is to:
- Investigate and understand existing code
- Read files and trace dependencies
- Answer "where", "what", "how" questions about the codebase
- Summarise findings in clear, structured reports
- Identify patterns, call chains, and architectural decisions

You do NOT write new code, create files, or modify anything.
You ONLY read, search, and report.

Format your findings as a structured report with:
1. Summary (2-3 sentences)
2. Location (file paths, line numbers)
3. How it works
4. Dependencies / callers (if relevant)"""

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        task = context.get("task", "")
        model_router = context.get("model_router")
        tool_executor = context.get("tool_executor")
        enriched_context = context.get("enriched_context", "")
        workspace_path = context.get("workspace_path", "")

        if not model_router:
            return {"success": False, "error": "model_router not available"}

        gathered = []

        if tool_executor:
            # Always include a workspace listing for orientation
            try:
                listing = await tool_executor.execute("file_list", {"path": ""})
                gathered.append(f"Workspace contents:\n{listing}")
            except Exception as e:
                self.logger.warning("workspace_list_failed", error=str(e))

            # Read any files explicitly mentioned in the task
            for fp in self._extract_mentioned_files(task, workspace_path)[:4]:
                try:
                    content = await tool_executor.execute("file_read", {"path": fp})
                    if content and not content.startswith("Error"):
                        gathered.append(f"--- {fp} ---\n{content[:3000]}")
                except Exception:
                    pass

        workspace_info = "\n\n".join(gathered)

        model = model_router.get_model("coding")
        if not model:
            return {"success": False, "error": "No model configured"}

        prompt = f"""{self.get_system_prompt()}

Research task: {task}

{workspace_info}
{enriched_context}

Provide a structured research report. Do not write new code or create files."""

        response = await model_router.generate(prompt, model)

        return {
            "success": True,
            "role": self.name,
            "response": response,
            "task": task,
            "files_created": [],
        }

    def _extract_mentioned_files(self, task: str, workspace_path: str) -> List[str]:
        """Return absolute paths for any file references found in the task string."""
        candidates = re.findall(
            r'[\w./\\-]+\.(?:py|ts|js|tsx|jsx|json|yaml|yml|md|toml|txt|cfg|ini)',
            task,
        )
        results: List[str] = []
        for candidate in candidates:
            for base in ([Path(workspace_path)] if workspace_path else []) + [Path(".")]:
                p = (base / candidate).resolve()
                if p.is_file():
                    results.append(str(p))
                    break
        return results


class ResearchAgent:
    def __init__(self, model_router, tools=None, file_system_tool=None, code_analyzer=None):
        from agent.agents.base_agent import BaseAgent
        role = ResearchRole(file_system_tool, code_analyzer)
        self.base = BaseAgent(role, model_router, tools)

    async def run(self, task: str, context: Dict[str, Any] = None):
        if context is None:
            context = {}
        return await self.base.run(task, context)
