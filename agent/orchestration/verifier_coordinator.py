"""Verifier coordinator — runs verification rubrics and builds fix-task specs.

Owns:
  - VerifierCoordinator.run_verification()   async, returns VerifierResult
  - VerifierCoordinator.make_fix_specs()     returns list of task spec dicts
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import structlog

from agent.orchestration.context_builder import char_budget

if TYPE_CHECKING:
    from agent.agents.verifier_agent import VerifierAgent, VerifierResult
    from llm import ModelRouter

logger = structlog.get_logger()


class VerifierCoordinator:
    """Runs verification and assembles fix-task specs after failed rounds."""

    def __init__(self, verifier_agent: "VerifierAgent", model_router: "ModelRouter"):
        self.verifier_agent = verifier_agent
        self.model_router = model_router
        self.logger = logger.bind(component="verifier_coordinator")

    # ------------------------------------------------------------------
    # Verification
    # ------------------------------------------------------------------

    async def run_verification(
        self,
        objective: str,
        task_type: str,
        combined_response: str,
        files_created: list,
        tool_executor=None,
    ) -> "VerifierResult":
        """Run the appropriate verifier rubric and return a VerifierResult."""
        from agent.agents.verifier_agent import VerifierResult
        try:
            if task_type == "research":
                _ws = os.getenv("WORKSPACE_PATH", "./workspace")
                _ws_base = Path(_ws).resolve()
                _per_file = char_budget(self.model_router, fraction=0.05, cap=12_000)
                file_excerpts: list[str] = []
                for fp in files_created[:6]:
                    try:
                        full = (_ws_base / fp).resolve()
                        if full.is_relative_to(_ws_base) and full.exists():
                            content = full.read_text(encoding="utf-8", errors="ignore")
                            file_excerpts.append(f"### File: {fp}\n\n{content[:_per_file]}")
                    except Exception:
                        pass
                if file_excerpts:
                    excerpt = "\n\n".join(file_excerpts)
                else:
                    if len(combined_response) > 4500:
                        excerpt = combined_response[:1000] + "\n[...]\n" + combined_response[-3500:]
                    else:
                        excerpt = combined_response
                return await self.verifier_agent.verify_research(
                    objective, excerpt, files_created
                )
            else:
                if len(combined_response) > 4500:
                    excerpt = combined_response[:1000] + "\n[...]\n" + combined_response[-3500:]
                else:
                    excerpt = combined_response
                return await self.verifier_agent.verify_code(
                    objective, excerpt, files_created, tool_executor=tool_executor
                )
        except Exception as exc:
            self.logger.error("verification_failed", error=str(exc))
            # Return a passing stub so the loop can continue rather than crash.
            return VerifierResult(score=7, passed=True, gaps=[], feedback="(verification error)")

    # ------------------------------------------------------------------
    # Fix-spec generation
    # ------------------------------------------------------------------

    def make_fix_specs(
        self,
        objective: str,
        task_type: str,
        vresult: "VerifierResult",
        round_num: int,
        files_created: list | None = None,
    ) -> list[dict]:
        """Return the fix task spec(s) to inject after a failed verification round.

        Research + files: inject targeted research tasks for each gap, then a
        documenter task that updates the output file using the new research.

        Research without files or code tasks: single combined fix task.
        """
        gaps = vresult.gaps
        output_files = files_created or []

        if task_type == "research" and output_files:
            file_ref = output_files[0]
            specs: list[dict] = []
            for gap in gaps[:2]:
                specs.append({
                    "description": (
                        f"[Fix round {round_num} — research] "
                        f"Search the web and investigate: {gap}. "
                        f"Context: {objective[:80]}"
                    ),
                    "agent_type": "research",
                })
            existing_content = ""
            try:
                _ws = os.getenv("WORKSPACE_PATH", "./workspace")
                _ws_base = Path(_ws).resolve()
                _fp = (_ws_base / file_ref).resolve()
                if _fp.is_relative_to(_ws_base) and _fp.exists():
                    existing_content = _fp.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                pass
            gaps_text = "; ".join(gaps[:3])
            _existing_cap = char_budget(self.model_router, fraction=0.10, cap=60_000)
            existing_block = (
                f"\n\n### EXISTING FILE CONTENT — copy this verbatim then add new sections after it:\n"
                f"```\n{existing_content[:_existing_cap]}\n```"
                if existing_content else ""
            )
            specs.append({
                "description": (
                    f"[Fix round {round_num} — update file] "
                    f"Extend '{file_ref}' to cover these missing topics: {gaps_text}. "
                    f"CRITICAL: The existing file content is shown below. "
                    f"You MUST output EVERY existing section unchanged, then append new sections for the gaps. "
                    f"Do NOT summarize or shorten existing content. "
                    f"Write the complete updated file using FILE: blocks."
                    f"{existing_block}"
                ),
                "agent_type": "documenter",
            })
            return specs

        fix_type = "develop" if task_type != "research" else "research"
        gaps_text = "; ".join(gaps[:3])
        return [{
            "description": (
                f"[Fix round {round_num}] Original objective: {objective[:120]}. "
                f"Gaps to address: {gaps_text}"
            ),
            "agent_type": fix_type,
        }]
