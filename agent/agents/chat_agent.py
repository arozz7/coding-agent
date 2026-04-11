from typing import Dict, Any
from agent.agents.base_agent import AgentRole


class ChatRole(AgentRole):
    def __init__(self):
        super().__init__(
            name="chat",
            description="Conversational agent for general questions, explanations, and discussion",
        )

    def get_system_prompt(self) -> str:
        return """You are a knowledgeable, friendly assistant. Your role is to:
- Answer questions clearly and concisely
- Explain concepts in plain language
- Have natural, helpful conversations
- Discuss ideas and help users think through problems
- Give opinions and make recommendations when asked

You are NOT a code-generation machine. When someone asks a general question,
explain and discuss — do not turn it into a coding task. Only include code
examples when they genuinely illustrate a point and the user has not asked
you to write production code.

Be direct and honest. If you don't know something, say so. Keep answers
focused — no need to pad with caveats or lengthy preambles."""

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        task = context.get("task", "")
        model_router = context.get("model_router")
        enriched_context = context.get("enriched_context", "")

        if not model_router:
            return {"success": False, "error": "model_router not available"}

        model = model_router.get_model("coding")
        if not model:
            return {"success": False, "error": "No model configured"}

        prompt = f"""{self.get_system_prompt()}

User: {task}
{enriched_context}

Respond conversationally and helpfully."""

        response = await model_router.generate(prompt, model)

        return {
            "success": True,
            "role": self.name,
            "response": response,
            "task": task,
            "files_created": [],
        }


class ChatAgent:
    def __init__(self, model_router, tools=None):
        from agent.agents.base_agent import BaseAgent
        role = ChatRole()
        self.base = BaseAgent(role, model_router, tools)

    async def run(self, task: str, context: Dict[str, Any] = None):
        if context is None:
            context = {}
        return await self.base.run(task, context)
