from prometheus_client import Counter, Histogram, Gauge, Info, generate_latest
from functools import wraps
import time
import structlog

logger = structlog.get_logger()

agent_requests_total = Counter(
    "agent_requests_total",
    "Total number of agent requests",
    ["agent", "status", "model"],
)

llm_requests_total = Counter(
    "llm_requests_total",
    "Total LLM API requests",
    ["model", "type", "status"],
)

tool_calls_total = Counter(
    "tool_calls_total",
    "Total tool invocations",
    ["tool", "status"],
)

request_duration_seconds = Histogram(
    "agent_request_duration_seconds",
    "Request duration in seconds",
    ["agent", "operation"],
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
)

llm_latency_seconds = Histogram(
    "llm_latency_seconds",
    "LLM response latency",
    ["model", "type"],
    buckets=(0.5, 1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 120.0),
)

active_sessions = Gauge(
    "agent_active_sessions",
    "Number of active sessions",
)

model_health_status = Gauge(
    "model_health_status",
    "Model health (1=healthy, 0=unhealthy)",
    ["model"],
)

tokens_used_total = Counter(
    "tokens_used_total",
    "Total tokens used",
    ["model", "type"],
)

cost_total_dollars = Gauge(
    "agent_cost_total_dollars",
    "Total API cost in dollars",
)

system_info = Info(
    "agent_system",
    "Agent system information",
)


def track_request(agent_name: str, operation: str):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            start = time.time()
            status = "success"
            try:
                result = await func(*args, **kwargs)
                return result
            except Exception as e:
                status = "error"
                raise
            finally:
                duration = time.time() - start
                request_duration_seconds.labels(
                    agent=agent_name, operation=operation
                ).observe(duration)
                agent_requests_total.labels(
                    agent=agent_name,
                    status=status,
                    model="unknown",
                ).inc()

        return wrapper

    return decorator


def track_llm_request(model: str, request_type: str):
    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            start = time.time()
            status = "success"
            try:
                result = await func(*args, **kwargs)
                return result
            except Exception as e:
                status = "error"
                raise
            finally:
                duration = time.time() - start
                llm_latency_seconds.labels(
                    model=model, type=request_type
                ).observe(duration)
                llm_requests_total.labels(
                    model=model, type=request_type, status=status
                ).inc()

        return wrapper

    return decorator


class MetricsCollector:
    def __init__(self):
        self.logger = logger.bind(component="metrics")

    def record_cost(self, model: str, cost: float) -> None:
        cost_total_dollars.inc(cost)
        self.logger.debug("cost_recorded", model=model, cost=cost)

    def record_tokens(self, model: str, token_type: str, count: int) -> None:
        tokens_used_total.labels(model=model, type=token_type).inc(count)

    def update_model_health(self, model: str, healthy: bool) -> None:
        model_health_status.labels(model=model).set(1 if healthy else 0)

    def initialize_system_info(self, version: str, config: dict) -> None:
        system_info.info(
            {
                "version": version,
                "python_version": config.get("python_version", "unknown"),
                "models_configured": str(len(config.get("models", []))),
            }
        )

    def get_metrics(self) -> bytes:
        return generate_latest()
