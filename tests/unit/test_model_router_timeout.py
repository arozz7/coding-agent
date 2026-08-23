"""Unit tests for ModelRouter.generate()'s timeout resolution.

Regression: developer_agent and tester_agent each carried a duplicated
module-level timeout constant (_DEVELOPER_TIMEOUT_SECS / _TESTER_TIMEOUT_SECS)
because the model_router default of 600s was too short for a thinking model
with a large max_tokens budget — the tester constant was only added after a
live failure, five days after the identical developer-role bug was fixed
(logs/api-20260819-201238.log). The fix moves the timeout budget onto
ModelConfig.timeout_secs so every role — not just the two that were patched
reactively — gets the right budget for whichever model it calls.
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from llm.model_router import ModelRouter
from llm.config import ModelConfig


def _make_router_with_configs(configs: list[ModelConfig]) -> ModelRouter:
    """Build a ModelRouter whose configs list is pre-populated (skips YAML)."""
    router = ModelRouter.__new__(ModelRouter)
    router.configs = configs
    router.config_by_name = {c.name: c for c in configs}
    router._defaults = {}
    router._local_runtime = {}
    router._active_model_name = None
    router._switch_callbacks = []
    router._evaluator_cache = None
    router._EVALUATOR_CACHE_TTL = 3600.0
    router._evaluator_blacklist = {}
    router._EVALUATOR_BLACKLIST_TTL = 86400.0
    router._openrouter_rate_limit_hits = []
    router._OPENROUTER_BREAKER_WINDOW = 600.0
    router._OPENROUTER_BREAKER_THRESHOLD = 2
    router._openrouter_cooldown_until = 0.0
    router._OPENROUTER_BREAKER_COOLDOWN = 1800.0
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


@pytest.mark.asyncio
async def test_generate_uses_model_declared_timeout_by_default():
    config = ModelConfig(name="local-thinker", type="local", endpoint="http://localhost:11434", timeout_secs=1500.0)
    router = _make_router_with_configs([config])
    router.ollama.generate = AsyncMock(return_value="ok")

    await router.generate("prompt", config)

    assert router.ollama.generate.call_args.kwargs["timeout"] == 1500.0


@pytest.mark.asyncio
async def test_generate_falls_back_to_default_timeout_secs():
    config = ModelConfig(name="local-plain", type="local", endpoint="http://localhost:11434")
    router = _make_router_with_configs([config])
    router.ollama.generate = AsyncMock(return_value="ok")

    await router.generate("prompt", config)

    assert router.ollama.generate.call_args.kwargs["timeout"] == 600.0


@pytest.mark.asyncio
async def test_generate_explicit_timeout_overrides_model_config():
    config = ModelConfig(name="local-thinker", type="local", endpoint="http://localhost:11434", timeout_secs=1500.0)
    router = _make_router_with_configs([config])
    router.ollama.generate = AsyncMock(return_value="ok")

    await router.generate("prompt", config, timeout=45.0)

    assert router.ollama.generate.call_args.kwargs["timeout"] == 45.0
