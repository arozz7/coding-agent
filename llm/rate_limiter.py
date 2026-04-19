import asyncio
from typing import Dict, Optional
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from collections import defaultdict
import structlog

logger = structlog.get_logger()


class RateLimitExceeded(Exception):
    def __init__(self, message: str, retry_after: float):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class TokenBucket:
    tokens: float
    refill_rate: float
    last_refill: datetime
    capacity: float

    def _refill(self) -> None:
        now = datetime.now(timezone.utc)
        elapsed = (now - self.last_refill).total_seconds()
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    async def acquire(self, requested: float = 1.0) -> bool:
        while True:
            self._refill()
            if self.tokens >= requested:
                self.tokens -= requested
                return True
            wait_time = (requested - self.tokens) / self.refill_rate
            logger.debug(
                "rate_limit_waiting",
                wait_seconds=round(wait_time, 2),
                available_tokens=round(self.tokens, 2),
            )
            await asyncio.sleep(min(wait_time, 1.0))


class RateLimiter:
    def __init__(self):
        self.buckets: Dict[str, TokenBucket] = {}
        self.rpm_config: Dict[str, int] = {}
        self.logger = logger.bind(component="rate_limiter")

    def configure(self, model: str, rpm: int) -> None:
        self.rpm_config[model] = rpm
        refill_rate = rpm / 60.0
        capacity = rpm * 1.5
        self.buckets[model] = TokenBucket(
            tokens=capacity,
            refill_rate=refill_rate,
            last_refill=datetime.now(timezone.utc),
            capacity=capacity,
        )
        self.logger.info("rate_limit_configured", model=model, rpm=rpm)

    async def acquire(self, model: str) -> None:
        if model not in self.buckets:
            rpm = self.rpm_config.get(model, 60)
            self.configure(model, rpm)

        bucket = self.buckets[model]
        await bucket.acquire(1.0)

    def get_remaining(self, model: str) -> int:
        if model not in self.buckets:
            return self.rpm_config.get(model, 60)
        bucket = self.buckets[model]
        bucket._refill()
        return max(0, int(bucket.tokens))

    def get_status(self, model: str) -> dict:
        if model not in self.buckets:
            return {"configured": False}
        bucket = self.buckets[model]
        return {
            "configured": True,
            "available_tokens": round(bucket.tokens, 2),
            "requests_per_minute": self.rpm_config.get(model, 60),
        }
