import re
from datetime import date
from typing import Dict, Any
from agent.agents.base_agent import AgentRole
from agent.tools.web_tool import extract_urls

# Questions that need current / external information — always trigger a web search.
# Err on the side of searching: a few extra searches cost nothing but a missed
# lookup gives the user a stale "I don't know" answer.
_SEARCH_TRIGGERS = re.compile(
    r"\b("
    # Temporal — answer depends on when you ask
    r"last\s+night|last\s+week|last\s+month|last\s+year|"
    r"yesterday|today|tonight|this\s+(morning|afternoon|evening|week|month|year|season)|"
    r"right\s+now|at\s+the\s+moment|currently|just\s+now|"
    r"latest|recent(ly)?|upcoming|live\b|"
    # Sports / scores / events
    r"score|scores|final\s+score|who\s+won|who\s+lost|winner|loser|champion|"
    r"standings|rankings|league|playoff|tournament|match\s+result|game\s+result|"
    r"nfl|nba|mlb|nhl|mls|fifa|ufc|f1\b|premier\s+league|champions\s+league|"
    # Specific query words suggesting external-world facts
    r"weather|temperature|forecast|humidity|"
    r"stock|share\s+price|crypto|bitcoin|ethereum|market|"
    r"exchange\s+rate|interest\s+rate|inflation|gdp|"
    r"news|headline|breaking|announcement|press\s+release|"
    r"released|launched|announced|discovered|"
    # Explicit search requests
    r"search\s+(for|the\s+web|online)|look\s+up|find\s+(out|me)|"
    r"google|can\s+you\s+(search|look|find)|"
    # Version / release facts
    r"current\s+version|latest\s+version|release\s+notes|changelog|"
    r"what('s|\s+is)\s+(new|the\s+latest|the\s+current|happening)"
    r")\b",
    re.IGNORECASE,
)

# Catch factual who/what/when/where questions that aren't about code.
# These very often need a web lookup even without explicit temporal markers.
_FACTUAL_QUESTION = re.compile(
    r"^(who\s+(is|are|was|were|won|did)|"
    r"what\s+(is|are|was|were|did|happened|time)|"
    r"when\s+(is|are|was|were|did|does|will)|"
    r"where\s+(is|are|was|were)|"
    r"how\s+(much|many|far|long|old|tall|big|fast)\b)",
    re.IGNORECASE,
)

# Questions that are clearly conversational or code-specific — skip search.
_SKIP_SEARCH = re.compile(
    r"^(hi\b|hello\b|hey\b|thanks|thank\s+you|okay|ok\b|bye\b|"
    r"what\s+(is|are)\s+(a|an|the)\s+\w+\s+(in\s+(programming|coding|python|javascript)|algorithm|pattern)|"
    r"explain\s+(to\s+me\s+)?(how|what|why)\s+\w+\s+(works?|is|does)\s*(in\s+(code|programming))?|"
    r"how\s+do\s+I\s+(write|implement|create|build|use|fix|debug))",
    re.IGNORECASE,
)


class ChatRole(AgentRole):
    def __init__(self):
        super().__init__(
            name="chat",
            description="Conversational agent for general questions, explanations, and discussion",
        )

    def get_system_prompt(self) -> str:
        today = date.today().strftime("%A, %B %d, %Y")
        return f"""Today's date is {today}.

You are a knowledgeable, friendly chat assistant. You help users
by answering questions, having conversations, and explaining concepts.

Available tools:
- search: Look up real-time information from the web when needed

Guidelines:
- Explain concepts in plain language without unnecessarily turning things into coding tasks
- Prioritize real-time data from [LIVE WEB SEARCH RESULTS] if present in context
- Be direct, honest, and concise in your responses
- Just answer the question without verbose caveats"""

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

        # Gather supplementary context from web when the question calls for it.
        # Search heuristic (in priority order):
        #   1. Always fetch explicitly referenced URLs
        #   2. Always search on temporal / sports / news / price triggers
        #   3. Search on factual who/what/when/where questions (unless clearly code/chat)
        web_context = ""
        if tool_executor:
            # 1. Fetch any URLs explicitly referenced in the task
            for url in extract_urls(task)[:2]:
                try:
                    fetched = await tool_executor.execute("web_fetch", {"url": url})
                    if fetched and not fetched.startswith("Error"):
                        web_context += f"\n\n[FETCHED PAGE CONTENT: {url}]\n{fetched[:2000]}"
                except Exception:
                    pass

            # 2 & 3. Decide whether a web search is warranted
            should_search = (
                _SEARCH_TRIGGERS.search(task)
                or (_FACTUAL_QUESTION.match(task.strip()) and not _SKIP_SEARCH.match(task.strip()))
            )

            if should_search:
                try:
                    results = await tool_executor.execute(
                        "web_search", {"query": task[:200], "max_results": 5}
                    )
                    if results and not results.startswith("Error"):
                        web_context += f"\n\n[LIVE WEB SEARCH RESULTS]\n{results[:3000]}"
                except Exception:
                    pass

        # Put live data BEFORE the question so the model reads it first.
        if web_context:
            prompt = f"""The following data was retrieved from the internet right now to help answer the user's question:
{web_context}
{enriched_context}

User question: {task}

Answer using the live data above. Be specific — cite headlines, scores, or facts directly."""
        else:
            prompt = f"""{enriched_context}

User: {task}

Respond conversationally and helpfully."""

        response = await model_router.generate(prompt, model, system_prompt=self.get_system_prompt())

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
