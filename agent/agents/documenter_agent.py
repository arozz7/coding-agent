"""Documenter agent: writes READMEs, inline docs, and changelogs.

Modelled after the pi-vs-claude-code documenter agent — generates clear,
concise documentation that matches the project's existing style.
"""

from typing import Any, Dict, List

import structlog

logger = structlog.get_logger()


class DocumenterRole:
    name = "documenter"

    def get_system_prompt(self) -> str:
        return (
            "You are a documentation agent. Write clear, concise documentation.\n\n"
            "Guidelines:\n"
            "- Match the project's existing documentation style and tone\n"
            "- Update READMEs with setup, usage, and API reference sections\n"
            "- Add inline comments only where the WHY is non-obvious\n"
            "- Write usage examples that actually run\n"
            "- Keep changelogs in Keep-a-Changelog format\n"
            "- Never duplicate what the code already says — document intent, not implementation\n\n"
            "Write files using FILE: blocks:\n"
            "FILE: path/to/file.md\n"
            "```markdown\n"
            "content\n"
            "```"
        )

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        task = context.get("task", "Write documentation")
        model_router = context.get("model_router")
        tool_executor = context.get("tool_executor")

        if not model_router:
            return {"success": False, "error": "model_router not available"}

        model = model_router.get_model("coding")
        if not model:
            return {"success": False, "error": "No coding model configured"}

        enriched = context.get("enriched_context", "")
        prompt = (
            f"{enriched}\n\n"
            f"## Documentation Task\n{task}\n\n"
            f"Read the relevant source files from the workspace listing above, "
            f"then write the requested documentation using FILE: blocks."
        )

        response = await model_router.generate(
            prompt, model, system_prompt=self.get_system_prompt()
        )

        files_created: List[str] = []
        if tool_executor:
            import re
            for path_str, content in re.findall(
                r'FILE:\s*(.+?)\n```\w*\n(.*?)```', response, re.DOTALL
            ):
                fp = path_str.strip()
                try:
                    await tool_executor.execute("file_write", {"path": fp, "content": content.strip()})
                    files_created.append(fp)
                    logger.info("documenter_file_written", path=fp)
                except Exception as e:
                    logger.warning("documenter_file_write_failed", path=fp, error=str(e))

        summary = f"Documentation written: {', '.join(files_created) or 'inline updates'}."
        return {
            "success": True,
            "role": self.name,
            "response": response,
            "task": task,
            "files_created": files_created,
            "completion_summary": summary,
        }


class DocumenterAgent:
    def __init__(self, model_router, tools=None, **kwargs):
        self.role = DocumenterRole()
        self.model_router = model_router

    async def run(self, task: str, context: Dict[str, Any] = None) -> Dict[str, Any]:
        if context is None:
            context = {}
        context["task"] = task
        context["model_router"] = context.get("model_router") or self.model_router
        return await self.role.execute(context)
