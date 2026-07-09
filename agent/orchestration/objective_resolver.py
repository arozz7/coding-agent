"""ObjectiveResolver — grounds vague "do the next thing" objectives in real work.

Small local models plan poorly against objectives like "move on to the next
tasks in NEXT_STEPS.md" because nothing in the planning context tells them
what that next task actually is — build_planning_context() only sends a
tech-stack fingerprint, not file contents. That forces every downstream task
description to stay generic ("Implement the first task listed in
NEXT_STEPS.md..."), which produces generic/stub output.

ObjectiveResolver runs once, before planning: if the objective references (or
vaguely implies) a task-tracking file that exists in the workspace, it reads
that file and asks the LLM to rewrite the objective as one concrete,
file-naming deliverable. The plan and completion criteria are then generated
against that concrete objective instead of the vague original.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

if TYPE_CHECKING:
    from llm import ModelRouter

logger = structlog.get_logger()

# Checked in priority order; the first one that exists in the workspace wins.
_CANDIDATE_TASK_FILES = ["NEXT_STEPS.md", "TODO.md", "TASKS.md", "ROADMAP.md", "PLAN.md"]

# Phrases that signal "figure out what to do next" rather than a concrete ask.
_VAGUE_RE = re.compile(
    r"next task|next step|\bcontinue\b|\bmove on\b|what.s next|keep going|pick up where",
    re.IGNORECASE,
)

_MAX_DOC_CHARS = 3000


@dataclass
class ResolvedObjective:
    """objective: the (possibly rewritten) concrete objective to plan against.
    context_excerpt: the task-file excerpt, for build_planning_context to include.
    """

    objective: str
    context_excerpt: str = ""


class ObjectiveResolver:
    """Rewrites vague continuation objectives into one concrete deliverable."""

    def __init__(self, model_router: "ModelRouter") -> None:
        self.model_router = model_router
        self.logger = logger.bind(component="objective_resolver")

    async def resolve(self, objective: str, workspace: Path) -> ResolvedObjective:
        """Return a ResolvedObjective. Falls back to the original objective
        untouched whenever no task file applies or resolution fails.
        """
        task_file = self._find_task_file(objective, workspace)
        if task_file is None:
            return ResolvedObjective(objective=objective, context_excerpt="")

        model = self.model_router.get_model("coding")
        if not model:
            return ResolvedObjective(objective=objective, context_excerpt="")

        try:
            content = task_file.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ResolvedObjective(objective=objective, context_excerpt="")

        excerpt = content[:_MAX_DOC_CHARS]
        system = (
            "You are a technical lead handing one concrete task to a developer. "
            "Given a task-tracking file and a vague request, pick exactly ONE "
            "pending item and rewrite it as a single concrete deliverable.\n\n"
            "RULES:\n"
            "  - Name the specific file(s) or directory the work targets.\n"
            "  - Describe ONE deliverable, not a whole phase or multiple items.\n"
            "  - Prefer the first not-yet-completed item in the file.\n"
            "  - Do NOT invent work that is not described in the file.\n"
            "  - Output ONLY the rewritten objective as plain text — no prose, "
            "no markdown, no explanation, no quotes."
        )
        prompt = (
            f"Task file ({task_file.name}):\n{excerpt}\n\n"
            f"Original request: {objective}\n\n"
            "Rewrite as one concrete, file-naming objective."
        )

        try:
            raw = await self.model_router.generate(prompt, model, system_prompt=system, enable_thinking=False)
        except Exception as exc:
            self.logger.warning("objective_resolution_failed", error=str(exc))
            return ResolvedObjective(objective=objective, context_excerpt="")

        resolved = (raw or "").strip()
        if not resolved:
            return ResolvedObjective(objective=objective, context_excerpt="")

        self.logger.info(
            "objective_resolved",
            task_file=task_file.name,
            original=objective[:60],
            resolved=resolved[:80],
        )
        return ResolvedObjective(
            objective=resolved,
            context_excerpt=f"### {task_file.name}\n{excerpt}",
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _find_task_file(self, objective: str, workspace: Path) -> "Path | None":
        """Return the highest-priority existing task file this objective implies.

        An objective implies a task file when it either names one of the
        candidate filenames directly, or uses vague continuation phrasing
        ("continue", "next steps", ...) with no concrete target of its own.
        """
        obj_lower = objective.lower()
        mentions_candidate = any(name.lower() in obj_lower for name in _CANDIDATE_TASK_FILES)
        vague = bool(_VAGUE_RE.search(obj_lower))
        if not mentions_candidate and not vague:
            return None

        for name in _CANDIDATE_TASK_FILES:
            path = workspace / name
            if path.exists():
                return path
        return None
