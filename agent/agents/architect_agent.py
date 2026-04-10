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
        return """You are an expert software architect. Your role is to:
- Analyze requirements and design scalable, maintainable systems
- Choose appropriate patterns and technologies
- Consider trade-offs and document decisions
- Provide clear architectural guidance
- Write ADRs (Architecture Decision Records) when needed

When existing codebase files are provided, analyze them to understand current architecture.
Write architecture specs/recommendations using format:
FILE: docs/adr/ADR-XXX.md
```markdown
# ADR: Title

## Status
Proposed|Accepted|Deprecated

## Context
...

## Decision
...

## Consequences
...
```

Focus on:
- Clean architecture and separation of concerns
- SOLID principles
- API design
- Data modeling
- Security considerations"""

    def _extract_file_writes(self, response: str) -> List[tuple]:
        import re
        pattern = r'FILE:\s*(.+?)\n```markdown\n(.*?)```'
        matches = re.findall(pattern, response, re.DOTALL)
        return [(path.strip(), content.strip()) for path, content in matches]
    
    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        task = context.get("task", "")
        
        workspace_context = ""
        if self.file_system_tool:
            try:
                files = self.file_system_tool.list_directory(".")
                workspace_context = f"\n\nWorkspace contains {len(files)} items:\n"
                for f in files[:20]:
                    workspace_context += f"- {f['name']} ({f['type']})\n"
            except Exception as e:
                self.logger.warning("workspace_list_failed", error=str(e))
        
        prompt = f"""{self.get_system_prompt()}

Task: {task}
{workspace_context}

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
        
        response = await model_router.generate(prompt, model)
        
        files_created = []
        if self.file_system_tool:
            file_writes = self._extract_file_writes(response)
            for file_path, content in file_writes:
                try:
                    self.file_system_tool.write_file(file_path, content)
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
        self.file_system_tool = file_system_tool
        self.code_analyzer = code_analyzer
    
    async def run(self, task: str, context: Dict[str, Any] = None):
        if context is None:
            context = {}
        if self.file_system_tool and "file_system_tool" not in context:
            context["file_system_tool"] = self.file_system_tool
        if self.code_analyzer and "code_analyzer" not in context:
            context["code_analyzer"] = self.code_analyzer
        return await self.base.run(task, context)