"""agent_factory — instantiates and wires all agent types.

Extracted from AgentOrchestrator.__init__ so that the orchestrator is not
responsible for constructing 13+ concrete agent objects.  Callers receive a
plain dict keyed by role name; no abstract base class required here because
every agent already has a compatible .run(task, context) signature.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from agent.tools import FileSystemTool, PytestTool, CodeAnalyzer
    from agent.tools.shell_tool import ShellTool
    from agent.tools.browser_tool import BrowserTool
    from agent.orchestration.requirements_extractor import RequirementsExtractor
    from llm import ModelRouter


def create_agents(
    model_router: "ModelRouter",
    fs_tool: "FileSystemTool",
    shell_tool: "ShellTool",
    browser_tool: "BrowserTool",
    code_analyzer: "CodeAnalyzer",
    pytest_tool: "PytestTool",
    requirements_extractor: "RequirementsExtractor",
) -> dict[str, Any]:
    """Instantiate every agent type and return them keyed by role name."""
    from agent.agents.developer_agent import DeveloperAgent
    from agent.agents.plan_agent import PlanAgent
    from agent.agents.planner_agent import PlannerAgent
    from agent.agents.plan_reviewer_agent import PlanReviewerAgent
    from agent.agents.tester_agent import TesterAgent
    from agent.agents.reviewer_agent import ReviewerAgent
    from agent.agents.architect_agent import ArchitectAgent
    from agent.agents.chat_agent import ChatAgent
    from agent.agents.research_agent import ResearchAgent
    from agent.agents.verifier_agent import VerifierAgent
    from agent.agents.mapper_agent import MapperAgent
    from agent.agents.red_team_agent import RedTeamAgent
    from agent.agents.documenter_agent import DocumenterAgent
    from agent.agents.acceptance_tester_agent import AcceptanceTesterAgent

    return {
        "developer": DeveloperAgent(
            model_router,
            tools=[fs_tool, shell_tool],
            file_system_tool=fs_tool,
            shell_tool=shell_tool,
            browser_tool=browser_tool,
        ),
        "plan": PlanAgent(model_router),
        "tester": TesterAgent(
            model_router,
            tools=[fs_tool, pytest_tool],
            file_system_tool=fs_tool,
            pytest_tool=pytest_tool,
        ),
        "reviewer": ReviewerAgent(
            model_router,
            tools=[code_analyzer, fs_tool],
            code_analyzer=code_analyzer,
            file_system_tool=fs_tool,
        ),
        "architect": ArchitectAgent(
            model_router,
            tools=[fs_tool, code_analyzer],
            file_system_tool=fs_tool,
            code_analyzer=code_analyzer,
        ),
        "chat": ChatAgent(model_router),
        "research": ResearchAgent(
            model_router,
            tools=[fs_tool, code_analyzer],
            file_system_tool=fs_tool,
            code_analyzer=code_analyzer,
        ),
        "mapper": MapperAgent(model_router, file_system_tool=fs_tool),
        "red_team": RedTeamAgent(model_router),
        "documenter": DocumenterAgent(model_router),
        "verifier": VerifierAgent(model_router),
        "acceptance_tester": AcceptanceTesterAgent(model_router),
        "planner": PlannerAgent(model_router, requirements_extractor=requirements_extractor),
        "plan_reviewer": PlanReviewerAgent(model_router),
    }
