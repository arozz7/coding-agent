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
VALID_AGENT_TYPES = frozenset({"develop", "research", "test", "review", "architect", "chat"})

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
        prompt = (
            "You are a task planning assistant for an autonomous coding agent.\n\n"
            "Break the following objective into 3–7 concrete, ordered tasks.\n"
            "Each task must be small enough that a single agent call can complete it.\n"
            "Assign the correct agent_type to each task.\n\n"
            "Valid agent_type values:\n"
            "- develop:   write, run, fix, or debug code\n"
            "- research:  search the web, read files, investigate codebase\n"
            "- test:      write or run tests\n"
            "- review:    code review or security audit\n"
            "- architect: system design or ADR\n"
            "- chat:      explain or answer questions\n\n"
            f"{strategy_hint}\n"
            f"Objective: {objective}\n\n"
            f"{f'Context: {context}' if context else ''}\n\n"
            "Return ONLY a JSON array — no prose, no markdown fences. Example:\n"
            '[\n'
            '  {"description": "Read package.json and note the start script", "agent_type": "develop"},\n'
            '  {"description": "Run npm start to capture the current error", "agent_type": "develop"},\n'
            '  {"description": "Fix the identified error in src/index.js", "agent_type": "develop"},\n'
            '  {"description": "Run npm start again to verify the fix", "agent_type": "develop"}\n'
            ']'
        )

        try:
            raw = await self.model_router.generate(prompt, model)
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
                "1. A 'develop' task to understand the project (read files, check structure)\n"
                "2. A 'develop' task to make the required code changes\n"
                "3. A 'develop' task to run the project and capture output/errors\n"
                "4. A 'develop' task to fix any errors found (omit if step 3 passes)\n"
                "5. A 'develop' task to verify the final state\n"
            )
        return ""

    def _fallback_plan(
        self, objective: str, task_type: str
    ) -> List[Dict[str, str]]:
        """Minimal fallback when LLM planning fails."""
        agent = "research" if task_type == "research" else "develop"
        return [{"description": objective, "agent_type": agent}]
