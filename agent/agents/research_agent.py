import asyncio
import re
from datetime import date
from pathlib import Path
from typing import Dict, Any, List, Optional
from agent.agents.base_agent import AgentRole
from agent.tools.web_tool import extract_urls

_DOCUMENT_EXTS = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".tsv"}

# Maximum sub-questions from decomposition; follow-up queries per gap pass; coverage rounds.
_MAX_QUESTIONS = 8
_MAX_FOLLOWUPS = 4
_MAX_COVERAGE_PASS = 3

# Total character budget for web-gathered content.
_WEB_CONTENT_BUDGET = 28_000

# Patterns that always trigger the iterative web-research path.
_SEARCH_TRIGGERS = re.compile(
    r"\b("
    r"search\s+(for|the\s+web|online)|look\s+up|find\s+online|google|"
    r"what('s|\s+is)\s+the\s+(latest|current|news)|current\s+version|"
    r"recent\s+news|last\s+night|yesterday|today|latest|recent(ly)?|"
    r"score|scores|weather|stock|price|market|news|headline|"
    r"who\s+(won|lost|is)|what\s+happened|"
    r"released|launched|announced|"
    r"research\s+(on|about|into|for)|deep\s+research|in.?depth|"
    r"comprehensive|thorough|exhaustive|"
    r"investigate|explore|study|analyze|analyse|"
    r"build.*agent|how\s+to\s+build|"
    r"best\s+practices?|compare|evaluate|assess|"
    r"survey|overview|landscape|state\s+of"
    r")\b",
    re.IGNORECASE,
)

# Patterns that indicate the user wants output captured to markdown/files.
_FILE_WRITE_RE = re.compile(
    r"\b("
    r"capture\s+(to|into|in)\s+(markdown|files?|docs?|documents?)|"
    r"save\s+(to|into|as)\s+(markdown|files?|docs?)|"
    r"write\s+(to|into)\s+(markdown|files?|docs?)|"
    r"create\s+(markdown\s+files?|docs?|documents?|files?)|"
    r"output\s+(to|as)\s+(markdown|files?)|"
    r"logically\s+(into\s+)?(files?|docs?|markdown)|"
    r"into\s+markdown\s+files?|as\s+markdown\s+files?"
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

You are an expert research assistant. You help users
with investigating tasks by reading files, searching the web,
and synthesizing findings into reports.

Available tools:
- read: Extensively examine files and documents
- search: Look up documentation or background info on the web
- synthesize: Combine findings into clear structured reports

Guidelines:
- Prioritize reading real data from the context (e.g. [FETCHED PAGE CONTENT])
- Do NOT write new code or modify existing files
- When asked to capture findings to files, structure output with ## headings per topic
- Format your findings with Summary, Sources, Findings, and Dependencies
- Be exhaustive and thorough — cover ALL aspects of the research task
- Cite specific facts, names, URLs, and examples in your responses"""

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
        # Web search only when the task contains an explicit search trigger.
        # The previous implicit fallback (no local content → web search) caused
        # every task on an empty workspace to trigger a full web-research cycle,
        # wasting context budget on irrelevant results.
        needs_web = bool(_SEARCH_TRIGGERS.search(task))

        wants_files = bool(_FILE_WRITE_RE.search(task))

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
            result = await self._synthesize(
                task, local_sections, enriched_context, model, model_router, wants_files=wants_files
            )
            if wants_files and tool_executor and result.get("success"):
                _emit(on_phase, "researching:writing-files")
                result["files_created"] = await self._write_research_files(
                    task, result["response"], tool_executor, workspace_path
                )
            return result

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

        # Step 4: Follow-up searches (max _MAX_FOLLOWUPS).
        if follow_ups:
            _emit(on_phase, f"researching:follow-up ({len(follow_ups)} queries)")
            fu_coros = [self._search_question(q, tool_executor) for q in follow_ups]
            fu_results = await asyncio.gather(*fu_coros, return_exceptions=True)
            web_sections += [r for r in fu_results if isinstance(r, str) and r]

        # Step 5: Coverage check — up to _MAX_COVERAGE_PASS additional rounds.
        for _pass in range(_MAX_COVERAGE_PASS):
            coverage_queries = await self._check_coverage(
                task, local_sections + web_sections, model, model_router
            )
            if not coverage_queries:
                break
            _emit(on_phase, f"researching:coverage-pass-{_pass + 1} ({len(coverage_queries)} queries)")
            cov_coros = [self._search_question(q, tool_executor) for q in coverage_queries]
            cov_results = await asyncio.gather(*cov_coros, return_exceptions=True)
            new_sections = [r for r in cov_results if isinstance(r, str) and r]
            if not new_sections:
                break
            web_sections += new_sections

        web_sections = _trim_to_budget(web_sections, _WEB_CONTENT_BUDGET)

        # Step 6: Synthesize everything.
        _emit(on_phase, "researching:synthesizing")
        all_sections = local_sections + web_sections
        wants_files = bool(_FILE_WRITE_RE.search(task))
        result = await self._synthesize(
            task, all_sections, enriched_context, model, model_router, wants_files=wants_files
        )

        # Step 7: Write output files if requested.
        if wants_files and tool_executor and result.get("success"):
            _emit(on_phase, "researching:writing-files")
            files_created = await self._write_research_files(
                task, result["response"], tool_executor, workspace_path
            )
            result["files_created"] = files_created

        return result

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

    async def _check_coverage(
        self, task: str, gathered: List[str], model, model_router
    ) -> List[str]:
        """Return additional search queries for topics still not covered."""
        combined = "\n\n".join(gathered)[:8000]
        prompt = (
            "You are a research quality checker. Review the gathered content against the task.\n"
            f"Identify up to {_MAX_FOLLOWUPS} CRITICAL topics from the task that are COMPLETELY "
            "absent or severely under-covered in the gathered content.\n"
            "If coverage is already comprehensive, return nothing.\n"
            "Return ONLY a numbered list of targeted search queries, one per line. No explanations.\n\n"
            f"Research task: {task}\n\n"
            f"Gathered so far (excerpt):\n{combined}"
        )
        try:
            raw = await model_router.generate(prompt, model, enable_thinking=False)
            queries = re.findall(r"^\d+\.\s*(.+)$", raw, re.MULTILINE)
            queries = [q.strip() for q in queries if q.strip()]
            return queries[:_MAX_FOLLOWUPS]
        except Exception as e:
            self.logger.warning("coverage_check_failed", error=str(e))
        return []

    async def _synthesize(
        self,
        task: str,
        gathered: List[str],
        enriched_context: str,
        model,
        model_router,
        wants_files: bool = False,
    ) -> Dict[str, Any]:
        """Final LLM synthesis over all gathered content."""
        workspace_info = "\n\n".join(gathered)
        if wants_files:
            file_instruction = (
                "Structure your response as multiple clearly delineated sections using "
                "level-2 markdown headings (## Section Title). Each section should represent "
                "a logical topic area that can be saved as a separate markdown file. "
                "Be exhaustive — include ALL relevant details from the gathered content."
            )
        else:
            file_instruction = "Provide a structured research report based on the gathered information above."
        prompt = (
            "The following information was gathered from the codebase, web, and documents:\n\n"
            f"{workspace_info}\n"
            f"{enriched_context}\n\n"
            f"Research task: {task}\n\n"
            f"{file_instruction}\n"
            "If live web search results are included, cite them directly."
        )
        response = await model_router.generate(prompt, model, system_prompt=self.get_system_prompt())
        return {
            "success": True,
            "role": self.name,
            "response": response,
            "task": task,
            "files_created": [],
        }

    async def _write_research_files(
        self,
        task: str,
        synthesis: str,
        tool_executor,
        workspace_path: str,  # noqa: ARG002 — kept for caller compat; root comes from env var
    ) -> List[str]:
        """Split the synthesis into sections and write each as a markdown file."""
        import os as _os
        # Env-var-only root — breaks CodeQL HTTP-taint chain at a trusted source.
        _ws_root = Path(_os.getenv("WORKSPACE_PATH", "./workspace")).resolve()
        _output_dir = _ws_root / "research-output"

        sections = re.split(r"\n(?=## )", synthesis)
        files_written: List[str] = []
        try:
            await tool_executor.execute("shell", {"command": f'mkdir -p "{_output_dir}"'})
        except Exception:
            pass

        for section in sections:
            section = section.strip()
            if not section:
                continue
            heading_match = re.match(r"^#+\s+(.+)$", section, re.MULTILINE)
            if not heading_match:
                continue
            title = heading_match.group(1).strip()
            slug = re.sub(r"[^\w\s-]", "", title.lower())
            slug = re.sub(r"[\s_]+", "-", slug).strip("-")[:60] + ".md"
            # Inline containment check — CodeQL-recognized taint terminator.
            _fp = (_output_dir / slug).resolve()
            if not _fp.is_relative_to(_ws_root):
                self.logger.warning("research_file_outside_workspace", path=str(_fp))
                continue
            rel_path = str(_fp.relative_to(_ws_root))
            try:
                await tool_executor.execute(
                    "file_write", {"path": rel_path, "content": section}
                )
                files_written.append(rel_path)
                self.logger.info("research_file_written", path=rel_path)
            except Exception as e:
                self.logger.warning("research_file_write_failed", path=rel_path, error=str(e))

        # Also write a full index file.
        if files_written:
            _idx = (_output_dir / "index.md").resolve()
            if _idx.is_relative_to(_ws_root):
                index_lines = [f"# Research Index\n\nTask: {task[:200]}\n"]
                for f in files_written:
                    name = Path(f).stem.replace("-", " ").title()
                    index_lines.append(f"- [{name}]({Path(f).name})")
                idx_rel = str(_idx.relative_to(_ws_root))
                try:
                    await tool_executor.execute(
                        "file_write", {"path": idx_rel, "content": "\n".join(index_lines)}
                    )
                    files_written.append(idx_rel)
                except Exception as e:
                    self.logger.warning("research_index_write_failed", error=str(e))

        return files_written

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
