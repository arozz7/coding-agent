from typing import TypedDict, Annotated, List, Optional, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import os
import subprocess
import structlog

from llm import ModelRouter
from agent.memory import SessionMemory, CodebaseMemory
from agent.memory.memory_wiki import MemoryWiki
from agent.tools import FileSystemTool, PytestTool, CodeAnalyzer
from agent.agents.developer_agent import DeveloperAgent
from agent.agents.plan_agent import PlanAgent
from agent.agents.planner_agent import PlannerAgent
from agent.agents.tester_agent import TesterAgent
from agent.agents.reviewer_agent import ReviewerAgent
from agent.agents.architect_agent import ArchitectAgent
from agent.agents.chat_agent import ChatAgent
from agent.agents.research_agent import ResearchAgent
from agent.skills.skill_loader import SkillManager
from agent.skills.wiki_manager import WikiManager
from agent.skills.skill_executor import SkillExecutor
from observability.logging import AgentLogger

logger = structlog.get_logger()


class AgentState(TypedDict):
    task: str
    response: str
    session_id: str


class AgentOrchestrator:
    def __init__(
        self,
        workspace_path: str,
        model_router: ModelRouter,
        session_db_path: str = "data/memory.db",
        chroma_path: str = "data/chroma_db",
    ):
        self.workspace_path = workspace_path
        self.model_router = model_router
        self.session_memory = SessionMemory(session_db_path)
        self.codebase_memory = CodebaseMemory(chroma_path)
        self.fs_tool = FileSystemTool(workspace_path)
        self.pytest_tool = PytestTool(workspace_path)
        self.code_analyzer = CodeAnalyzer()
        from agent.tools.shell_tool import ShellTool
        from agent.tools.browser_tool import BrowserTool
        from agent.tools.tool_executor import ToolExecutor, EventEmittingExecutor
        self._EventEmittingExecutor = EventEmittingExecutor
        self.shell_tool = ShellTool(workspace_path)
        self.browser_tool = BrowserTool(workspace_path)
        self.tool_executor = ToolExecutor(workspace_path, self.code_analyzer, self.pytest_tool)
        self.skill_manager = SkillManager("skills")
        self.wiki_manager = WikiManager(workspace_path)
        self.wiki_manager._ensure_dirs()   # create .agent-wiki/ structure on startup
        self.skill_executor = SkillExecutor(self.wiki_manager, self.skill_manager)
        self.memory_wiki = MemoryWiki(project_id=Path(workspace_path).name)

        # Create MCP server for tool exposure
        from mcp.server import create_mcp_server
        self.mcp_server = create_mcp_server(workspace_path)
        
        self.logger = logger.bind(component="agent_orchestrator")
        self.agent_logger = AgentLogger("orchestrator")
        
        self.developer_agent = DeveloperAgent(
            model_router,
            tools=[self.fs_tool, self.shell_tool],
            file_system_tool=self.fs_tool,
            shell_tool=self.shell_tool,
            browser_tool=self.browser_tool,
        )
        self.plan_agent = PlanAgent(model_router)
        self.tester_agent = TesterAgent(
            model_router,
            tools=[self.fs_tool, self.pytest_tool],
            file_system_tool=self.fs_tool,
            pytest_tool=self.pytest_tool,
        )
        self.reviewer_agent = ReviewerAgent(
            model_router,
            tools=[self.code_analyzer, self.fs_tool],
            code_analyzer=self.code_analyzer,
            file_system_tool=self.fs_tool,
        )
        self.architect_agent = ArchitectAgent(
            model_router,
            tools=[self.fs_tool, self.code_analyzer],
            file_system_tool=self.fs_tool,
            code_analyzer=self.code_analyzer,
        )
        
        self.chat_agent = ChatAgent(model_router)
        self.research_agent = ResearchAgent(
            model_router,
            tools=[self.fs_tool, self.code_analyzer],
            file_system_tool=self.fs_tool,
            code_analyzer=self.code_analyzer,
        )
        self.planner_agent = PlannerAgent(model_router)

        # Task store — shares the same SQLite file as the job store
        from api.task_store import TaskStore
        self.task_store = TaskStore("data/jobs.db")

        # Subagent management
        self.subagents: dict[str, "SubagentSession"] = {}
        
    async def spawn_subagent(
        self,
        task: str,
        role: str = "developer",
        parent_session_id: str = None,
        context_limits: dict = None,
    ) -> dict:
        """Spawn a subagent with isolated context for large tasks.
        
        Args:
            task: The task for the subagent
            role: Agent role (developer, tester, reviewer, architect)
            parent_session_id: Parent session for result aggregation
            context_limits: Limits on what subagent can access
        
        Returns:
            Subagent session info with execution results
        """
        import uuid
        subagent_id = f"subagent_{uuid.uuid4().hex[:8]}"
        
        self.logger.info("spawning_subagent", subagent_id=subagent_id, role=role, task=task[:100])

        # Ensure session exists before creating executor (EventEmittingExecutor requires it)
        self.session_memory.get_or_create_session(subagent_id, self.workspace_path)
        enriched_context = await self._build_enriched_context(task)

        # Create isolated context for subagent
        isolated_context = {
            "session_id": subagent_id,
            "parent_session_id": parent_session_id,
            "workspace_path": self.workspace_path,
            "model_router": self.model_router,
            "tool_executor": self._create_session_executor(subagent_id),
            "enriched_context": enriched_context,
            "context_limits": context_limits or {},
            "is_subagent": True,
        }
        
        # Select agent based on role
        if role == "tester":
            agent = self.tester_agent
        elif role == "reviewer":
            agent = self.reviewer_agent
        elif role == "architect":
            agent = self.architect_agent
        elif role == "researcher":
            agent = self.research_agent
        elif role == "chat":
            agent = self.chat_agent
        else:
            agent = self.developer_agent
        
        # Run subagent with isolated context
        try:
            result = await agent.run(task, isolated_context)
            
            # Store subagent session
            self.subagents[subagent_id] = {
                "id": subagent_id,
                "role": role,
                "task": task,
                "parent_session_id": parent_session_id,
                "result": result,
                "status": "completed" if result.get("success") else "failed",
            }
            
            # Aggregate result back to parent session
            if parent_session_id:
                self.session_memory.save_message(
                    parent_session_id,
                    "subagent",
                    f"[{role}] {task[:50]}... -> {result.get('response', '')[:200]}",
                )

            # Merge any files the subagent created back into the RAG index
            # so future searches in the parent session can find them.
            files_created = result.get("files_created", [])
            if files_created and result.get("success"):
                project_id = Path(self.workspace_path).name
                for rel_path in files_created:
                    abs_path = Path(self.workspace_path) / rel_path  # lgtm[py/path-injection]
                    if abs_path.exists() and abs_path.is_file():
                        try:
                            self.codebase_memory.index_files(
                                [str(abs_path)], project_id
                            )
                        except Exception as index_err:
                            self.logger.warning(
                                "subagent_rag_merge_failed",
                                file=rel_path,
                                error=str(index_err),
                            )

            self.logger.info("subagent_completed", subagent_id=subagent_id, status=self.subagents[subagent_id]["status"])
            
            return {
                "success": True,
                "subagent_id": subagent_id,
                "role": role,
                "result": result,
            }
        except Exception as e:
            self.logger.error("subagent_failed", subagent_id=subagent_id, error=str(e))
            return {
                "success": False,
                "subagent_id": subagent_id,
                "error": str(e),
            }
    
    async def spawn_multiple_subagents(
        self,
        tasks: list[str],
        roles: list[str] = None,
        parent_session_id: str = None,
    ) -> list[dict]:
        """Spawn multiple subagents in parallel for parallel task execution.
        
        Args:
            tasks: List of tasks to execute
            roles: Optional list of roles (defaults to developer)
            parent_session_id: Parent session for aggregation
        
        Returns:
            List of subagent results
        """
        import asyncio
        
        if roles is None:
            roles = ["developer"] * len(tasks)
        
        # Create tasks for parallel execution
        async def run_task_pair(task: str, role: str):
            return await self.spawn_subagent(task, role, parent_session_id)
        
        # Execute all subagents in parallel
        results = await asyncio.gather(
            *[run_task_pair(task, role) for task, role in zip(tasks, roles)],
            return_exceptions=True
        )
        
        # Convert exceptions to error results
        processed_results = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                processed_results.append({
                    "success": False,
                    "error": str(result),
                    "task": tasks[i],
                })
            else:
                processed_results.append(result)
        
        return processed_results
    
    def get_subagent_result(self, subagent_id: str) -> dict:
        """Get result from a specific subagent."""
        return self.subagents.get(subagent_id, {"error": "Subagent not found"})
    
    def list_subagents(self) -> list[dict]:
        """List all active subagent sessions."""
        return [
            {
                "id": sa["id"],
                "role": sa["role"],
                "task": sa["task"][:50],
                "status": sa["status"],
            }
            for sa in self.subagents.values()
        ]
    
    def _detect_task_type_keyword(self, task: str) -> str:
        """Keyword-based task classifier — used as fallback when LLM is unavailable.

        Priority (highest first):
          0. SDLC — full plan+build+test+debug+run+verify pipeline
          1. Plan — user explicitly wants a plan before any code is written
          2. Explicit coding — output is new/modified source files
          3. Review / security audit
          4. Testing — write or run tests
          5. Architecture / ADR
          6. Research — investigate the existing codebase
          7. Chat — everything else (conversation, general questions)
        """
        t = task.lower()

        # 0. Full SDLC pipeline — build, run, test, and verify autonomously
        _SDLC = [
            "build me a complete", "build a complete", "build a full",
            "create a full", "create a complete",
            "develop a complete", "develop a full",
            "build and test", "build, test",
            "build and run", "build and deploy",
            "implement and test", "implement, test",
            "full app", "full application", "entire application",
            "end to end", "end-to-end",
            "full development", "full stack",
            "build the whole", "build the entire",
        ]
        if any(kw in t for kw in _SDLC):
            return "sdlc"

        # 0b. Run / debug / launch — user wants to execute existing code and fix errors
        _RUN_DEBUG = [
            "run and debug", "run and fix", "debug and fix", "run the game",
            "run the app", "run the server", "run the project", "run the code",
            "run and test", "launch the", "start the app", "start the server",
            "start the game", "start the project",
            "debug the", "debug it", "debugging the",
            "fix the runtime", "fix the error", "fix the errors", "fix the bug",
            "fix the bugs", "fix and run", "fix this error", "fix these errors",
            "there are still errors", "still not running", "not starting",
            "can't run", "cannot run", "won't run", "fails to run",
            "fails to start", "failing to run",
            # Execution / build phrases commonly missed by above
            "run the build", "running the build", "try running", "run it",
            "run with verbose", "run with", "run verbose",
            "go ahead and run", "go run", "now run", "run now",
            "run the application", "run the program",
            "run npm", "npm run", "npm install", "npm start",
            "compile the", "execute the", "execute it",
            "build it", "build the project", "build the app",
            "running builds", "running the app", "running the application",
        ]
        if any(kw in t for kw in _RUN_DEBUG):
            return "develop"

        # 1. Planning mode — user wants a blueprint before implementation
        _PLAN = [
            "plan first", "solid plan", "show me a plan", "want to plan",
            "want first work on a", "planning phase", "let's plan", "lets plan",
            "before we build", "before building", "before implementing",
            "roadmap", "outline the approach", "outline a plan", "create a plan",
            "work on a plan", "i want a plan",
        ]
        if any(kw in t for kw in _PLAN):
            return "plan"

        # 1. Explicit development: output is code/files (including document/content writing)
        _DEVELOP = [
            "implement", "refactor", "write a function", "write a class",
            "write a script", "write the code", "write code",
            "create a file", "create the file",
            "build a ", "build the ", "develop a ",
            "add feature", "add a feature",
            "fix the bug", "fix this bug", "fix the error", "fix this error",
            "fix the issue", "fix this issue",
            "update the code", "update the function", "update the class",
            "generate code", "generate a script",
            "create an api", "create a server", "create a bot", "create a cli",
            "make an app", "make a server", "make a bot", "make a script",
            "make a function", "make a class",
            # Content / document writing — these all produce files
            "flush out", "flesh out", "fill in", "fill out",
            "complete the", "complete this", "finish the", "finish writing",
            "continue to write", "continue writing", "continue to flush",
            "continue to flesh", "continue to fill", "continue to build",
            "continue to develop", "continue to work on",
            "write the narrative", "write the story", "write the lore",
            "write the docs", "write the document", "write the content",
            "draft the", "draft a document", "draft a narrative",
            "expand the", "expand on", "add content", "add more content",
            "add to the", "update the doc", "update the narrative",
            "update the story", "write more", "add more detail",
            "create the document", "create the narrative", "create the story",
            "create the lore", "create the wiki", "create the design doc",
            "write up", "document the", "write out",
        ]
        if any(kw in t for kw in _DEVELOP):
            return "develop"

        # 2. Code review / security audit
        if any(kw in t for kw in [
            "review the code", "code review", "critique", "check for bugs",
            "security audit", "security review", "analyze this code",
            "review this file", "review this function",
        ]):
            return "review"

        # 3. Tests
        if any(kw in t for kw in [
            "write tests", "write unit tests", "add tests", "create tests",
            "generate tests", "unit test", "pytest", "test suite", "test case",
            "run the tests", "run tests",
        ]):
            return "test"

        # 4. Architecture
        if any(kw in t for kw in [
            "system design", "design the architecture", "architecture for",
            "write an adr", "create an adr", "architect the", "high-level design",
            "design pattern for", "design a system",
        ]):
            return "architect"

        # 5. Research: codebase investigation (read-only)
        if any(kw in t for kw in [
            "where is ", "where are ", "find the ", "find where",
            "locate ", "which file", "what file",
            "trace ", "how does the existing", "how is ", "how does ",
            "what does the code", "show me where",
            "search the codebase", "look for ", "search for ",
            "what files", "investigate", "explore the code",
            "explain this code", "explain the code", "explain this file",
        ]):
            return "research"

        # 6. Default: chat (general questions, explanations, conversation)
        return "chat"

    async def _detect_task_type_llm(self, task: str) -> str:
        """LLM-based task classifier. Returns one of the 6 valid task types.

        Sends a tiny zero-shot prompt to the active model with a short timeout.
        Raises on timeout or unexpected output so the caller can fall back.
        """
        import asyncio
        import re as _re
        import yaml as _yaml

        # Load classifier config (prompt + valid_types).
        # _PROJECT_ROOT is the directory containing this package (the repo root).
        from local_coding_agent import _PROJECT_ROOT
        cfg_path = _PROJECT_ROOT / "config" / "task_classifier.yaml"
        if not cfg_path.exists():
            raise FileNotFoundError(f"task_classifier.yaml not found at {cfg_path}")

        with open(cfg_path) as fh:
            cfg = _yaml.safe_load(fh)

        valid_types: List[str] = cfg["valid_types"]
        timeout_s: float = float(cfg.get("timeout_seconds", 3))
        prompt_template: str = cfg["prompt"]
        prompt = prompt_template.format(task=task)

        config = self.model_router.get_model("coding")
        if not config:
            raise RuntimeError("No model configured")

        raw = await asyncio.wait_for(
            self.model_router.generate(prompt, config),
            timeout=timeout_s,
        )

        # Extract first word on first non-empty line
        first_line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
        candidate = _re.sub(r"[^a-z]", "", first_line.lower().split()[0]) if first_line else ""

        if candidate not in valid_types:
            raise ValueError(f"LLM returned unexpected type: {candidate!r}")

        return candidate

    async def _detect_task_type(self, task: str) -> str:
        """Return the most appropriate agent role for this task.

        Tries the LLM classifier first; falls back to keyword matching on
        any failure (timeout, model unavailable, unexpected output).
        """
        try:
            result = await self._detect_task_type_llm(task)
            self.logger.info("task_type_llm", task_type=result)
            return result
        except Exception as e:
            self.logger.warning("task_type_llm_fallback", reason=str(e))
            return self._detect_task_type_keyword(task)
    
    # Keyword → skill name mapping for pre/post phase detection
    _PRE_TRIGGERS: dict[str, list[str]] = {
        "test": ["tdd-enforcer"],
        "security": ["security-auditor"],
        "audit": ["security-auditor"],
        "database": ["architect-decision-engine"],
        "api": ["architect-decision-engine"],
        "auth": ["architect-decision-engine"],
        "architecture": ["architect-decision-engine"],
        "adr": ["architect-decision-engine"],
    }
    _POST_TRIGGERS: dict[str, list[str]] = {
        "compile": ["wiki-compile"],
        "save": ["wiki-compile"],
        "remember": ["wiki-compile"],
        "wiki": ["wiki-compile"],
        "handover": ["handover"],
        "context bridge": ["handover"],
    }

    def _detect_skill_names(self, task: str, phase: str = "pre") -> List[str]:
        """Return skill names triggered by task keywords for the given phase."""
        task_lower = task.lower()
        triggers = self._PRE_TRIGGERS if phase == "pre" else self._POST_TRIGGERS
        seen: list[str] = []
        for keyword, names in triggers.items():
            if keyword in task_lower:
                for name in names:
                    if name not in seen:
                        seen.append(name)
        return seen

    # Fallback handover template used when the skill file cannot be loaded.
    _HANDOVER_FALLBACK = (
        "Generate a concise Context Bridge document so a future AI session can "
        "resume exactly where this one left off.\n\n"
        "Output ONLY this structure:\n\n"
        "### Current State\n"
        "3-sentence summary of objectives, decisions, and work completed.\n\n"
        "### Technical Details\n"
        "Bulleted list of specific constraints, file paths, function names, "
        "config values, and preferences established in this session.\n\n"
        "### Next Steps\n"
        "Prioritised list of what the next session should focus on.\n\n"
        "### Opening Instruction\n"
        "A single sentence the user can paste into a new chat to instantly "
        "prime the next AI with this context.\n\n"
        "Be concise but comprehensive — no context should be lost."
    )

    # ------------------------------------------------------------------ #
    # Context-budget helpers                                               #
    # ------------------------------------------------------------------ #

    def _estimate_context_tokens(self, session_id: str, task: str) -> int:
        """Rough token estimate for the next LLM call.

        Measures the session history string plus a fixed overhead that accounts
        for the system prompt (~1 500 tokens) and enriched context (~3 000 tokens).
        Uses char/4 as the token estimator — precise enough for a threshold check.
        """
        history = self._build_context_from_events(session_id)
        char_count = len(history) + len(task)
        return char_count // 4 + 4_500  # overhead: system prompt + enriched context

    def _check_context_budget(self, session_id: str, task: str) -> str:
        """Return 'ok', 'warn' (≥75 %), or 'bridge' (≥82 %) based on token usage."""
        config = self.model_router.get_model("coding")
        if not config or not config.context_window:
            return "ok"
        estimated = self._estimate_context_tokens(session_id, task)
        ratio = estimated / config.context_window
        self.logger.debug(
            "context_budget_check",
            estimated_tokens=estimated,
            context_window=config.context_window,
            ratio=f"{ratio:.1%}",
        )
        if ratio >= 0.82:
            return "bridge"
        if ratio >= 0.75:
            return "warn"
        return "ok"

    async def _run_handover(self, session_id: str, task: str) -> tuple:
        """Generate a Context Bridge, create a new session pre-seeded with it.

        Returns (bridge_text: str, new_session_id: str).
        """
        # Load the handover SKILL.md if available, else use fallback template.
        skill = self.skill_manager.get_skill("handover")
        instructions = (skill.content if skill else self._HANDOVER_FALLBACK).strip()

        # Gather recent git context (best-effort).
        git_summary = ""
        try:
            r = subprocess.run(
                ["git", "log", "--oneline", "-10"],
                cwd=str(self.workspace_path),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0:
                git_summary = r.stdout.strip()
        except Exception:
            pass

        history = self._build_context_from_events(session_id)
        prompt = (
            f"{instructions}\n\n"
            f"## Session to summarise\n"
            f"Session ID: {session_id}\n"
            f"Next task (triggered this handover): {task}\n\n"
            f"Recent git commits:\n{git_summary or '(unavailable)'}\n\n"
            f"Conversation history:\n{history or '(no history yet)'}\n\n"
            f"Generate the Context Bridge now."
        )

        config = self.model_router.get_model("coding")
        bridge_text = await self.model_router.generate(prompt, config)

        # Create the new session and pre-seed it with the bridge document.
        new_session_id = f"session_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_bridge"
        self.session_memory.get_or_create_session(new_session_id, self.workspace_path)
        self.session_memory.save_message(
            new_session_id,
            "assistant",
            f"[Context Bridge — resumed from session {session_id}]\n\n{bridge_text}",
        )
        self.logger.info(
            "handover_complete",
            old_session=session_id,
            new_session=new_session_id,
        )
        return bridge_text, new_session_id

    @staticmethod
    def _build_environment_context() -> str:
        """Return a compact block describing the runtime environment.

        Injected at the top of every task context so agents never have to
        probe the OS by running commands — they already know.
        """
        import platform as _platform
        system = _platform.system()
        release = _platform.release()

        if system == "Windows":
            shell_guide = (
                "Shell: PowerShell / cmd.exe (Windows)\n"
                "IMPORTANT — Windows command equivalents:\n"
                "  dir          (not ls)\n"
                "  type         (not cat)\n"
                "  del          (not rm)\n"
                "  copy         (not cp)\n"
                "  move         (not mv)\n"
                "  cls          (not clear)\n"
                "  where        (not which)\n"
                "  findstr      (not grep)\n"
                "  $env:VAR     (not export VAR=)\n"
                "Chain with: &&  (not ; or ||)\n"
                "Paths use backslash or forward slash both work in npm/node/python."
            )
        elif system == "Darwin":
            shell_guide = "Shell: zsh/bash (macOS)"
        else:
            shell_guide = "Shell: bash/sh (Linux)"

        active_project = os.environ.get("PROJECT_DIR", "").strip()
        project_line = (
            f"Active project: {active_project} "
            f"(workspace is already scoped — write files at workspace root, "
            f"NOT inside a new subdirectory)\n"
            if active_project
            else ""
        )

        return (
            f"\n\n## Runtime Environment\n"
            f"OS: {system} {release}\n"
            f"{shell_guide}\n"
            f"{project_line}"
        )

    async def _build_enriched_context(self, task: str) -> str:
        """Build prompt enrichment: wiki-query results + RAG chunks + skill instructions.

        Replaces the four dead helper methods (_detect_skills, _get_skill_context,
        _load_wiki_context, _load_rag_context) that were disconnected when
        _run_general_agent() was removed.
        """
        parts: list[str] = []

        # 0a. Runtime environment — OS, shell, command cheat-sheet.
        #     Injected first so agents never have to probe the OS.
        parts.append(self._build_environment_context())

        # 0c. AGENTS.md — global coding agent instructions (injected once per task)
        agents_md = Path("AGENTS.md")
        if agents_md.exists():
            try:
                agents_content = agents_md.read_text(encoding="utf-8")
                parts.append(f"\n\n## Global Agent Instructions (AGENTS.md)\n{agents_content}")
            except Exception:
                pass

        # 0b. Workspace file listing — shallow snapshot so agents know what files exist
        #     without having to run a shell command. Capped at 80 entries to stay concise.
        try:
            ws_root = Path(self.workspace_path)
            if ws_root.exists():
                ws_lines: list[str] = []
                _IGNORE = {".git", "node_modules", "__pycache__", ".agent-wiki", "logs", "-p"}
                for item in sorted(ws_root.rglob("*")):
                    # Skip hidden/noisy directories
                    if any(part in _IGNORE for part in item.parts):
                        continue
                    rel = item.relative_to(ws_root)
                    prefix = "📁 " if item.is_dir() else "📄 "
                    ws_lines.append(f"  {prefix}{rel}")
                    if len(ws_lines) >= 80:
                        ws_lines.append("  … (truncated)")
                        break
                if ws_lines:
                    parts.append(
                        f"\n\n## Workspace Files ({self.workspace_path})\n"
                        + "\n".join(ws_lines)
                    )
        except Exception as _ws_err:
            self.logger.warning("workspace_listing_failed", error=str(_ws_err))

        # 1. Wiki query — check persistent knowledge before every task
        wiki_ctx = await self.skill_executor.execute_pre("wiki-query", task)
        if wiki_ctx:
            parts.append(wiki_ctx)

        # 2. RAG — semantic code search from vector store
        try:
            project_id = Path(self.workspace_path).name
            rag_ctx = self.codebase_memory.get_relevant_context(task, project_id, max_chunks=3)
            if rag_ctx:
                parts.append(rag_ctx)
        except Exception as e:
            self.logger.warning("rag_context_failed", error=str(e))

        # 3. Pre-execution skill instructions (tdd-enforcer, security-auditor, etc.)
        for skill_name in self._detect_skill_names(task, "pre"):
            skill_ctx = await self.skill_executor.execute_pre(skill_name, task)
            if skill_ctx:
                parts.append(skill_ctx)

        return "\n".join(parts)

    def _create_session_executor(self, session_id: str) -> "EventEmittingExecutor":
        """Create an EventEmittingExecutor bound to this session."""
        return self._EventEmittingExecutor(
            self.tool_executor,
            self.session_memory,
            session_id,
        )

    async def _run_specialized_agent(
        self,
        task: str,
        task_type: str,
        session_id: str,
        on_phase: Optional[Callable[[str], None]] = None,
        job_id: Optional[str] = None,
        _direct: bool = False,
    ) -> dict:
        """Route a task to the appropriate agent.

        For "develop" and "research" task types the task is first decomposed
        into a plan and run through the task loop — unless _direct=True, which
        bypasses the loop (used when called from inside the loop to avoid
        infinite recursion).

        For all other types (plan, review, test, architect, chat, sdlc) the
        agent is called directly as before.
        """
        # SDLC workflow is handled by its own class — does not need a context dict
        if task_type == "sdlc":
            from agent.sdlc_workflow import SDLCWorkflow
            workflow = SDLCWorkflow(self)
            return await workflow.run(task, session_id, on_phase=on_phase, job_id=job_id)

        # Task loop for develop and research (when called from run_task, not from loop itself)
        if not _direct and task_type in ("develop", "research"):
            return await self._run_task_loop(
                task, task_type, session_id, on_phase=on_phase, job_id=job_id
            )

        # --- Direct execution (all other types, or inner loop calls) ---
        session_executor = self._create_session_executor(session_id)
        enriched_context = await self._build_enriched_context(task)
        history = self._build_context_from_events(session_id)
        context = {
            "session_id": session_id,
            "workspace_path": self.workspace_path,
            "model_router": self.model_router,
            "tool_executor": session_executor,
            "enriched_context": enriched_context + history,
        }

        if task_type == "plan":
            return await self.plan_agent.run(task, context)
        elif task_type == "review":
            return await self.reviewer_agent.run(task, context)
        elif task_type == "test":
            return await self.tester_agent.run(task, context)
        elif task_type == "architect":
            return await self.architect_agent.run(task, context)
        elif task_type == "research":
            return await self.research_agent.run(task, context)
        elif task_type == "chat":
            return await self.chat_agent.run(task, context)
        else:
            return await self.developer_agent.run(task, context)

    async def _run_task_loop(
        self,
        objective: str,
        task_type: str,
        session_id: str,
        on_phase: Optional[Callable[[str], None]] = None,
        job_id: Optional[str] = None,
    ) -> dict:
        """Decompose an objective into tasks and execute them sequentially.

        Flow:
          1. PlannerAgent decomposes objective → [{description, agent_type}]
          2. Tasks are stored in TaskStore (if job_id provided)
          3. Loop: pick next pending task → route to agent → store result
          4. Agent results may contain "new_tasks" to append mid-loop
          5. Return combined response when all tasks are terminal
        """
        import asyncio as _asyncio

        def _emit(label: str) -> None:
            if on_phase:
                try:
                    on_phase(label)
                except Exception:
                    pass

        # 1. Plan
        _emit("planning:tasks")
        enriched_preview = await self._build_enriched_context(objective)
        task_specs = await self.planner_agent.plan(
            objective,
            context=enriched_preview[:600],
            task_type=task_type,
        )

        # 2. Persist tasks
        if job_id:
            self.task_store.create_tasks(job_id, task_specs)

        total = len(task_specs)
        self.logger.info(
            "task_loop_started",
            objective=objective[:80],
            task_count=total,
            job_id=job_id,
        )

        all_responses: list[str] = []
        all_files: list[str] = []
        screenshot_path: Optional[str] = None
        task_num = 0

        # 3. Execute loop
        while True:
            if job_id:
                task_obj = self.task_store.get_next_pending(job_id)
                if task_obj is None:
                    break
                task_id = task_obj.task_id
                task_num = task_obj.sequence
                description = task_obj.description
                agent_type = task_obj.agent_type
                total = max(total, task_num)  # may have grown via new_tasks
                self.task_store.update_task(task_id, "running")
            else:
                # No persistence — run specs in order
                if task_num >= len(task_specs):
                    break
                spec = task_specs[task_num]
                task_num += 1
                task_id = None
                description = spec["description"]
                agent_type = spec.get("agent_type", "develop")

            _emit(f"task:{task_num}/{total}:{description[:40]}")
            self.logger.info(
                "task_loop_executing",
                task_num=task_num,
                total=total,
                agent_type=agent_type,
                description=description[:60],
            )

            try:
                result = await self._run_specialized_agent(
                    description,
                    agent_type,
                    session_id,
                    on_phase=on_phase,
                    job_id=None,     # prevent re-entering the loop
                    _direct=True,    # go straight to agent
                )

                if result.get("success"):
                    response_text = result.get("response", "")
                    all_responses.append(
                        f"**Task {task_num}: {description[:60]}**\n\n{response_text}"
                    )
                    new_files = result.get("files_created", [])
                    all_files.extend(new_files)
                    if result.get("screenshot_path"):
                        screenshot_path = result.get("screenshot_path")

                    # Agent may append new tasks dynamically
                    new_task_specs = result.get("new_tasks", [])
                    if new_task_specs:
                        for spec in new_task_specs:
                            if job_id:
                                new_task = self.task_store.create_task(
                                    job_id=job_id,
                                    description=spec["description"],
                                    agent_type=spec.get("agent_type", "develop"),
                                )
                                total = max(total, new_task.sequence)
                            else:
                                task_specs.append(spec)
                                total = len(task_specs)
                        self.logger.info(
                            "new_tasks_added",
                            count=len(new_task_specs),
                            total=total,
                        )

                    result_summary = response_text[:300]
                    if task_id:
                        self.task_store.update_task(task_id, "done", result_summary)
                else:
                    error = result.get("error", "agent failed")
                    all_responses.append(
                        f"**Task {task_num}: {description[:60]}** — failed: {error}"
                    )
                    if task_id:
                        self.task_store.update_task(task_id, "failed", error)
                    self.logger.warning(
                        "task_loop_task_failed",
                        task_num=task_num,
                        error=error,
                    )

            except Exception as exc:
                self.logger.error(
                    "task_loop_exception",
                    task_num=task_num,
                    error=str(exc),
                )
                if task_id:
                    self.task_store.update_task(task_id, "failed", str(exc))
                all_responses.append(
                    f"**Task {task_num}: {description[:60]}** — error: {exc}"
                )

            # Safety guard for no-persistence mode
            if not job_id and task_num >= len(task_specs):
                break

        combined = "\n\n---\n\n".join(all_responses) if all_responses else "(no output)"
        # Deduplicate files while preserving order
        seen: set[str] = set()
        unique_files: list[str] = []
        for f in all_files:
            if f not in seen:
                seen.add(f)
                unique_files.append(f)

        self.logger.info(
            "task_loop_complete",
            tasks_run=task_num,
            files_created=len(unique_files),
        )
        return {
            "success": True,
            "response": combined,
            "files_created": unique_files,
            "screenshot_path": screenshot_path,
            "task_count": total,
        }

    def _build_context_from_events(self, session_id: str) -> str:
        """Build conversation context from paginated events.

        Fetches the last 20 events and truncates large tool_result payloads
        to avoid stuffing the full execution trace into the context window.
        """
        events = self.session_memory.get_events(session_id, offset=-20, limit=20)
        if not events:
            return ""

        context_lines = ["\n\nRecent conversation:\n"]
        for ev in events:
            role = ev["role"]
            content = ev["content"]

            if role.startswith("event:"):
                event_type = role[len("event:"):]
                if event_type == "tool_result":
                    # Cap large tool outputs so they don't flood the prompt
                    content = content[:500] + ("…" if len(content) > 500 else "")
                context_lines.append(f"[{event_type}] {content}")
            elif role in ("user", "assistant"):
                context_lines.append(f"{role.capitalize()}: {content[:500]}")

        return "\n".join(context_lines)

    def _build_context(self, session_id: str, include_history: bool = True) -> str:
        """Build context string for streaming endpoint."""
        if not include_history:
            return ""
        return self._build_context_from_events(session_id)

    async def run_task(
        self,
        task: str,
        session_id: Optional[str] = None,
        include_history: bool = True,
        on_phase: Optional[Callable[[str], None]] = None,
        job_id: Optional[str] = None,
    ) -> dict:
        """Run a task and return the agent result.

        Args:
            task: The user task string.
            session_id: Existing session to continue, or None to create one.
            include_history: Whether to include conversation history.
            on_phase: Optional callback fired with a phase label string at
                key milestones. Used by the background job API to push live
                progress updates into the job store without polling.
        """
        def _emit_phase(label: str) -> None:
            if on_phase:
                try:
                    on_phase(label)
                except Exception:
                    pass

        if not session_id:
            session_id = f"session_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

        self.session_memory.get_or_create_session(session_id, self.workspace_path)
        self.session_memory.save_message(session_id, "user", task)

        config = self.model_router.get_model("coding")
        if not config:
            return {
                "success": False,
                "session_id": session_id,
                "error": "No coding model configured",
            }

        self.agent_logger.log_task_start("agent", {"task": task})

        # --- Context bridge check -------------------------------------------
        # Measure the session history size before dispatching.  If we are at
        # 82 %+ of the model's context window, generate a Context Bridge first,
        # swap to a fresh session pre-seeded with the bridge, and continue the
        # current task in that new session.  At 75–82 % we just flag a warning
        # so the Discord bot can nudge the user.
        handover_triggered = False
        handover_bridge: Optional[str] = None
        original_session_id: Optional[str] = None
        budget = self._check_context_budget(session_id, task)
        if budget == "bridge":
            _emit_phase("handover")
            self.logger.info("context_bridge_triggered", session_id=session_id)
            try:
                bridge_text, new_session_id = await self._run_handover(session_id, task)
                original_session_id = session_id
                session_id = new_session_id
                handover_triggered = True
                handover_bridge = bridge_text
                self.logger.info("session_swapped", new_session_id=session_id)
            except Exception as _he:
                self.logger.error("handover_failed", error=str(_he))
                # Continue with the old session rather than aborting the task.

        # Run LLM classifier and context building in parallel to save wall time.
        _emit_phase("preparing")
        import asyncio as _asyncio
        task_type, _ = await _asyncio.gather(
            self._detect_task_type(task),
            self._build_enriched_context(task),  # warm the RAG cache
        )
        # Re-build properly below (we discard the result here; context is
        # re-built inside _run_specialized_agent to pass it correctly).

        self.logger.info("task_type_detected", task_type=task_type)
        self.session_memory.emit_event(session_id, "status", {"phase": "start", "task_type": task_type})

        _phase_labels = {
            "plan": "planning",
            "develop": "developing",
            "review": "reviewing",
            "test": "testing",
            "architect": "designing",
            "research": "researching",
            "chat": "thinking",
            "sdlc": "sdlc:planning",
        }
        _emit_phase(_phase_labels.get(task_type, "working"))

        try:
            result = await self._run_specialized_agent(
                task, task_type, session_id, on_phase=on_phase, job_id=job_id
            )

            if result.get("success"):
                response = result.get("response", "")
                model_name = config.name

                self.session_memory.save_message(
                    session_id,
                    "assistant",
                    response,
                    model_name=model_name,
                )
                self.session_memory.emit_event(
                    session_id, "status", {"phase": "complete", "files": result.get("files_created", [])}
                )
                self.agent_logger.log_task_complete(
                    "agent", 0, {"response_length": len(response)}
                )

                # Post-execution skills — wiki-compile always runs; others on keyword match.
                post_skill_reports: list[str] = []
                _always_post = ["wiki-compile"]
                _keyword_post = [s for s in self._detect_skill_names(task, "post") if s not in _always_post]
                for skill_name in _always_post + _keyword_post:
                    try:
                        report = await self.skill_executor.execute_post(
                            skill_name, task, result, self.model_router
                        )
                        if report.get("report"):
                            post_skill_reports.append(report["report"])
                    except Exception as se:
                        self.logger.error("post_skill_failed", skill=skill_name, error=str(se))

                return {
                    "success": True,
                    "session_id": session_id,
                    "handover_triggered": handover_triggered,
                    "original_session_id": original_session_id,
                    "context_budget": budget,
                    "result": {
                        "response": response,
                        "task": task,
                        "task_type": task_type,
                        "files_created": result.get("files_created", []),
                        "skill_reports": post_skill_reports,
                        "screenshot_path": result.get("screenshot_path"),
                        "handover_bridge": handover_bridge,
                    },
                }
            else:
                raise Exception(result.get("error", "Agent failed"))

        except Exception as ex:
            import traceback
            self.logger.error("task_failed", error=str(ex), traceback=traceback.format_exc())
            self.session_memory.emit_event(session_id, "status", {"phase": "error", "error": str(ex)})
            self.session_memory.update_task_status(
                session_id, task, "failed", {"error": str(ex)}
            )
            return {
                "success": False,
                "session_id": session_id,
                "error": str(ex),
            }

    async def wake(self, session_id: str) -> dict:
        """Resume an interrupted session by replaying its last known state.

        Implements the Anthropic Managed Agents wake(sessionId) pattern.
        Reads the last events from the session, emits a wake event, and
        returns summary info so the caller can decide whether to re-run
        the last task.
        """
        summary = self.session_memory.get_session_summary(session_id)
        if not summary or summary.get("message_count", 0) == 0:
            return {"success": False, "error": f"Session '{session_id}' not found or empty"}

        # Fetch last events to find the most recent user message
        events = self.session_memory.get_events(session_id, offset=-10, limit=10)
        last_user_task = None
        for ev in reversed(events):
            if ev["role"] == "user":
                last_user_task = ev["content"]
                break

        self.session_memory.emit_event(session_id, "status", {"phase": "wake", "resumed": True})
        self.session_memory.update_session_status(session_id, "active")

        self.logger.info("session_woken", session_id=session_id, last_task=last_user_task)

        return {
            "success": True,
            "session_id": session_id,
            "message_count": summary["message_count"],
            "last_user_task": last_user_task,
            "status": "active",
        }

    def index_workspace(self, project_id: str = None) -> dict:
        """Index all files in the workspace for RAG and populate the MemoryWiki graph."""
        if project_id is None:
            project_id = Path(self.workspace_path).name

        rag_result = self.codebase_memory.index_workspace(self.workspace_path, project_id)

        # Populate MemoryWiki from static analysis of Python files
        self.memory_wiki.clear()
        py_files = list(Path(self.workspace_path).rglob("*.py"))  # lgtm[py/path-injection]
        wiki_errors = 0
        for py_file in py_files:
            rel_path = str(py_file.relative_to(self.workspace_path))
            try:
                analysis = self.code_analyzer.analyze_file(str(py_file))
                if not analysis.get("success"):
                    continue

                self.memory_wiki.add_file(rel_path, file_type="source", language="python")

                for fn in analysis.get("functions", []):
                    self.memory_wiki.add_function(
                        file_path=rel_path,
                        function_name=fn["name"],
                        signature=fn["name"],
                        line_start=fn["line_start"],
                        line_end=fn["line_end"],
                    )

                for cls in analysis.get("classes", []):
                    self.memory_wiki.add_class(
                        file_path=rel_path,
                        class_name=cls["name"],
                        line_start=cls["line_start"],
                        line_end=cls["line_end"],
                        methods=[m["name"] for m in cls.get("methods", [])],
                    )

                for imp in analysis.get("imports", []):
                    module = imp.get("module") or ""
                    names = imp.get("names") or []
                    if module:
                        self.memory_wiki.add_import(rel_path, module, names)

            except Exception as e:
                wiki_errors += 1
                self.logger.warning("wiki_index_error", file=rel_path, error=str(e))

        wiki_stats = self.memory_wiki.get_statistics()
        self.logger.info(
            "wiki_indexed",
            files=wiki_stats["files"],
            functions=wiki_stats["functions"],
            classes=wiki_stats["classes"],
            errors=wiki_errors,
        )

        return {**rag_result, "wiki": wiki_stats}

    async def run_stream(
        self, task: str, session_id: Optional[str] = None, include_history: bool = True
    ):
        if not session_id:
            session_id = f"session_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"

        self.session_memory.get_or_create_session(session_id, self.workspace_path)
        self.session_memory.save_message(session_id, "user", task)

        config = self.model_router.get_model("coding")
        if not config:
            raise ValueError("No coding model configured")

        context = self._build_context(session_id, include_history)
        
        prompt = f"""You are a helpful coding assistant. Respond to the following request:

{task}{context}"""

        full_response = ""
        async for chunk in self.model_router.generate_stream(prompt, config):
            full_response += chunk
            yield {"chunk": chunk, "full_response": full_response}

        self.session_memory.save_message(
            session_id, "assistant", full_response, model_name=config.name
        )

    def get_session_history(self, session_id: str) -> List[dict]:
        return self.session_memory.get_conversation_history(session_id)

    def list_sessions(self, limit: int = 20) -> List[dict]:
        return self.session_memory.list_sessions(limit)

    def get_session_info(self, session_id: str) -> dict:
        return self.session_memory.get_session_summary(session_id)
