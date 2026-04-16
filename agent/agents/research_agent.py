import asyncio
import re
from datetime import date
from pathlib import Path
from typing import Dict, Any, List, Optional
from agent.agents.base_agent import AgentRole
from agent.tools.web_tool import extract_urls

_DOCUMENT_EXTS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".tsv"}

# Maximum sub-questions from decomposition; follow-up queries per gap pass.
_MAX_QUESTIONS = 5
_MAX_FOLLOWUPS = 2

# Total character budget for web-gathered content.
_WEB_CONTENT_BUDGET = 14_000

# Patterns that always trigger the iterative web-research path.
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


# Patterns that indicate the task is about local workspace content — no web
# search needed even if no specific file names are mentioned.
_LOCAL_TASK_RE = re.compile(
    r"\b("
    r"in\s+the\s+(workspace|project|codebase|repo(?:sitory)?)|"
    r"last\s+(failed\s+)?(job|error|run|task|build)|"
    r"(?:find|show|check|look\s+at)\s+(?:the\s+)?(?:errors?|bugs?|issues?|logs?|output|files?)|"
    r"what\s+(?:is|was|went)\s+wrong|"
    r"why\s+(?:is|did|does)\s+it\s+fail"
    r")\b",
    re.IGNORECASE,
)


def _emit(on_phase, label: str) -> None:
    if on_phase:
        try:
            on_phase(label)
        except Exception:
            pass


class ResearchRole(AgentRole):
    def __init__(self, file_system_tool=None, code_analyzer=None):
        super().__init__(
            name="researcher",
            description="Investigates the codebase, reads files, and synthesises findings — never writes new code",
        )
        self.file_system_tool = file_system_tool
        self.code_analyzer = code_analyzer

    def get_system_prompt(self) -> str:
        today = date.today().strftime("%A, %B %d, %Y")
        return f"""Today's date is {today}.

You are an expert research analyst with access to the codebase,
the web, and documents. Your role is to:
- Investigate and understand existing code
- Read files and trace dependencies
- Search the web for documentation, changelogs, or background information
- Read PDFs, Word documents, spreadsheets, and CSVs
- Answer "where", "what", "how" questions with evidence
- Synthesise findings from multiple sources into clear, structured reports

CRITICAL INSTRUCTION — LIVE DATA IN CONTEXT:
When the gathered context below contains sections marked [LIVE WEB SEARCH RESULTS],
[FETCHED PAGE CONTENT], or similar, those are REAL results retrieved from the
internet or filesystem moments ago. You MUST use that data as your primary source.
Do NOT claim you cannot access the internet — the data is already in your context.
Cite specific facts, quotes, and URLs from it.

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
        on_phase = context.get("on_phase")

        if not model_router:
            return {"success": False, "error": "model_router not available"}

        model = model_router.get_model("coding")
        if not model:
            return {"success": False, "error": "No model configured"}

        # --- Local orientation (always runs) ---
        local_sections: List[str] = []
        if tool_executor:
            try:
                listing = await tool_executor.execute("file_list", {"path": ""})
                local_sections.append(f"Workspace contents:\n{listing}")
            except Exception as e:
                self.logger.warning("workspace_list_failed", error=str(e))

            for fp in self._extract_mentioned_files(task, workspace_path)[:4]:
                try:
                    content = await tool_executor.execute("file_read", {"path": fp})
                    if content and not content.startswith("Error"):
                        local_sections.append(f"--- {fp} ---\n{content[:3000]}")
                except Exception:
                    pass

            for dp in self._extract_document_paths(task, workspace_path)[:3]:
                try:
                    result = await tool_executor.execute("read_document", {"path": dp})
                    if result and "Error" not in result[:20]:
                        local_sections.append(result[:4000])
                except Exception as e:
                    self.logger.warning("doc_read_failed", path=dp, error=str(e))

        # --- Routing decision ---
        # "local content found" = actual file/document content was retrieved, OR
        # the workspace listing returned real entries (📄/📁 icons present).
        local_content_found = any(
            s.startswith("---") or s.startswith("[PDF") or s.startswith("[DOCX")
            or (("📄" in s or "📁" in s) and "Workspace contents:" in s)
            for s in local_sections
        )
        is_local_task = bool(_LOCAL_TASK_RE.search(task))
        needs_web = _SEARCH_TRIGGERS.search(task) or (
            not local_content_found and not is_local_task
        )

        if not needs_web:
            # Fast path: task is about local code/files — single-pass synthesis.
            _emit(on_phase, "researching:reading")
            if tool_executor:
                for url in extract_urls(task)[:3]:
                    try:
                        fetched = await tool_executor.execute("web_fetch", {"url": url})
                        if fetched and "Error" not in fetched[:20]:
                            local_sections.append(f"[FETCHED PAGE CONTENT: {url}]\n{fetched[:3000]}")
                    except Exception as e:
                        self.logger.warning("web_fetch_failed", url=url, error=str(e))
            return await self._synthesize(task, local_sections, enriched_context, model, model_router)

        # --- Iterative web-research path ---

        # Step 1: Decompose task into focused sub-questions.
        _emit(on_phase, "researching:planning")
        sub_questions = await self._decompose(task, model, model_router)
        self.logger.info("research_decomposed", questions=len(sub_questions))

        # Step 2: Parallel web searches for each sub-question.
        _emit(on_phase, f"researching:searching ({len(sub_questions)} questions)")
        search_coros = [self._search_question(q, tool_executor) for q in sub_questions]
        raw_results = await asyncio.gather(*search_coros, return_exceptions=True)
        web_sections: List[str] = [r for r in raw_results if isinstance(r, str) and r]
        web_sections = _trim_to_budget(web_sections, _WEB_CONTENT_BUDGET // 2)

        # Step 3: Gap analysis — identify what's still missing.
        _emit(on_phase, "researching:checking gaps")
        follow_ups = await self._identify_gaps(
            task, local_sections + web_sections, model, model_router
        )

        # Step 4: Follow-up searches (max 2).
        if follow_ups:
            _emit(on_phase, f"researching:follow-up ({len(follow_ups)} queries)")
            fu_coros = [self._search_question(q, tool_executor) for q in follow_ups]
            fu_results = await asyncio.gather(*fu_coros, return_exceptions=True)
            web_sections += [r for r in fu_results if isinstance(r, str) and r]

        web_sections = _trim_to_budget(web_sections, _WEB_CONTENT_BUDGET)

        # Step 5: Synthesize everything.
        _emit(on_phase, "researching:synthesizing")
        all_sections = local_sections + web_sections
        return await self._synthesize(task, all_sections, enriched_context, model, model_router)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _decompose(self, task: str, model, model_router) -> List[str]:
        """Ask the LLM to break the task into 3-5 focused sub-questions."""
        prompt = (
            "Break the following research task into 3-5 focused sub-questions that "
            "together cover its full scope.\n"
            "Return ONLY a numbered list, one question per line. No preamble, no explanations.\n\n"
            f"Research task: {task}"
        )
        try:
            raw = await model_router.generate(prompt, model, enable_thinking=False)
            questions = re.findall(r"^\d+\.\s*(.+)$", raw, re.MULTILINE)
            questions = [q.strip() for q in questions if q.strip()]
            if questions:
                return questions[:_MAX_QUESTIONS]
        except Exception as e:
            self.logger.warning("decompose_failed", error=str(e))
        return [task]  # fallback: treat whole task as one search query

    async def _identify_gaps(
        self, task: str, gathered: List[str], model, model_router
    ) -> List[str]:
        """Return up to 2 follow-up search queries for important missing information."""
        combined = "\n\n".join(gathered)[:6000]
        prompt = (
            "You are reviewing gathered research for a task. "
            f"Identify up to {_MAX_FOLLOWUPS} specific follow-up search queries for "
            "information that is MISSING or INCOMPLETE in the gathered content.\n"
            "If the gathered content is already sufficient, return nothing.\n"
            "Return ONLY a numbered list of search queries, one per line. No explanations.\n\n"
            f"Research task: {task}\n\n"
            f"Gathered so far (excerpt):\n{combined}"
        )
        try:
            raw = await model_router.generate(prompt, model, enable_thinking=False)
            queries = re.findall(r"^\d+\.\s*(.+)$", raw, re.MULTILINE)
            queries = [q.strip() for q in queries if q.strip()]
            return queries[:_MAX_FOLLOWUPS]
        except Exception as e:
            self.logger.warning("gap_analysis_failed", error=str(e))
        return []

    async def _search_question(self, query: str, tool_executor) -> str:
        """Search for one sub-question and deep-fetch the top result page."""
        if not tool_executor:
            return ""
        sections: List[str] = []
        try:
            raw = await tool_executor.execute(
                "web_search", {"query": query[:200], "max_results": 3}
            )
            if raw and not raw.startswith("Error"):
                sections.append(f"[Search: {query[:80]}]\n{raw[:1200]}")
                # Deep-fetch the first URL for richer content.
                urls = re.findall(r"https?://\S+", raw)
                if urls:
                    try:
                        page = await tool_executor.execute("web_fetch", {"url": urls[0]})
                        if page and not page.startswith("Error"):
                            sections.append(
                                f"[Page: {urls[0][:80]}]\n{page[:1500]}"
                            )
                    except Exception:
                        pass
        except Exception as e:
            self.logger.warning("search_question_failed", query=query[:60], error=str(e))
        return "\n\n".join(sections)

    async def _synthesize(
        self,
        task: str,
        gathered: List[str],
        enriched_context: str,
        model,
        model_router,
    ) -> Dict[str, Any]:
        """Final LLM synthesis over all gathered content."""
        workspace_info = "\n\n".join(gathered)
        prompt = (
            f"{self.get_system_prompt()}\n\n"
            "The following information was gathered from the codebase, web, and documents:\n\n"
            f"{workspace_info}\n"
            f"{enriched_context}\n\n"
            f"Research task: {task}\n\n"
            "Provide a structured research report based on the gathered information above.\n"
            "If live web search results are included, cite them directly. "
            "Do not write new code or create files."
        )
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


def _trim_to_budget(sections: List[str], budget: int) -> List[str]:
    """Return sections trimmed so total chars stay within budget."""
    result: List[str] = []
    total = 0
    for s in sections:
        if total + len(s) > budget:
            remaining = budget - total
            if remaining > 200:
                result.append(s[:remaining] + "\n[trimmed]")
            break
        result.append(s)
        total += len(s)
    return result


class ResearchAgent:
    def __init__(self, model_router, tools=None, file_system_tool=None, code_analyzer=None):
        from agent.agents.base_agent import BaseAgent
        role = ResearchRole(file_system_tool, code_analyzer)
        self.base = BaseAgent(role, model_router, tools)

    async def run(self, task: str, context: Dict[str, Any] = None):
        if context is None:
            context = {}
        return await self.base.run(task, context)
