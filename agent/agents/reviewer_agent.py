"""Enhanced code reviewer agent using structured LLM-based review engine."""
from typing import Dict, Any, Optional
from pathlib import Path
import re
import structlog

from agent.agents.base_agent import AgentRole, BaseAgent
from agent.tools.code_review_tool import CodeReviewTool
from agent.tools.git_tool import GitTool
from agent.workspace_context import get_workspace

logger = structlog.get_logger()


class ReviewerRole(AgentRole):
    """Enhanced reviewer with structured prompts, severity levels, and multi-file support."""

    def __init__(self, code_analyzer=None, file_system_tool=None):
        super().__init__(
            name="reviewer",
            description="Reviews code for quality, security, and best practices with structured findings",
        )
        self.code_analyzer = code_analyzer
        self.file_system_tool = file_system_tool

    def get_system_prompt(self) -> str:
        return """You are an expert code reviewer assistant. You help users
with auditing tasks by reading code, analyzing logic, and reporting issues.

Available tools:
- read: Extensively examine files and documentation

Guidelines:
- Identify potential bugs, security issues, and code smells
- Ensure adherence to best practices and check test coverage
- Suggest actionable improvements
- Be concise in your responses"""

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        task = context.get("task", "")
        model_router = context.get("model_router")
        enriched_context = context.get("enriched_context", "")

        if not model_router:
            return {"success": False, "error": "model_router not available"}

        review_tool = CodeReviewTool(model_router)
        ws = get_workspace()

        # Detect review mode from task
        mode = self._detect_mode(task)

        if mode == "diff":
            return await self._review_diff(task, review_tool, ws)
        elif mode == "file":
            return await self._review_file(task, review_tool, enriched_context)
        elif mode == "directory":
            return await self._review_directory(task, review_tool, ws)
        elif mode == "inline":
            return await self._review_inline(task, review_tool, enriched_context)
        else:
            return await self._review_from_context(task, review_tool, context, enriched_context)

    def _detect_mode(self, task: str) -> str:
        """Detect review mode from task description."""
        t = task.lower().strip()

        # Diff mode
        if any(kw in t for kw in ["review the diff", "review diff", "review changes",
                                   "review commit", "review this diff", "diff review"]):
            return "diff"

        # Directory mode
        if any(kw in t for kw in ["review the directory", "review all files",
                                   "review this folder", "review the project",
                                   "review all", "review directory"]):
            return "directory"

        # File mode — looks like a path
        path_patterns = [
            r"(?:review|audit|check)\s+(?:this\s+)?(?:file\s+)?[\w/\-_.]+\.(?:py|js|ts|go|rs|java|cpp|c|h|rb|php|cs|sh|sql)",
            r"(?:review|audit|check)\s+[\w/\-_.]+/",
        ]
        for pattern in path_patterns:
            if re.search(pattern, t):
                return "file"

        # Inline code mode — contains code fences
        if "```" in task:
            return "inline"

        # Default: try to extract from context
        return "context"

    async def _review_diff(self, task: str, review_tool: CodeReviewTool, ws: str) -> Dict[str, Any]:
        """Review current git diff."""
        git_tool = GitTool(ws)
        try:
            diff_result = git_tool.diff()
            if not diff_result.get("success"):
                return {
                    "success": False,
                    "error": diff_result.get("error", "Git diff failed"),
                    "response": f"❌ Could not get git diff: {diff_result.get('error')}",
                }
            diff = diff_result.get("output", "")
        except Exception as e:
            return {
                "success": False,
                "error": f"Failed to get git diff: {e}",
                "response": f"❌ Could not get git diff: {e}",
            }

        if not diff.strip():
            return {
                "success": True,
                "role": self.name,
                "response": "ℹ️ No changes detected in the working directory.",
                "review": {"overall_score": 10, "summary": "No changes to review.", "findings": []},
            }

        # Try to extract file path from task
        file_path = self._extract_file_path(task)

        result = await review_tool.review_diff(diff, file_path=file_path, fmt="markdown")

        return {
            "success": result.get("success", False),
            "role": self.name,
            "response": result.get("output", ""),
            "review": result.get("review"),
            "task": task,
        }

    async def _review_file(
        self, task: str, review_tool: CodeReviewTool, enriched_context: str
    ) -> Dict[str, Any]:
        """Review a specific file."""
        file_path = self._extract_file_path(task)
        if not file_path:
            file_path = task.strip()

        result = await review_tool.review_file(file_path, context=enriched_context, fmt="markdown")

        return {
            "success": result.get("success", False),
            "role": self.name,
            "response": result.get("output", ""),
            "review": result.get("review"),
            "task": task,
        }

    async def _review_directory(
        self, task: str, review_tool: CodeReviewTool, ws: str
    ) -> Dict[str, Any]:
        """Review all files in a directory."""
        # Try to extract directory path
        dir_match = re.search(
            r"(?:review|audit|check)\s+(?:the\s+)?(?:directory|folder|project|all\s+files\s+in)\s+([\w/\-_.]+)",
            task.lower(),
        )
        dir_path = dir_match.group(1) if dir_match else "."

        # Detect pattern from task
        lang_match = re.search(r"(?:python|javascript|typescript|go|rust|java|cpp)\s+files?", task.lower())
        if lang_match:
            lang = lang_match.group(1).lower()
            ext_map = {
                "python": "**/*.py",
                "javascript": "**/*.{js,jsx}",
                "typescript": "**/*.{ts,tsx}",
                "go": "**/*.go",
                "rust": "**/*.rs",
                "java": "**/*.java",
                "cpp": "**/*.{cpp,cpp,h,hpp}",
            }
            pattern = ext_map.get(lang, "**/*.py")
        else:
            pattern = "**/*.py"

        result = await review_tool.review_directory(dir_path, pattern=pattern, fmt="markdown")

        return {
            "success": result.get("success", False),
            "role": self.name,
            "response": result.get("output", ""),
            "review": result.get("review"),
            "task": task,
        }

    async def _review_inline(
        self, task: str, review_tool: CodeReviewTool, enriched_context: str
    ) -> Dict[str, Any]:
        """Review code pasted inline in the task."""
        # Extract code from markdown fences
        code_match = re.search(r"```(?:\w*)\n?(.*?)```", task, re.DOTALL)
        if code_match:
            code = code_match.group(1)
        else:
            code = task

        # Try to detect language from fence
        lang_match = re.search(r"```(\w+)", task)
        language = lang_match.group(1) if lang_match else None

        # Context before/after the code block
        context_parts = []
        if code_match:
            before = task[:code_match.start()]
            after = task[code_match.end():]
            for part in [before, after]:
                part = part.strip().replace("```", "").strip()
                if part:
                    context_parts.append(part)
        context = "\n".join(context_parts) or enriched_context

        result = await review_tool.review_code(
            code, language=language, context=context, fmt="markdown"
        )

        return {
            "success": result.get("success", False),
            "role": self.name,
            "response": result.get("output", ""),
            "review": result.get("review"),
            "task": task,
        }

    async def _review_from_context(
        self, task: str, review_tool: CodeReviewTool, context: Dict[str, Any], enriched_context: str
    ) -> Dict[str, Any]:
        """Review code provided in context (legacy support)."""
        code = context.get("code", "")
        file_path = context.get("file_path", "")

        if not code and file_path:
            # Read file from workspace
            ws = get_workspace()
            full_path = Path(ws) / file_path
            if full_path.exists():
                code = full_path.read_text(encoding="utf-8", errors="replace")
            else:
                return {
                    "success": False,
                    "error": f"File not found: {file_path}",
                    "response": f"❌ File not found: {file_path}",
                }

        if not code:
            return {
                "success": False,
                "error": "No code provided to review",
                "response": "❌ No code provided to review. Please specify a file, diff, or paste code in markdown fences.",
            }

        result = await review_tool.review_code(
            code, file_path=file_path, context=enriched_context, fmt="markdown"
        )

        return {
            "success": result.get("success", False),
            "role": self.name,
            "response": result.get("output", ""),
            "review": result.get("review"),
            "task": task,
        }

    def _extract_file_path(self, task: str) -> Optional[str]:
        """Extract a file path from a task description."""
        # Match patterns like "review src/main.py" or "review file src/main.py"
        match = re.search(
            r"(?:review|audit|check)\s+(?:this\s+)?(?:file\s+)?([\w/\-_.]+\.(?:py|js|ts|jsx|tsx|go|rs|java|cpp|c|h|hpp|rb|php|cs|sh|sql|yaml|yml|json|html|css|scss))",
            task,
            re.IGNORECASE,
        )
        return match.group(1) if match else None


class ReviewerAgent:
    def __init__(self, model_router, tools=None, code_analyzer=None, file_system_tool=None):
        role = ReviewerRole(code_analyzer, file_system_tool)
        self.base = BaseAgent(role, model_router, tools)

    async def run(self, task: str, context: Dict[str, Any] = None):
        if context is None:
            context = {}
        return await self.base.run(task, context)
