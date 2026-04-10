from typing import TypedDict, Annotated, List, Optional
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import structlog

from llm import ModelRouter
from agent.memory import SessionMemory, CodebaseMemory
from agent.tools import FileSystemTool, PytestTool, CodeAnalyzer
from agent.agents.developer_agent import DeveloperAgent
from agent.agents.tester_agent import TesterAgent
from agent.agents.reviewer_agent import ReviewerAgent
from agent.agents.architect_agent import ArchitectAgent
from agent.skills.skill_loader import SkillManager
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
        
        # Create isolated context for subagent
        isolated_context = {
            "session_id": subagent_id,
            "parent_session_id": parent_session_id,
            "workspace_path": self.workspace_path,
            "model_router": self.model_router,
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
            
            # Optionally aggregate result back to parent
            if parent_session_id:
                self.session_memory.save_message(
                    parent_session_id,
                    "subagent",
                    f"[{role}] {task[:50]}... -> {result.get('response', '')[:200]}",
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
    
    def _detect_task_type(self, task: str) -> str:
        task_lower = task.lower()
        
        if any(kw in task_lower for kw in ["review", "critique", "analyze code", "check for bugs", "security"]):
            return "review"
        if any(kw in task_lower for kw in ["test", "spec", "unit test", "pytest", "fixture"]):
            return "test"
        if any(kw in task_lower for kw in ["architect", "design", "architecture", "adr", "structure", "high-level"]):
            return "architect"
        if any(kw in task_lower for kw in ["write code", "implement", "create file", "make function", "build", "create", "generate code"]):
            return "develop"
        
        return "general"
    
    def _detect_skills(self, task: str, phase: str = "pre") -> List[str]:
        """Detect which skills should run based on task content."""
        triggered_skills = []
        
        # Pre-execution skill triggers
        pre_triggers = {
            "test": ["tdd-enforcer"],
            "security": ["security-auditor"],
            "audit": ["security-auditor"],
            "database": ["architect-adr"],
            "api": ["architect-adr"],
            "auth": ["architect-adr"],
            "architecture": ["architect-adr"],
            "adr": ["architect-adr"],
        }
        
        post_triggers = {
            "compile": ["wiki-compile"],
            "save": ["wiki-compile"],
            "remember": ["wiki-compile"],
            "handover": ["handover"],
            "context bridge": ["handover"],
        }
        
        task_lower = task.lower()
        triggers = pre_triggers if phase == "pre" else post_triggers
        
        for keyword, skill_names in triggers.items():
            if keyword in task_lower:
                for skill_name in skill_names:
                    if skill_name not in triggered_skills:
                        triggered_skills.append(skill_name)
        
        return triggered_skills
    
    def _get_skill_context(self, skill_names: List[str]) -> str:
        """Get skill content to add to context."""
        context_parts = []
        
        for skill_name in skill_names:
            skill = self.skill_manager.get_skill(skill_name)
            if skill:
                context_parts.append(f"\n\n## Skill: {skill.name}\n{skill.content}")
        
        return "\n".join(context_parts)
    
    def _create_session_executor(self, session_id: str) -> "EventEmittingExecutor":
        """Create an EventEmittingExecutor bound to this session."""
        return self._EventEmittingExecutor(
            self.tool_executor,
            self.session_memory,
            session_id,
        )

    async def _run_specialized_agent(self, task: str, task_type: str, session_id: str) -> dict:
        session_executor = self._create_session_executor(session_id)
        context = {
            "session_id": session_id,
            "workspace_path": self.workspace_path,
            "model_router": self.model_router,
            "tool_executor": session_executor,
        }

        if task_type == "review":
            return await self.reviewer_agent.run(task, context)
        elif task_type == "test":
            return await self.tester_agent.run(task, context)
        elif task_type == "architect":
            return await self.architect_agent.run(task, context)
        else:
            # "develop" and "general" both go to developer agent
            return await self.developer_agent.run(task, context)

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
        self, task: str, session_id: Optional[str] = None, include_history: bool = True
    ) -> dict:
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
        task_type = self._detect_task_type(task)
        self.logger.info("task_type_detected", task_type=task_type)
        self.session_memory.emit_event(session_id, "status", {"phase": "start", "task_type": task_type})

        try:
            result = await self._run_specialized_agent(task, task_type, session_id)

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

                return {
                    "success": True,
                    "session_id": session_id,
                    "result": {
                        "response": response,
                        "task": task,
                        "task_type": task_type,
                        "files_created": result.get("files_created", []),
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

    def _load_wiki_context(self, task: str) -> str:
        """Load relevant wiki entries for the current task."""
        wiki_path = Path(self.workspace_path) / ".agent-wiki" / "index.md"
        if not wiki_path.exists():
            return ""
        
        try:
            with open(wiki_path, "r", encoding="utf-8") as f:
                index_content = f.read()
            
            task_lower = task.lower()
            relevant_entries = []
            
            for line in index_content.split("\n"):
                if any(keyword in line.lower() for keyword in task_lower.split()[:3]):
                    relevant_entries.append(line)
            
            if relevant_entries:
                context = "\n**From Agent Wiki:**\n"
                for entry in relevant_entries[:5]:
                    context += f"- {entry}\n"
                return context
        except Exception as e:
            self.logger.warn("wiki_load_failed", error=str(e))
        
        return ""
    
    def _load_rag_context(self, task: str, max_chunks: int = 3) -> str:
        """Load relevant code context from vector store using RAG."""
        try:
            # Get project ID from workspace name
            project_id = Path(self.workspace_path).name
            
            # Use CodebaseMemory to get relevant context
            context = self.codebase_memory.get_relevant_context(
                task=task,
                project_id=project_id,
                max_chunks=max_chunks,
            )
            
            if context:
                self.logger.info("rag_context_loaded", task=task[:50], chunks=max_chunks)
            
            return context
        except Exception as e:
            self.logger.warn("rag_context_failed", error=str(e))
            return ""
    
    def index_workspace(self, project_id: str = None) -> dict:
        """Index all files in the workspace for RAG."""
        if project_id is None:
            project_id = Path(self.workspace_path).name
        
        return self.codebase_memory.index_workspace(self.workspace_path, project_id)

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
