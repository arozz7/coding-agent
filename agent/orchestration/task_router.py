"""Task router — classifies tasks and detects skill triggers.

Owns:
  - TaskRouter.detect(task)          async, returns task-type string
  - TaskRouter.detect_skills(task, phase)  returns skill names for pre/post
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING, List

import structlog

if TYPE_CHECKING:
    from llm import ModelRouter

logger = structlog.get_logger()


class TaskRouter:
    """Classifies a task string into an agent type and detects skill triggers."""

    # Strong develop signals that the LLM classifier sometimes mislabels as chat.
    _DEFINITIVE_DEVELOP = re.compile(
        r"""
        \b(
            fix\s+the\s+(code\s+)?errors?         # "fix the errors" / "fix the code errors"
          | fix\s+(all\s+)?the\s+bugs?             # "fix the bugs"
          | fix\s+(?:and\s+)?run                   # "fix and run"
          | get\s+\S+\s+running                    # "get the game running"
          | get\s+it\s+running                     # "get it running"
          | debugging                              # bare "debugging"
          | debug\s+(?:and|the|it|this)            # "debug the", "debug it"
        )\b
        """,
        re.VERBOSE | re.IGNORECASE,
    )

    _PRE_TRIGGERS: dict[str, list[str]] = {
        "test": ["tdd-enforcer"],
        "security": ["security-auditor"],
        "audit": ["security-auditor"],
        "database": ["architect-decision-engine"],
        "api": ["architect-decision-engine"],
        "auth": ["architect-decision-engine"],
        "architecture": ["architect-decision-engine"],
        "adr": ["architect-decision-engine"],
    }
    _POST_TRIGGERS: dict[str, list[str]] = {
        "compile": ["wiki-compile"],
        "save": ["wiki-compile"],
        "remember": ["wiki-compile"],
        "wiki": ["wiki-compile"],
        "handover": ["handover"],
        "context bridge": ["handover"],
    }

    def __init__(self, model_router: "ModelRouter"):
        self.model_router = model_router
        self.logger = logger.bind(component="task_router")

    async def detect(self, task: str) -> str:
        """Return the most appropriate agent role for this task.

        Runs a definitive-develop regex first (fast, no LLM call) to catch
        common patterns the LLM mislabels.  Falls back to LLM classifier,
        then keyword matching.
        """
        if self._DEFINITIVE_DEVELOP.search(task):
            self.logger.info("task_type_definitive", task_type="develop")
            return "develop"

        try:
            result = await self._detect_llm(task)
            self.logger.info("task_type_llm", task_type=result)
            return result
        except Exception as e:
            self.logger.warning("task_type_llm_fallback", reason=str(e))
            return self._detect_keyword(task)

    def detect_skills(self, task: str, phase: str = "pre") -> list[str]:
        """Return skill names triggered by task keywords for the given phase."""
        task_lower = task.lower()
        triggers = self._PRE_TRIGGERS if phase == "pre" else self._POST_TRIGGERS
        seen: list[str] = []
        for keyword, names in triggers.items():
            if keyword in task_lower:
                for name in names:
                    if name not in seen:
                        seen.append(name)
        return seen

    # ------------------------------------------------------------------
    # Internal classifiers
    # ------------------------------------------------------------------

    async def _detect_llm(self, task: str) -> str:
        """LLM-based task classifier. Returns one of the 6 valid task types."""
        import re as _re
        import yaml as _yaml

        from local_coding_agent import _PROJECT_ROOT
        cfg_path = _PROJECT_ROOT / "config" / "task_classifier.yaml"
        if not cfg_path.exists():
            raise FileNotFoundError(f"task_classifier.yaml not found at {cfg_path}")

        with open(cfg_path) as fh:
            cfg = _yaml.safe_load(fh)

        valid_types: List[str] = cfg["valid_types"]
        timeout_s: float = float(cfg.get("timeout_seconds", 3))
        prompt_template: str = cfg["prompt"]
        prompt = prompt_template.format(task=task)

        config = self.model_router.get_model("coding")
        if not config:
            raise RuntimeError("No model configured")

        raw = await self.model_router.generate(
            prompt, config, timeout=timeout_s, enable_thinking=False
        )

        first_line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
        candidate = _re.sub(r"[^a-z]", "", first_line.lower().split()[0]) if first_line else ""

        if candidate not in valid_types:
            raise ValueError(f"LLM returned unexpected type: {candidate!r}")

        return candidate

    def _detect_keyword(self, task: str) -> str:
        """Keyword-based fallback classifier."""
        t = task.lower()

        _SDLC = [
            "build me a complete", "build a complete", "build a full",
            "create a full", "create a complete",
            "develop a complete", "develop a full",
            "build and test", "build, test",
            "build and run", "build and deploy",
            "implement and test", "implement, test",
            "full app", "full application", "entire application",
            "end to end", "end-to-end",
            "full development", "full stack",
            "build the whole", "build the entire",
        ]
        if any(kw in t for kw in _SDLC):
            return "sdlc"

        _RUN_DEBUG = [
            "run and debug", "run and fix", "debug and fix", "run the game",
            "run the app", "run the server", "run the project", "run the code",
            "run and test", "launch the", "start the app", "start the server",
            "start the game", "start the project",
            "debug the", "debug it", "debug and", "debugging",
            "fix the runtime", "fix the error", "fix the errors", "fix the bug",
            "fix the bugs", "fix and run", "fix this error", "fix these errors",
            "fix the code errors", "fix code errors", "fix all errors",
            "there are still errors", "still not running", "not starting",
            "can't run", "cannot run", "won't run", "fails to run",
            "fails to start", "failing to run",
            "get it running", "get the game running", "get the app running",
            "get the server running", "get the project running", "get it working",
            "run the build", "running the build", "try running", "run it",
            "run with verbose", "run with", "run verbose",
            "go ahead and run", "go run", "now run", "run now",
            "run the application", "run the program",
            "run npm", "npm run", "npm install", "npm start",
            "compile the", "execute the", "execute it",
            "build it", "build the project", "build the app",
            "running builds", "running the app", "running the application",
        ]
        if any(kw in t for kw in _RUN_DEBUG):
            return "develop"

        _PLAN = [
            "plan first", "solid plan", "show me a plan", "want to plan",
            "want first work on a", "planning phase", "let's plan", "lets plan",
            "before we build", "before building", "before implementing",
            "roadmap", "outline the approach", "outline a plan", "create a plan",
            "work on a plan", "i want a plan",
        ]
        if any(kw in t for kw in _PLAN):
            return "plan"

        _DEVELOP = [
            "implement", "refactor", "write a function", "write a class",
            "write a script", "write the code", "write code",
            "create a file", "create the file",
            "build a ", "build the ", "develop a ",
            "add feature", "add a feature",
            "fix the bug", "fix this bug", "fix the error", "fix this error",
            "fix the issue", "fix this issue",
            "update the code", "update the function", "update the class",
            "generate code", "generate a script",
            "create an api", "create a server", "create a bot", "create a cli",
            "make an app", "make a server", "make a bot", "make a script",
            "make a function", "make a class",
            "flush out", "flesh out", "fill in", "fill out",
            "complete the", "complete this", "finish the", "finish writing",
            "continue to write", "continue writing", "continue to flush",
            "continue to flesh", "continue to fill", "continue to build",
            "continue to develop", "continue to work on",
            "write the narrative", "write the story", "write the lore",
            "write the docs", "write the document", "write the content",
            "draft the", "draft a document", "draft a narrative",
            "expand the", "expand on", "add content", "add more content",
            "add to the", "update the doc", "update the narrative",
            "update the story", "write more", "add more detail",
            "create the document", "create the narrative", "create the story",
            "create the lore", "create the wiki", "create the design doc",
            "write up", "document the", "write out",
        ]
        if any(kw in t for kw in _DEVELOP):
            return "develop"

        if any(kw in t for kw in [
            "review the code", "code review", "critique", "check for bugs",
            "security audit", "security review", "analyze this code",
            "review this file", "review this function",
        ]):
            return "review"

        if any(kw in t for kw in [
            "write tests", "write unit tests", "add tests", "create tests",
            "generate tests", "unit test", "pytest", "test suite", "test case",
            "run the tests", "run tests",
        ]):
            return "test"

        if any(kw in t for kw in [
            "system design", "design the architecture", "architecture for",
            "write an adr", "create an adr", "architect the", "high-level design",
            "design pattern for", "design a system",
        ]):
            return "architect"

        if any(kw in t for kw in [
            "where is ", "where are ", "find the ", "find where",
            "locate ", "which file", "what file",
            "trace ", "how does the existing", "how is ", "how does ",
            "what does the code", "show me where",
            "search the codebase", "look for ", "search for ",
            "what files", "investigate", "explore the code",
            "explain this code", "explain the code", "explain this file",
        ]):
            return "research"

        return "chat"
