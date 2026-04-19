import asyncio
from dataclasses import dataclass
from typing import Dict, Optional
from datetime import datetime, timezone, timedelta
import structlog

from .circuit_breaker import (
    CircuitState,
    CircuitBreakerError,
    CircuitBreakerManager,
)

logger = structlog.get_logger()

# Re-export alias so existing imports from this module keep working
CircuitBreakerOpenError = CircuitBreakerError


@dataclass
class HealthStatus:
    model: str
    available: bool
    last_check: datetime
    success_rate: float
    avg_latency_ms: float
    consecutive_failures: int


class HealthChecker:
    def __init__(self, router):
        self.router = router
        self.statuses: Dict[str, HealthStatus] = {}
        self.successes: Dict[str, list] = {}
        self.failures: Dict[str, list] = {}
        self._cb_manager = CircuitBreakerManager()
        self.logger = logger.bind(component="health_checker")

    async def check(self, config) -> bool:
        model = config.name
        start = datetime.now(timezone.utc)
        cb = self._cb_manager.get_or_create(model)

        try:
            if config.type == "local":
                available = await self.router.ollama.health_check(config.name)
            else:
                available = await self.router.cloud.health_check(config.endpoint)

            latency = (datetime.now(timezone.utc) - start).total_seconds() * 1000
            self._record_success(model, latency)
            self.statuses[model] = HealthStatus(
                model=model,
                available=True,
                last_check=datetime.now(timezone.utc),
                success_rate=self._calculate_success_rate(model),
                avg_latency_ms=self._calculate_avg_latency(model),
                consecutive_failures=0,
            )
            return True

        except Exception as e:
            self._record_failure(model)
            self.statuses[model] = HealthStatus(
                model=model,
                available=False,
                last_check=datetime.now(timezone.utc),
                success_rate=self._calculate_success_rate(model),
                avg_latency_ms=self._calculate_avg_latency(model),
                consecutive_failures=cb._failure_count,
            )
            self.logger.error("health_check_failed", model=model, error=str(e))
            return False

    def _record_success(self, model: str, latency_ms: float) -> None:
        now = datetime.now(timezone.utc)
        if model not in self.successes:
            self.successes[model] = []
        self.successes[model].append((now, latency_ms))
        self.successes[model] = [
            (t, l) for t, l in self.successes[model]
            if now - t < timedelta(hours=1)
        ]
        cb = self._cb_manager.get_or_create(model)
        cb._on_success()

    def _record_failure(self, model: str) -> None:
        now = datetime.now(timezone.utc)
        if model not in self.failures:
            self.failures[model] = []
        self.failures[model].append(now)
        self.failures[model] = [
            t for t in self.failures[model] if now - t < timedelta(hours=1)
        ]
        cb = self._cb_manager.get_or_create(model)
        cb._on_failure()

    def record_success(self, model: str) -> None:
        self._record_success(model, 0)

    def record_rate_limit(self, model: str) -> None:
        self._record_failure(model)

    def record_failure(self, model: str) -> None:
        self._record_failure(model)

    def _calculate_success_rate(self, model: str) -> float:
        successes = len(self.successes.get(model, []))
        failures = len(self.failures.get(model, []))
        total = successes + failures
        return successes / total if total > 0 else 1.0

    def _calculate_avg_latency(self, model: str) -> float:
        latencies = [l for _, l in self.successes.get(model, []) if l > 0]
        return sum(latencies) / len(latencies) if latencies else 0.0

    def get_healthy_models(self) -> list[str]:
        return [
            name
            for name, status in self.statuses.items()
            if status.available
            and self._cb_manager.get_or_create(name)._failure_count < 3
            and self._cb_manager.get_or_create(name).state != CircuitState.OPEN
        ]

    def check_circuit_breaker(self, model: str) -> None:
        cb = self._cb_manager.get_or_create(model)
        # CircuitBreaker.state auto-transitions OPEN → HALF_OPEN when recovery_timeout elapses
        if cb.state == CircuitState.OPEN:
            raise CircuitBreakerOpenError(
                f"Circuit breaker for '{model}' is open"
            )
