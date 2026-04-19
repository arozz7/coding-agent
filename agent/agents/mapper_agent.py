"""Mapper agent: scout-style codebase analysis with structured output.

Produces ARCHITECTURE.md and STACK.md in the workspace root so every
subsequent developer/research agent has a reliable project map to reference.

Triggered automatically by the planner as the first step of any develop/sdlc
task against an unfamiliar project (i.e. when no ARCHITECTURE.md exists yet).
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

_IGNORE_DIRS = {
    ".git", "node_modules", "__pycache__", ".next", "dist", "build",
    ".cache", "coverage", ".agent-wiki", "logs",
}

_PROJECT_MARKERS = {
    "node":   ["package.json"],
    "python": ["requirements.txt", "pyproject.toml", "setup.py"],
    "rust":   ["Cargo.toml"],
    "go":     ["go.mod"],
    "dotnet": [".csproj", ".sln"],
}


def _detect_project_type(workspace: str) -> str:
    ws = Path(workspace)
    for kind, markers in _PROJECT_MARKERS.items():
        for m in markers:
            if m.startswith("."):
                if any(ws.glob(f"**/*{m}")):
                    return kind
            else:
                if (ws / m).exists():
                    return kind
    return "unknown"


def _list_structure(workspace: str, max_entries: int = 60) -> str:
    ws = Path(workspace)
    lines: List[str] = []
    for item in sorted(ws.rglob("*")):
        if any(part in _IGNORE_DIRS for part in item.parts):
            continue
        rel = item.relative_to(ws)
        prefix = "📁 " if item.is_dir() else "📄 "
        lines.append(f"  {prefix}{rel}")
        if len(lines) >= max_entries:
            lines.append("  … (truncated)")
            break
    return "\n".join(lines)


class MapperRole:
    name = "mapper"

    def get_system_prompt(self) -> str:
        return (
            "You are a codebase mapper agent. Analyze the project structure and "
            "produce two concise markdown documents: ARCHITECTURE.md and STACK.md.\n\n"
            "ARCHITECTURE.md must contain:\n"
            "- Overview (2–3 sentences)\n"
            "- Component list (name, purpose, location)\n"
            "- Entry points\n"
            "- Data flow summary\n"
            "- Technical debt items (TODOs, deprecated code)\n\n"
            "STACK.md must contain:\n"
            "- Runtime and language versions\n"
            "- Production and dev dependencies (from manifest files)\n"
            "- Build/run commands\n"
            "- Environment variables (from .env.example or README)\n\n"
            "Output both files using FILE: blocks:\n"
            "FILE: ARCHITECTURE.md\n"
            "```markdown\n"
            "content\n"
            "```\n\n"
            "FILE: STACK.md\n"
            "```markdown\n"
            "content\n"
            "```"
        )

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        task = context.get("task", "Map the project")
        model_router = context.get("model_router")
        tool_executor = context.get("tool_executor")
        workspace_path = context.get("workspace_path", ".")

        if not model_router:
            return {"success": False, "error": "model_router not available"}

        model = model_router.get_model("coding")
        if not model:
            return {"success": False, "error": "No coding model configured"}

        # Gather project intelligence without LLM
        project_type = _detect_project_type(workspace_path)
        structure = _list_structure(workspace_path)

        # Read key manifest files for dependency info
        manifest_content = ""
        ws = Path(workspace_path)
        for candidate in ("package.json", "Cargo.toml", "go.mod", "requirements.txt", "pyproject.toml"):
            p = ws / candidate
            if p.exists():
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")[:3000]
                    manifest_content += f"\n\n=== {candidate} ===\n{text}"
                except Exception:
                    pass

        # Read README if present
        readme = ""
        for name in ("README.md", "README.rst", "README.txt"):
            p = ws / name
            if p.exists():
                try:
                    readme = p.read_text(encoding="utf-8", errors="replace")[:2000]
                    break
                except Exception:
                    pass

        enriched = context.get("enriched_context", "")
        prompt = (
            f"{enriched}\n\n"
            f"## Mapper Task\n{task}\n\n"
            f"## Project Type\n{project_type}\n\n"
            f"## Directory Structure\n{structure}\n\n"
            f"{manifest_content}\n\n"
            f"{f'## README{chr(10)}{readme}' if readme else ''}\n\n"
            f"Produce ARCHITECTURE.md and STACK.md using FILE: blocks."
        )

        response = await model_router.generate(
            prompt, model, system_prompt=self.get_system_prompt()
        )

        files_created: List[str] = []
        if tool_executor:
            file_pattern = r'FILE:\s*(.+?)\n```\w*\n(.*?)```'
            import re
            for path_str, content in re.findall(file_pattern, response, re.DOTALL):
                fp = path_str.strip()
                try:
                    await tool_executor.execute("file_write", {"path": fp, "content": content.strip()})
                    files_created.append(fp)
                    logger.info("mapper_file_written", path=fp)
                except Exception as e:
                    logger.warning("mapper_file_write_failed", path=fp, error=str(e))

        summary = f"Mapped {project_type} project. Created: {', '.join(files_created) or 'no files'}."
        return {
            "success": True,
            "role": self.name,
            "response": response,
            "task": task,
            "files_created": files_created,
            "completion_summary": summary,
        }


class MapperAgent:
    def __init__(self, model_router, tools=None, file_system_tool=None, **kwargs):
        self.role = MapperRole()
        self.model_router = model_router

    async def run(self, task: str, context: Dict[str, Any] = None) -> Dict[str, Any]:
        if context is None:
            context = {}
        context["task"] = task
        context["model_router"] = context.get("model_router") or self.model_router
        return await self.role.execute(context)
