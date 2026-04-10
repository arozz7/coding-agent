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
