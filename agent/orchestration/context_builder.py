"""Context builder — assembles enriched prompt context for every task.

Owns:
  - char_budget()          free function: model-context-aware char limit
  - ContextBuilder class:
      build()              full enriched context string (wiki + RAG + env + skills)
      build_environment()  static: OS/shell block
      estimate_tokens()    rough token estimate for context budget
      check_budget()       'ok' | 'warn' | 'bridge'
      build_handover()     generate Context Bridge and swap session
      build_events_context() session history string
      build_context()      thin wrapper used by run_stream
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Optional

import structlog

from agent.session_id import new_session_id as _make_session_id

# Strips raw FILE:/APPEND: blocks and fenced code from episodic summaries so
# they don't contaminate the model context with code from unrelated past tasks.
_EPISODIC_CODE_RE = re.compile(
    r'(?:FILE:|APPEND:)\s+\S[^\n]*[\s\S]*?(?=\n(?:FILE:|APPEND:|##|\Z)|\Z)'
    r'|```[\s\S]*?```',
    re.DOTALL,
)


def _clean_episodic_summary(text: str, max_len: int = 300) -> str:
    """Remove code blocks and file blocks from an episodic result summary."""
    cleaned = _EPISODIC_CODE_RE.sub("", text)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned[:max_len]

from agent.workspace_context import get_workspace

if TYPE_CHECKING:
    from agent.memory import SessionMemory
    from agent.memory.codebase_memory import CodebaseMemory
    from agent.memory.memory_wiki import MemoryWiki
    from agent.skills.skill_executor import SkillExecutor
    from agent.skills.skill_loader import SkillManager
    from llm import ModelRouter

logger = structlog.get_logger()


def char_budget(model_router: "ModelRouter", fraction: float, cap: int) -> int:
    """Return a char limit scaled to the active model's context window.

    For small-context models the fraction clips the budget naturally.
    For large-context models the explicit cap provides a practical ceiling.
    Assumes ~4 chars per token (English prose average).
    """
    config = model_router.get_model("coding")
    cw = config.context_window if config else 32_000
    return min(cap, int(cw * 4 * fraction))


class ContextBuilder:
    """Assembles enriched prompt context for every agent task."""

    # Fallback used when the handover skill file cannot be loaded.
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

    def __init__(
        self,
        model_router: "ModelRouter",
        skill_executor: "SkillExecutor",
        codebase_memory: "CodebaseMemory",
        session_memory: "SessionMemory",
        skill_manager: "SkillManager",
        memory_wiki: Optional["MemoryWiki"] = None,
        skill_router: Optional["TaskRouter"] = None,  # type: ignore[name-defined]
    ):
        self.model_router = model_router
        self.skill_executor = skill_executor
        self.codebase_memory = codebase_memory
        self.session_memory = session_memory
        self.skill_manager = skill_manager
        self.memory_wiki = memory_wiki
        self.skill_router = skill_router
        self.logger = logger.bind(component="context_builder")

    # ------------------------------------------------------------------
    # Budget helpers
    # ------------------------------------------------------------------

    def char_budget(self, fraction: float, cap: int) -> int:
        """Instance-level convenience wrapper around the free function."""
        return char_budget(self.model_router, fraction, cap)

    def estimate_tokens(self, session_id: str, task: str) -> int:
        """Rough token estimate for the next LLM call."""
        history = self.build_events_context(session_id)
        char_count = len(history) + len(task)
        return char_count // 4 + 4_500  # overhead: system prompt + enriched context

    def check_budget(self, session_id: str, task: str) -> str:
        """Return 'ok', 'warn' (≥75 %), or 'bridge' (≥82 %) based on token usage."""
        config = self.model_router.get_model("coding")
        if not config or not config.context_window:
            return "ok"
        estimated = self.estimate_tokens(session_id, task)
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

    # ------------------------------------------------------------------
    # Session history
    # ------------------------------------------------------------------

    def build_events_context(self, session_id: str) -> str:
        """Build conversation context from paginated events."""
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
                    content = content[:500] + ("…" if len(content) > 500 else "")
                context_lines.append(f"[{event_type}] {content}")
            elif role in ("user", "assistant"):
                context_lines.append(f"{role.capitalize()}: {content[:500]}")

        return "\n".join(context_lines)

    def build_context(self, session_id: str, include_history: bool = True) -> str:
        """Build context string for the streaming endpoint."""
        if not include_history:
            return ""
        return self.build_events_context(session_id)

    # ------------------------------------------------------------------
    # Handover
    # ------------------------------------------------------------------

    async def build_handover(
        self, session_id: str, task: str, workspace_path: str
    ) -> tuple[str, str]:
        """Generate a Context Bridge, create a new session pre-seeded with it.

        Returns (bridge_text: str, new_session_id: str).
        """
        skill = self.skill_manager.get_skill("handover")
        instructions = (skill.content if skill else self._HANDOVER_FALLBACK).strip()

        git_summary = ""
        try:
            r = subprocess.run(
                ["git", "log", "--oneline", "-10"],
                cwd=str(workspace_path),
                capture_output=True,
                text=True,
                timeout=5,
            )
            if r.returncode == 0:
                git_summary = r.stdout.strip()
        except Exception:
            pass

        history = self.build_events_context(session_id)
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

        new_session_id = _make_session_id("session") + "_bridge"
        self.session_memory.get_or_create_session(new_session_id, workspace_path)
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

    # ------------------------------------------------------------------
    # Environment context
    # ------------------------------------------------------------------

    @staticmethod
    def build_environment() -> str:
        """Return a compact block describing the runtime environment."""
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

    # ------------------------------------------------------------------
    # Main enriched context builder
    # ------------------------------------------------------------------

    async def build_planning_context(self, objective: str) -> str:
        """Compact strategic context for the planner (~2000 chars max).

        Includes tech-stack fingerprint, workspace snapshot, top-2 episodic
        memories, and known pitfalls from the agent wiki.  Does NOT call
        build() internally — kept intentionally lightweight.
        """
        parts: list[str] = []

        # 1. Tech-stack fingerprint + workspace snapshot
        try:
            _ws_now = get_workspace()
            ws_root = Path(_ws_now)
            _STACK_FILES = {
                "package.json":    "Node.js/JavaScript",
                "Cargo.toml":      "Rust",
                "requirements.txt": "Python",
                "pyproject.toml":  "Python",
                "go.mod":          "Go",
                "pom.xml":         "Java/Maven",
                "build.gradle":    "Java/Gradle",
                "composer.json":   "PHP",
                "Gemfile":         "Ruby",
            }
            detected = [label for fname, label in _STACK_FILES.items() if (ws_root / fname).exists()]
            _IGNORE_TOP = {"node_modules", "__pycache__", "logs", ".git", ".agent-wiki"}
            top_items = [
                item.name for item in sorted(ws_root.iterdir())
                if not item.name.startswith(".") and item.name not in _IGNORE_TOP
            ][:12]
            stack_line = f"Tech stack: {', '.join(detected) or 'unknown'}"
            ws_line = f"Workspace: {', '.join(top_items) or '(empty)'}"
            parts.append(f"## Project Context\n{stack_line}\n{ws_line}")
        except Exception:
            pass

        # 2. Episodic memory — top 2 similar past tasks (score >= 6)
        try:
            past = self.session_memory.get_similar_tasks(objective, limit=2, min_score=6)
            if past:
                lines = ["## Similar past work"]
                for p in past:
                    lines.append(
                        f"- [{p.get('task_type') or 'task'}, {p['score']}/10] "
                        f"{p['task_text'][:60]}: {p['result_summary'][:120]}"
                    )
                parts.append("\n".join(lines))
        except Exception:
            pass

        # 3. Known pitfalls from agent wiki (top 3 skill matches)
        try:
            from agent.skills.wiki_manager import WikiManager
            _ws_path = get_workspace()
            if _ws_path:
                _skill_wiki = WikiManager(_ws_path, project_name=Path(_ws_path).name)
                _stop = {"the", "a", "an", "and", "or", "in", "on", "to", "for", "is"}
                _terms = [
                    w for w in objective.lower().split() if w not in _stop and len(w) > 3
                ][:6]
                pitfalls = _skill_wiki.query_skills(_terms, agent_type="develop")
                if pitfalls:
                    parts.append(f"## Known pitfalls\n{pitfalls[:600]}")
        except Exception:
            pass

        return "\n\n".join(parts)[:2000]

    async def build(self, task: str, agent_type: str = "") -> str:
        """Build prompt enrichment: wiki-query + RAG + environment + skill instructions.

        agent_type specializes memory injection:
          developer/reviewer/tester  → code-graph context from MemoryWiki
          any type with prior work   → episodic memory from SessionMemory
        """
        parts: list[str] = []

        # 0a. Runtime environment
        parts.append(self.build_environment())

        # 0b. AGENTS.md global coding agent instructions
        agents_md = Path("AGENTS.md")
        if agents_md.exists():
            try:
                agents_content = agents_md.read_text(encoding="utf-8")
                parts.append(
                    f"\n\n## Global Agent Instructions (AGENTS.md)\n{agents_content[:600]}"
                )
            except Exception:
                pass

        # 0c. Workspace file listing — shallow snapshot
        try:
            _ws_now = get_workspace()
            ws_root = Path(_ws_now)
            if ws_root.exists():
                ws_lines: list[str] = []
                _IGNORE = {".git", "node_modules", "__pycache__", ".agent-wiki", "logs", "-p"}
                for item in sorted(ws_root.rglob("*")):
                    if any(part in _IGNORE for part in item.parts):
                        continue
                    rel = item.relative_to(ws_root)
                    prefix = "📁 " if item.is_dir() else "📄 "
                    ws_lines.append(f"  {prefix}{rel}")
                    if len(ws_lines) >= 25:
                        ws_lines.append("  … (truncated)")
                        break
                if ws_lines:
                    parts.append(
                        f"\n\n## Workspace Files ({_ws_now})\n" + "\n".join(ws_lines)
                    )
        except Exception as _ws_err:
            self.logger.warning("workspace_listing_failed", error=str(_ws_err))

        # 1. Wiki query
        wiki_ctx = await self.skill_executor.execute_pre("wiki-query", task)
        if wiki_ctx:
            parts.append(wiki_ctx[:500])

        # 2. RAG — semantic code search
        try:
            project_id = Path(get_workspace()).name
            rag_ctx = self.codebase_memory.get_relevant_context(task, project_id, max_chunks=3)
            if rag_ctx:
                parts.append(rag_ctx[:500])
        except Exception as e:
            self.logger.warning("rag_context_failed", error=str(e))

        # 3. Pre-execution skill instructions
        if self.skill_router:
            skill_names = self.skill_router.detect_skills(task, "pre")
        else:
            skill_names = []
        for skill_name in skill_names:
            skill_ctx = await self.skill_executor.execute_pre(skill_name, task)
            if skill_ctx:
                parts.append(skill_ctx)

        # 4. Code-graph context — developer/reviewer/tester only
        if agent_type in ("developer", "reviewer", "tester") and self.memory_wiki:
            _STOP = {"the", "a", "an", "and", "or", "in", "on", "to", "for"}
            terms = [w for w in task.lower().split() if w not in _STOP and len(w) > 3][:6]
            graph_ctx = self.memory_wiki.query(terms)
            if graph_ctx:
                _graph_cap = self.char_budget(fraction=0.05, cap=20_000)
                parts.append(graph_ctx[:_graph_cap])

        # 5. Episodic memory — high-quality past work similar to this task
        try:
            past = self.session_memory.get_similar_tasks(task, limit=3, min_score=7)
            if past:
                lines = ["## Episodic memory — similar past work (score ≥ 7)"]
                for p in past:
                    lines.append(
                        f"- [{p['task_type'] or 'task'}, score {p['score']}/10] "
                        f"**{p['task_text'][:80]}**\n  {_clean_episodic_summary(p['result_summary'])}"
                    )
                _ep_cap = self.char_budget(fraction=0.05, cap=15_000)
                parts.append("\n".join(lines)[:_ep_cap])
        except Exception as _ep_err:
            self.logger.debug("episodic_query_failed", error=str(_ep_err))

        # 6. Agent-wiki skills — load matching fix recipes scoped to this agent type
        try:
            from agent.skills.wiki_manager import WikiManager
            _ws_path = get_workspace()
            if _ws_path:
                _skill_wiki = WikiManager(_ws_path, project_name=Path(_ws_path).name)
                _stop = {"the", "a", "an", "and", "or", "in", "on", "to", "for", "is", "was"}
                _skill_terms = [
                    w for w in task.lower().split() if w not in _stop and len(w) > 3
                ][:8]
                skills_ctx = _skill_wiki.query_skills(_skill_terms, agent_type=agent_type)
                if skills_ctx:
                    _skill_cap = self.char_budget(fraction=0.04, cap=8_000)
                    parts.append(skills_ctx[:_skill_cap])
        except Exception as _sk_err:
            self.logger.debug("skill_context_failed", error=str(_sk_err))

        return "\n".join(parts)
