"""Verifier coordinator — runs verification rubrics and builds fix-task specs.

Owns:
  - VerifierCoordinator.run_verification()   async, returns VerifierResult
  - VerifierCoordinator.make_fix_specs()     returns list of task spec dicts
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Coroutine, List, Optional

import structlog

from agent.orchestration.context_builder import char_budget
from agent.orchestration.criterion_evaluator import CriterionEvaluator, CriterionResult

if TYPE_CHECKING:
    from agent.agents.acceptance_tester_agent import AcceptanceTesterAgent, AcceptanceResult
    from agent.agents.verifier_agent import VerifierAgent, VerifierResult
    from agent.orchestration.app_probe import AppProbe
    from llm import ModelRouter

logger = structlog.get_logger()


# Structured manifest files where APPEND: would corrupt the file's syntax
# (JSON/TOML don't tolerate trailing raw text) and where "the substring is
# present" doesn't mean "the dependency actually works" — it needs to be
# installed through the package manager, not typed into the file.
_MANIFEST_INSTALL_CMDS: dict[str, tuple[str, str]] = {
    "package.json": ("npm", "npm install {pkg}"),
    "cargo.toml":   ("cargo", "cargo add {pkg}"),
    "pyproject.toml": ("pip", "pip install {pkg}"),
    "requirements.txt": ("pip", "pip install {pkg}"),
}


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
        # Set by run_acceptance_tests(); read by orchestrator to populate the job record.
        self.last_screenshot_path: Optional[str] = None
        self._criterion_evaluator = CriterionEvaluator(model_router)

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

        Everything else falls back to an LLM pass/fail call. Delegates to
        CriterionEvaluator — see agent/orchestration/criterion_evaluator.py.
        """
        return await self._criterion_evaluator.evaluate_criteria(
            criteria, ws, shell_fn, combined_response
        )

    async def run_acceptance_tests(
        self,
        criteria: List[str],
        workspace: Path,
        app_probe: "AppProbe",
        acceptance_tester: "AcceptanceTesterAgent",
        agent_output: str = "",
    ) -> List["AcceptanceResult"]:
        """Launch the app, screenshot it, evaluate acceptance criteria, teardown.

        Returns one AcceptanceResult per criterion.  On any launch failure all
        criteria are returned as failed so the fix loop gets signal.
        """

        if not criteria:
            return []

        handle = await app_probe.launch()
        screenshot_path: Optional[str] = None

        if handle is None:
            # detect_start_command() already tries a generic static-file-server
            # fallback for any workspace with HTML to serve (app_probe.py), so
            # reaching here with HTML files present means that fallback itself
            # failed to launch (port conflict, spawn error, readiness timeout) —
            # not merely "no entry point found". Either way, still try a direct
            # file:// screenshot and actually evaluate it, rather than silently
            # skipping acceptance testing for the whole workspace.
            html_files = sorted(
                workspace.glob("*.html"),
                key=lambda p: p.stat().st_size,
                reverse=True,
            )
            if not html_files:
                self.logger.info("acceptance_skipped_no_entry_point", workspace=str(workspace))
                return []

            screenshot_path = await app_probe.screenshot_file(html_files[0])
            self.last_screenshot_path = screenshot_path
            if screenshot_path is None:
                self.logger.info(
                    "acceptance_skipped_static_screenshot_failed", workspace=str(workspace)
                )
                return []

            self.logger.info("acceptance_static_fallback", workspace=str(workspace))
            return await acceptance_tester.run_tests(
                criteria, workspace, screenshot_path=screenshot_path, agent_output=agent_output
            )

        try:
            screenshot_path = await app_probe.screenshot(handle)
            self.last_screenshot_path = screenshot_path
            results = await acceptance_tester.run_tests(
                criteria, workspace, screenshot_path=screenshot_path, agent_output=agent_output
            )
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
        lower_c = failing.criterion.lower()

        # Structural criteria have obvious, deterministic fixes — bypass phase detection.
        if lower_c.startswith("file contains:"):
            payload = failing.criterion[len("file contains:"):].strip()
            if ":" in payload:
                rel, substring = payload.split(":", 1)
                rel, substring = rel.strip(), substring.strip()
                detail_lower = failing.detail.lower()
                manifest_cmd = _MANIFEST_INSTALL_CMDS.get(Path(rel).name.lower())
                if manifest_cmd and not (
                    detail_lower.startswith("read error") or detail_lower.startswith("missing:")
                ):
                    # The file exists but lacks this substring — for a manifest,
                    # that means a dependency is missing. Adding raw text via
                    # APPEND: corrupts JSON/TOML syntax and wouldn't actually
                    # install anything; the package manager is the only correct fix.
                    _tool, _cmd_tpl = manifest_cmd
                    pkg = substring.strip('"\'')
                    cmd = _cmd_tpl.format(pkg=pkg)
                    instruction = (
                        f"Run `{cmd}` to add the dependency through the package manager. "
                        f"Do NOT hand-edit `{rel}` or use an APPEND: block — manually inserted "
                        f"text corrupts the file's syntax and does not install the package."
                    )
                elif detail_lower.startswith("read error") or detail_lower.startswith("missing:"):
                    instruction = (
                        f"Create the file `{rel}` and ensure it contains the text `{substring}`. "
                        f"Use a FILE: block with appropriate content."
                    )
                elif substring.strip() == "[x]":
                    # Checkbox-completion criterion — appending [x] is wrong; checkboxes must be
                    # edited in-place.  Using FILE: risks destroying co-located content (e.g. a
                    # "Next Steps" section), causing oscillation with other criteria.
                    instruction = (
                        f"Read `{rel}` first. Use REPLACE: or EDIT: blocks to mark all incomplete "
                        f"task checkboxes as done by changing `- [ ]` to `- [x]`. "
                        f"Do NOT use FILE: — it will overwrite sections needed by other criteria."
                    )
                else:
                    instruction = (
                        f"Read `{rel}` first, then add the text `{substring}` to the file using an "
                        f"APPEND: block. Do NOT rewrite the file with FILE: — only append the missing content."
                    )
                phase = "file-content"
            else:
                phase, instruction = self._detect_fix_phase(test_out, failing.detail, round_num)

        elif lower_c.startswith("file exists:"):
            rel = failing.criterion[len("file exists:"):].strip()
            if "*" in rel or "?" in rel:
                # Give the agent a concrete filename — seeing a glob wildcard as a filename is confusing.
                example = re.sub(r"[*?]+", "COMPLETE", rel)
                instruction = (
                    f"Create a file matching the pattern `{rel}` at the project root. "
                    f"Use a concrete name such as `{example}` and fill it with appropriate content "
                    f"(a summary of what was built, next steps, etc.). "
                    f"Place the file directly in the workspace root, not in a subdirectory."
                )
            else:
                instruction = f"Create the missing file `{rel}` at the project root with appropriate content."
            phase = "file-missing"

        elif lower_c.startswith("command exits 0:"):
            cmd = failing.criterion[len("command exits 0:"):].strip()
            for _prose_sep in (" | end state:", " | constraint:", " |end state:", " |constraint:"):
                _idx = cmd.lower().find(_prose_sep)
                if _idx != -1:
                    cmd = cmd[:_idx].strip()
                    break
            instruction = (
                f"Run `{cmd}` and read its complete output. "
                f"Fix ALL errors that prevent the command from exiting 0. "
                f"Re-run `{cmd}` after each change to confirm progress."
            )
            phase = "command-fix"

        else:
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
            "phase": phase,
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
