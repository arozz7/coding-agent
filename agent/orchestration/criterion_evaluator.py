"""Criterion evaluation — auto-checks (file/command) + LLM-judged fallback.

Extracted from VerifierCoordinator: this cluster only depends on
model_router (for the LLM-fallback path used by behavioral/visual
criteria) and a logger, unlike run_verification/run_acceptance_tests/
make_fix_specs which need the verifier_agent and richer coordinator state.
"""
from __future__ import annotations

import json
import platform
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Coroutine

import structlog

if TYPE_CHECKING:
    from llm import ModelRouter

logger = structlog.get_logger()


@dataclass
class CriterionResult:
    """Result of evaluating a single completion criterion."""

    criterion: str
    passed: bool
    detail: str = ""  # what was checked or why it failed


# Directories that should never be searched for deliverable files.
_EXCLUDE_DIRS = frozenset({
    "node_modules", ".git", ".venv", "venv", "__pycache__",
    "vendor", "target", "dist", "build", ".next", ".nuxt",
})

# "find <dir> -type f -name '<pattern>'" is the shape an LLM reaches for when
# it means "does a file matching this pattern exist" — but as a literal shell
# command it is Unix-only (cmd.exe's built-in `find` searches file *content*,
# not paths) and is redundant even on Unix, since the codebase already has a
# pure-Python, OS-agnostic equivalent: the "file exists: <glob>" auto-check
# (Path.glob, no shell involved). Rewriting at generation time means this
# never depends on the model's prompt-following, matching the same reasoning
# as PlannerAgent's hard task-count cap — enforce portability programmatically.
_FIND_TYPE_F_NAME_RE = re.compile(
    r"^find\s+(?P<dir>\S+?)/?\s+-type\s+f\s+-name\s+['\"]?(?P<pattern>[^\s'\"]+)['\"]?",
    re.IGNORECASE,
)

# Bare Unix text/file utilities with no reliable Windows equivalent for the
# way an LLM typically invokes them (piped, with Unix-only flags). A
# criterion built around one of these can never be trusted to behave the
# same on both platforms, so it's dropped rather than kept and left to fail
# unconditionally on whichever OS the agent happens to run on.
_UNPORTABLE_COMMAND_VERBS = frozenset({"grep", "ls", "cat", "wc", "sed", "awk", "xargs", "head", "tail", "find"})


def normalize_criterion(criterion: str) -> "str | None":
    """Rewrite or drop a 'command exits 0' criterion that can't run reliably
    as a shell command on both Windows and Linux.

    Returns the rewritten criterion, the original unchanged (nothing
    applies), or None (the criterion should be dropped entirely).
    """
    lower = criterion.lower()
    if not lower.startswith("command exits 0:"):
        return criterion

    cmd = criterion[len("command exits 0:"):].strip()
    m = _FIND_TYPE_F_NAME_RE.match(cmd)
    if m:
        rel_dir = m.group("dir").strip("/")
        rel_dir = rel_dir if rel_dir else "."
        return f"file exists: {rel_dir}/**/{m.group('pattern')}"

    first_verb = cmd.split()[0].lower() if cmd.split() else ""
    if first_verb in _UNPORTABLE_COMMAND_VERBS:
        return None

    return criterion


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


class CriterionEvaluator:
    """Evaluates completion/acceptance criteria: auto-checks + LLM fallback."""

    def __init__(self, model_router: "ModelRouter"):
        self.model_router = model_router
        self.logger = logger.bind(component="criterion_evaluator")

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
            # Strip prose suffixes the LLM sometimes appends after the shell command,
            # e.g. "| End state: ..." or "| Constraint: ...". These are not shell syntax
            # and would cause the shell to pipe to a nonexistent program.
            for _prose_sep in (" | end state:", " | constraint:", " |end state:", " |constraint:"):
                _idx = cmd.lower().find(_prose_sep)
                if _idx != -1:
                    cmd = cmd[:_idx].strip()
                    break
            # Guard against long-running server commands
            _BLOCKING = ("npm start", "flask run", "uvicorn", "gunicorn", "python -m http.server", "serve")
            if any(b in cmd.lower() for b in _BLOCKING):
                return CriterionResult(criterion=criterion, passed=True, detail="(server command skipped)")
            try:
                _sep = " & echo __EXIT__%ERRORLEVEL%" if platform.system() == "Windows" else "; echo __EXIT__$?"
                out = await shell_fn(f"{cmd}{_sep}")
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
                if not matches and not rel.startswith("**/"):
                    # Root-level glob found nothing; try recursive search.
                    matches = _glob_filtered(ws, f"**/{rel.lstrip('/')}")
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
