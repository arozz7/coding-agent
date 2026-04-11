import re
from pathlib import Path
from typing import Dict, Any, List, Optional
from agent.agents.base_agent import AgentRole
from agent.tools.web_tool import extract_urls

_DOCUMENT_EXTS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".tsv"}

# Patterns that always warrant a web search in the research agent.
_SEARCH_TRIGGERS = re.compile(
    r"\b("
    r"search\s+(for|the\s+web|online)|look\s+up|find\s+online|google|"
    r"what('s|\s+is)\s+the\s+(latest|current|news)|current\s+version|"
    r"recent\s+news|last\s+night|yesterday|today|latest|recent(ly)?|"
    r"score|scores|weather|stock|price|market|news|headline|"
    r"who\s+(won|lost|is)|what\s+happened|"
    r"released|launched|announced"
    r")\b",
    re.IGNORECASE,
)


class ResearchRole(AgentRole):
    def __init__(self, file_system_tool=None, code_analyzer=None):
        super().__init__(
            name="researcher",
            description="Investigates the codebase, reads files, and synthesises findings — never writes new code",
        )
        self.file_system_tool = file_system_tool
        self.code_analyzer = code_analyzer

    def get_system_prompt(self) -> str:
        return """You are an expert research analyst with access to the codebase,
the web, and documents. Your role is to:
- Investigate and understand existing code
- Read files and trace dependencies
- Search the web for documentation, changelogs, or background information
- Read PDFs, Word documents, spreadsheets, and CSVs
- Answer "where", "what", "how" questions with evidence
- Synthesise findings from multiple sources into clear, structured reports

You do NOT write new code, create files, or modify anything.
You ONLY read, search, and report.

Format your findings as:
1. Summary (2-3 sentences)
2. Sources consulted (files / URLs / documents)
3. Detailed findings
4. Dependencies or related areas (if relevant)"""

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
            # 1. Workspace listing (orientation for codebase tasks)
            try:
                listing = await tool_executor.execute("file_list", {"path": ""})
                gathered.append(f"Workspace contents:\n{listing}")
            except Exception as e:
                self.logger.warning("workspace_list_failed", error=str(e))

            # 2. Source files mentioned in the task
            for fp in self._extract_mentioned_files(task, workspace_path)[:4]:
                try:
                    content = await tool_executor.execute("file_read", {"path": fp})
                    if content and not content.startswith("Error"):
                        gathered.append(f"--- {fp} ---\n{content[:3000]}")
                except Exception:
                    pass

            # 3. Documents (PDF / DOCX / XLSX / CSV) mentioned in the task
            for dp in self._extract_document_paths(task, workspace_path)[:3]:
                try:
                    result = await tool_executor.execute("read_document", {"path": dp})
                    if result and "Error" not in result[:20]:
                        gathered.append(result[:4000])
                except Exception as e:
                    self.logger.warning("doc_read_failed", path=dp, error=str(e))

            # 4. URLs mentioned in the task — fetch their content
            for url in extract_urls(task)[:3]:
                try:
                    fetched = await tool_executor.execute("web_fetch", {"url": url})
                    if fetched and "Error" not in fetched[:20]:
                        gathered.append(fetched[:3000])
                except Exception as e:
                    self.logger.warning("web_fetch_failed", url=url, error=str(e))

            # 5. Web search:
            #    a) explicit trigger keywords
            #    b) fallback: no local files were found — task is likely about external info
            local_content_found = any(
                g.startswith("---") or g.startswith("[PDF") or g.startswith("[DOCX")
                for g in gathered[1:]  # skip the workspace listing
            )
            if _SEARCH_TRIGGERS.search(task) or not local_content_found:
                try:
                    search_results = await tool_executor.execute(
                        "web_search", {"query": task[:200], "max_results": 5}
                    )
                    if search_results and not search_results.startswith("Error"):
                        gathered.append(search_results[:3000])
                except Exception as e:
                    self.logger.warning("web_search_failed", error=str(e))

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

    def _extract_document_paths(self, task: str, workspace_path: str) -> List[str]:
        """Return absolute paths for document files (PDF/DOCX/XLSX/CSV) in the task."""
        pattern = r'[\w./\\-]+\.(?:pdf|docx?|xlsx?|csv|tsv)'
        candidates = re.findall(pattern, task, re.IGNORECASE)
        results: List[str] = []
        for candidate in candidates:
            for base in ([Path(workspace_path)] if workspace_path else []) + [Path(".")]:
                p = (base / candidate).resolve()
                if p.is_file() and p.suffix.lower() in _DOCUMENT_EXTS:
                    results.append(str(p))
                    break
        return results

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
