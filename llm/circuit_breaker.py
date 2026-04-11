from enum import Enum
from datetime import datetime, timedelta
from typing import Optional
import time
import structlog

logger = structlog.get_logger()


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerError(Exception):
    pass


# Alias so callers that expect CircuitBreakerOpenError work without change
CircuitBreakerOpenError = CircuitBreakerError


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        success_threshold: int = 2,
    ):
        self.name = name
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.success_threshold = success_threshold
        
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: Optional[datetime] = None
        self._opened_at: Optional[float] = None
        
        self.logger = logger.bind(component="circuit_breaker", name=name)
    
    @property
    def state(self) -> CircuitState:
        if self._state == CircuitState.OPEN and self._should_attempt_reset():
            self._state = CircuitState.HALF_OPEN
            self.logger.info("circuit_half_open", name=self.name)
        return self._state
    
    def _should_attempt_reset(self) -> bool:
        if self._opened_at is None:
            return True
        return time.time() - self._opened_at >= self.recovery_timeout
    
    def call(self, func, *args, **kwargs):
        if self.state == CircuitState.OPEN:
            raise CircuitBreakerError(f"Circuit {self.name} is OPEN")
        
        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise
    
    async def call_async(self, func, *args, **kwargs):
        import asyncio
        
        if self.state == CircuitState.OPEN:
            raise CircuitBreakerError(f"Circuit {self.name} is OPEN")
        
        try:
            result = await func(*args, **kwargs)
            self._on_success()
            return result
        except Exception as e:
            self._on_failure()
            raise
    
    def _on_success(self) -> None:
        if self._state == CircuitState.HALF_OPEN:
            self._success_count += 1
            if self._success_count >= self.success_threshold:
                self._state = CircuitState.CLOSED
                self._failure_count = 0
                self._success_count = 0
                self._opened_at = None
                self.logger.info("circuit_closed", name=self.name)
        else:
            self._failure_count = 0
    
    def _on_failure(self) -> None:
        self._failure_count += 1
        self._last_failure_time = datetime.utcnow()
        
        if self._state == CircuitState.HALF_OPEN:
            self._state = CircuitState.OPEN
            self._opened_at = time.time()
            self.logger.warning("circuit_reopened", name=self.name)
        elif self._failure_count >= self.failure_threshold:
            self._state = CircuitState.OPEN
            self._opened_at = time.time()
            self.logger.warning("circuit_opened", name=self.name, failures=self._failure_count)
    
    def reset(self) -> None:
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time = None
        self._opened_at = None
        self.logger.info("circuit_reset", name=self.name)
    
    def get_state(self) -> dict:
        return {
            "name": self.name,
            "state": self.state.value,
            "failure_count": self._failure_count,
            "success_count": self._success_count,
            "last_failure": self._last_failure_time.isoformat() if self._last_failure_time else None,
        }


class CircuitBreakerManager:
    def __init__(self):
        self._breakers: dict[str, CircuitBreaker] = {}
        self.logger = logger.bind(component="circuit_breaker_manager")
    
    def get_or_create(
        self,
        name: str,
        failure_threshold: int = 5,
        recovery_timeout: int = 60,
        success_threshold: int = 2,
    ) -> CircuitBreaker:
        if name not in self._breakers:
            self._breakers[name] = CircuitBreaker(
                name=name,
                failure_threshold=failure_threshold,
                recovery_timeout=recovery_timeout,
                success_threshold=success_threshold,
            )
            self.logger.info("circuit_breaker_created", name=name)
        return self._breakers[name]
    
    def get_all_states(self) -> dict:
        return {name: breaker.get_state() for name, breaker in self._breakers.items()}
    
    def reset_all(self) -> None:
        for breaker in self._breakers.values():
            breaker.reset()
        self.logger.info("all_circuits_reset")