"""RequirementsExtractor — derives behavioral acceptance criteria from workspace docs.

Reads candidate documentation files from the workspace and asks the LLM to
produce 3–7 concrete, testable acceptance criteria for a running application.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, List

import structlog

if TYPE_CHECKING:
    from llm import ModelRouter

logger = structlog.get_logger()

# Candidate docs to read, in priority order
_CANDIDATE_DOCS = [
    "ARCHITECTURE.md",
    "STACK.md",
    "GAME_DESIGN.md",
    "README.md",
]

_JSON_ARRAY_RE = re.compile(r'\[[\s\S]*?\]', re.DOTALL)

_MAX_DOC_CHARS = 1500
_MAX_CRITERIA = 7


class RequirementsExtractor:
    """Derives behavioral acceptance criteria from workspace documentation."""

    def __init__(self, model_router: "ModelRouter") -> None:
        self.model_router = model_router
        self.logger = logger.bind(component="requirements_extractor")

    async def extract(self, objective: str, workspace: Path) -> List[str]:
        """Return up to 7 behavioral acceptance criteria for the running app.

        Reads workspace docs for context; falls back to [] on any failure.
        """
        model = self.model_router.get_model("coding")
        if not model:
            return []

        doc_context = self._read_docs(workspace)

        system = (
            "You are a strict QA engineer. Given an objective and optional project docs, "
            "produce 3–7 acceptance criteria that a separate evaluator model can judge as "
            "pass or fail from the agent's output, terminal logs, or a screenshot alone.\n\n"
            "STRUCTURE — every criterion must have three parts:\n"
            "  1. One measurable end state (what is true when done)\n"
            "  2. A stated check (how to prove it — the exact command, file path, or visible evidence)\n"
            "  3. A constraint if relevant (what must NOT change on the way there)\n\n"
            "FORMATS — pick the most specific one that applies:\n"
            '  "command exits 0: <exact shell command>" — for any CLI-verifiable outcome\n'
            '  "file exists: <relative/path>" — ONLY when the objective explicitly names that file\n'
            '  "file contains: <relative/path>:<substring>" — use a colon (:) separator; '
            "for generated source/config file content checks\n"
            '  "visual: <precise description of what must be visible in a screenshot or DOM>" '
            "— for UI checks; be specific enough that a model can give yes/no from one image\n\n"
            "RULES:\n"
            "  - Do NOT write vague criteria like 'the app works' or 'no errors'.\n"
            "  - Do NOT generate 'file exists' or 'file contains' from naming conventions.\n"
            "  - Do NOT generate criteria that check planning/documentation/changelog files — "
            "including PROJECT_PLAN.md, README.md, CHANGELOG.md, ARCHITECTURE.md, STACK.md, "
            "PHASE*.md, ROADMAP.md, or any file that tracks project state rather than "
            "application behavior. Those are artifacts, not acceptance criteria.\n"
            "  - Each criterion must be falsifiable: there must be a concrete observation that "
            "would make it fail.\n"
            "  - If the objective has no server/process, omit 'command exits 0' criteria that "
            "require a running process.\n\n"
            "EXAMPLES of good criteria:\n"
            '  "command exits 0: python -m pytest tests/ -q"\n'
            '  "visual: canvas element is present and shows a car shape moving rightward across '
            'the frame; background hills scroll leftward"\n'
            '  "file contains: src/index.html:<canvas id=\\"gameCanvas\\">"\n\n'
            "Return ONLY a JSON array of strings. No prose, no markdown fences."
        )
        prompt = (
            f"Objective: {objective}\n\n"
            f"{doc_context}\n\n"
            "Generate 3–7 acceptance criteria following the structure above."
        )

        try:
            raw = await self.model_router.generate(prompt, model, system_prompt=system, enable_thinking=False)
            criteria = self._parse(raw)
            self.logger.info("requirements_extracted", count=len(criteria), objective=objective[:60])
            return criteria
        except Exception as exc:
            self.logger.warning("requirements_extraction_failed", error=str(exc))
            return []

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _read_docs(self, workspace: Path) -> str:
        """Read candidate docs, returning a combined context string."""
        sections: list[str] = []
        for name in _CANDIDATE_DOCS:
            path = workspace / name
            try:
                if path.exists():
                    content = path.read_text(encoding="utf-8", errors="ignore")
                    sections.append(f"### {name}\n{content[:_MAX_DOC_CHARS]}")
            except Exception:
                pass
        if not sections:
            return ""
        return "Project documentation:\n\n" + "\n\n".join(sections)

    def _parse(self, raw: str) -> List[str]:
        """Extract a JSON array from the LLM response."""
        if not raw:
            return []
        matches = _JSON_ARRAY_RE.findall(raw)
        if not matches:
            return []
        candidate = max(matches, key=len)
        try:
            items = json.loads(candidate)
        except json.JSONDecodeError:
            return []
        if not isinstance(items, list):
            return []
        cleaned = [str(c).strip() for c in items if str(c).strip()]
        return cleaned[:_MAX_CRITERIA]
