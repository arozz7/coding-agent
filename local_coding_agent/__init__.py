from pathlib import Path

from agent.orchestrator import AgentOrchestrator
from llm import ModelRouter
from observability.logging import configure_logging

__version__ = "0.1.0"

# Project root is the directory that contains this package.
_PROJECT_ROOT = Path(__file__).parent.parent


def _load_env() -> None:
    """Load .env from the project root if present.

    Uses python-dotenv so that every ${VAR} reference in config files
    resolves correctly regardless of how the process was launched.
    Missing .env is silently ignored — production envs set vars directly.
    """
    try:
        from dotenv import load_dotenv
        env_path = _PROJECT_ROOT / ".env"
        load_dotenv(dotenv_path=env_path, override=False)
    except ImportError:
        pass  # python-dotenv not installed; rely on the shell environment


# Load at import time so env vars are present before any config reads.
_load_env()


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
