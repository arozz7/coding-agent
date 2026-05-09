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
            "produce 3–7 concrete behavioral acceptance criteria for the RUNNING application. "
            "Criteria must be observable from a screenshot or terminal output — not just "
            "'code compiles'. Prefer:\n"
            '  "command exits 0: <shell command>" for CLI checks\n'
            '  "file exists: <relative path>" for artifact checks\n'
            "  plain English for visual/behavioral checks\n\n"
            "Return ONLY a JSON array of strings. No prose, no markdown fences."
        )
        prompt = (
            f"Objective: {objective}\n\n"
            f"{doc_context}\n\n"
            "Generate 3–7 behavioral acceptance criteria for the running app."
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
