import re
from typing import Dict, Any
from agent.agents.base_agent import AgentRole
from agent.tools.web_tool import extract_urls

_CHAT_SEARCH_TRIGGERS = re.compile(
    r"\b(search\s+for|look\s+up|what('s|\s+is)\s+the\s+latest|current\s+version|"
    r"recent\s+news|find\s+out|is\s+there\s+a\s+new)\b",
    re.IGNORECASE,
)


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

If the context below includes web search results or fetched page content,
use that information to give an up-to-date, accurate answer.

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
        tool_executor = context.get("tool_executor")

        if not model_router:
            return {"success": False, "error": "model_router not available"}

        model = model_router.get_model("coding")
        if not model:
            return {"success": False, "error": "No model configured"}

        # Gather supplementary context from web when the question calls for it
        web_context = ""
        if tool_executor:
            # Fetch any URLs explicitly referenced
            for url in extract_urls(task)[:2]:
                try:
                    fetched = await tool_executor.execute("web_fetch", {"url": url})
                    if fetched and "Error" not in fetched[:20]:
                        web_context += f"\n\n[Fetched: {url}]\n{fetched[:2000]}"
                except Exception:
                    pass

            # Web search for factual / current-events questions
            if _CHAT_SEARCH_TRIGGERS.search(task):
                try:
                    results = await tool_executor.execute("web_search", {"query": task[:200], "max_results": 3})
                    if results:
                        web_context += f"\n\n[Web search results]\n{results[:2000]}"
                except Exception:
                    pass

        prompt = f"""{self.get_system_prompt()}

User: {task}
{web_context}
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
