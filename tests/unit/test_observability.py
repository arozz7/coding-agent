"""Unit tests for observability module."""
import pytest


class TestMetricsCollector:
    def test_initialization(self):
        from observability import MetricsCollector

        collector = MetricsCollector()
        assert collector is not None

    def test_get_metrics(self):
        from observability import MetricsCollector

        collector = MetricsCollector()
        metrics = collector.get_metrics()
        assert isinstance(metrics, bytes)


class TestAgentLogger:
    def test_initialization(self):
        from observability.logging import AgentLogger

        logger = AgentLogger("test_agent")
        assert logger.agent_name == "test_agent"

    def test_log_methods(self):
        from observability.logging import AgentLogger

        logger = AgentLogger("test_agent")
        logger.log_task_start("test_task", {"key": "value"})
        logger.log_task_complete("test_task", 100.0, {"result": "ok"})
