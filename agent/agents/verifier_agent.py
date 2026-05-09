"""Verifier (critic) agent — checks agent output against the original objective.

Two rubrics:
  - Research: two LLM calls — (1) Coverage+Depth scored 0-10, (2) Format binary.
  - Code: requirement fulfilment, test execution, correctness (single call).

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
        """Score research output using two focused LLM calls."""
        # _run_verification composes a smart multi-file excerpt; do not re-truncate it.
        excerpt = response

        # Call 1: Coverage (0-5) + Depth (0-5)
        system1 = (
            "You are a strict research quality reviewer. Be critical and precise. "
            "Identify what is MISSING or INCOMPLETE, not what is present."
        )
        prompt1 = (
            f"Original objective:\n{objective}\n\n"
            f"Agent response (excerpt):\n{excerpt}\n\n"
            "Score ONLY these two dimensions and return ONLY valid JSON:\n\n"
            "1. Coverage (0-5): Are ALL topics/aspects in the objective addressed? "
            "5=everything covered, 0=almost nothing covered.\n"
            "2. Depth (0-5): Is content substantive with specific facts, names, examples — "
            "not just surface summaries? 5=rich detail, 0=vague.\n\n"
            'Return: {"coverage": <0-5>, "depth": <0-5>, '
            '"gaps": ["<specific missing topic>", ...], '
            '"feedback": "<one concise sentence>"}'
        )
        raw1 = await self._call_llm(system1, prompt1)

        coverage = max(0, min(5, int(raw1.get("coverage", 2))))
        depth = max(0, min(5, int(raw1.get("depth", 2))))
        score = coverage + depth
        gaps = [str(g) for g in raw1.get("gaps", []) if g]
        feedback = str(raw1.get("feedback", ""))

        # Call 2: Format binary — were files created if the objective asked for them?
        files_requested = any(
            kw in objective.lower()
            for kw in ("file", "markdown", "report", "document", "write", "save", "output")
        )
        if files_requested:
            system2 = "You are a strict output format checker. Answer only yes or no."
            prompt2 = (
                f"Objective: {objective}\n\n"
                f"Files created: {files_created if files_created else ['(none)']}\n\n"
                "Did the agent create the files or documents the objective requested? "
                'Return ONLY valid JSON: {"files_created": true} or {"files_created": false}'
            )
            raw2 = await self._call_llm(system2, prompt2)
            if not raw2.get("files_created", True):
                gaps.append("Required output files were not created")

        files_ok = not any("file" in g.lower() for g in gaps if "Required output" in g)
        report = self._format_research_report(coverage, depth, files_ok, score, gaps)
        self.logger.info(
            "verify_research_complete",
            score=score,
            passed=score >= PASS_THRESHOLD,
            gaps=len(gaps),
        )
        return VerifierResult(
            score=score,
            passed=score >= PASS_THRESHOLD,
            gaps=gaps,
            feedback=report,
            task_type="research",
        )

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
            f"Agent response (excerpt):\n{response[:3000]}\n\n"
            f"Files created: {files_created if files_created else ['(none)']}\n\n"
            f"Test execution output:\n{test_output}\n\n"
            "Evaluate on these three dimensions and return ONLY valid JSON:\n\n"
            "1. Requirement fulfilment (0-5): Does the implementation address EVERYTHING "
            "requested in the objective?\n"
            "2. Completeness (0-3): Are edge cases, error handling, and all requested "
            "files/features present?\n"
            "3. Test results (0-2): 2 if tests pass; 1 if no tests were requested; "
            "0 if tests exist and are failing.\n\n"
            'Return: {"score": <sum 0-10>, '
            '"gaps": ["<specific missing requirement or defect>", ...], '
            '"feedback": "<one concise sentence>"}\n\n'
            f"Score >= {PASS_THRESHOLD} is a PASS."
        )
        raw = await self._call_llm(system, prompt)
        score = max(0, min(10, int(raw.get("score", 5))))
        gaps = [str(g) for g in raw.get("gaps", []) if g]
        report = self._format_code_report(score, gaps, test_output)
        self.logger.info(
            "verify_code_complete",
            score=score,
            passed=score >= PASS_THRESHOLD,
            gaps=len(gaps),
            tests_ran=bool(test_output),
        )
        return VerifierResult(
            score=score,
            passed=score >= PASS_THRESHOLD,
            gaps=gaps,
            feedback=report,
            task_type="code",
        )

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

    def _format_research_report(
        self, coverage: int, depth: int, files_ok: bool, score: int, gaps: List[str]
    ) -> str:
        cov_line = f"Coverage : {'PASS' if coverage >= 3 else 'FAIL'} ({coverage}/5)"
        dep_line = f"Depth    : {'PASS' if depth >= 3 else 'FAIL'} ({depth}/5)"
        fmt_line = f"Format   : {'PASS' if files_ok else 'FAIL'}"
        overall  = f"Overall  : {'PASS' if score >= PASS_THRESHOLD else 'FAIL'} ({score}/10)"
        lines = ["RESEARCH QUALITY", "=" * 16, cov_line, dep_line, fmt_line, "", overall]
        if gaps:
            lines += ["", "Gaps:"] + [f"  • {g}" for g in gaps[:5]]
        return "\n".join(lines)

    def _format_code_report(self, score: int, gaps: List[str], test_output: str) -> str:
        tests_ok = "pass" in test_output.lower() and "fail" not in test_output.lower()
        test_line = f"Tests    : {'PASS' if tests_ok else 'SKIP/FAIL'}"
        overall  = f"Overall  : {'PASS' if score >= PASS_THRESHOLD else 'FAIL'} ({score}/10)"
        lines = ["CODE QUALITY", "=" * 12, test_line, "", overall]
        if gaps:
            lines += ["", "Issues:"] + [f"  • {g}" for g in gaps[:5]]
        return "\n".join(lines)

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
