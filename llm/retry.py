import asyncio
from typing import TypeVar, Callable, Optional
from dataclasses import dataclass
from enum import Enum
import structlog

logger = structlog.get_logger()

T = TypeVar("T")


class RetryStrategy(Enum):
    EXPONENTIAL = "exponential"
    LINEAR = "linear"
    CONSTANT = "constant"


@dataclass
class RetryConfig:
    max_attempts: int = 3
    initial_delay: float = 1.0
    max_delay: float = 60.0
    multiplier: float = 2.0
    strategy: RetryStrategy = RetryStrategy.EXPONENTIAL
    retriable_exceptions: tuple = (Exception,)


async def retry_with_backoff(
    func: Callable,
    config: Optional[RetryConfig] = None,
    *args,
    **kwargs,
) -> T:
    config = config or RetryConfig()
    last_exception = None

    for attempt in range(config.max_attempts):
        try:
            result = await func(*args, **kwargs)
            if attempt > 0:
                logger.info(
                    "retry_succeeded",
                    attempt=attempt,
                    func=getattr(func, "__name__", str(func)),
                )
            return result

        except config.retriable_exceptions as e:
            last_exception = e
            delay = _calculate_delay(attempt, config)
            logger.warning(
                "retry_attempt",
                attempt=attempt + 1,
                max_attempts=config.max_attempts,
                delay=delay,
                error=str(e),
            )
            await asyncio.sleep(delay)

        except Exception as e:
            logger.error(
                "non_retriable_error",
                error=str(e),
                func=getattr(func, "__name__", str(func)),
            )
            raise

    raise last_exception


def _calculate_delay(attempt: int, config: RetryConfig) -> float:
    if config.strategy == RetryStrategy.EXPONENTIAL:
        delay = config.initial_delay * (config.multiplier**attempt)
    elif config.strategy == RetryStrategy.LINEAR:
        delay = config.initial_delay * (attempt + 1)
    else:
        delay = config.initial_delay

    return min(delay, config.max_delay)
