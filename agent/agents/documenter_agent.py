"""Documenter agent: writes READMEs, inline docs, and changelogs.

Modelled after the pi-vs-claude-code documenter agent — generates clear,
concise documentation that matches the project's existing style.
"""

import re
from typing import Any, Dict, List

import structlog

logger = structlog.get_logger()


def _extract_file_blocks(response: str) -> list[tuple[str, str]]:
    """Extract (path, content) pairs from FILE: blocks, handling nested code fences.

    The naive regex stops at the first ``` it sees, which breaks when the content
    itself contains code fences (e.g. mermaid diagrams inside a markdown file).
    This parser splits on FILE: boundaries and takes content up to the LAST ```
    within each segment, which is the true outer closing fence.
    """
    results = []
    parts = re.split(r'(?m)^(?=FILE:)', response)
    for part in parts:
        m = re.match(r'FILE:\s*(.+?)\n```\w*\n(.*)', part, re.DOTALL)
        if not m:
            continue
        path = m.group(1).strip()
        inner = m.group(2)
        last_fence = inner.rfind('\n```')
        content = inner[:last_fence].strip() if last_fence != -1 else inner.strip().rstrip('`')
        if path and content:
            results.append((path, content))
    return results


def _extract_append_blocks(response: str) -> list[tuple[str, str]]:
    """Extract (path, content) pairs from APPEND: blocks, handling nested code fences.

    APPEND: blocks are used for fix-round updates where only new sections should
    be written — the caller reads the existing file and merges rather than
    requiring the model to regenerate the full document.
    """
    results = []
    parts = re.split(r'(?m)^(?=APPEND:)', response)
    for part in parts:
        m = re.match(r'APPEND:\s*(.+?)\n```\w*\n(.*)', part, re.DOTALL)
        if not m:
            continue
        path = m.group(1).strip()
        inner = m.group(2)
        last_fence = inner.rfind('\n```')
        content = inner[:last_fence].strip() if last_fence != -1 else inner.strip().rstrip('`')
        if path and content:
            results.append((path, content))
    return results


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
            "Write NEW files using FILE: blocks:\n"
            "FILE: path/to/file.md\n"
            "```markdown\n"
            "content\n"
            "```\n\n"
            "When asked to ADD sections to an EXISTING file, use APPEND: blocks instead.\n"
            "Write ONLY the new sections — do not repeat content that already exists.\n"
            "APPEND: path/to/file.md\n"
            "```markdown\n"
            "## New Section\n"
            "new content only\n"
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
        _is_append_task = any(
            kw in task.lower()
            for kw in ("add new sections", "append", "add sections to", "add missing sections")
        )
        _block_instruction = (
            "using APPEND: blocks that add ONLY the missing sections — "
            "do NOT rewrite or repeat any content that already exists in the file"
            if _is_append_task else
            "with FILE: blocks"
        )
        prompt = (
            f"{enriched}\n\n"
            f"## Documentation Task\n{task}\n\n"
            f"Using the research findings above, write the required documentation "
            f"{_block_instruction}. Include specific facts, names, comparisons, and "
            f"recommendations from the research — do not summarize or compress."
        )

        response = await model_router.generate(
            prompt, model, system_prompt=self.get_system_prompt(), enable_thinking=False
        )

        files_created: List[str] = []
        if tool_executor:
            for path_str, content in _extract_file_blocks(response):
                fp = path_str.strip()
                try:
                    await tool_executor.execute("file_write", {"path": fp, "content": content.strip()})
                    files_created.append(fp)
                    logger.info("documenter_file_written", path=fp)
                except Exception as e:
                    logger.warning("documenter_file_write_failed", path=fp, error=str(e))

            for path_str, new_content in _extract_append_blocks(response):
                fp = path_str.strip()
                try:
                    existing = await tool_executor.execute("file_read", {"path": fp})
                    if existing.startswith("Error reading file:"):
                        existing = ""
                    combined = (existing.rstrip() + "\n\n" + new_content.strip()) if existing else new_content.strip()
                    await tool_executor.execute("file_write", {"path": fp, "content": combined})
                    files_created.append(fp)
                    logger.info("documenter_file_appended", path=fp, new_bytes=len(new_content))
                except Exception as e:
                    logger.warning("documenter_append_failed", path=fp, error=str(e))

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
