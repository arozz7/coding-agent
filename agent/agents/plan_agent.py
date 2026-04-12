from typing import Dict, Any
from agent.agents.base_agent import AgentRole


class PlanRole(AgentRole):
    """Generates a structured implementation plan without writing any code or files.

    Triggered when the user explicitly asks to plan before building — phrases like
    "I want to plan first", "show me a plan", "solid plan", etc.  The agent
    returns a markdown plan that the user can review and approve.  On approval
    ("looks good", "build it", "proceed") the conversation history carries the plan
    into the next turn so the developer agent uses it as a blueprint.
    """

    def __init__(self):
        super().__init__(
            name="planner",
            description="Creates structured implementation plans before any code is written",
        )

    def get_system_prompt(self) -> str:
        return """You are an expert software architect and project planner.
Your role is to produce a clear, detailed implementation plan — but NO actual code files.

The user wants to review and approve the plan before any code is written.

## Your Output Format

Respond with a structured markdown plan containing:

1. **Project Overview** — What is being built and why (2-3 sentences)
2. **Tech Stack** — Languages, frameworks, libraries with brief justification
3. **Architecture** — High-level structure (components, layers, data flow)
4. **File Structure** — Proposed directory tree with descriptions
5. **Implementation Phases** — Ordered list of phases with tasks per phase
6. **Key Decisions** — Notable trade-offs or design choices worth noting
7. **Risks & Mitigations** — Known challenges and how to address them

## Rules
- Do NOT write any actual code
- Do NOT use FILE: syntax
- Do NOT run any shell commands
- Keep the plan concise but complete enough to act on
- End with: "Reply **'build it'** to start implementation, or request changes."

## Example opener
```
## Plan: RPG Game

### Project Overview
A turn-based RPG with a player, enemies, and a combat system...
```"""

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        task = context.get("task", "")
        model_router = context.get("model_router")
        enriched_context = context.get("enriched_context", "")

        if not model_router:
            return {"success": False, "error": "model_router not available"}

        prompt = f"""{self.get_system_prompt()}

{enriched_context}

User request: {task}

Write the implementation plan now."""

        model = model_router.get_model("coding")
        if not model:
            return {"success": False, "error": "No coding model configured"}

        response = await model_router.generate(prompt, model)

        return {
            "success": True,
            "role": self.name,
            "response": response,
            "task": task,
            "files_created": [],  # plans never create files
        }


class PlanAgent:
    def __init__(self, model_router, tools=None):
        from agent.agents.base_agent import BaseAgent
        role = PlanRole()
        self.base = BaseAgent(role, model_router, tools)

    async def run(self, task: str, context: Dict[str, Any] = None):
        if context is None:
            context = {}
        return await self.base.run(task, context)
