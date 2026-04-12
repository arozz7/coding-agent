"""
SkillExecutor - Pre/post execution dispatch for skills.

Skills have two execution phases:
  pre  - runs before the agent LLM call; returns a context string to inject
  post - runs after the task completes; takes action (write files, run scans)

Skill routing:
  wiki-query       pre  → reads .agent-wiki, returns relevant entries as context
  wiki-compile     post → calls LLM to synthesize + writes wiki entry
  wiki-lint        post → scans .agent-wiki health, returns report
  security-auditor pre  → runs scan_secrets.py, injects findings
  tdd-enforcer     pre  → injects SKILL.md content as instruction text
  handover         post → generates context bridge summary (text only)
  (all others)     pre  → inject SKILL.md content as instruction text

Usage:
    executor = SkillExecutor(wiki_manager, skills_dir)
    context_str  = await executor.execute_pre("wiki-query", task)
    report       = await executor.execute_post("wiki-compile", task, result, model_router)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any
import structlog

if TYPE_CHECKING:
    from agent.skills.wiki_manager import WikiManager
    from agent.skills.skill_loader import SkillManager

logger = structlog.get_logger()

# Skills that take *action* post-task (others are text-only pre-injection)
POST_ACTION_SKILLS = {"wiki-compile", "wiki-lint", "handover"}
# Skills that take *action* pre-task (beyond text injection)
PRE_ACTION_SKILLS = {"wiki-query", "security-auditor"}


class SkillExecutor:
    """Dispatches pre/post skill execution for each registered skill."""

    def __init__(self, wiki_manager: "WikiManager", skill_manager: "SkillManager"):
        self.wiki = wiki_manager
        self.skill_manager = skill_manager
        self.logger = logger.bind(component="skill_executor")

    # ------------------------------------------------------------------
    # Pre-execution skills
    # ------------------------------------------------------------------

    async def execute_pre(self, skill_name: str, task: str) -> str:
        """Run a pre-task skill. Returns a context string to inject into the prompt.

        Returns empty string if skill produces no output or is not found.
        """
        skill = self.skill_manager.get_skill(skill_name)
        self.logger.info("skill_pre_execute", skill=skill_name)

        if skill_name == "wiki-query":
            return self._wiki_query(task)

        if skill_name == "security-auditor":
            return self._security_scan()

        # Default: inject SKILL.md content as prompt instructions
        if skill:
            return f"\n\n## Skill Instruction: {skill.name}\n{skill.content}"

        return ""

    # ------------------------------------------------------------------
    # Post-execution skills
    # ------------------------------------------------------------------

    async def execute_post(
        self,
        skill_name: str,
        task: str,
        result: dict[str, Any],
        model_router=None,
    ) -> dict[str, Any]:
        """Run a post-task skill. Returns a report dict."""
        self.logger.info("skill_post_execute", skill=skill_name)

        if skill_name == "wiki-compile":
            return await self._wiki_compile(task, result, model_router)

        if skill_name == "wiki-lint":
            return {"report": self.wiki.lint()}

        if skill_name == "handover":
            return {"report": "Handover skill: run /handover in a new session to generate context bridge."}

        return {"report": f"Skill '{skill_name}' has no post-action implementation."}

    # ------------------------------------------------------------------
    # Skill implementations
    # ------------------------------------------------------------------

    def _wiki_query(self, task: str) -> str:
        """Query wiki for terms extracted from the task."""
        # Use the first 5 meaningful words as search terms
        stop_words = {"the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with"}
        terms = [w for w in task.lower().split() if w not in stop_words and len(w) > 2][:5]
        context = self.wiki.query(terms)
        if context:
            self.logger.info("wiki_query_hit", terms=terms)
        return context

    def _security_scan(self) -> str:
        """Run scan_secrets.py and return findings as context."""
        scan_script = Path("skills/security-auditor/scripts/scan_secrets.py")
        if not scan_script.exists():
            return ""

        try:
            # Import and run the scanner directly
            import importlib.util
            spec = importlib.util.spec_from_file_location("scan_secrets", scan_script)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            findings = mod.scan_files(".")

            if not findings:
                return "\n\n**Security Scan:** No secrets detected."

            lines = ["\n\n**Security Scan Findings (review before commit):**"]
            for f in findings[:10]:
                lines.append(f"- `{f['file']}` — {f['type']}")
            if len(findings) > 10:
                lines.append(f"- ... and {len(findings) - 10} more")
            return "\n".join(lines)
        except Exception as e:
            self.logger.error("security_scan_failed", error=str(e))
            return ""

    async def _wiki_compile(
        self, task: str, result: dict[str, Any], model_router=None
    ) -> dict[str, Any]:
        """Synthesize a wiki entry from task+result using the LLM, then write it."""
        response = result.get("response", "")
        if not response:
            return {"report": "wiki-compile: no response to compile."}

        if model_router is None:
            # Fallback: write a basic entry without LLM synthesis
            content = f"## Task\n{task}\n\n## Outcome\n{response[:800]}"
            rel_path = self.wiki.compile(
                title=task[:60],
                content=content,
                tags=["auto-compiled"],
                confidence="speculative",
            )
            return {"report": f"wiki-compile: entry written to {rel_path} (no LLM synthesis)"}

        synthesis_prompt = f"""You are a knowledge compiler for an agent wiki.
Given the task and result below, write a concise wiki entry in markdown.

Task: {task}

Result summary (truncated):
{response[:1200]}

Write a wiki entry with:
1. A SHORT title (5-8 words) on the first line starting with "TITLE: "
2. Tags (2-4 keywords) on the second line starting with "TAGS: "
3. Category on the third line starting with "CATEGORY: " (one of: tech-patterns, bugs, decisions, api-usage, synthesis)
4. Confidence on the fourth line starting with "CONFIDENCE: " (high, medium, or speculative)
5. Then the body: ## Summary (2-3 sentences), ## Key Details (bullet points), ## Connections (wikilinks if any)

Be concise. The entry should be useful for future tasks, not a task log."""

        try:
            config = model_router.get_model("coding")
            if not config:
                raise ValueError("No coding model")
            synthesis = await model_router.generate(synthesis_prompt, config)

            # Parse structured output
            lines = synthesis.strip().splitlines()
            title = task[:60]
            tags: list[str] = ["auto-compiled"]
            category = None
            confidence = "medium"
            body_start = 0

            for i, line in enumerate(lines[:6]):
                if line.startswith("TITLE:"):
                    title = line[6:].strip()
                    body_start = i + 1
                elif line.startswith("TAGS:"):
                    tags = [t.strip() for t in line[5:].split(",")]
                    body_start = i + 1
                elif line.startswith("CATEGORY:"):
                    category = line[9:].strip()
                    body_start = i + 1
                elif line.startswith("CONFIDENCE:"):
                    confidence = line[11:].strip()
                    body_start = i + 1

            body = "\n".join(lines[body_start:]).strip()
            rel_path = self.wiki.compile(
                title=title,
                content=body,
                category=category,
                tags=tags,
                confidence=confidence,
            )
            return {"report": f"wiki-compile: entry written to {rel_path}"}

        except Exception as e:
            self.logger.error("wiki_compile_failed", error=str(e))
            return {"report": f"wiki-compile failed: {str(e)}"}


__all__ = ["SkillExecutor", "PRE_ACTION_SKILLS", "POST_ACTION_SKILLS"]
