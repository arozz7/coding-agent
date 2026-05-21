"""Core code review engine — collects code, calls LLM, parses structured findings."""
from __future__ import annotations

from typing import Optional

import structlog

from .code_chunker import get_language_from_extension
from .review_prompts import (
    get_system_prompt,
    get_user_prompt,
    get_diff_review_prompt,
    parse_review_response,
    normalize_review,
)
from .review_output import render

logger = structlog.get_logger()


class CodeReviewTool:
    """Structured code review tool that uses LLM to analyze code.

    Usage:
        tool = CodeReviewTool(model_router)
        result = await tool.review_file("src/main.py")
        result = await tool.review_diff(diff_text, file_path="src/main.py")
        result = await tool.review_code(code, language="python")
    """

    def __init__(self, model_router):
        self.model_router = model_router
        self.logger = logger.bind(component="code_review_tool")

    async def review_file(
        self,
        file_path: str,
        context: Optional[str] = None,
        fmt: str = "terminal",
    ) -> dict:
        """Review a single file by path.

        Args:
            file_path: Path to the file (relative to workspace)
            context: Optional additional context for the review
            fmt: Output format ('terminal', 'markdown', 'json')

        Returns:
            Dict with 'success', 'review' (normalized), and 'output' (rendered string)
        """
        from .file_system_tool import FileSystemTool
        from agent.workspace_context import get_workspace
        from pathlib import Path

        ws = get_workspace()
        full_path = Path(ws) / file_path

        if not full_path.exists():
            return {
                "success": False,
                "error": f"File not found: {file_path}",
                "output": f"❌ File not found: {file_path}",
            }

        content = full_path.read_text(encoding="utf-8", errors="replace")
        language = get_language_from_extension(file_path)

        return await self._review(
            code=content,
            language=language,
            file_path=file_path,
            context=context,
            fmt=fmt,
        )

    async def review_diff(
        self,
        diff: str,
        file_path: Optional[str] = None,
        fmt: str = "terminal",
    ) -> dict:
        """Review a git diff.

        Args:
            diff: Git diff text
            file_path: Optional file path for language detection
            fmt: Output format ('terminal', 'markdown', 'json')

        Returns:
            Dict with 'success', 'review' (normalized), and 'output' (rendered string)
        """
        system, user = get_diff_review_prompt(diff, file_path)

        model = self.model_router.get_model("coding")
        if not model:
            return {
                "success": False,
                "error": "No coding model configured",
                "output": "❌ No coding model configured",
            }

        try:
            raw = await self.model_router.generate(
                user, model, system_prompt=system
            )
        except Exception as e:
            self.logger.error("review_llm_error", error=str(e))
            return {
                "success": False,
                "error": f"LLM error: {e}",
                "output": f"❌ Review failed: {e}",
            }

        parsed = parse_review_response(raw)
        if parsed:
            review = normalize_review(parsed)
        else:
            # Fallback: treat raw response as the summary
            review = {
                "overall_score": 5,
                "summary": raw[:500],
                "findings": [],
            }

        output = render(review, fmt=fmt, file_path=file_path)

        return {
            "success": True,
            "review": review,
            "output": output,
        }

    async def review_code(
        self,
        code: str,
        language: Optional[str] = None,
        file_path: Optional[str] = None,
        context: Optional[str] = None,
        fmt: str = "terminal",
    ) -> dict:
        """Review arbitrary code string.

        Args:
            code: Code content to review
            language: Language name (auto-detected from file_path if not given)
            file_path: Optional file path for display
            context: Optional additional context
            fmt: Output format ('terminal', 'markdown', 'json')

        Returns:
            Dict with 'success', 'review' (normalized), and 'output' (rendered string)
        """
        if not language and file_path:
            language = get_language_from_extension(file_path)

        return await self._review(
            code=code,
            language=language,
            file_path=file_path,
            context=context,
            fmt=fmt,
        )

    async def review_directory(
        self,
        dir_path: str,
        pattern: str = "**/*.py",
        fmt: str = "terminal",
    ) -> dict:
        """Review all matching files in a directory.

        Args:
            dir_path: Directory path
            pattern: Glob pattern for files to review
            fmt: Output format

        Returns:
            Dict with combined review results
        """
        from pathlib import Path
        from agent.workspace_context import get_workspace

        ws = get_workspace()
        full_dir = Path(ws) / dir_path

        if not full_dir.is_dir():
            return {
                "success": False,
                "error": f"Directory not found: {dir_path}",
                "output": f"❌ Directory not found: {dir_path}",
            }

        files = sorted(full_dir.glob(pattern))
        if not files:
            return {
                "success": True,
                "review": {"overall_score": 5, "summary": "No files matched.", "findings": []},
                "output": f"ℹ️ No files matching '{pattern}' in {dir_path}",
            }

        all_findings: list[dict] = []
        all_outputs: list[str] = []
        scores: list[int] = []

        for f in files:
            rel_path = str(f.relative_to(full_dir))
            self.logger.info("reviewing_file", file=rel_path)

            result = await self.review_file(rel_path, fmt=fmt)
            if result.get("success"):
                review = result.get("review", {})
                all_findings.extend(review.get("findings", []))
                scores.append(review.get("overall_score", 5))
                all_outputs.append(result.get("output", ""))
            else:
                all_outputs.append(result.get("output", f"❌ Error reviewing {rel_path}"))

        avg_score = round(sum(scores) / len(scores)) if scores else 5
        combined_review = {
            "overall_score": avg_score,
            "summary": f"Reviewed {len(files)} files. Average score: {avg_score}/10.",
            "findings": sorted(all_findings, key=lambda f: {
                "critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4
            }.get(f.get("severity", "low"), 5)),
        }

        combined_output = render(combined_review, fmt=fmt)
        if fmt == "terminal" or fmt == "markdown":
            combined_output += "\n\n---\n\n" + "\n---\n".join(all_outputs)

        return {
            "success": True,
            "review": combined_review,
            "output": combined_output,
        }

    async def _review(
        self,
        code: str,
        language: Optional[str],
        file_path: Optional[str],
        context: Optional[str],
        fmt: str,
    ) -> dict:
        """Internal review method that calls the LLM."""
        system = get_system_prompt(language)
        user = get_user_prompt(code, file_path, context)

        model = self.model_router.get_model("coding")
        if not model:
            return {
                "success": False,
                "error": "No coding model configured",
                "output": "❌ No coding model configured",
            }

        self.logger.info(
            "review_started",
            file=file_path,
            language=language,
            code_lines=code.count("\n") + 1,
        )

        try:
            raw = await self.model_router.generate(
                user, model, system_prompt=system
            )
        except Exception as e:
            self.logger.error("review_llm_error", error=str(e))
            return {
                "success": False,
                "error": f"LLM error: {e}",
                "output": f"❌ Review failed: {e}",
            }

        parsed = parse_review_response(raw)
        if parsed:
            review = normalize_review(parsed)
        else:
            # Fallback: treat raw response as the summary
            self.logger.warning("review_json_parse_failed", file=file_path)
            review = {
                "overall_score": 5,
                "summary": raw[:500],
                "findings": [],
            }

        output = render(review, fmt=fmt, file_path=file_path)

        self.logger.info(
            "review_completed",
            file=file_path,
            score=review["overall_score"],
            findings=len(review["findings"]),
        )

        return {
            "success": True,
            "review": review,
            "output": output,
        }
