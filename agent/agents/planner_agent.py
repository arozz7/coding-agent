"""Planner agent: decomposes a high-level objective into an ordered task list.

Each task carries:
  - description: a concrete instruction the executing agent can act on
  - agent_type:  which agent handles it (develop / research / test / review /
                 architect / chat)

Two built-in strategies are selected by task_type:
  - "develop"  → understand → change code → run → fix → verify
  - "research" → gather sources → read/fetch → synthesize → (optionally) develop

Falls back to a single-task plan if the LLM fails or returns invalid JSON.
"""

import json
import re
from typing import Dict, List, Optional

import structlog

logger = structlog.get_logger()

# Agent types the orchestrator can route to
VALID_AGENT_TYPES = frozenset({
    "develop", "research", "test", "review", "architect", "chat",
    "mapper",      # codebase mapper — produces ARCHITECTURE.md / STACK.md
    "security",    # red-team security audit
    "documenter",  # documentation writer
})

# Matches a JSON array in the LLM response even if wrapped in prose/markdown
_JSON_ARRAY_RE = re.compile(r'\[[\s\S]*?\]', re.DOTALL)


class PlannerAgent:
    """Decomposes an objective into a typed task list via a single LLM call."""

    def __init__(self, model_router):
        self.model_router = model_router
        self.logger = logger.bind(component="planner_agent")

    async def plan(
        self,
        objective: str,
        context: str = "",
        task_type: str = "develop",
    ) -> List[Dict[str, str]]:
        """Return [{description, agent_type}, ...] for the given objective.

        Falls back to a minimal single-task list on any failure.
        """
        model = self.model_router.get_model("coding")
        if not model:
            self.logger.warning("planner_no_model")
            return self._fallback_plan(objective, task_type)

        strategy_hint = self._strategy_hint(task_type)
        system_prompt = (
            "You are an expert task planning assistant for an autonomous coding agent.\n\n"
            "Break the following objective into 5–8 concrete, ordered tasks.\n"
            "Each task must be small enough that a single agent call can complete it.\n"
            "Assign the correct agent_type to each task.\n\n"
            "Valid agent_type values:\n"
            "- mapper:     map project structure → ARCHITECTURE.md + STACK.md (use as first step for unfamiliar projects)\n"
            "- research:   search the web, read files, investigate codebase (read-only)\n"
            "- develop:    write, run, fix, or debug code\n"
            "- test:       write or run tests\n"
            "- review:     code review or quality check\n"
            "- security:   adversarial security audit (OWASP, secrets, injection risks)\n"
            "- documenter: write READMEs, changelogs, or inline documentation\n"
            "- architect:  system design or ADR\n"
            "- chat:       explain or answer questions"
        )

        prompt = (
            f"{strategy_hint}\n"
            f"Objective: {objective}\n\n"
            f"{f'Context: {context}' if context else ''}\n\n"
            "Return ONLY a JSON array — no prose, no markdown fences. Example:\n"
            '[\n'
            '  {"description": "Read package.json, tsconfig.json, and src/ layout to understand the project. No code changes.", "agent_type": "research"},\n'
            '  {"description": "Run `npm start` and capture the full error output. Do NOT fix anything — only run and report.", "agent_type": "develop"},\n'
            '  {"description": "Read the source files referenced in the error output to understand what needs changing.", "agent_type": "develop"},\n'
            '  {"description": "Apply all code fixes using EDIT: blocks to resolve the errors found in the previous task.", "agent_type": "develop"},\n'
            '  {"description": "Run `npm run build` to compile and check for type errors. Fix any compile errors found.", "agent_type": "develop"},\n'
            '  {"description": "Run `npm start` again to confirm the application launches cleanly without errors.", "agent_type": "develop"}\n'
            ']'
        )

        try:
            raw = await self.model_router.generate(prompt, model, system_prompt=system_prompt)
            tasks = self._parse_task_list(raw)
            if tasks:
                self.logger.info(
                    "plan_created",
                    objective=objective[:80],
                    task_count=len(tasks),
                    task_type=task_type,
                )
                return tasks
            self.logger.warning("planner_empty_result", raw=raw[:200])
        except Exception as e:
            self.logger.warning("planner_llm_failed", error=str(e))

        return self._fallback_plan(objective, task_type)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_task_list(self, raw: str) -> List[Dict[str, str]]:
        """Extract and validate a JSON task array from LLM output."""
        # Try the largest JSON array found (handles partial markdown fences)
        matches = _JSON_ARRAY_RE.findall(raw)
        if not matches:
            return []

        # Take the longest match — it's most likely the full task list
        candidate = max(matches, key=len)
        try:
            items = json.loads(candidate)
        except json.JSONDecodeError:
            return []

        if not isinstance(items, list):
            return []

        validated: List[Dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            desc = str(item.get("description", "")).strip()
            agent = str(item.get("agent_type", "develop")).strip().lower()
            if not desc:
                continue
            if agent not in VALID_AGENT_TYPES:
                agent = "develop"
            validated.append({"description": desc, "agent_type": agent})

        return validated

    def _strategy_hint(self, task_type: str) -> str:
        if task_type == "research":
            return (
                "Strategy for research objectives:\n"
                "1. One or more 'research' tasks to search/gather information\n"
                "2. A 'research' task to synthesize findings into a report\n"
                "3. Optionally a 'develop' task if code output is needed\n"
            )
        if task_type in ("sdlc", "develop"):
            return (
                "Strategy for development/debugging objectives:\n"
                "1. A 'mapper' task to map the project structure (produces ARCHITECTURE.md + STACK.md). Skip if ARCHITECTURE.md already exists.\n"
                "2. A 'research' task to read key source files identified by the mapper. No code changes.\n"
                "3. A 'develop' task to run the project entry command (npm start / python app.py / cargo run). Capture the full error output. Do NOT fix anything — only run and report.\n"
                "4. A 'develop' task to apply all code fixes using REPLACE: blocks (preferred) or EDIT: blocks. Fix every error identified in step 3.\n"
                "5. A 'develop' task to run the build command (npm run build / tsc / cargo build) to compile and verify there are no remaining type or compile errors. Fix any found.\n"
                "6. A 'develop' task to run the project entry command again to confirm it starts cleanly. Report success or list any remaining errors.\n"
            )
        return ""

    def _fallback_plan(
        self, objective: str, task_type: str
    ) -> List[Dict[str, str]]:
        """Minimal fallback when LLM planning fails."""
        agent = "research" if task_type == "research" else "develop"
        return [{"description": objective, "agent_type": agent}]
