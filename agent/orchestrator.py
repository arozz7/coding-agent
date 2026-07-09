from typing import TYPE_CHECKING, TypedDict, List, Optional, Callable
from pathlib import Path
import os
import structlog

if TYPE_CHECKING:
    from agent.tools.tool_executor import EventEmittingExecutor

from agent import project_lifecycle
from agent.security.prompt_guard import guard_task
from agent.session_id import new_session_id
from agent.workspace_context import get_workspace
from llm import ModelRouter
from agent.memory import SessionMemory, CodebaseMemory
from agent.memory.memory_wiki import MemoryWiki
from agent.tools import FileSystemTool, PytestTool, CodeAnalyzer
from agent.chain_runner import ChainRunner
from agent.skills.skill_loader import SkillManager
from agent.skills.wiki_manager import WikiManager
from agent.skills.skill_executor import SkillExecutor
from agent.orchestration import ContextBuilder, CriterionScoreStore, TaskRouter, VerifierCoordinator
from agent.orchestration.requirements_extractor import RequirementsExtractor
from agent.orchestration.objective_resolver import ObjectiveResolver
from agent.orchestration.agent_factory import create_agents
from agent.orchestration.subagent_manager import SubagentManager
from agent.orchestration.task_loop import TaskLoop, TaskLoopDeps
from observability.logging import AgentLogger

logger = structlog.get_logger()

# _run_specialized_agent dispatch: most task_type values match their
# self.agents[...] key directly (plan, architect, research, mapper,
# documenter, chat); these three don't, plus anything unmatched falls
# through to "developer".
_TASK_TYPE_TO_AGENT_KEY = {"review": "reviewer", "test": "tester", "security": "red_team"}


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
        # Env-var-only pattern: workspace comes from trusted env vars, not the
        # HTTP-tainted workspace_path parameter — breaks the CodeQL taint chain.
        # Every tool/manager below is built from this same resolved value (`_ws`),
        # never from the raw `workspace_path` parameter, so there is exactly one
        # source of truth for "where is this orchestrator's workspace" — a prior
        # version built self.workspace_path from `_ws` but the tools from the
        # ignored parameter, which could silently diverge if the two ever disagreed.
        _effective = os.getenv("AGENT_EFFECTIVE_WORKSPACE", "").strip()
        _ws = _effective if _effective else os.getenv("WORKSPACE_PATH", "./workspace")
        self.workspace_path = _ws
        self.model_router = model_router
        self.session_memory = SessionMemory(session_db_path)
        self.codebase_memory = CodebaseMemory(chroma_path)
        self.fs_tool = FileSystemTool(_ws)
        self.pytest_tool = PytestTool(_ws)
        self.code_analyzer = CodeAnalyzer()
        from agent.tools.shell_tool import ShellTool
        from agent.tools.browser_tool import BrowserTool
        from agent.tools.tool_executor import ToolExecutor, EventEmittingExecutor
        self._EventEmittingExecutor = EventEmittingExecutor
        self.shell_tool = ShellTool(_ws)
        self.browser_tool = BrowserTool(_ws)
        self.tool_executor = ToolExecutor(_ws, self.code_analyzer, self.pytest_tool)
        self.skill_manager = SkillManager("skills")
        _ws_root = os.getenv("WORKSPACE_PATH", "./workspace")
        _ws_root_resolved = str(Path(_ws_root).resolve())
        _ws_resolved = str(Path(_ws).resolve())
        _project_name = Path(_ws).name if _ws_resolved != _ws_root_resolved else ""
        self.wiki_manager = WikiManager(_ws, project_name=_project_name)
        self.wiki_manager._ensure_dirs()
        self.skill_executor = SkillExecutor(self.wiki_manager, self.skill_manager)
        self.memory_wiki = MemoryWiki(project_id=Path(_ws).name)

        from mcp.server import create_mcp_server
        self.mcp_server = create_mcp_server(_ws)

        self.logger = logger.bind(component="agent_orchestrator")
        self.agent_logger = AgentLogger("orchestrator")

        self.requirements_extractor = RequirementsExtractor(model_router)
        self.objective_resolver = ObjectiveResolver(model_router)
        _agents = create_agents(
            model_router,
            fs_tool=self.fs_tool,
            shell_tool=self.shell_tool,
            browser_tool=self.browser_tool,
            code_analyzer=self.code_analyzer,
            pytest_tool=self.pytest_tool,
            requirements_extractor=self.requirements_extractor,
        )
        self.agents = _agents
        # Kept as direct attributes: agent/sdlc_workflow.py reads these three
        # off the orchestrator instance directly (self.orch.plan_agent, etc.),
        # outside of _run_specialized_agent's dispatch. Every other agent type
        # is looked up via self.agents[...] — see _run_specialized_agent.
        self.plan_agent = _agents["plan"]
        self.developer_agent = _agents["developer"]
        self.tester_agent = _agents["tester"]
        self.chain_runner = ChainRunner(self)

        from api.task_store import TaskStore
        self.task_store = TaskStore("data/jobs.db")

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
        self.verifier_coordinator = VerifierCoordinator(_agents["verifier"], model_router)
        self.criterion_score_store = CriterionScoreStore()

        self._model_switch_notices: list[str] = []
        self.model_router.register_switch_callback(self._on_model_switch)

        self.subagent_manager = SubagentManager(
            agents=_agents,
            context_builder=self.context_builder,
            codebase_memory=self.codebase_memory,
            session_memory=self.session_memory,
            EventEmittingExecutor=EventEmittingExecutor,
            tool_executor=self.tool_executor,
        )

        self.task_loop = TaskLoop(TaskLoopDeps(
            tool_executor=self.tool_executor,
            context_builder=self.context_builder,
            planner_agent=_agents["planner"],
            plan_reviewer_agent=_agents["plan_reviewer"],
            task_store=self.task_store,
            verifier_coordinator=self.verifier_coordinator,
            criterion_score_store=self.criterion_score_store,
            session_memory=self.session_memory,
            skill_executor=self.skill_executor,
            memory_wiki=self.memory_wiki,
            acceptance_tester_agent=_agents["acceptance_tester"],
            run_agent_fn=self._run_specialized_agent,
            drain_switch_fn=self._drain_switch_notices,
            objective_resolver=self.objective_resolver,
        ))

    async def spawn_subagent(
        self,
        task: str,
        role: str = "developer",
        parent_session_id: str = None,
        context_limits: dict = None,
    ) -> dict:
        return await self.subagent_manager.spawn(task, role, parent_session_id, context_limits)

    async def spawn_multiple_subagents(
        self,
        tasks: list[str],
        roles: list[str] = None,
        parent_session_id: str = None,
    ) -> list[dict]:
        return await self.subagent_manager.spawn_multiple(tasks, roles, parent_session_id)

    def get_subagent_result(self, subagent_id: str) -> dict:
        return self.subagent_manager.get_result(subagent_id)

    def list_subagents(self) -> list[dict]:
        return self.subagent_manager.list_all()

    def _on_model_switch(self, event) -> None:
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
        if not self._model_switch_notices:
            return []
        notices = list(self._model_switch_notices)
        self._model_switch_notices.clear()
        if on_phase and notices:
            first = notices[0]
            compact = first.replace("⚠️ ", "")
            try:
                on_phase(f"model_switch:{compact}")
            except Exception:
                pass
        return notices

    def _create_session_executor(self, session_id: str) -> "EventEmittingExecutor":
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

        if task_type == "sdlc":
            from agent.sdlc_workflow import SDLCWorkflow
            workflow = SDLCWorkflow(self)
            return await workflow.run(task, session_id, on_phase=on_phase, job_id=job_id)

        if not _direct and task_type in ("develop", "research"):
            return await self._run_task_loop(
                task, task_type, session_id, on_phase=on_phase, job_id=job_id
            )

        session_executor = self._create_session_executor(session_id)
        enriched_context = await self.context_builder.build(task, agent_type=task_type)
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

        agent_key = _TASK_TYPE_TO_AGENT_KEY.get(task_type, task_type)
        agent = self.agents.get(agent_key, self.agents["developer"])
        return await agent.run(task, context)

    async def _run_task_loop(
        self,
        objective: str,
        task_type: str,
        session_id: str,
        on_phase: Optional[Callable[[str], None]] = None,
        job_id: Optional[str] = None,
    ) -> dict:
        return await self.task_loop.run(objective, task_type, session_id, on_phase=on_phase, job_id=job_id)

    def _detect_task_type_keyword(self, task: str) -> str:
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
        def _emit_phase(label: str) -> None:
            if on_phase:
                try:
                    on_phase(label)
                except Exception:
                    pass

        if not session_id:
            session_id = new_session_id()

        try:
            task = guard_task(task)
        except ValueError as e:
            return {"success": False, "session_id": session_id, "error": str(e)}

        self.session_memory.get_or_create_session(session_id, self.workspace_path)
        self.session_memory.save_message(session_id, "user", task)
        config = self.model_router.get_model("coding")
        if not config:
            return {"success": False, "session_id": session_id, "error": "No coding model configured"}
        self.agent_logger.log_task_start("agent", {"task": task})
        handover_triggered = False
        handover_bridge: Optional[str] = None
        original_session_id: Optional[str] = None
        budget = self.context_builder.check_budget(session_id, task)
        if budget == "bridge":
            _emit_phase("handover")
            self.logger.info("context_bridge_triggered", session_id=session_id)
            try:
                bridge_text, bridge_session_id = await self.context_builder.build_handover(
                    session_id, task, self.workspace_path
                )
                original_session_id = session_id
                session_id = bridge_session_id
                handover_triggered = True
                handover_bridge = bridge_text
                self.logger.info("session_swapped", new_session_id=session_id)
            except Exception as _he:
                self.logger.error("handover_failed", error=str(_he))

        import asyncio as _asyncio
        _emit_phase("preparing")
        if force_task_type:
            task_type = force_task_type
            await self.context_builder.build(task)
        else:
            task_type, _ = await _asyncio.gather(
                self.task_router.detect(task),
                self.context_builder.build(task),
            )

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
        summary = self.session_memory.get_session_summary(session_id)
        if not summary or summary.get("message_count", 0) == 0:
            return {"success": False, "error": f"Session '{session_id}' not found or empty"}

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
        if project_id is None:
            project_id = Path(self.workspace_path).name

        rag_result = self.codebase_memory.index_workspace(self.workspace_path, project_id)
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
                        file_path=rel_path, function_name=fn["name"], signature=fn["name"],
                        line_start=fn["line_start"], line_end=fn["line_end"],
                    )
                for cls in analysis.get("classes", []):
                    self.memory_wiki.add_class(
                        file_path=rel_path, class_name=cls["name"],
                        line_start=cls["line_start"], line_end=cls["line_end"],
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
            session_id = new_session_id()

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
        """Remove all agent-managed data for a project from every storage layer."""
        return project_lifecycle.delete_project(
            project_name,
            self.workspace_path,
            self.session_memory,
            self.task_store,
            self.codebase_memory,
            dry_run=dry_run,
        )
