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
from dataclasses import dataclass, field
from pathlib import Path
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

# Agent types safe for research tasks. architect is excluded because it writes
# ADR files to disk, which is wrong for a read-only research workflow.
_RESEARCH_SAFE_TYPES = frozenset({"research", "documenter", "chat"})

# Matches a JSON array in the LLM response even if wrapped in prose/markdown
_JSON_ARRAY_RE = re.compile(r'\[[\s\S]*?\]', re.DOTALL)

# Matches the first JSON object in LLM output (for criteria extraction)
_JSON_OBJ_RE = re.compile(r'\{[\s\S]*\}', re.DOTALL)


@dataclass
class PlanResult:
    """Output of plan_with_criteria(): tasks + testable completion criteria."""

    tasks: List[Dict[str, str]]
    completion_criteria: List[str] = field(default_factory=list)
    acceptance_criteria: List[str] = field(default_factory=list)

    # Backward compat: allow iteration/indexing so callers that treat the
    # result as a plain list continue to work without modification.
    def __iter__(self):
        return iter(self.tasks)

    def __len__(self):
        return len(self.tasks)

    def __getitem__(self, idx):
        return self.tasks[idx]


class PlannerAgent:
    """Decomposes an objective into a typed task list via a single LLM call."""

    def __init__(self, model_router, requirements_extractor=None):
        self.model_router = model_router
        self.requirements_extractor = requirements_extractor
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
            "Each task description must be ≤ 60 words and describe exactly ONE concrete action.\n"
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
            if task_type == "research":
                tasks = self._enforce_research_types(tasks)
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

    async def plan_with_criteria(
        self,
        objective: str,
        context: str = "",
        task_type: str = "develop",
        workspace: Optional[Path] = None,
    ) -> PlanResult:
        """Plan tasks AND generate testable completion criteria.

        Makes two LLM calls:
          1. Same as plan() — returns the task list.
          2. A focused follow-up that asks for 3-5 testable criteria based on
             the tasks and any tech-stack context already in `context`.

        If a requirements_extractor is available and a workspace path is provided,
        also generates behavioral acceptance criteria for the running app.

        Falls back gracefully — if any criteria call fails the tasks are still
        returned with whatever criteria were produced.
        """
        tasks = await self.plan(objective, context=context, task_type=task_type)

        # Skip criteria for non-develop workflows (research is verified differently)
        if task_type not in ("develop", "sdlc"):
            return PlanResult(tasks=tasks, completion_criteria=[], acceptance_criteria=[])

        criteria = await self._generate_criteria(objective, tasks, context)

        acceptance_criteria: List[str] = []
        if self.requirements_extractor is not None and workspace is not None:
            try:
                acceptance_criteria = await self.requirements_extractor.extract(objective, workspace)
            except Exception as exc:
                self.logger.warning("acceptance_criteria_extraction_failed", error=str(exc))

        return PlanResult(tasks=tasks, completion_criteria=criteria, acceptance_criteria=acceptance_criteria)

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

    def _enforce_research_types(self, tasks: List[Dict[str, str]]) -> List[Dict[str, str]]:
        """Remap unsafe agent types for research workflows.

        architect writes ADR files to disk — never appropriate for read-only research.
        """
        return [
            {**t, "agent_type": "documenter" if t["agent_type"] not in _RESEARCH_SAFE_TYPES else t["agent_type"]}
            for t in tasks
        ]

    def _strategy_hint(self, task_type: str) -> str:
        if task_type == "research":
            return (
                "Strategy for research objectives:\n"
                "1. One or more 'research' tasks to search/gather information from the web or files\n"
                "2. A 'documenter' task to synthesize findings into a structured markdown report\n"
                "IMPORTANT: NEVER use 'architect' or 'develop' agent_type for research tasks.\n"
                "NEVER create scaffold, directory structure, or project setup tasks.\n"
                "Research tasks produce REPORTS, not code or folders.\n"
            )
        if task_type in ("sdlc", "develop"):
            return (
                "Choose the strategy that matches the objective — do NOT default to the longest one:\n\n"
                "CREATION — objective uses words like create/write/build/generate/make and describes "
                "producing new file(s) with no mention of existing errors or an existing project:\n"
                "  Simple output (plain HTML page, short script, config file, < ~150 lines):\n"
                "    → 1 task: [develop] Write the complete FILE: block directly.\n"
                "  Complex output (canvas animation, game, simulation, multi-section document, > ~150 lines):\n"
                "    → 2–3 tasks: split into logical chunks, each under 150 lines:\n"
                "      1. [develop] Write FILE: <filename> — HTML skeleton, CSS, opening <script>, constants, config. End at the opening brace of the first function.\n"
                "      2. [develop] ⚠️ APPEND ONLY — do NOT use FILE:. Use APPEND: <filename> to add the core logic functions (drawing helpers, update functions, scenery generators).\n"
                "      3. [develop] ⚠️ APPEND ONLY — do NOT use FILE:. Use APPEND: <filename> to add the animation loop, initialization call, and closing </script></body></html> tags.\n"
                "    CRITICAL: Tasks 2 and 3 MUST start with '⚠️ APPEND ONLY — do NOT use FILE:' "
                "in their description. Using FILE: on tasks 2+ destroys all earlier content.\n\n"
                "DEBUGGING — objective uses words like fix/debug/error/crash/broken/failing/not working:\n"
                "  1. [mapper]  Map the project structure (skip if ARCHITECTURE.md already exists).\n"
                "  2. [research] Read the source files identified by the mapper.\n"
                "  3. [develop] Run the entry command. Capture full error output. Do NOT fix yet.\n"
                "  4. [develop] Apply all fixes.\n"
                "  5. [develop] Run again to confirm the fix.\n\n"
                "MODIFICATION — objective uses words like add/extend/update/refactor/improve and "
                "references existing code:\n"
                "  1. [research] Read the relevant source files.\n"
                "  2. [develop] Implement the change.\n"
                "  3. [develop] Run or test to verify.\n"
            )
        return ""

    def _fallback_plan(
        self, objective: str, task_type: str
    ) -> List[Dict[str, str]]:
        """Minimal fallback when LLM planning fails."""
        agent = "research" if task_type == "research" else "develop"
        return [{"description": objective, "agent_type": agent}]

    async def _generate_criteria(
        self,
        objective: str,
        tasks: List[Dict[str, str]],
        context: str,
    ) -> List[str]:
        """Return 3-5 testable completion criteria for the objective.

        Each criterion is a short, concrete, checkable statement.  Three
        auto-checkable patterns are preferred when applicable:
          - "command exits 0: <shell command>"
          - "file exists: <relative path>"
          - "file contains: <path>:<substring>"
        Anything not auto-checkable uses plain English (LLM-evaluated).
        """
        model = self.model_router.get_model("coding")
        if not model:
            return []

        task_summary = "\n".join(
            f"  {i + 1}. [{t.get('agent_type')}] {t['description'][:80]}"
            for i, t in enumerate(tasks[:8])
        )
        system = (
            "You are a strict QA engineer. Generate exactly 3-5 TESTABLE completion "
            "criteria for the objective. Be concrete and verifiable — a CI system "
            "should be able to check each one automatically or a reviewer should be "
            "able to tick it off in under 10 seconds.\n\n"
            "Prefer these auto-checkable formats in priority order:\n"
            '  "file exists: <path>"        — checks path is present. '
            "Use a glob pattern (e.g. *.html, src/*.js) when the task does NOT specify an exact filename. "
            "Only use a literal filename (e.g. index.html) when the objective explicitly names that file.\n"
            '  "file contains: <path>:<substring>" — checks file includes exact text. '
            "Path may be a glob (e.g. *.html:<canvas) when no exact filename is given.\n"
            '  "command exits 0: <shell command>"  — runs a BUILD or TEST command (e.g. npm run build, cargo check, python -m pytest). '
            "Use ONLY for build/test verification, NEVER for checking file contents. "
            "NEVER use grep, sed, awk, find, cat, ls, or any Unix-only utility in this field — "
            "they do not exist on Windows and will always fail.\n"
            "For behavioral/visual criteria use plain English.\n\n"
            'Return ONLY valid JSON: {"criteria": ["<criterion 1>", ...]}'
        )
        prompt = (
            f"Objective: {objective}\n\n"
            f"Planned tasks:\n{task_summary}\n\n"
            f"{f'Tech context: {context[:300]}' if context else ''}\n\n"
            "Generate 3-5 testable completion criteria."
        )
        try:
            raw = await self.model_router.generate(prompt, model, system_prompt=system, enable_thinking=False)
            m = _JSON_OBJ_RE.search(raw or "")
            if not m:
                return []
            obj = json.loads(m.group())
            criteria = [str(c).strip() for c in obj.get("criteria", []) if c]
            self.logger.info("criteria_generated", count=len(criteria), objective=objective[:60])
            return criteria[:5]
        except Exception as exc:
            self.logger.warning("criteria_generation_failed", error=str(exc))
            return []
