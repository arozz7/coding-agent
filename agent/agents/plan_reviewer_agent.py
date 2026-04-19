"""Plan reviewer agent: critiques and improves a proposed task list.

Implements the plan-review-plan loop from the pi-vs-claude-code pattern:
  planner → plan-reviewer (critique) → planner (revise)

The reviewer challenges assumptions, detects wrong agent types, flags missing
steps (e.g. no build verification step, no research step before editing), and
returns a revised task list. If the plan is already solid the original list is
returned unchanged.
"""

import json
import re
from typing import Dict, List

import structlog

from agent.agents.planner_agent import VALID_AGENT_TYPES, _JSON_ARRAY_RE

logger = structlog.get_logger()


class PlanReviewerAgent:
    """Validates and improves a task list produced by PlannerAgent."""

    def __init__(self, model_router):
        self.model_router = model_router
        self.logger = logger.bind(component="plan_reviewer_agent")

    async def review(
        self,
        tasks: List[Dict[str, str]],
        objective: str,
    ) -> List[Dict[str, str]]:
        """Critique *tasks* and return an improved (or identical) task list.

        Falls back to the original list on any LLM or parse failure.
        """
        model = self.model_router.get_model("coding")
        if not model:
            return tasks

        task_text = "\n".join(
            f"  {i + 1}. [{t.get('agent_type', 'develop')}] {t['description']}"
            for i, t in enumerate(tasks)
        )

        system_prompt = (
            "You are a plan reviewer agent. Critically evaluate implementation plans "
            "and return an improved version.\n\n"
            "Challenge assumptions, identify missing steps, correct wrong agent types, "
            "and ensure the plan is complete and correctly sequenced.\n\n"
            "Return ONLY a JSON array — no prose, no markdown fences. "
            "Improve or reorder as needed. Never return fewer tasks than the input "
            "unless tasks are genuinely redundant."
        )

        prompt = (
            f"Objective: {objective}\n\n"
            f"Proposed plan ({len(tasks)} tasks):\n{task_text}\n\n"
            f"Review checklist:\n"
            f"- Does it start with a research/scout step to understand the project before editing?\n"
            f"- Are agent_type values correct? Valid types: {sorted(VALID_AGENT_TYPES)}\n"
            f"  • mapper — generate ARCHITECTURE.md/STACK.md for an unfamiliar project\n"
            f"  • research — read-only investigation\n"
            f"  • develop — write/edit/run code\n"
            f"  • test — write or run tests\n"
            f"  • review — code review\n"
            f"  • security — security/adversarial audit\n"
            f"  • documenter — write docs/READMEs\n"
            f"  • architect — system design or ADR\n"
            f"- Is there a dedicated build/compile step before the run step?\n"
            f"- Are any steps too vague or too large for one agent call?\n"
            f"- Are there hidden ordering dependencies violated?\n\n"
            f"Return the corrected task list as a JSON array:\n"
            f'[{{"description": "...", "agent_type": "..."}}, ...]'
        )

        try:
            raw = await self.model_router.generate(
                prompt, model, system_prompt=system_prompt, enable_thinking=False
            )
            matches = _JSON_ARRAY_RE.findall(raw)
            if not matches:
                return tasks
            candidate = max(matches, key=len)
            items = json.loads(candidate)
            if not isinstance(items, list):
                return tasks

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

            if not validated:
                return tasks

            self.logger.info(
                "plan_reviewed",
                original_count=len(tasks),
                revised_count=len(validated),
                objective=objective[:80],
            )
            return validated

        except Exception as e:
            self.logger.warning("plan_review_failed", error=str(e))
            return tasks
