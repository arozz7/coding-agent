"""Unit tests for LLM module."""
import pytest


class TestCostTracker:
    def test_estimate_tokens(self):
        from llm.cost_tracker import CostTracker

        tracker = CostTracker()
        text = "Hello world"
        tokens = tracker.estimate_tokens(text)
        assert tokens > 0

    def test_track_usage_local(self):
        from llm.cost_tracker import CostTracker
        from llm.model_router import ModelConfig

        tracker = CostTracker()
        config = ModelConfig(name="test", type="local")
        tracker.track_usage(config, "Hello", "World")
        assert len(tracker.records) == 1
        assert tracker.records[0].cost == 0.0

    def test_get_summary(self):
        from llm.cost_tracker import CostTracker

        tracker = CostTracker()
        summary = tracker.get_summary()
        assert "total_cost" in summary
        assert "total_tokens" in summary

    def test_track_usage_prefers_real_usage_over_estimate(self):
        """When a UsageInfo is passed, it must be used verbatim instead of
        re-deriving token counts from prompt/response text — real usage from
        the provider is always more accurate than chars//4 or tiktoken
        (which is the wrong vocabulary for local Qwen/DeepSeek models)."""
        from llm.cost_tracker import CostTracker
        from llm.usage import UsageInfo
        from llm.model_router import ModelConfig

        tracker = CostTracker()
        config = ModelConfig(name="test", type="local", provider="turboquant")
        # Prompt/response text would estimate very differently from this —
        # proves the real usage numbers win, not the text-derived estimate.
        real_usage = UsageInfo(prompt_tokens=42, completion_tokens=7, total_tokens=49)
        tracker.track_usage(config, "a" * 1000, "b" * 1000, usage=real_usage)

        record = tracker.records[0]
        assert record.prompt_tokens == 42
        assert record.completion_tokens == 7

    def test_track_usage_falls_back_to_estimate_when_no_usage(self):
        """Regression guard: omitting `usage=` must behave exactly like
        before this change — the estimate path is untouched."""
        from llm.cost_tracker import CostTracker
        from llm.model_router import ModelConfig

        tracker = CostTracker()
        config = ModelConfig(name="test", type="local")
        tracker.track_usage(config, "Hello", "World")

        record = tracker.records[0]
        assert record.prompt_tokens == tracker.estimate_tokens("Hello")
        assert record.completion_tokens == tracker.estimate_tokens("World")


class TestRateLimiter:
    def test_configure(self):
        from llm.rate_limiter import RateLimiter

        limiter = RateLimiter()
        limiter.configure("test_model", 60)
        assert limiter.rpm_config["test_model"] == 60

    def test_get_status(self):
        from llm.rate_limiter import RateLimiter

        limiter = RateLimiter()
        status = limiter.get_status("unknown_model")
        assert status["configured"] is False


class TestHealthChecker:
    def test_initialization(self):
        from llm.health import HealthChecker

        class MockRouter:
            pass

        checker = HealthChecker(MockRouter())
        assert checker is not None
        assert checker.statuses == {}

    @pytest.mark.asyncio
    async def test_check_reports_unavailable_when_health_check_returns_false(self):
        """health_check() can return False without raising (model not loaded,
        endpoint reachable but unhealthy) -- check() must treat that as a
        failure, not silently report the model as available."""
        from unittest.mock import AsyncMock, MagicMock
        from llm.health import HealthChecker

        class MockRouter:
            ollama = MagicMock()
            cloud = MagicMock()

        router = MockRouter()
        router.ollama.health_check = AsyncMock(return_value=False)
        checker = HealthChecker(router)

        config = MagicMock()
        config.name = "local-model"
        config.type = "local"

        result = await checker.check(config)

        assert result is False
        assert checker.statuses["local-model"].available is False

    @pytest.mark.asyncio
    async def test_check_reports_available_when_health_check_returns_true(self):
        from unittest.mock import AsyncMock, MagicMock
        from llm.health import HealthChecker

        class MockRouter:
            ollama = MagicMock()
            cloud = MagicMock()

        router = MockRouter()
        router.ollama.health_check = AsyncMock(return_value=True)
        checker = HealthChecker(router)

        config = MagicMock()
        config.name = "local-model"
        config.type = "local"

        result = await checker.check(config)

        assert result is True
        assert checker.statuses["local-model"].available is True
