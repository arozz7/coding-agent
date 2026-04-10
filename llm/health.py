import asyncio
from dataclasses import dataclass, field
from typing import Dict, Optional
from datetime import datetime, timedelta
from enum import Enum
from collections import defaultdict
import structlog

logger = structlog.get_logger()


class CircuitState(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreakerOpenError(Exception):
    pass


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
        self.circuit_states: Dict[str, CircuitState] = defaultdict(
            lambda: CircuitState.CLOSED
        )
        self.failure_counts: Dict[str, int] = defaultdict(int)
        self.logger = logger.bind(component="health_checker")

    async def check(self, config) -> bool:
        model = config.name
        start = datetime.utcnow()

        try:
            if config.type == "local":
                available = await self.router.ollama.health_check(config.name)
            else:
                available = await self.router.cloud.health_check(config.endpoint)

            latency = (datetime.utcnow() - start).total_seconds() * 1000
            self._record_success(model, latency)
            self.statuses[model] = HealthStatus(
                model=model,
                available=True,
                last_check=datetime.utcnow(),
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
                last_check=datetime.utcnow(),
                success_rate=self._calculate_success_rate(model),
                avg_latency_ms=self._calculate_avg_latency(model),
                consecutive_failures=self.failure_counts.get(model, 0),
            )
            self.logger.error(
                "health_check_failed", model=model, error=str(e)
            )
            return False

    def _record_success(self, model: str, latency_ms: float) -> None:
        now = datetime.utcnow()
        if model not in self.successes:
            self.successes[model] = []
        self.successes[model].append((now, latency_ms))
        self.successes[model] = [
            (t, l) for t, l in self.successes[model]
            if now - t < timedelta(hours=1)
        ]
        if model in self.circuit_states:
            if self.circuit_states[model] == CircuitState.HALF_OPEN:
                self.circuit_states[model] = CircuitState.CLOSED
                self.failure_counts[model] = 0
                self.logger.info("circuit_breaker_closed", model=model)

    def _record_failure(self, model: str) -> None:
        self.failure_counts[model] += 1
        if model not in self.failures:
            self.failures[model] = []
        now = datetime.utcnow()
        self.failures[model].append(now)
        self.failures[model] = [
            t for t in self.failures[model] if now - t < timedelta(hours=1)
        ]

        if self.failure_counts[model] >= 5:
            self.circuit_states[model] = CircuitState.OPEN
            self.logger.warning(
                "circuit_breaker_opened",
                model=model,
                failure_count=self.failure_counts[model],
            )

    def record_success(self, model: str) -> None:
        self._record_success(model, 0)

    def record_rate_limit(self, model: str) -> None:
        self._record_failure(model)

    def record_failure(self, model: str) -> None:
        self._record_failure(model)
        if self.circuit_states[model] == CircuitState.HALF_OPEN:
            self.circuit_states[model] = CircuitState.OPEN
            self.logger.warning("circuit_breaker_opened", model=model)

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
            and self.failure_counts.get(name, 0) < 3
            and self.circuit_states.get(name) != CircuitState.OPEN
        ]

    def check_circuit_breaker(self, model: str) -> None:
        state = self.circuit_states.get(model, CircuitState.CLOSED)
        if state == CircuitState.OPEN:
            if self.failure_counts[model] >= 5:
                self.circuit_states[model] = CircuitState.HALF_OPEN
                self.logger.info("circuit_breaker_half_open", model=model)
            else:
                raise CircuitBreakerOpenError(
                    f"Circuit breaker for '{model}' is open"
                )
