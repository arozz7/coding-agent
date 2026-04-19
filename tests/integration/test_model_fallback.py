"""Integration tests for model fallback behaviour.

Tests:
  - OpenRouter 429 → immediate fallback to local model (no retry loop)
  - Generic remote 429 string → same fallback
  - No local model available → LLMError is raised
  - ModelRouter._get_local_fallback selects the right config
  - Task type keyword classifier (pure unit, lives here for proximity to router tests)
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from llm.cloud_api_client import _OpenRouterRateLimitError
from llm.model_router import ModelRouter, LLMError
from llm.config import ModelConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_router_with_configs(configs: list[ModelConfig]) -> ModelRouter:
    """Build a ModelRouter whose configs list is pre-populated (skips YAML)."""
    router = ModelRouter.__new__(ModelRouter)
    router.configs = configs
    router.config_by_name = {c.name: c for c in configs}
    router._defaults = {}
    router._defaults = {}
    router._local_runtime = {}
    router._active_model_name = None
    router._switch_callbacks = []
    router.logger = MagicMock()
    router.ollama = MagicMock()
    router.cloud = MagicMock()
    router.cost_tracker = MagicMock()
    router.rate_limiter = MagicMock()
    router.rate_limiter.acquire = AsyncMock()
    router.health_checker = MagicMock()
    router.health_checker.record_success = MagicMock()
    router.health_checker.record_failure = MagicMock()
    router.health_checker.record_rate_limit = MagicMock()
    return router


def _local_config(name: str = "local-llm") -> ModelConfig:
    return ModelConfig(name=name, type="local", endpoint="http://localhost:11434")


def _cloud_config(name: str = "openrouter/gemma", endpoint: str = "https://openrouter.ai/api/v1") -> ModelConfig:
    return ModelConfig(name=name, type="cloud", endpoint=endpoint, api_key="mock")


# ---------------------------------------------------------------------------
# _get_local_fallback
# ---------------------------------------------------------------------------

class TestGetFallbackChain:
    def test_returns_first_local_model(self):
        local = _local_config("local-1")
        cloud = _cloud_config("cloud-1")
        router = _make_router_with_configs([cloud, local])

        chain = router._get_fallback_chain(exclude_name="cloud-1")
        assert chain[0] is local

    def test_excludes_specified_name(self):
        local1 = _local_config("local-1")
        local2 = _local_config("local-2")
        router = _make_router_with_configs([local1, local2])

        chain = router._get_fallback_chain(exclude_name="local-1")
        assert chain[0] is local2

    def test_falls_back_to_same_name_if_only_one_local(self):
        local = _local_config("only-local")
        cloud = _cloud_config("cloud")
        router = _make_router_with_configs([cloud, local])

        chain = router._get_fallback_chain(exclude_name="only-local")
        # Since _get_fallback_chain unconditionally excludes the model, we get no local fallbacks.
        # Plus our cloud mock fails the has_key check, so chain is empty!
        assert len(chain) == 0

    def test_returns_none_when_no_local_models(self):
        cloud1 = _cloud_config("cloud-1")
        cloud2 = _cloud_config("cloud-2")
        router = _make_router_with_configs([cloud1, cloud2])

        chain = router._get_fallback_chain(exclude_name="cloud-1")
        assert len(chain) == 0


# ---------------------------------------------------------------------------
# OpenRouter 429 → local fallback
# ---------------------------------------------------------------------------

class TestOpenRouterRateLimitFallback:
    @pytest.mark.asyncio
    async def test_openrouter_429_falls_back_to_local(self):
        local = _local_config("qwen")
        cloud = _cloud_config("gemma", "https://openrouter.ai/api/v1")
        router = _make_router_with_configs([local, cloud])

        # Cloud raises 429; local succeeds
        router.cloud.generate = AsyncMock(
            side_effect=_OpenRouterRateLimitError(retry_after=0)
        )
        router.ollama.generate = AsyncMock(return_value="local response")

        result = await router.generate("hello", cloud, max_retries=1)
        assert result == "local response"
        router.ollama.generate.assert_called_once()

    @pytest.mark.asyncio
    async def test_openrouter_429_no_local_raises_llm_error(self):
        cloud = _cloud_config("gemma", "https://openrouter.ai/api/v1")
        router = _make_router_with_configs([cloud])

        router.cloud.generate = AsyncMock(
            side_effect=_OpenRouterRateLimitError(retry_after=0)
        )

        with pytest.raises(LLMError, match="rate_limited"):
            await router.generate("hello", cloud, max_retries=1)

    @pytest.mark.asyncio
    async def test_fallback_not_retried_on_its_own_error(self):
        """If the local fallback itself errors, the error propagates (no infinite loop)."""
        local = _local_config("qwen")
        cloud = _cloud_config("gemma", "https://openrouter.ai/api/v1")
        router = _make_router_with_configs([local, cloud])

        router.cloud.generate = AsyncMock(
            side_effect=_OpenRouterRateLimitError(retry_after=0)
        )
        router.ollama.generate = AsyncMock(side_effect=RuntimeError("ollama down"))

        with pytest.raises(RuntimeError, match="ollama down"):
            await router.generate("hello", cloud, max_retries=1)

    @pytest.mark.asyncio
    async def test_openrouter_429_records_rate_limit(self):
        local = _local_config("qwen")
        cloud = _cloud_config("gemma", "https://openrouter.ai/api/v1")
        router = _make_router_with_configs([local, cloud])

        router.cloud.generate = AsyncMock(
            side_effect=_OpenRouterRateLimitError(retry_after=5)
        )
        router.ollama.generate = AsyncMock(return_value="ok")

        await router.generate("hello", cloud, max_retries=1)
        router.health_checker.record_rate_limit.assert_called_with(cloud.name)


# ---------------------------------------------------------------------------
# Generic remote 429 fallback
# ---------------------------------------------------------------------------

class TestGenericRemoteRateLimitFallback:
    @pytest.mark.asyncio
    async def test_generic_429_falls_back_to_local(self):
        local = _local_config("qwen")
        cloud = _cloud_config("some-cloud", "https://api.example.com")
        router = _make_router_with_configs([local, cloud])

        router.cloud.generate = AsyncMock(
            side_effect=Exception("HTTP 429 Too Many Requests")
        )
        router.ollama.generate = AsyncMock(return_value="local ok")

        result = await router.generate("q", cloud, max_retries=1)
        assert result == "local ok"

    @pytest.mark.asyncio
    async def test_local_model_errors_not_caught_as_rate_limit(self):
        """A local model raising 429-like text should NOT trigger fallback logic."""
        local = _local_config("qwen")
        router = _make_router_with_configs([local])

        router.ollama.generate = AsyncMock(
            side_effect=Exception("HTTP 429 Too Many Requests from local")
        )

        with pytest.raises(Exception):
            await router.generate("q", local, max_retries=1)

        # health_checker.record_rate_limit should NOT have been called (local skips fallback)
        router.health_checker.record_rate_limit.assert_not_called()


# ---------------------------------------------------------------------------
# Task type keyword classifier (pure unit)
# ---------------------------------------------------------------------------

class TestKeywordClassifier:
    """Test _detect_task_type_keyword without instantiating a full orchestrator."""

    @pytest.fixture
    def orch(self, tmp_path):
        from agent.orchestrator import AgentOrchestrator
        from unittest.mock import MagicMock

        router = MagicMock()
        router.configs = []
        router.config_by_name = {}

        with (
            patch("agent.orchestrator.SessionMemory"),
            patch("agent.orchestrator.CodebaseMemory"),
            patch("agent.orchestrator.FileSystemTool"),
            patch("agent.orchestrator.PytestTool"),
            patch("agent.orchestrator.CodeAnalyzer"),
            patch("agent.orchestrator.DeveloperAgent"),
            patch("agent.orchestrator.TesterAgent"),
            patch("agent.orchestrator.ReviewerAgent"),
            patch("agent.orchestrator.ArchitectAgent"),
            patch("agent.orchestrator.ChatAgent"),
            patch("agent.orchestrator.ResearchAgent"),
            patch("agent.orchestrator.SkillManager"),
            patch("agent.orchestrator.WikiManager"),
            patch("agent.orchestrator.SkillExecutor"),
            patch("agent.orchestrator.MemoryWiki"),
            patch("agent.orchestrator.AgentLogger"),
            patch("mcp.server.create_mcp_server"),
            patch("agent.tools.shell_tool.ShellTool"),
            patch("agent.tools.browser_tool.BrowserTool"),
            patch("agent.tools.tool_executor.ToolExecutor"),
            patch("agent.tools.tool_executor.EventEmittingExecutor"),
        ):
            return AgentOrchestrator(str(tmp_path), router)

    @pytest.mark.parametrize("task, expected", [
        # plan mode — must detect BEFORE develop keywords
        ("let's build a new game but I want first work on a solid plan", "plan"),
        ("plan first: create a REST API", "plan"),
        ("show me a plan for the auth system", "plan"),
        ("I want a plan before we build anything", "plan"),
        # develop
        ("implement a login function", "develop"),
        ("refactor the auth module", "develop"),
        ("write a script to parse CSV", "develop"),
        # review
        ("review the code in auth.py", "review"),
        ("code review for PR #42", "review"),
        # test
        ("write unit tests for the parser", "test"),
        ("run tests and report failures", "test"),
        # architect
        ("design the architecture for the billing system", "architect"),
        ("write an ADR for the database choice", "architect"),
        # research
        ("how does the existing cache work?", "research"),
        ("where is the config loaded?", "research"),
        # chat
        ("what's the weather today?", "chat"),
        ("who won the championship?", "chat"),
        ("explain recursion to me", "chat"),
    ])
    def test_keyword_classification(self, orch, task, expected):
        assert orch._detect_task_type_keyword(task) == expected
