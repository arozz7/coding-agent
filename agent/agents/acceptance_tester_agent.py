"""AcceptanceTesterAgent — evaluates behavioral acceptance criteria via LLM.

Takes a list of criteria and an optional screenshot path, makes a single LLM
call to evaluate all criteria at once, and returns an AcceptanceResult per
criterion with a pass/fail flag and rich text detail.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

import structlog

if TYPE_CHECKING:
    from llm import ModelRouter

logger = structlog.get_logger()

_JSON_OBJ_RE = re.compile(r'\{[\s\S]*\}', re.DOTALL)


@dataclass
class AcceptanceResult:
    """Result for a single acceptance criterion."""

    criterion: str
    passed: bool
    detail: str = ""


class AcceptanceTesterAgent:
    """Evaluates acceptance criteria with a single LLM call per round."""

    def __init__(self, model_router: "ModelRouter") -> None:
        self.model_router = model_router
        self.logger = logger.bind(component="acceptance_tester_agent")

    async def run_tests(
        self,
        criteria: List[str],
        workspace: Path,
        screenshot_path: Optional[str],
        agent_output: str = "",
    ) -> List[AcceptanceResult]:
        """Evaluate all criteria in a single LLM call.

        Returns one AcceptanceResult per criterion.  On any failure all
        criteria are returned as failed with an error detail so the fix loop
        gets signal.
        """
        if not criteria:
            return []

        model = self.model_router.get_model("coding")
        if not model:
            return [AcceptanceResult(criterion=c, passed=False, detail="(no model available)") for c in criteria]

        criteria_block = "\n".join(f"  {i + 1}. {c}" for i, c in enumerate(criteria))
        screenshot_note = (
            f"\nA screenshot of the running application was taken and saved to: {screenshot_path}\n"
            "Use this path as context when evaluating visual/behavioral criteria."
            if screenshot_path else ""
        )
        # When there is no screenshot, include the agent's output as evidence so
        # the LLM can evaluate behavioral criteria (e.g. "Phase 1 expanded into 5+
        # tasks") against what was actually produced rather than returning blind failures.
        output_note = ""
        if not screenshot_path and agent_output:
            excerpt = agent_output[:6000]
            output_note = f"\n\nAgent output (evidence for evaluation):\n{excerpt}"

        system = (
            "You are a strict acceptance test evaluator. "
            "Evaluate each criterion based on the evidence provided. "
            "Be concrete — cite what you observed or what was missing. "
            "Return ONLY valid JSON in the exact format shown."
        )
        prompt = (
            f"Workspace: {workspace}\n"
            f"{screenshot_note}"
            f"{output_note}\n\n"
            f"Acceptance criteria to evaluate:\n{criteria_block}\n\n"
            "For each criterion return pass/fail with a detail sentence explaining your verdict.\n"
            "Return ONLY:\n"
            '{"results": [\n'
            '  {"criterion": "<exact criterion text>", "passed": true|false, "detail": "<explanation>"},\n'
            '  ...\n'
            ']}'
        )

        try:
            raw = await self.model_router.generate(prompt, model, system_prompt=system, enable_thinking=False)
            results = self._parse(raw, criteria)
            passed_count = sum(1 for r in results if r.passed)
            self.logger.info(
                "acceptance_tests_complete",
                total=len(results),
                passed=passed_count,
                screenshot=screenshot_path is not None,
            )
            return results
        except Exception as exc:
            self.logger.warning("acceptance_tests_failed", error=str(exc))
            return [AcceptanceResult(criterion=c, passed=False, detail=f"(evaluation error: {exc})") for c in criteria]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _parse(self, raw: str, criteria: List[str]) -> List[AcceptanceResult]:
        """Extract AcceptanceResult list from LLM output."""
        m = _JSON_OBJ_RE.search(raw or "")
        if not m:
            return [AcceptanceResult(criterion=c, passed=False, detail="(unparseable response)") for c in criteria]
        try:
            obj = json.loads(m.group())
            items = obj.get("results", [])
            results: List[AcceptanceResult] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                results.append(AcceptanceResult(
                    criterion=str(item.get("criterion", "")),
                    passed=bool(item.get("passed", False)),
                    detail=str(item.get("detail", "")),
                ))
            # If LLM returned fewer results than criteria, pad with failures
            if len(results) < len(criteria):
                found = {r.criterion for r in results}
                for c in criteria:
                    if c not in found:
                        results.append(AcceptanceResult(criterion=c, passed=False, detail="(not evaluated)"))
            return results
        except (json.JSONDecodeError, Exception) as exc:
            return [AcceptanceResult(criterion=c, passed=False, detail=f"(parse error: {exc})") for c in criteria]
