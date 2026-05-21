from typing import TypedDict, Annotated, List, Optional, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import os
import re
import structlog

_PROJECT_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.\-]*$")

from agent.security.prompt_guard import guard_task
from agent.workspace_context import get_workspace

from llm import ModelRouter
from agent.memory import SessionMemory, CodebaseMemory
from agent.memory.memory_wiki import MemoryWiki
from agent.tools import FileSystemTool, PytestTool, CodeAnalyzer
from agent.agents.developer_agent import DeveloperAgent
from agent.agents.plan_agent import PlanAgent
from agent.agents.planner_agent import PlannerAgent
from agent.agents.plan_reviewer_agent import PlanReviewerAgent
from agent.agents.tester_agent import TesterAgent
from agent.agents.reviewer_agent import ReviewerAgent
from agent.agents.architect_agent import ArchitectAgent
from agent.agents.chat_agent import ChatAgent
from agent.agents.research_agent import ResearchAgent
from agent.agents.verifier_agent import VerifierAgent, VerifierResult
from agent.agents.mapper_agent import MapperAgent
from agent.agents.red_team_agent import RedTeamAgent
from agent.agents.documenter_agent import DocumenterAgent
from agent.agents.acceptance_tester_agent import AcceptanceTesterAgent
from agent.chain_runner import ChainRunner
from agent.skills.skill_loader import SkillManager
from agent.skills.wiki_manager import WikiManager
from agent.skills.skill_executor import SkillExecutor
from agent.orchestration import ContextBuilder, CriterionScoreStore, TaskRouter, VerifierCoordinator
from agent.orchestration.requirements_extractor import RequirementsExtractor
from agent.orchestration.app_probe import AppProbe
from observability.logging import AgentLogger

logger = structlog.get_logger()


class AgentState(TypedDict):
    task: str
    response: str
    session_id: str


class AgentOrchestrator:
    def __init__(
        self,
        workspace_path: str,  # noqa: ARG002 — kept for API compat; env vars used instead
        model_router: ModelRouter,
        session_db_path: str = "data/memory.db",
        chroma_path: str = "data/chroma_db",
    ):
        # Env-var-only pattern (GitTool pattern): workspace comes from trusted env vars,
        # not the HTTP-tainted workspace_path parameter — breaks the CodeQL taint chain.
        _effective = os.getenv("AGENT_EFFECTIVE_WORKSPACE", "").strip()
        _ws = _effective if _effective else os.getenv("WORKSPACE_PATH", "./workspace")
        self.workspace_path = _ws
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
        # Derive project name: non-empty when workspace is a subdirectory of the root.
        _ws_root = os.getenv("WORKSPACE_PATH", "./workspace")
        _ws_root_resolved = str(Path(_ws_root).resolve())
        _ws_resolved = str(Path(_ws).resolve())
        _project_name = Path(_ws).name if _ws_resolved != _ws_root_resolved else ""
        self.wiki_manager = WikiManager(workspace_path, project_name=_project_name)
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
        self.mapper_agent = MapperAgent(model_router, file_system_tool=self.fs_tool)
        self.red_team_agent = RedTeamAgent(model_router)
        self.documenter_agent = DocumenterAgent(model_router)
        self.requirements_extractor = RequirementsExtractor(model_router)
        self.planner_agent = PlannerAgent(model_router, requirements_extractor=self.requirements_extractor)
        self.plan_reviewer_agent = PlanReviewerAgent(model_router)
        self.verifier_agent = VerifierAgent(model_router)
        self.acceptance_tester_agent = AcceptanceTesterAgent(model_router)
        self.chain_runner = ChainRunner(self)

        # Task store — shares the same SQLite file as the job store
        from api.task_store import TaskStore
        self.task_store = TaskStore("data/jobs.db")

        # Orchestration sub-components
        self.task_router = TaskRouter(model_router)
        self.context_builder = ContextBuilder(
            model_router=model_router,
            skill_executor=self.skill_executor,
            codebase_memory=self.codebase_memory,
            session_memory=self.session_memory,
            skill_manager=self.skill_manager,
            memory_wiki=self.memory_wiki,
            skill_router=self.task_router,
        )
        self.verifier_coordinator = VerifierCoordinator(self.verifier_agent, model_router)
        self.criterion_score_store = CriterionScoreStore()

        # Collect model-switch notices emitted by the router during task execution.
        # Drained at each task-loop boundary and surfaced in job phase + response text.
        self._model_switch_notices: list[str] = []
        self.model_router.register_switch_callback(self._on_model_switch)

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
        _ws_now = get_workspace()
        self.session_memory.get_or_create_session(subagent_id, _ws_now)
        enriched_context = await self.context_builder.build(task)

        # Create isolated context for subagent
        isolated_context = {
            "session_id": subagent_id,
            "parent_session_id": parent_session_id,
            "workspace_path": _ws_now,
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
                project_id = Path(_ws_now).name
                for rel_path in files_created:
                    abs_path = Path(_ws_now) / rel_path
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
    
    def _on_model_switch(self, event) -> None:
        """Callback registered on ModelRouter — accumulates switch notices."""
        notice = (
            f"⚠️ Model switch: `{event.from_model}` → `{event.to_model}` "
            f"(reason: {event.reason})"
        )
        self._model_switch_notices.append(notice)
        self.logger.warning(
            "model_switch_detected",
            from_model=event.from_model,
            to_model=event.to_model,
            reason=event.reason,
        )

    def _drain_switch_notices(self, on_phase: Optional[Callable[[str], None]] = None) -> list[str]:
        """Return accumulated model-switch notices and clear the buffer.

        If *on_phase* is provided, also emits a ``model_switch:`` phase label
        so the job-store poller (and Discord bot) can surface it in real time.
        """
        if not self._model_switch_notices:
            return []
        notices = list(self._model_switch_notices)
        self._model_switch_notices.clear()
        if on_phase and notices:
            # Emit a single phase update with the first switch summary.
            first = notices[0]
            # Strip the emoji prefix for the compact phase label
            compact = first.replace("⚠️ ", "")
            try:
                on_phase(f"model_switch:{compact}")
            except Exception:
                pass
        return notices

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
        extra_context: str = "",
    ) -> dict:
        """Route a task to the appropriate agent.

        For "develop" and "research" task types the task is first decomposed
        into a plan and run through the task loop — unless _direct=True, which
        bypasses the loop (used when called from inside the loop to avoid
        infinite recursion).

        For all other types (plan, review, test, architect, chat, sdlc) the
        agent is called directly as before.
        """
        # Chain tasks: task string is "__chain__:<name>:<user_input>"
        if task_type == "chain" or task.startswith("__chain__:"):
            parts = task.split(":", 2)
            if len(parts) == 3 and parts[0] == "__chain__":
                chain_name = parts[1]
                user_input = parts[2]
            else:
                chain_name = task_type
                user_input = task
            return await self.chain_runner.run(
                chain_name, user_input, session_id, on_phase=on_phase, job_id=job_id
            )

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
        enriched_context = await self.context_builder.build(task, agent_type=task_type)
        # Session history is NOT injected for research/develop tasks inside
        # the task loop — only the wiki+RAG enriched context is used.  This
        # prevents cross-project session events (from a prior project switch)
        # from bleeding into the current project's research output and wiki.
        # Chat and other interactive types still get the full history so
        # conversational continuity is preserved.
        _include_history = task_type not in ("research", "develop", "test", "researcher")
        history = self.context_builder.build_events_context(session_id) if _include_history else ""
        context = {
            "session_id": session_id,
            "workspace_path": get_workspace(),
            "model_router": self.model_router,
            "tool_executor": session_executor,
            "enriched_context": enriched_context + history + extra_context,
            "on_phase": on_phase,
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
        elif task_type == "mapper":
            return await self.mapper_agent.run(task, context)
        elif task_type == "security":
            return await self.red_team_agent.run(task, context)
        elif task_type == "documenter":
            return await self.documenter_agent.run(task, context)
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

        # 1. Plan — build compact planning context then generate tasks + criteria
        _emit("planning:tasks")
        _ws_path = Path(getattr(self.tool_executor, "workspace_path", ".") if self.tool_executor else ".")
        planning_ctx = await self.context_builder.build_planning_context(objective)
        plan_result = await self.planner_agent.plan_with_criteria(
            objective,
            context=planning_ctx,
            task_type=task_type,
            workspace=_ws_path,
        )
        task_specs = list(plan_result.tasks)
        completion_criteria = plan_result.completion_criteria
        _acceptance_criteria = plan_result.acceptance_criteria

        # 1b. Review and improve the plan before executing (plan-review-plan loop).
        # Only runs for develop/sdlc tasks with ≥3 steps — skips trivial plans.
        if task_type in ("develop", "sdlc") and len(task_specs) >= 3:
            _emit("planning:review")
            task_specs = await self.plan_reviewer_agent.review(task_specs, objective)

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
        task_summaries: list[str] = []
        screenshot_path: Optional[str] = None
        task_num = 0
        # Keyed by agent_type — holds outputs from prior tasks for injection
        # into downstream tasks in the same loop.
        _task_outputs: dict[str, list[str]] = {}
        _verifier_rounds = 0
        _prev_verifier_score = -1   # sentinel: no previous round yet
        _plateau_count = 0          # consecutive rounds at the same score
        _zero_score_count = 0       # consecutive rounds at score == 0
        _final_verifier_score: int | None = None
        _verifier_snapshot_files: set[str] = set()  # files known after last verifier round
        # Criterion-driven loop state
        _fix_budget = max(1, int(os.getenv("FIX_BUDGET", "20")))
        _criterion_fix_count = 0
        _criterion_attempts: dict[str, int] = {}  # per-criterion failure count
        # Acceptance test loop state
        _acceptance_budget = max(1, int(os.getenv("ACCEPTANCE_BUDGET", "5")))
        _acceptance_fix_count = 0
        _last_acceptance_screenshot: Optional[str] = None
        # Develop/sdlc tasks get more rounds — complex builds need more fix cycles.
        # Research stays at 3 since each round is an LLM-heavy web search.
        if task_type in ("develop", "sdlc"):
            _MAX_VERIFIER_ROUNDS = max(1, int(os.getenv("DEVELOP_VERIFIER_ROUNDS", "6")))
        else:
            _MAX_VERIFIER_ROUNDS = max(1, int(os.getenv("RESEARCH_VERIFIER_ROUNDS", "3")))
        # Task types where verification is meaningful (skip chat, plan, mapper).
        _VERIFIABLE_TYPES = {"develop", "research", "sdlc"}

        # 3. Execute loop
        while True:
            if job_id:
                task_obj = self.task_store.get_next_pending(job_id)
                if task_obj is None:
                    # All tasks done — run verifier if applicable.
                    if completion_criteria and task_type in _VERIFIABLE_TYPES:
                        # Criterion-driven fix loop
                        _tex = self.tool_executor

                        async def _crit_shell_jid(cmd: str, _t=_tex) -> str:
                            if not _t:
                                return ""
                            try:
                                out = await _t.execute("shell", {"command": cmd})
                                return str(out) if out else ""
                            except Exception:
                                return ""

                        _ws_crit = Path(getattr(_tex, "workspace_path", ".") if _tex else ".")
                        combined_so_far = "\n\n---\n\n".join(all_responses)
                        _emit(f"verifying:criteria-{_criterion_fix_count + 1}")
                        crit_results = await self.verifier_coordinator.evaluate_criteria(
                            completion_criteria, _ws_crit, _crit_shell_jid, combined_so_far
                        )
                        _failing = [r for r in crit_results if not r.passed]
                        _passed_count = len(crit_results) - len(_failing)

                        # Split failing criteria: auto-checkable (file/command) vs behavioral
                        # (LLM-evaluated free-text). Only auto-checkable criteria gate fix
                        # rounds — behavioral criteria are advisory and cannot be verified
                        # without a live environment (e.g. "when opened in a browser").
                        _AUTO_PREFIXES = ("file exists:", "file contains:", "command exits 0:")
                        _auto_failing = [
                            r for r in _failing
                            if any(r.criterion.lower().startswith(p) for p in _AUTO_PREFIXES)
                        ]
                        _behavioral_failing = [r for r in _failing if r not in _auto_failing]

                        async def _run_final_verifier_jid() -> VerifierResult:
                            nonlocal _final_verifier_score
                            _emit("verifying:final")
                            _c = "\n\n---\n\n".join(all_responses)
                            _vr = await self.verifier_coordinator.run_verification(
                                objective, task_type, _c, all_files, tool_executor=self.tool_executor
                            )
                            _final_verifier_score = _vr.score
                            if _vr.passed:
                                try:
                                    self.session_memory.store_episodic(session_id, objective, _c[:500], _vr.score, task_type)
                                except Exception:
                                    pass
                            return _vr

                        if not _auto_failing:
                            _vr = await _run_final_verifier_jid()
                            _behavioral_note = (
                                f" ({len(_behavioral_failing)} behavioral criteria not auto-verifiable)"
                                if _behavioral_failing else ""
                            )
                            task_summaries.append(f"✅ **All auto-checkable criteria satisfied**{_behavioral_note} — score {_vr.score}/10")
                            for _c in _criterion_attempts:
                                self.criterion_score_store.record(_c, succeeded=True)
                            _build_criteria_done = True
                        elif _criterion_fix_count >= _fix_budget:
                            _vr = await _run_final_verifier_jid()
                            task_summaries.append(
                                f"🎯 Fix budget ({_fix_budget}) exhausted — {_passed_count}/{len(completion_criteria)} criteria passing — score {_vr.score}/10"
                            )
                            _still_failing = {r.criterion for r in _auto_failing}
                            for _c in _criterion_attempts:
                                self.criterion_score_store.record(_c, succeeded=_c not in _still_failing)
                            _build_criteria_done = True
                        else:
                            _target = next(
                                (r for r in _auto_failing
                                 if _criterion_attempts.get(r.criterion, 0) < self.criterion_score_store.attempt_budget(r.criterion)),
                                None,
                            )
                            if _target is None:
                                _vr = await _run_final_verifier_jid()
                                task_summaries.append(f"🔍 All auto-checkable failing criteria abandoned — score {_vr.score}/10")
                                _still_failing = {r.criterion for r in _auto_failing}
                                for _c in _criterion_attempts:
                                    self.criterion_score_store.record(_c, succeeded=_c not in _still_failing)
                                _build_criteria_done = True
                            else:
                                _build_criteria_done = False

                        if not _build_criteria_done:
                            _criterion_attempts[_target.criterion] = _criterion_attempts.get(_target.criterion, 0) + 1
                            _criterion_fix_count += 1
                            _fix_spec = self.verifier_coordinator.make_targeted_fix_spec(
                                _target, objective, _criterion_fix_count,
                                screenshot_path=_last_acceptance_screenshot,
                            )
                            new_fix = self.task_store.create_task(job_id=job_id, description=_fix_spec["description"], agent_type=_fix_spec["agent_type"])
                            total = max(total, new_fix.sequence)
                            task_summaries.append(f"🎯 **Criterion fix** ({_criterion_fix_count}/{_fix_budget}) — failing: {_target.criterion[:60]}")
                            self.logger.info("criterion_fix_injected", criterion=_target.criterion[:60], attempt=_criterion_attempts[_target.criterion], fix_num=_criterion_fix_count)
                            continue

                        # Acceptance test loop — run after build criteria complete
                        if _acceptance_criteria and task_type in ("develop", "sdlc") and _acceptance_fix_count < _acceptance_budget:
                            _emit(f"verifying:acceptance-{_acceptance_fix_count + 1}")
                            _tex_acc = self.tool_executor

                            async def _acc_shell(cmd: str, _t=_tex_acc) -> str:
                                if not _t:
                                    return ""
                                try:
                                    out = await _t.execute("shell", {"command": cmd})
                                    return str(out) if out else ""
                                except Exception:
                                    return ""

                            _ws_acc = Path(getattr(_tex_acc, "workspace_path", ".") if _tex_acc else ".")
                            _app_probe = AppProbe(_ws_acc, shell_fn=_acc_shell)
                            acc_results = await self.verifier_coordinator.run_acceptance_tests(
                                _acceptance_criteria, _ws_acc, _app_probe, self.acceptance_tester_agent
                            )
                            # Capture screenshot for fix-loop context and final Discord post
                            _cap = self.verifier_coordinator.last_screenshot_path
                            if _cap:
                                _last_acceptance_screenshot = _cap
                                screenshot_path = _cap
                            if not acc_results:
                                # Empty = no server entry point (static deliverable) — skip loop
                                task_summaries.append("⏭️ Acceptance tests skipped — no server entry point detected")
                                break

                            _acc_failing = [r for r in acc_results if not r.passed]
                            _acc_passed = len(acc_results) - len(_acc_failing)

                            if not _acc_failing:
                                task_summaries.append(f"✅ **All {len(_acceptance_criteria)} acceptance criteria satisfied**")
                                break

                            if _acceptance_fix_count >= _acceptance_budget - 1:
                                task_summaries.append(
                                    f"🎯 Acceptance budget ({_acceptance_budget}) exhausted — {_acc_passed}/{len(_acceptance_criteria)} passing"
                                )
                                break

                            _acceptance_fix_count += 1
                            _acc_target = _acc_failing[0]
                            _acc_fix_spec = self.verifier_coordinator.make_targeted_fix_spec(
                                type("CR", (), {"criterion": _acc_target.criterion, "passed": False, "detail": _acc_target.detail})(),
                                objective,
                                _acceptance_fix_count,
                                screenshot_path=_last_acceptance_screenshot,
                            )
                            new_acc_fix = self.task_store.create_task(
                                job_id=job_id,
                                description=_acc_fix_spec["description"],
                                agent_type=_acc_fix_spec["agent_type"],
                            )
                            total = max(total, new_acc_fix.sequence)
                            task_summaries.append(
                                f"🖼️ **Acceptance fix** ({_acceptance_fix_count}/{_acceptance_budget}) — {_acc_target.criterion[:60]}"
                            )
                            self.logger.info(
                                "acceptance_fix_injected",
                                criterion=_acc_target.criterion[:60],
                                fix_num=_acceptance_fix_count,
                            )
                            continue

                        break

                    elif task_type in _VERIFIABLE_TYPES and _verifier_rounds < _MAX_VERIFIER_ROUNDS:
                        combined_so_far = "\n\n---\n\n".join(all_responses)
                        _emit(f"verifying:round-{_verifier_rounds + 1}")
                        # Capture which files were added since the last verifier round
                        _new_files_this_round = [f for f in all_files if f not in _verifier_snapshot_files]
                        _verifier_snapshot_files = set(all_files)
                        vresult = await self.verifier_coordinator.run_verification(
                            objective, task_type, combined_so_far, all_files,
                            tool_executor=self.tool_executor,
                        )
                        self.logger.info(
                            "verifier_result",
                            round=_verifier_rounds + 1,
                            score=vresult.score,
                            passed=vresult.passed,
                            gaps=len(vresult.gaps),
                        )
                        _verifier_rounds += 1
                        _final_verifier_score = vresult.score
                        if vresult.passed:
                            try:
                                self.session_memory.store_episodic(
                                    session_id, objective,
                                    combined_so_far[:500], vresult.score, task_type,
                                )
                            except Exception:
                                pass
                        if not vresult.passed:
                            # Stagnation rules:
                            #  - Hard stop: score drops 2+ pts (getting worse, not just noise)
                            #  - Soft stop: score >= 5 and has plateaued for 2 consecutive rounds
                            #  - Zero stop: score == 0 for 2 consecutive rounds (futile)
                            #  - Very low scores (< 5) always get full round budget
                            if vresult.score == 0:
                                _zero_score_count += 1
                                if _zero_score_count >= 2:
                                    task_summaries.append(
                                        f"🔍 **Verifier** stuck at 0/10 for {_zero_score_count} rounds — stopping"
                                    )
                                    break
                            else:
                                _zero_score_count = 0
                            if _prev_verifier_score >= 0:
                                _drop = _prev_verifier_score - vresult.score
                                if _drop >= 2:
                                    task_summaries.append(
                                        f"🔍 **Verifier** stagnated at {vresult.score}/10 "
                                        f"(dropped {_drop} pts from {_prev_verifier_score}/10) — stopping"
                                    )
                                    break
                                if vresult.score == _prev_verifier_score and vresult.score >= 5:
                                    _plateau_count += 1
                                    if _plateau_count >= 2:
                                        task_summaries.append(
                                            f"🔍 **Verifier** stagnated at {vresult.score}/10 "
                                            f"(plateau ×2) — stopping"
                                        )
                                        break
                                else:
                                    _plateau_count = 0  # score improved or still in low range
                            fix_specs = self.verifier_coordinator.make_fix_specs(
                                objective, task_type, vresult, _verifier_rounds,
                                files_created=all_files,
                                files_changed_this_round=_new_files_this_round,
                                prev_score=_prev_verifier_score,
                            )
                            _prev_verifier_score = vresult.score
                            for spec in fix_specs:
                                new_fix = self.task_store.create_task(
                                    job_id=job_id,
                                    description=spec["description"],
                                    agent_type=spec["agent_type"],
                                )
                                total = max(total, new_fix.sequence)
                            task_summaries.append(
                                f"🔍 **Verifier** (round {_verifier_rounds}/{_MAX_VERIFIER_ROUNDS}) "
                                f"— score {vresult.score}/10, injecting {len(fix_specs)} fix task(s)"
                            )
                            continue  # loop back to pick up fix tasks
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
                    if completion_criteria and task_type in _VERIFIABLE_TYPES:
                        # Criterion-driven fix loop (no-persistence path)
                        _tex2 = self.tool_executor

                        async def _crit_shell_np(cmd: str, _t=_tex2) -> str:
                            if not _t:
                                return ""
                            try:
                                out = await _t.execute("shell", {"command": cmd})
                                return str(out) if out else ""
                            except Exception:
                                return ""

                        _ws_crit2 = Path(getattr(_tex2, "workspace_path", ".") if _tex2 else ".")
                        combined_so_far = "\n\n---\n\n".join(all_responses)
                        _emit(f"verifying:criteria-{_criterion_fix_count + 1}")
                        crit_results2 = await self.verifier_coordinator.evaluate_criteria(
                            completion_criteria, _ws_crit2, _crit_shell_np, combined_so_far
                        )
                        _failing2 = [r for r in crit_results2 if not r.passed]
                        _passed_count2 = len(crit_results2) - len(_failing2)

                        async def _run_final_verifier_np() -> VerifierResult:
                            nonlocal _final_verifier_score
                            _emit("verifying:final")
                            _c = "\n\n---\n\n".join(all_responses)
                            _vr = await self.verifier_coordinator.run_verification(
                                objective, task_type, _c, all_files, tool_executor=self.tool_executor
                            )
                            _final_verifier_score = _vr.score
                            if _vr.passed:
                                try:
                                    self.session_memory.store_episodic(session_id, objective, _c[:500], _vr.score, task_type)
                                except Exception:
                                    pass
                            return _vr

                        if not _failing2:
                            _vr2 = await _run_final_verifier_np()
                            task_summaries.append(f"✅ **All {len(completion_criteria)} criteria satisfied** — score {_vr2.score}/10")
                            for _c2 in _criterion_attempts:
                                self.criterion_score_store.record(_c2, succeeded=True)
                            _build_criteria_done2 = True
                        elif _criterion_fix_count >= _fix_budget:
                            _vr2 = await _run_final_verifier_np()
                            task_summaries.append(
                                f"🎯 Fix budget ({_fix_budget}) exhausted — {_passed_count2}/{len(completion_criteria)} criteria passing — score {_vr2.score}/10"
                            )
                            _still_failing2 = {r.criterion for r in _failing2}
                            for _c2 in _criterion_attempts:
                                self.criterion_score_store.record(_c2, succeeded=_c2 not in _still_failing2)
                            _build_criteria_done2 = True
                        else:
                            _target2 = next(
                                (r for r in _failing2
                                 if _criterion_attempts.get(r.criterion, 0) < self.criterion_score_store.attempt_budget(r.criterion)),
                                None,
                            )
                            if _target2 is None:
                                _vr2 = await _run_final_verifier_np()
                                task_summaries.append(f"🔍 All failing criteria abandoned — score {_vr2.score}/10")
                                _still_failing2 = {r.criterion for r in _failing2}
                                for _c2 in _criterion_attempts:
                                    self.criterion_score_store.record(_c2, succeeded=_c2 not in _still_failing2)
                                _build_criteria_done2 = True
                            else:
                                _build_criteria_done2 = False

                        if not _build_criteria_done2:
                            _criterion_attempts[_target2.criterion] = _criterion_attempts.get(_target2.criterion, 0) + 1
                            _criterion_fix_count += 1
                            _fix_spec2 = self.verifier_coordinator.make_targeted_fix_spec(
                                _target2, objective, _criterion_fix_count,
                                screenshot_path=_last_acceptance_screenshot,
                            )
                            task_specs.append(_fix_spec2)
                            total = len(task_specs)
                            task_summaries.append(f"🎯 **Criterion fix** ({_criterion_fix_count}/{_fix_budget}) — failing: {_target2.criterion[:60]}")
                            self.logger.info("criterion_fix_injected", criterion=_target2.criterion[:60], attempt=_criterion_attempts[_target2.criterion], fix_num=_criterion_fix_count)
                            continue

                        # Acceptance test loop — run after build criteria complete
                        if _acceptance_criteria and task_type in ("develop", "sdlc") and _acceptance_fix_count < _acceptance_budget:
                            _emit(f"verifying:acceptance-{_acceptance_fix_count + 1}")
                            _tex_acc2 = self.tool_executor

                            async def _acc_shell2(cmd: str, _t=_tex_acc2) -> str:
                                if not _t:
                                    return ""
                                try:
                                    out = await _t.execute("shell", {"command": cmd})
                                    return str(out) if out else ""
                                except Exception:
                                    return ""

                            _ws_acc2 = Path(getattr(_tex_acc2, "workspace_path", ".") if _tex_acc2 else ".")
                            _app_probe2 = AppProbe(_ws_acc2, shell_fn=_acc_shell2)
                            acc_results2 = await self.verifier_coordinator.run_acceptance_tests(
                                _acceptance_criteria, _ws_acc2, _app_probe2, self.acceptance_tester_agent
                            )
                            _cap2 = self.verifier_coordinator.last_screenshot_path
                            if _cap2:
                                _last_acceptance_screenshot = _cap2
                                screenshot_path = _cap2
                            if not acc_results2:
                                task_summaries.append("⏭️ Acceptance tests skipped — no server entry point detected")
                                break

                            _acc_failing2 = [r for r in acc_results2 if not r.passed]
                            _acc_passed2 = len(acc_results2) - len(_acc_failing2)

                            if not _acc_failing2:
                                task_summaries.append(f"✅ **All {len(_acceptance_criteria)} acceptance criteria satisfied**")
                                break

                            if _acceptance_fix_count >= _acceptance_budget - 1:
                                task_summaries.append(
                                    f"🎯 Acceptance budget ({_acceptance_budget}) exhausted — {_acc_passed2}/{len(_acceptance_criteria)} passing"
                                )
                                break

                            _acceptance_fix_count += 1
                            _acc_target2 = _acc_failing2[0]
                            _acc_fix_spec2 = self.verifier_coordinator.make_targeted_fix_spec(
                                type("CR", (), {"criterion": _acc_target2.criterion, "passed": False, "detail": _acc_target2.detail})(),
                                objective,
                                _acceptance_fix_count,
                                screenshot_path=_last_acceptance_screenshot,
                            )
                            task_specs.append(_acc_fix_spec2)
                            total = len(task_specs)
                            task_summaries.append(
                                f"🖼️ **Acceptance fix** ({_acceptance_fix_count}/{_acceptance_budget}) — {_acc_target2.criterion[:60]}"
                            )
                            self.logger.info(
                                "acceptance_fix_injected",
                                criterion=_acc_target2.criterion[:60],
                                fix_num=_acceptance_fix_count,
                            )
                            continue

                        break

                    elif task_type in _VERIFIABLE_TYPES and _verifier_rounds < _MAX_VERIFIER_ROUNDS:
                        combined_so_far = "\n\n---\n\n".join(all_responses)
                        _emit(f"verifying:round-{_verifier_rounds + 1}")
                        _new_files_this_round = [f for f in all_files if f not in _verifier_snapshot_files]
                        _verifier_snapshot_files = set(all_files)
                        vresult = await self.verifier_coordinator.run_verification(
                            objective, task_type, combined_so_far, all_files,
                            tool_executor=self.tool_executor,
                        )
                        _verifier_rounds += 1
                        _final_verifier_score = vresult.score
                        if vresult.passed:
                            try:
                                self.session_memory.store_episodic(
                                    session_id, objective,
                                    combined_so_far[:500], vresult.score, task_type,
                                )
                            except Exception:
                                pass
                        if not vresult.passed:
                            if vresult.score == 0:
                                _zero_score_count += 1
                                if _zero_score_count >= 2:
                                    task_summaries.append(
                                        f"🔍 **Verifier** stuck at 0/10 for {_zero_score_count} rounds — stopping"
                                    )
                                    break
                            else:
                                _zero_score_count = 0
                            if _prev_verifier_score >= 0:
                                _drop = _prev_verifier_score - vresult.score
                                if _drop >= 2:
                                    task_summaries.append(
                                        f"🔍 **Verifier** stagnated at {vresult.score}/10 "
                                        f"(dropped {_drop} pts) — stopping"
                                    )
                                    break
                                if vresult.score == _prev_verifier_score and vresult.score >= 5:
                                    _plateau_count += 1
                                    if _plateau_count >= 2:
                                        task_summaries.append(
                                            f"🔍 **Verifier** stagnated at {vresult.score}/10 "
                                            f"(plateau ×2) — stopping"
                                        )
                                        break
                                else:
                                    _plateau_count = 0
                            fix_specs = self.verifier_coordinator.make_fix_specs(
                                objective, task_type, vresult, _verifier_rounds,
                                files_created=all_files,
                                files_changed_this_round=_new_files_this_round,
                                prev_score=_prev_verifier_score,
                            )
                            _prev_verifier_score = vresult.score
                            task_specs.extend(fix_specs)
                            total = len(task_specs)
                            task_summaries.append(
                                f"🔍 **Verifier** (round {_verifier_rounds}/{_MAX_VERIFIER_ROUNDS}) "
                                f"— score {vresult.score}/10, injecting {len(fix_specs)} fix task(s)"
                            )
                            continue
                    break
                spec = task_specs[task_num]
                task_num += 1
                task_id = None
                description = spec["description"]
                agent_type = spec.get("agent_type", "develop")

            _emit(f"task:{task_num}/{total}:{agent_type}:{description[:40]}")
            self.logger.info(
                "task_loop_executing",
                task_num=task_num,
                total=total,
                agent_type=agent_type,
                description=description[:60],
            )

            _current_agent_type = agent_type  # capture for closure

            def _wrapped_on_phase(inner_label: str, _at=_current_agent_type) -> None:
                _emit(f"task:{task_num}/{total}:{_at}:{inner_label}")

            try:
                # Inject prior research findings into downstream tasks.
                # develop/test get recent snippets; documenter gets the full set
                # so it has all research content to synthesize from.
                _extra = ""
                _research_outputs = _task_outputs.get("research", []) + _task_outputs.get("researcher", [])
                if _research_outputs:
                    _snippet_cap = self.context_builder.char_budget(fraction=0.015, cap=3_000)
                    _doc_cap = self.context_builder.char_budget(fraction=0.08, cap=40_000)
                    if agent_type in ("develop", "test"):
                        snippets = [s[:_snippet_cap] for s in _research_outputs[-2:]]
                        _extra = "\n\n## Prior research findings\n\n" + "\n\n---\n\n".join(snippets)
                    elif agent_type == "documenter":
                        _extra = "\n\n## Research findings to synthesize\n\n" + "\n\n---\n\n".join(
                            s[:_doc_cap] for s in _research_outputs
                        )

                result = await self._run_specialized_agent(
                    description,
                    agent_type,
                    session_id,
                    on_phase=_wrapped_on_phase,
                    job_id=None,     # prevent re-entering the loop
                    _direct=True,    # go straight to agent
                    extra_context=_extra,
                )

                # Surface any model-switch events that fired during this step.
                switch_notices = self._drain_switch_notices(on_phase)
                if switch_notices:
                    for notice in switch_notices:
                        all_responses.append(notice)
                        task_summaries.append(notice)

                if result.get("success"):
                    response_text = result.get("response", "")
                    all_responses.append(
                        f"**Task {task_num}: {description[:60]}**\n\n{response_text}"
                    )
                    new_files = result.get("files_created", [])
                    all_files.extend(new_files)
                    if result.get("screenshot_path"):
                        screenshot_path = result.get("screenshot_path")

                    # Capture task output so later tasks in the loop can use it.
                    _store_cap = self.context_builder.char_budget(fraction=0.05, cap=10_000)
                    _task_outputs.setdefault(agent_type, []).append(response_text[:_store_cap])

                    # Update code-dependency graph when developer changes files.
                    if agent_type in ("develop", "developer") and new_files:
                        try:
                            self.memory_wiki.update_from_files(new_files)
                        except Exception:
                            pass

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

                    # Collect a one-line summary per task for the Discord Done message.
                    completion_summary = result.get("completion_summary", "").strip()
                    short = completion_summary or response_text[:80].replace("\n", " ").strip()
                    task_summaries.append(f"✅ **{description[:60]}** — {short}")

                    # Persist subtask learnings to wiki — skip for research tasks to
                    # avoid N extra LLM calls in a research loop.
                    if agent_type not in ("research", "researcher"):
                        try:
                            await self.skill_executor.execute_post(
                                "wiki-compile", description, result, self.model_router
                            )
                        except Exception as _we:
                            self.logger.warning("subtask_wiki_compile_failed", task_num=task_num, error=str(_we))
                else:
                    error = result.get("error", "agent failed")
                    all_responses.append(
                        f"**Task {task_num}: {description[:60]}** — failed: {error}"
                    )
                    task_summaries.append(f"❌ **{description[:60]}** — {error[:80]}")
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
                task_summaries.append(f"❌ **{description[:60]}** — {str(exc)[:80]}")

            # Safety guard for no-persistence mode
            if not job_id and task_num >= len(task_specs):
                break

        combined = "\n\n---\n\n".join(all_responses) if all_responses else "(no output)"

        # Build enhanced job summary: header counts + per-task lines + next-steps hint.
        failed_count = sum(1 for s in task_summaries if s.startswith("❌"))
        done_count = sum(1 for s in task_summaries if s.startswith("✅"))
        if task_summaries:
            header = (
                f"**{done_count}/{task_num} tasks completed**"
                + (f" · {failed_count} failed" if failed_count else "")
            )
            if failed_count:
                next_steps = (
                    "\n\n**Next steps:** Review the errors above. "
                    "Use `!dev <description>` to continue fixing or `!result` for full details."
                )
            else:
                next_steps = (
                    "\n\n**Next steps:** Changes applied. Run your test suite to verify, "
                    "or `!result` to review the full output."
                )
            job_summary = header + "\n\n" + "\n".join(task_summaries) + next_steps
        else:
            job_summary = ""
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
            "job_summary": job_summary,
            "verifier_score": _final_verifier_score,
        }

    def _detect_task_type_keyword(self, task: str) -> str:
        """Synchronous keyword-only task classifier — zero latency, no LLM call.

        Used by the background job API to pre-classify a task before the async
        LLM classifier runs inside run_task(). Delegates to TaskRouter._detect_keyword.
        """
        return self.task_router._detect_keyword(task)

    async def run_task(
        self,
        task: str,
        session_id: Optional[str] = None,
        include_history: bool = True,
        on_phase: Optional[Callable[[str], None]] = None,
        job_id: Optional[str] = None,
        force_task_type: Optional[str] = None,
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
            session_id = f"session_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

        # Sanitize input and reject known prompt-injection patterns before
        # the task string enters any LLM prompt.
        try:
            task = guard_task(task)
        except ValueError as e:
            return {"success": False, "session_id": session_id, "error": str(e)}

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
        budget = self.context_builder.check_budget(session_id, task)
        if budget == "bridge":
            _emit_phase("handover")
            self.logger.info("context_bridge_triggered", session_id=session_id)
            try:
                bridge_text, new_session_id = await self.context_builder.build_handover(
                    session_id, task, self.workspace_path
                )
                original_session_id = session_id
                session_id = new_session_id
                handover_triggered = True
                handover_bridge = bridge_text
                self.logger.info("session_swapped", new_session_id=session_id)
            except Exception as _he:
                self.logger.error("handover_failed", error=str(_he))
                # Continue with the old session rather than aborting the task.

        # Resolve task type.  force_task_type bypasses the classifier entirely —
        # useful for Discord commands like !dev that guarantee the type is known.
        # Otherwise run the LLM classifier and context building in parallel.
        _emit_phase("preparing")
        import asyncio as _asyncio
        if force_task_type:
            task_type = force_task_type
            await self.context_builder.build(task)   # warm cache only
        else:
            task_type, _ = await _asyncio.gather(
                self.task_router.detect(task),
                self.context_builder.build(task),  # warm the RAG cache
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
                _keyword_post = [s for s in self.task_router.detect_skills(task, "post") if s not in _always_post]
                _wiki_verifier_score = result.get("verifier_score")
                for skill_name in _always_post + _keyword_post:
                    try:
                        report = await self.skill_executor.execute_post(
                            skill_name, task, result, self.model_router,
                            verifier_score=_wiki_verifier_score,
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
                        "job_summary": result.get("job_summary", ""),
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
        py_files = list(Path(self.workspace_path).rglob("*.py"))
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
        try:
            task = guard_task(task)
        except ValueError as e:
            yield {"error": str(e), "chunk": "", "full_response": ""}
            return

        if not session_id:
            session_id = f"session_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"

        self.session_memory.get_or_create_session(session_id, self.workspace_path)
        self.session_memory.save_message(session_id, "user", task)

        config = self.model_router.get_model("coding")
        if not config:
            raise ValueError("No coding model configured")

        context = self.context_builder.build_context(session_id, include_history)
        
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

    def delete_project(self, project_name: str, dry_run: bool = False) -> dict:
        """Remove all agent-managed data for a project from every storage layer.

        Clears the Chroma vector index, SQLite jobs/tasks, SQLite sessions, and
        the .agent-wiki directory.  The project source files are never touched.

        Args:
            project_name: Project subdirectory name (or absolute path — containment
                          is enforced against WORKSPACE_PATH regardless).
            dry_run:      When True, return a count preview without deleting.

        Returns:
            Summary dict with counts of what was (or would be) deleted.
        """
        import shutil

        _project_name = project_name.strip()
        if (
            not _project_name
            or not _PROJECT_NAME_RE.match(_project_name)
            or _project_name in {".", ".."}
            or Path(_project_name).name != _project_name
        ):
            raise ValueError(f"Invalid project_name {project_name!r}")
        _ws_root = Path(self.workspace_path).resolve()
        _project_dir = (_ws_root / _project_name).resolve()
        if not _project_dir.is_relative_to(_ws_root):
            raise ValueError(
                f"project_name {project_name!r} is outside workspace root {_ws_root}"
            )

        _project_path_str = str(_project_dir)
        _project_short_name = _project_dir.name

        # --- Preview phase (always runs) ---
        session_ids = self.session_memory.list_sessions_by_project(_project_path_str)
        job_count = self.task_store.count_by_session_ids(session_ids)
        chroma_chunks = self.codebase_memory.count_project_chunks(_project_short_name)

        wiki_entries = 0
        wiki_dir = (_project_dir / ".agent-wiki").resolve()
        if not wiki_dir.is_relative_to(_ws_root):
            raise ValueError(f"wiki_dir {wiki_dir!r} outside workspace root {_ws_root}")
        wiki_index = (wiki_dir / "index.md").resolve()
        if not wiki_index.is_relative_to(_ws_root):
            raise ValueError(f"wiki_index {wiki_index!r} outside workspace root {_ws_root}")
        if wiki_index.exists():
            try:
                lines = wiki_index.read_text(encoding="utf-8").splitlines()
                wiki_entries = sum(
                    1 for ln in lines
                    if ln.startswith("|") and ".md" in ln and "Path" not in ln
                )
            except OSError:
                pass

        summary = {
            "project_path": _project_path_str,
            "project_name": _project_short_name,
            "sessions": len(session_ids),
            "jobs": job_count,
            "chroma_chunks": chroma_chunks,
            "wiki_entries": wiki_entries,
            "dry_run": dry_run,
        }

        if dry_run:
            return summary

        # --- Delete phase ---
        self.codebase_memory.clear_project(_project_short_name)
        self.task_store.delete_by_session_ids(session_ids)
        deleted_sessions = self.session_memory.delete_sessions_by_project(_project_path_str)

        if wiki_dir.exists():
            shutil.rmtree(wiki_dir)

        summary["deleted_sessions"] = deleted_sessions
        self.logger.info("project_deleted", **{k: v for k, v in summary.items() if isinstance(v, (str, int, bool, float))})
        return summary
