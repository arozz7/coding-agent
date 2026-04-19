"""Red-team / security agent: adversarial security audit.

Modelled after the pi-vs-claude-code red-team agent — finds vulnerabilities,
edge cases, exposed secrets, injection risks, and unsafe defaults.
Read-only: never modifies files.
"""

from typing import Any, Dict, List

import structlog

logger = structlog.get_logger()


class RedTeamRole:
    name = "security"

    def get_system_prompt(self) -> str:
        return (
            "You are a red-team security agent. Find vulnerabilities, edge cases, "
            "and failure modes in the code.\n\n"
            "Check for:\n"
            "1. **Injection risks** — SQL injection, command injection, path traversal\n"
            "2. **Exposed secrets** — hardcoded API keys, passwords, tokens in source files\n"
            "3. **Missing validation** — unvalidated user inputs at system boundaries\n"
            "4. **Unsafe defaults** — debug modes enabled, open CORS, weak crypto\n"
            "5. **Authentication gaps** — missing auth checks, insecure session handling\n"
            "6. **Dependency risks** — known-vulnerable packages, unpinned versions\n"
            "7. **Error information leakage** — stack traces or internals exposed to users\n\n"
            "Output a structured report:\n"
            "## Security Findings\n"
            "### Critical\n- Finding + file:line + recommendation\n"
            "### High\n- ...\n"
            "### Medium\n- ...\n"
            "### Info\n- ...\n\n"
            "## Summary\n"
            "One paragraph: overall risk level and top 3 priorities.\n\n"
            "DO NOT modify any files — report only."
        )

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        task = context.get("task", "Perform a security audit")
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
            f"## Security Audit Task\n{task}\n\n"
            f"Perform a thorough adversarial security review. "
            f"Read any relevant source files using the workspace file listing above. "
            f"Prioritise findings by severity. Be specific — include file paths and line references."
        )

        response = await model_router.generate(
            prompt, model, system_prompt=self.get_system_prompt()
        )

        return {
            "success": True,
            "role": self.name,
            "response": response,
            "task": task,
            "files_created": [],
            "completion_summary": "Security audit complete — see report for findings.",
        }


class RedTeamAgent:
    def __init__(self, model_router, tools=None, **kwargs):
        self.role = RedTeamRole()
        self.model_router = model_router

    async def run(self, task: str, context: Dict[str, Any] = None) -> Dict[str, Any]:
        if context is None:
            context = {}
        context["task"] = task
        context["model_router"] = context.get("model_router") or self.model_router
        return await self.role.execute(context)
