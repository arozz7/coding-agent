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
from pathlib import Path
from typing import Callable, Coroutine, List, Optional
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
    test_output: str = ""  # raw output from _run_tests(), passed to fix task descriptions

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "passed": self.passed,
            "gaps": self.gaps,
            "feedback": self.feedback,
            "task_type": self.task_type,
            "test_output": self.test_output,
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

        # Structural coherence: penalise documents with duplicate headings — a sign
        # that fix rounds appended content that already existed.
        dup_penalty = self._duplicate_heading_penalty(response, files_created)
        if dup_penalty > 0:
            gaps.insert(0, f"Document contains duplicate sections (structural redundancy detected)")
            score = max(0, score - dup_penalty)

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

    # Extensions that indicate a self-contained static deliverable requiring no build/test step.
    _STATIC_EXTENSIONS = frozenset({".html", ".css", ".svg", ".xml", ".json", ".md", ".txt"})

    @staticmethod
    def _is_static_deliverable(files_created: List[str], test_output: str) -> bool:
        """Return True when output is static files with no applicable test runner."""
        if not files_created:
            return False
        from pathlib import Path as _P
        all_static = all(
            _P(f).suffix.lower() in VerifierAgent._STATIC_EXTENSIONS
            for f in files_created
        )
        no_tests_ran = not test_output or test_output.startswith("(")
        return all_static and no_tests_ran

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

        if self._is_static_deliverable(files_created, test_output):
            # Static deliverable (HTML, CSS, SVG, etc.) — no test runner applies.
            # Use a 2-dimension rubric so missing tests don't distort the score.
            prompt = (
                f"Original objective:\n{objective}\n\n"
                f"Deliverable content:\n{response[:4000]}\n\n"
                f"Files created: {files_created}\n\n"
                "Evaluate on these two dimensions and return ONLY valid JSON:\n\n"
                "1. Requirement fulfilment (0-7): Does the content address EVERYTHING "
                "requested in the objective? Score 7 only if every named feature/behaviour "
                "is present. Deduct for each missing or incorrect requirement.\n"
                "2. Completeness (0-3): Is the file syntactically complete and non-truncated? "
                "Score 3 if the file has a proper closing tag/brace and no mid-sentence cuts; "
                "0 if obviously truncated.\n\n"
                'Return: {"score": <sum 0-10>, '
                '"gaps": ["<specific missing requirement or defect>", ...], '
                '"feedback": "<one concise sentence>"}\n\n'
                f"Score >= {PASS_THRESHOLD} is a PASS."
            )
        else:
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
            test_output=test_output,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _run_tests(self, tool_executor) -> str:
        if not tool_executor:
            return "(no tool executor — tests not run)"

        async def _sh(cmd: str) -> str:
            try:
                out = await tool_executor.execute("shell", {"command": cmd})
                return str(out) if out else ""
            except Exception:
                return ""

        try:
            ws = Path(getattr(tool_executor, "workspace_path", "."))
            parts: list[str] = []

            if (ws / "package.json").exists():
                parts.append(await self._run_nodejs_checks(ws, _sh))
            elif (ws / "Cargo.toml").exists():
                out = await _sh("cargo test 2>&1 | tail -30")
                parts.append(out if out and not out.startswith("Error") else "(cargo test unavailable)")
            else:
                out = await _sh("python -m pytest --tb=short -q 2>&1 | tail -20")
                parts.append(out if out and not out.startswith("Error") else "(test execution unavailable)")

            # General truncation check — language-agnostic, runs for all project types
            truncated = self._detect_truncated_files(ws)
            if truncated:
                parts.append(
                    "[truncated] FAIL: the following files appear incomplete (cut off mid-content):\n"
                    + "\n".join(f"  {f}" for f in truncated)
                    + "\nSplit large files into smaller focused modules to avoid generation cutoff."
                )

            return "\n\n".join(p for p in parts if p)
        except Exception as exc:
            self.logger.warning("verifier_test_run_failed", error=str(exc))
            return "(test execution unavailable)"

    def _detect_truncated_files(self, ws: Path) -> list[str]:
        """Return relative paths of files that appear truncated using a tail heuristic."""
        _OPEN_ENDINGS = ("{", "(", ",")
        _BRACE_LANG_EXTS = {
            ".js", ".mjs", ".cjs", ".ts", ".tsx",
            ".rs", ".go", ".c", ".cpp", ".java", ".cs", ".swift",
        }
        _CHECKED_EXTS = _BRACE_LANG_EXTS | {".py", ".html", ".json"}
        _IGNORE_DIRS = {"node_modules", ".git", "__pycache__", ".agent-wiki", "logs"}

        def _is_truncated(path: Path) -> bool:
            try:
                lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
                non_blank = [ln.rstrip() for ln in lines if ln.strip()]
                if len(non_blank) < 3:
                    return False
                last = non_blank[-1]
                suffix = path.suffix.lower()
                if suffix == ".html":
                    return "</html>" not in " ".join(non_blank).lower()
                if suffix in _BRACE_LANG_EXTS:
                    return last.endswith(_OPEN_ENDINGS)
                if suffix == ".py":
                    last_code = next(
                        (ln for ln in reversed(non_blank) if not ln.lstrip().startswith("#")),
                        "",
                    )
                    return last_code.endswith(":") and bool(
                        re.match(
                            r"\s*(def |class |if |elif |else:|for |while |with |try:|except|finally:)",
                            last_code,
                        )
                    )
                if suffix == ".json":
                    joined = "".join(non_blank).strip()
                    return not (joined.endswith("}") or joined.endswith("]"))
            except Exception:
                pass
            return False

        truncated: list[str] = []
        for path in sorted(ws.rglob("*")):
            if any(part in _IGNORE_DIRS for part in path.parts):
                continue
            if path.is_file() and path.suffix.lower() in _CHECKED_EXTS and _is_truncated(path):
                truncated.append(path.relative_to(ws).as_posix())
            if len(truncated) >= 10:
                break
        return truncated

    async def _run_nodejs_checks(
        self,
        ws: Path,
        _sh: Callable[[str], Coroutine],
    ) -> str:
        """Syntax-check all JS source files and run npm test if configured."""
        parts: list[str] = []

        # Parse package.json directly (avoids shell quoting issues)
        entry = "src/index.js"
        has_real_tests = False
        try:
            pkg = json.loads((ws / "package.json").read_text(encoding="utf-8", errors="ignore"))
            start_cmd = pkg.get("scripts", {}).get("start", "")
            m = re.search(r"node\s+(\S+\.(?:js|mjs|cjs))", start_cmd)
            if m:
                entry = m.group(1)
            elif pkg.get("main"):
                entry = str(pkg["main"])
            test_script = pkg.get("scripts", {}).get("test", "")
            has_real_tests = bool(test_script) and "no test specified" not in test_script.lower()
        except Exception:
            pass

        # Collect JS files from src/ using Python (cross-platform, no quoting issues)
        js_files: list[str] = []
        src_dir = ws / "src"
        if src_dir.is_dir():
            for f in sorted(src_dir.rglob("*")):
                if f.suffix in (".js", ".mjs", ".cjs") and "node_modules" not in f.parts:
                    js_files.append(f.relative_to(ws).as_posix())
        js_files = js_files[:15]

        # Syntax-check each file with node --check
        syntax_errors: list[str] = []
        for rel_path in js_files or [entry]:
            check = await _sh(f'node --check "{rel_path}" 2>&1')
            first_line = check.strip().splitlines()[0] if check.strip() else ""
            if first_line and ("SyntaxError" in check or "Error" in first_line):
                syntax_errors.append(f"  {rel_path}: {first_line[:160]}")

        if syntax_errors:
            parts.append("[syntax check] ERRORS:\n" + "\n".join(syntax_errors))
        elif js_files:
            parts.append(f"[syntax check] {len(js_files)} source files: OK")
        else:
            parts.append(f"[syntax check] {entry}: could not locate source files")

        # npm test — only if a real test script is configured
        if has_real_tests:
            test_out = await _sh("npm test 2>&1 | tail -30")
            parts.append(f"[npm test]\n{test_out}")

        # Web-game check — validate public/index.html completeness and serve setup
        public_html = ws / "public" / "index.html"
        if public_html.exists():
            try:
                html_content = public_html.read_text(encoding="utf-8", errors="ignore")
                if "</html>" not in html_content.lower():
                    parts.append(
                        "[web-game] FAIL: public/index.html is truncated (missing </html>) — "
                        "the file was not fully written"
                    )
                else:
                    parts.append("[web-game] public/index.html exists and is complete")
                # Check for a serve/start script that can actually host the game
                try:
                    scripts = pkg.get("scripts", {})
                    has_serve = any(
                        "serve" in v or "http-server" in v or "live-server" in v
                        for v in scripts.values()
                    )
                except Exception:
                    has_serve = False
                if not has_serve:
                    parts.append(
                        "[web-game] WARNING: no serve script in package.json — "
                        "add `\"serve\": \"npx serve public\"` so the game can be hosted and tested"
                    )
            except Exception:
                parts.append("[web-game] public/index.html exists but could not be read")

        return "\n\n".join(parts)

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

    def _duplicate_heading_penalty(self, response: str, files_created: List[str]) -> int:
        """Return a score penalty (0-2) if output files contain duplicate H2/H3 headings.

        Reads the primary output file directly so we inspect the actual written
        content, not just the agent's response excerpt.
        """
        import os
        from pathlib import Path as _Path

        texts_to_check: list[str] = []

        if files_created:
            _ws = os.getenv("WORKSPACE_PATH", "./workspace")
            _ws_base = _Path(_ws).resolve()
            for fp in files_created[:3]:
                try:
                    full = (_ws_base / fp).resolve()
                    if full.is_relative_to(_ws_base) and full.exists() and full.suffix in (".md", ".txt"):
                        texts_to_check.append(full.read_text(encoding="utf-8", errors="ignore"))
                except Exception:
                    pass

        if not texts_to_check:
            texts_to_check = [response]

        for text in texts_to_check:
            headings = [
                ln.strip().lower()
                for ln in text.splitlines()
                if re.match(r"^#{2,3}\s", ln)
            ]
            if len(headings) != len(set(headings)):
                dup_count = len(headings) - len(set(headings))
                self.logger.warning("verifier_duplicate_headings", count=dup_count)
                return 2

        return 0

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
