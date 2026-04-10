from agent.orchestrator import AgentOrchestrator
from llm import ModelRouter
from observability.logging import configure_logging

__version__ = "0.1.0"


def create_agent(
    workspace_path: str = "./workspace",
    config_path: str = "config/models.yaml",
) -> AgentOrchestrator:
    configure_logging()
    model_router = ModelRouter(config_path)
    return AgentOrchestrator(workspace_path, model_router)


__all__ = ["AgentOrchestrator", "ModelRouter", "create_agent", "__version__"]
