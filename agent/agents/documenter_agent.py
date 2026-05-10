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

        enriched_full = context.get("enriched_context", "")
        # Reserve headroom for the model to generate a full document:
        # use 60% of context window for input (chars = tokens * 4), min 20k.
        _output_reserve_chars = max(8_000, model.context_window * 4 // 5)
        _max_input_chars = max(20_000, model.context_window * 4 - _output_reserve_chars - 2_000)
        # Research content is appended after the standard context under a known marker.
        # Cap the standard preamble tightly; cap the research section to leave room for generation.
        _RESEARCH_MARKER = "## Research findings to synthesize"
        if _RESEARCH_MARKER in enriched_full:
            split_idx = enriched_full.index(_RESEARCH_MARKER)
            preamble = enriched_full[:split_idx][:800]
            research = enriched_full[split_idx:]
            research_budget = _max_input_chars - len(preamble) - 200
            if len(research) > research_budget:
                research = research[:research_budget] + "\n\n[Research content trimmed to fit context window]"
            enriched = preamble + "\n\n" + research
        else:
            enriched = enriched_full[:_max_input_chars]
        prompt = (
            f"{enriched}\n\n"
            f"## Documentation Task\n{task}\n\n"
            f"Using the research findings above, write comprehensive documentation "
            f"with FILE: blocks. Include specific facts, names, comparisons, and "
            f"recommendations from the research — do not summarize or compress."
        )

        response = await model_router.generate(
            prompt, model, system_prompt=self.get_system_prompt(), enable_thinking=False
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
