"""Verifier (critic) agent — checks agent output against the original objective.

Two rubrics:
  - Research: coverage, depth, topic completeness, file output when requested.
  - Code: requirement fulfilment, test execution, correctness.

Both return VerifierResult with a 0-10 score, a passed flag (threshold >= 7),
a list of specific gaps, and a free-text feedback string.
"""

import json
import re
from dataclasses import dataclass, field
from typing import List, Optional
import structlog

PASS_THRESHOLD = 7

logger = structlog.get_logger()


@dataclass
class VerifierResult:
    score: int
    passed: bool
    gaps: List[str] = field(default_factory=list)
    feedback: str = ""
    task_type: str = ""

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "passed": self.passed,
            "gaps": self.gaps,
            "feedback": self.feedback,
            "task_type": self.task_type,
        }


class VerifierAgent:
    """Critic agent that scores and approves/rejects agent output.

    Instantiate once in the orchestrator and call verify_research() or
    verify_code() at the end of each task loop.  Both methods return a
    VerifierResult; if passed is False the orchestrator injects the
    feedback as extra_context and re-runs the loop (max 2 rounds).
    """

    def __init__(self, model_router):
        self.model_router = model_router
        self.logger = logger.bind(component="verifier_agent")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def verify_research(
        self,
        objective: str,
        response: str,
        files_created: List[str],
    ) -> VerifierResult:
        """Score research output on coverage, depth, format, and actionability."""
        system = (
            "You are a strict research quality reviewer. Be critical and precise. "
            "Your job is to identify what is MISSING or INCOMPLETE, not to praise what is present."
        )
        prompt = (
            f"Original objective:\n{objective}\n\n"
            f"Agent response (excerpt):\n{response[:6000]}\n\n"
            f"Files created: {files_created if files_created else ['(none)']}\n\n"
            "Evaluate on these four dimensions and return ONLY valid JSON:\n\n"
            "1. Coverage (0-4): Are ALL topics/aspects explicitly mentioned in the objective addressed?\n"
            "2. Depth (0-3): Is content substantive with specific facts, names, examples — "
            "not just surface summaries?\n"
            "3. Format compliance (0-2): If the objective asked for files/markdown/documents, "
            "were they actually created (check files_created list)?\n"
            "4. Actionability (0-1): Are findings concrete and immediately usable?\n\n"
            'Return: {"score": <sum 0-10>, '
            '"gaps": ["<specific missing topic or issue>", ...], '
            '"feedback": "<one concise paragraph>"}\n\n'
            "Be specific in gaps — name the exact topics missing. "
            f"Score >= {PASS_THRESHOLD} is a PASS."
        )
        raw = await self._call_llm(system, prompt)
        result = self._parse_result(raw, "research")
        self.logger.info(
            "verify_research_complete",
            score=result.score,
            passed=result.passed,
            gaps=len(result.gaps),
        )
        return result

    async def verify_code(
        self,
        objective: str,
        response: str,
        files_created: List[str],
        tool_executor=None,
    ) -> VerifierResult:
        """Score code output — also executes tests when tool_executor is available."""
        test_output = await self._run_tests(tool_executor)

        system = (
            "You are a strict code reviewer. Focus on correctness and completeness. "
            "Do not accept partial implementations as sufficient."
        )
        prompt = (
            f"Original objective:\n{objective}\n\n"
            f"Agent response (excerpt):\n{response[:6000]}\n\n"
            f"Files created: {files_created if files_created else ['(none)']}\n\n"
            f"Test execution output:\n{test_output}\n\n"
            "Evaluate on these four dimensions and return ONLY valid JSON:\n\n"
            "1. Requirement fulfilment (0-4): Does the implementation address EVERYTHING "
            "requested in the objective?\n"
            "2. Completeness (0-3): Are edge cases, error handling, and all requested "
            "files/features present?\n"
            "3. Code correctness (0-2): No obvious logic errors, follows good patterns?\n"
            "4. Test results (0-1): 1 if tests pass or no tests were requested; "
            "0 if tests exist and are failing.\n\n"
            'Return: {"score": <sum 0-10>, '
            '"gaps": ["<specific missing requirement or defect>", ...], '
            '"feedback": "<one concise paragraph>"}\n\n'
            f"Score >= {PASS_THRESHOLD} is a PASS."
        )
        raw = await self._call_llm(system, prompt)
        result = self._parse_result(raw, "code")
        self.logger.info(
            "verify_code_complete",
            score=result.score,
            passed=result.passed,
            gaps=len(result.gaps),
            tests_ran=bool(test_output),
        )
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _run_tests(self, tool_executor) -> str:
        if not tool_executor:
            return "(no tool executor — tests not run)"
        try:
            out = await tool_executor.execute(
                "shell",
                {"command": "python -m pytest --tb=short -q 2>&1 | tail -20"},
            )
            if out and not str(out).startswith("Error"):
                return str(out)
        except Exception as exc:
            self.logger.warning("verifier_test_run_failed", error=str(exc))
        return "(test execution unavailable)"

    async def _call_llm(self, system: str, prompt: str) -> dict:
        try:
            model = self.model_router.get_model("coding")
            response = await self.model_router.generate(
                prompt,
                model,
                system_prompt=system,
                enable_thinking=False,
            )
            match = re.search(r"\{[\s\S]*\}", response or "")
            if not match:
                self.logger.warning("verifier_no_json", preview=(response or "")[:120])
                return {}
            return json.loads(match.group())
        except json.JSONDecodeError as exc:
            self.logger.warning("verifier_json_parse_error", error=str(exc))
            return {}
        except Exception as exc:
            self.logger.warning("verifier_llm_error", error=str(exc))
            return {}

    def _parse_result(self, raw: dict, task_type: str) -> VerifierResult:
        score = max(0, min(10, int(raw.get("score", 5))))
        gaps = [str(g) for g in raw.get("gaps", []) if g]
        feedback = str(raw.get("feedback", ""))
        return VerifierResult(
            score=score,
            passed=score >= PASS_THRESHOLD,
            gaps=gaps,
            feedback=feedback,
            task_type=task_type,
        )
