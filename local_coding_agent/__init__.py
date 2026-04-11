from pathlib import Path

from agent.orchestrator import AgentOrchestrator
from llm import ModelRouter
from observability.logging import configure_logging

__version__ = "0.1.0"

# Project root is the directory that contains this package.
_PROJECT_ROOT = Path(__file__).parent.parent


def create_agent(
    workspace_path: str = "./workspace",
    config_path: str = "config/models.yaml",
) -> AgentOrchestrator:
    configure_logging()

    # Resolve relative paths against the project root so the config is found
    # regardless of where the process is launched from.
    resolved = Path(config_path)
    if not resolved.is_absolute():
        resolved = (_PROJECT_ROOT / config_path).resolve()

    model_router = ModelRouter(str(resolved))
    return AgentOrchestrator(workspace_path, model_router)


__all__ = ["AgentOrchestrator", "ModelRouter", "create_agent", "__version__"]
