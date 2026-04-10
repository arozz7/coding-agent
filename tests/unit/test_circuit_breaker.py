"""Unit tests for circuit breaker."""
import pytest
import time
from unittest.mock import Mock

from llm.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerError,
    CircuitState,
    CircuitBreakerManager,
)


class TestCircuitBreaker:
    def test_initialization(self):
        cb = CircuitBreaker("test", failure_threshold=3, recovery_timeout=60)
        
        assert cb.name == "test"
        assert cb.failure_threshold == 3
        assert cb.state == CircuitState.CLOSED

    def test_success_resets_failure_count(self):
        cb = CircuitBreaker("test", failure_threshold=3)
        
        cb._failure_count = 2
        cb._on_success()
        
        assert cb._failure_count == 0

    def test_failure_opens_circuit(self):
        cb = CircuitBreaker("test", failure_threshold=3)
        
        cb._on_failure()
        assert cb.state == CircuitState.CLOSED
        
        cb._on_failure()
        assert cb.state == CircuitState.CLOSED
        
        cb._on_failure()
        assert cb.state == CircuitState.OPEN

    def test_call_when_open_raises_error(self):
        cb = CircuitBreaker("test", failure_threshold=2)
        
        cb._on_failure()
        cb._on_failure()
        
        with pytest.raises(CircuitBreakerError, match="is OPEN"):
            cb.call(lambda: "test")

    def test_call_succeeds_when_closed(self):
        cb = CircuitBreaker("test", failure_threshold=3)
        
        result = cb.call(lambda: "success")
        assert result == "success"

    def test_half_open_after_timeout(self):
        cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=1)
        
        cb._on_failure()
        cb._on_failure()
        assert cb.state == CircuitState.OPEN
        
        time.sleep(1.1)
        assert cb.state == CircuitState.HALF_OPEN

    def test_half_open_to_closed(self):
        cb = CircuitBreaker("test", failure_threshold=2, recovery_timeout=1, success_threshold=2)
        
        cb._on_failure()
        cb._on_failure()
        assert cb.state == CircuitState.OPEN
        
        time.sleep(1.1)
        assert cb.state == CircuitState.HALF_OPEN
        
        cb._on_success()
        assert cb.state == CircuitState.HALF_OPEN
        
        cb._on_success()
        assert cb.state == CircuitState.CLOSED

    def test_reset(self):
        cb = CircuitBreaker("test", failure_threshold=2)
        
        cb._on_failure()
        cb._on_failure()
        assert cb.state == CircuitState.OPEN
        
        cb.reset()
        assert cb.state == CircuitState.CLOSED
        assert cb._failure_count == 0

    def test_get_state(self):
        cb = CircuitBreaker("test", failure_threshold=3)
        
        state = cb.get_state()
        
        assert state["name"] == "test"
        assert state["state"] == "closed"
        assert state["failure_count"] == 0


@pytest.mark.asyncio
class TestCircuitBreakerAsync:
    async def test_call_async_succeeds(self):
        async def success_func():
            return "success"
        
        cb = CircuitBreaker("test", failure_threshold=3)
        result = await cb.call_async(success_func)
        assert result == "success"

    async def test_call_async_fails(self):
        async def fail_func():
            raise ValueError("test error")
        
        cb = CircuitBreaker("test", failure_threshold=2)
        
        with pytest.raises(ValueError):
            await cb.call_async(fail_func)
        
        assert cb.state == CircuitState.CLOSED
        
        with pytest.raises(ValueError):
            await cb.call_async(fail_func)
        
        assert cb.state == CircuitState.OPEN


class TestCircuitBreakerManager:
    def test_get_or_create_new(self):
        manager = CircuitBreakerManager()
        
        cb = manager.get_or_create("test_circuit", failure_threshold=5)
        
        assert cb is not None
        assert cb.name == "test_circuit"
        assert cb.failure_threshold == 5

    def test_get_or_create_existing(self):
        manager = CircuitBreakerManager()
        
        cb1 = manager.get_or_create("test_circuit")
        cb2 = manager.get_or_create("test_circuit")
        
        assert cb1 is cb2

    def test_get_all_states(self):
        manager = CircuitBreakerManager()
        
        manager.get_or_create("circuit1")
        manager.get_or_create("circuit2")
        
        states = manager.get_all_states()
        
        assert "circuit1" in states
        assert "circuit2" in states

    def test_reset_all(self):
        manager = CircuitBreakerManager()
        
        cb1 = manager.get_or_create("circuit1")
        cb2 = manager.get_or_create("circuit2")
        
        cb1._on_failure()
        cb1._on_failure()
        cb2._on_failure()
        cb2._on_failure()
        
        manager.reset_all()
        
        assert cb1.state == CircuitState.CLOSED
        assert cb2.state == CircuitState.CLOSED