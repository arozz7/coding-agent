"""Verifier coordinator — runs verification rubrics and builds fix-task specs.

Owns:
  - VerifierCoordinator.run_verification()   async, returns VerifierResult
  - VerifierCoordinator.make_fix_specs()     returns list of task spec dicts
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Coroutine, List, Optional

import structlog

from agent.orchestration.context_builder import char_budget

if TYPE_CHECKING:
    from agent.agents.acceptance_tester_agent import AcceptanceTesterAgent, AcceptanceResult
    from agent.agents.verifier_agent import VerifierAgent, VerifierResult
    from agent.orchestration.app_probe import AppProbe
    from llm import ModelRouter


@dataclass
class CriterionResult:
    """Result of evaluating a single completion criterion."""

    criterion: str
    passed: bool
    detail: str = ""  # what was checked or why it failed

logger = structlog.get_logger()


# Directories that should never be searched for deliverable files.
_EXCLUDE_DIRS = frozenset({
    "node_modules", ".git", ".venv", "venv", "__pycache__",
    "vendor", "target", "dist", "build", ".next", ".nuxt",
})


def _glob_filtered(ws: Path, pattern: str) -> list[Path]:
    """Glob `pattern` relative to `ws`, excluding dependency/build directories.

    Results are sorted by path depth (shallowest first) so that project-root
    files are preferred over deeply nested ones in subdirectories.
    """
    matches = [
        p for p in ws.glob(pattern)
        if not any(part in _EXCLUDE_DIRS for part in p.parts)
        and p.is_file()
    ]
    matches.sort(key=lambda p: len(p.parts))
    return matches


def _select_primary_file(output_files: list[str]) -> str:
    """Prefer root-level files over subdirectory files; fall back to first."""
    for f in output_files:
        if "/" not in f.replace("\\", "/").lstrip("/"):
            return f
    return output_files[0]


def _summarize_existing_sections(text: str, max_chars: int = 3000) -> str:
    """Return a compact map of heading + first 150 chars of each section body.

    This gives the documenter enough context to know what each section already
    covers without sending the full file, preventing it from generating content
    that duplicates existing sections under different headings.
    """
    lines = text.splitlines()
    sections: list[str] = []
    current_heading = ""
    body_lines: list[str] = []

    def _flush():
        if not current_heading:
            return
        body = " ".join(body_lines).strip()
        snippet = body[:150].rstrip()
        if len(body) > 150:
            snippet += "…"
        sections.append(f"{current_heading}  →  {snippet}" if snippet else current_heading)

    for line in lines:
        if line.startswith("#"):
            _flush()
            current_heading = line.strip()
            body_lines = []
        else:
            stripped = line.strip()
            if stripped and not stripped.startswith("|") and len(body_lines) < 4:
                body_lines.append(stripped)
    _flush()

    result = "\n".join(sections)
    return result[:max_chars]


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
                # For code tasks, prefer reading actual files over agent dialogue text.
                # The dialogue is often shell commands / fix-round noise that confuses
                # the scoring LLM and produces 0/10 even when the file is valid.
                _ws = os.getenv("WORKSPACE_PATH", "./workspace")
                _ws_base = Path(_ws).resolve()
                _per_file = char_budget(self.model_router, fraction=0.08, cap=16_000)
                file_excerpts: list[str] = []
                for fp in files_created[:4]:
                    try:
                        full = (_ws_base / fp).resolve()
                        if full.is_relative_to(_ws_base) and full.exists():
                            content = full.read_text(encoding="utf-8", errors="ignore")
                            file_excerpts.append(f"### File: {fp}\n\n{content[:_per_file]}")
                    except Exception:
                        pass
                if file_excerpts:
                    excerpt = "\n\n".join(file_excerpts)
                elif len(combined_response) > 4500:
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
    # Criterion evaluation
    # ------------------------------------------------------------------

    async def evaluate_criteria(
        self,
        criteria: list[str],
        ws: Path,
        shell_fn: Callable[[str], Coroutine],
        combined_response: str = "",
    ) -> list[CriterionResult]:
        """Evaluate each criterion and return a CriterionResult per item.

        Auto-check patterns (no LLM call):
          "command exits 0: <cmd>"     — runs cmd; passes if exit code 0
          "file exists: <path>"        — passes if workspace-relative path exists
          "file contains: <path>:<sub>" — passes if file text contains substring

        Everything else falls back to an LLM pass/fail call.
        """
        results: list[CriterionResult] = []
        for criterion in criteria:
            result = await self._eval_one(criterion, ws, shell_fn, combined_response)
            results.append(result)
        self.logger.info(
            "criteria_evaluated",
            total=len(results),
            passed=sum(1 for r in results if r.passed),
        )
        return results

    async def _eval_one(
        self,
        criterion: str,
        ws: Path,
        shell_fn: Callable[[str], Coroutine],
        combined_response: str,
    ) -> CriterionResult:
        lower = criterion.lower()

        # --- auto-check: command exits 0 ---
        if lower.startswith("command exits 0:"):
            cmd = criterion[len("command exits 0:"):].strip()
            # Guard against long-running server commands
            _BLOCKING = ("npm start", "flask run", "uvicorn", "gunicorn", "python -m http.server", "serve")
            if any(b in cmd.lower() for b in _BLOCKING):
                return CriterionResult(criterion=criterion, passed=True, detail="(server command skipped)")
            try:
                out = await shell_fn(f"{cmd}; echo __EXIT__$?")
                m = re.search(r"__EXIT__(\d+)", out or "")
                if m and m.group(1) == "0":
                    return CriterionResult(criterion=criterion, passed=True, detail=f"exit 0: {cmd}")
                return CriterionResult(criterion=criterion, passed=False, detail=f"non-zero exit: {cmd}\n{(out or '')[:400]}")
            except Exception as exc:
                return CriterionResult(criterion=criterion, passed=False, detail=f"shell error: {exc}")

        # --- auto-check: file exists ---
        if lower.startswith("file exists:"):
            rel = criterion[len("file exists:"):].strip()
            if "*" in rel or "?" in rel:
                matches = _glob_filtered(ws, rel)
                ok = len(matches) > 0
                detail = f"found: {matches[0].relative_to(ws)}" if ok else f"no match: {rel}"
            else:
                target = (ws / rel).resolve()
                ok = target.exists()
                if not ok:
                    # Fallback: agent may have used a different name — search recursively by ext
                    ext = Path(rel).suffix
                    fallback = _glob_filtered(ws, f"**/*{ext}") if ext else []
                    if fallback:
                        ok = True
                        detail = f"found (as {fallback[0].relative_to(ws)}, not {rel})"
                    else:
                        detail = f"missing: {rel}"
                else:
                    detail = f"found: {rel}"
            return CriterionResult(criterion=criterion, passed=ok, detail=detail)

        # --- auto-check: file contains ---
        if lower.startswith("file contains:"):
            payload = criterion[len("file contains:"):].strip()
            if ":" not in payload:
                return CriterionResult(criterion=criterion, passed=False, detail="malformed — expected path:substring")
            rel, substring = payload.split(":", 1)
            rel, substring = rel.strip(), substring.strip()
            try:
                if "*" in rel or "?" in rel:
                    matches = _glob_filtered(ws, rel)
                    if not matches:
                        # Try recursive variant if no match at root level
                        if not rel.startswith("**"):
                            matches = _glob_filtered(ws, f"**/{rel.lstrip('/')}")
                    if not matches:
                        return CriterionResult(criterion=criterion, passed=False, detail=f"no match: {rel}")
                    for match in matches:
                        content = match.read_text(encoding="utf-8", errors="ignore")
                        if substring in content:
                            return CriterionResult(criterion=criterion, passed=True, detail=f"found: '{substring[:60]}' in {match.relative_to(ws)}")
                    return CriterionResult(criterion=criterion, passed=False, detail=f"not found: '{substring[:60]}' in any {rel}")
                # Exact-filename path — if absent, fall back to recursive *.ext search
                target_path = ws / rel
                if not target_path.exists():
                    ext = Path(rel).suffix
                    fallback = _glob_filtered(ws, f"**/*{ext}") if ext else []
                    if fallback:
                        target_path = fallback[0]
                content = target_path.read_text(encoding="utf-8", errors="ignore")
                ok = substring in content
                return CriterionResult(criterion=criterion, passed=ok, detail=f"{'found' if ok else 'not found'}: '{substring[:60]}' in {target_path.relative_to(ws)}")
            except Exception as exc:
                return CriterionResult(criterion=criterion, passed=False, detail=f"read error: {exc}")

        # --- LLM fallback ---
        return await self._llm_eval_criterion(criterion, combined_response)

    async def _llm_eval_criterion(self, criterion: str, combined_response: str) -> CriterionResult:
        """Use a free evaluator model to judge a behavioral/visual criterion."""
        try:
            model = await self.model_router.get_evaluator_model()
            system = "You are a strict pass/fail evaluator. Answer only with valid JSON."
            excerpt = combined_response[:2000] if combined_response else "(no response available)"
            prompt = (
                f"Criterion: {criterion}\n\n"
                f"Agent output (excerpt):\n{excerpt}\n\n"
                'Does the agent output satisfy this criterion? Return ONLY: {"passed": true} or {"passed": false, "detail": "<why not>"}'
            )
            raw = await self.model_router.generate(prompt, model, system_prompt=system, enable_thinking=False)
            raw_s = (raw or "").strip()
            brace = raw_s.find("{")
            if brace != -1:
                obj, _ = json.JSONDecoder().raw_decode(raw_s[brace:])
                passed = bool(obj.get("passed", False))
                detail = str(obj.get("detail", ""))
                return CriterionResult(criterion=criterion, passed=passed, detail=detail)
        except Exception as exc:
            self.logger.warning("llm_criterion_eval_failed", error=str(exc))
        return CriterionResult(criterion=criterion, passed=False, detail="(evaluation error)")

    async def run_acceptance_tests(
        self,
        criteria: List[str],
        workspace: Path,
        app_probe: "AppProbe",
        acceptance_tester: "AcceptanceTesterAgent",
    ) -> List["AcceptanceResult"]:
        """Launch the app, screenshot it, evaluate acceptance criteria, teardown.

        Returns one AcceptanceResult per criterion.  On any launch failure all
        criteria are returned as failed so the fix loop gets signal.
        """
        from agent.agents.acceptance_tester_agent import AcceptanceResult

        if not criteria:
            return []

        handle = await app_probe.launch()
        screenshot_path: Optional[str] = None

        if handle is None:
            # No server entry point detected (e.g. pure static HTML deliverable).
            # Return empty list so the orchestrator skips the acceptance loop rather
            # than burning all fix rounds on an app that can never be launched.
            self.logger.info("acceptance_skipped_no_entry_point", workspace=str(workspace))
            return []

        try:
            screenshot_path = await app_probe.screenshot(handle)
            results = await acceptance_tester.run_tests(criteria, workspace, screenshot_path=screenshot_path)
        finally:
            await app_probe.teardown(handle)

        passed = sum(1 for r in results if r.passed)
        self.logger.info(
            "acceptance_tests_done",
            total=len(results),
            passed=passed,
            screenshot=screenshot_path is not None,
        )
        return results

    def make_targeted_fix_spec(
        self,
        failing: "CriterionResult",
        objective: str,
        round_num: int,
        test_out: str = "",
        screenshot_path: Optional[str] = None,
    ) -> dict:
        """Return a single fix task spec targeted at one failing criterion."""
        phase, instruction = self._detect_fix_phase(test_out, failing.detail, round_num)
        detail_block = f"\n\nWhy it failed: {failing.detail}" if failing.detail else ""
        test_block = f"\n\nVerifier output:\n```\n{test_out[:600]}\n```" if test_out else ""
        shot_block = (
            f"\n\nScreenshot of the running app: {screenshot_path}\n"
            "The screenshot shows the visual state at the time of failure — use it to understand what the user sees."
            if screenshot_path else ""
        )
        return {
            "description": (
                f"[Criterion fix — round {round_num}] {instruction}\n"
                f"Original objective: {objective[:100]}\n"
                f"Failing criterion: {failing.criterion}"
                f"{detail_block}"
                f"{test_block}"
                f"{shot_block}"
            ),
            "agent_type": "develop",
        }

    # ------------------------------------------------------------------
    # Fix-spec generation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_truncated_file_info(test_out: str) -> list[tuple[str, list[str], str, int]]:
        """Parse truncated file paths from verifier test output and read their tail.

        Returns list of (rel_path, last_lines, file_ext, total_lines) for each truncated file.
        """
        _ws = os.getenv("WORKSPACE_PATH", "./workspace")
        ws = Path(_ws).resolve()
        results: list[tuple[str, list[str], str, int]] = []
        in_block = False
        for line in test_out.splitlines():
            if "[truncated]" in line.lower() and "fail" in line.lower():
                in_block = True
                continue
            if in_block:
                if re.match(r"^\s{2,}\S", line):
                    rel = line.strip()
                    try:
                        full = (ws / rel).resolve()
                        if full.is_relative_to(ws) and full.exists():
                            raw = full.read_text(encoding="utf-8", errors="ignore")
                            all_lines = raw.splitlines()
                            last_lines = [ln for ln in all_lines[-30:] if ln.strip()]
                            results.append((rel, last_lines, full.suffix.lower(), len(all_lines)))
                    except Exception:
                        pass
                else:
                    in_block = False
        return results

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
        files_changed_this_round: list | None = None,
        prev_score: int = -1,
    ) -> list[dict]:
        """Return the fix task spec(s) to inject after a failed verification round.

        Research + files: inject targeted research tasks for each gap, then a
        documenter task that updates the output file using the new research.

        Research without files or code tasks: single combined fix task.
        """
        gaps = vresult.gaps
        output_files = files_created or []

        if task_type == "research" and output_files:
            file_ref = _select_primary_file(output_files)
            specs: list[dict] = []
            for gap in gaps[:2]:
                specs.append({
                    "description": (
                        f"[Fix round {round_num} — research] "
                        f"Review the workspace docs and research-cache directories for existing coverage, "
                        f"then search the web if the information is not already present. "
                        f"Investigate: {gap}. "
                        f"Context: {objective[:80]}"
                    ),
                    "agent_type": "research",
                })
            existing_coverage = ""
            try:
                _ws = os.getenv("WORKSPACE_PATH", "./workspace")
                _ws_base = Path(_ws).resolve()
                _fp = (_ws_base / file_ref).resolve()
                if _fp.is_relative_to(_ws_base) and _fp.exists():
                    raw = _fp.read_text(encoding="utf-8", errors="ignore")
                    existing_coverage = _summarize_existing_sections(raw, max_chars=3000)
            except Exception:
                pass
            gaps_text = "; ".join(gaps[:3])
            headings_block = (
                f"\n\nExisting file structure (do not repeat any of this):\n{existing_coverage}"
                if existing_coverage else ""
            )
            specs.append({
                "description": (
                    f"[Fix round {round_num} — update file] "
                    f"Add new sections to '{file_ref}' covering these missing topics: {gaps_text}. "
                    f"Write ONLY the new sections using APPEND: blocks — "
                    f"do NOT rewrite or repeat any content that already exists."
                    f"{headings_block}"
                ),
                "agent_type": "documenter",
            })
            return specs

        fix_type = "develop" if task_type != "research" else "research"
        gaps_text = "; ".join(gaps[:3])
        test_out = getattr(vresult, "test_output", "")

        # --- Dynamic phase detection from test output signals ---
        fix_phase, phase_instruction = self._detect_fix_phase(test_out, gaps_text, round_num)

        # --- Regression feedback ---
        regression_section = ""
        if prev_score >= 0 and vresult.score < prev_score and files_changed_this_round:
            changed_list = ", ".join(files_changed_this_round[:8])
            regression_section = (
                f"\n\n⚠️ REGRESSION: Your last fix REDUCED the score from "
                f"{prev_score}/10 to {vresult.score}/10. "
                f"The files modified were: {changed_list}. "
                f"Avoid re-modifying those files unless the gaps below directly call them out. "
                f"Try a different approach."
            )

        # --- Test output section ---
        test_section = ""
        if test_out:
            test_section = f"\n\nVerifier test output:\n```\n{test_out[:800]}\n```"

        # --- File chunking / append directive when truncation detected ---
        chunking_section = ""
        combined_signals = (test_out + " " + gaps_text).lower()
        _CODE_EXTS = frozenset({".js", ".mjs", ".cjs", ".ts", ".tsx", ".rs", ".go", ".py", ".java", ".cs"})
        if "[truncated]" in combined_signals or "truncat" in combined_signals:
            truncated_infos = self._extract_truncated_file_info(test_out)
            if truncated_infos:
                append_parts: list[str] = []
                has_code = False
                for rel, last_lines, ext, total_lines in truncated_infos:
                    tail = "\n".join(last_lines[-25:])
                    if ext == ".html":
                        append_parts.append(
                            f"\n  • `{rel}` is truncated. The last content is:\n"
                            f"    ```html\n{tail}\n    ```\n"
                            f"    APPEND only the minimal closing tags needed (e.g. `</div></body></html>`) "
                            f"using an APPEND: block — do NOT rewrite the file with FILE:."
                        )
                    elif ext in _CODE_EXTS:
                        has_code = True
                        append_parts.append(
                            f"\n  • `{rel}` is truncated at line {total_lines}. "
                            f"Last content:\n    ```\n{tail}\n    ```"
                        )
                chunking_section = "\n\n⚠️ FILE TRUNCATION DETECTED:" + "".join(append_parts)
                if has_code:
                    chunking_section += (
                        "\n\n  For code files: do NOT rewrite as one large block. "
                        "Split into small focused modules (<150 lines each) and use imports to compose them."
                    )
            else:
                chunking_section = (
                    "\n\n⚠️ FILE TRUNCATION DETECTED: One or more files were cut off during generation. "
                    "For HTML: use APPEND: blocks to add only the missing closing tags — do NOT rewrite with FILE:. "
                    "For code: split into small focused modules (<150 lines each) and use imports."
                )

        # --- Changed files section ---
        changed_section = ""
        if files_changed_this_round and not regression_section:
            changed_section = (
                f"\n\nFiles changed in last fix round: {', '.join(files_changed_this_round[:10])}"
            )

        return [{
            "description": (
                f"[Fix round {round_num} — {fix_phase}] {phase_instruction}\n"
                f"Original objective: {objective[:100]}.\n"
                f"Gaps to address: {gaps_text}"
                f"{regression_section}"
                f"{test_section}"
                f"{chunking_section}"
                f"{changed_section}"
            ),
            "agent_type": fix_type,
        }]

    # ------------------------------------------------------------------
    # Phase detection
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_fix_phase(test_out: str, gaps_text: str, round_num: int) -> tuple[str, str]:
        """Return (phase_label, phase_instruction) based on test output signals.

        Signal-based detection is the primary path; round_num is a tiebreaker
        only when no signal is present.
        """
        combined = (test_out + " " + gaps_text).lower()

        if "[truncated]" in combined or ("truncat" in combined and "incomplete" in combined):
            if ".html" in combined:
                return "file-incomplete", (
                    "One or more HTML files appear TRUNCATED (cut off during generation). "
                    "Do NOT rewrite the file using FILE: — that will truncate again. "
                    "Use APPEND: to add only the minimal closing tags needed to make the file valid "
                    "(e.g. `</div></body></html>`)."
                )
            return "file-incomplete", (
                "One or more files appear TRUNCATED (cut off during generation). "
                "Do NOT rewrite them as single large blocks. "
                "Split into small focused modules (<150 lines each) and use imports/includes to compose them."
            )

        _SYNTAX_SIGNALS = (
            "syntaxerror", "parse error", "error ts", "cargo check", "--check",
            "syntax error", "unexpected token", "invalid syntax", "expected expression",
        )
        if any(sig in combined for sig in _SYNTAX_SIGNALS):
            return "syntax", (
                "PRIORITY: Fix ALL syntax errors first — they block every other fix. "
                "Run the appropriate syntax checker (node --check, cargo check, python -m py_compile, etc.) "
                "on every source file and fix every error."
            )

        _RUNTIME_SIGNALS = (
            "error:", "traceback", "panic", "exception", "importerror",
            "modulenotfounderror", "referenceerror", "typeerror", "cannot find module",
            "nameerror", "attributeerror", "is not defined",
        )
        if any(sig in combined for sig in _RUNTIME_SIGNALS):
            return "runtime", (
                "Syntax is clean. Fix runtime errors: undefined references, missing imports, "
                "wrong module paths, misconfigured entry points. "
                "The application must start without crashing."
            )

        _TEST_FAILURE_SIGNALS = ("failed", "assertionerror", "assertion failed", "fail:", "tests failed")
        if any(sig in combined for sig in _TEST_FAILURE_SIGNALS):
            return "test-failures", (
                "Runtime is stable. Fix the failing tests. "
                "Read each failure message and fix the root cause in the implementation."
            )

        # Fallback: use round number as tiebreaker
        if round_num <= 1:
            return "syntax", (
                "PRIORITY: Run syntax checks on every source file. "
                "Fix ALL syntax errors before touching anything else."
            )
        if round_num == 2:
            return "runtime", (
                "Syntax is clean. Run the application and capture the full output. "
                "Fix undefined references, missing imports, and wrong module paths."
            )
        return "functionality", (
            "Runtime is clean. Make the application do what the objective requires. "
            "Implement missing features, fix logic errors, ensure visible output."
        )
