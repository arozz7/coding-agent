import structlog
import logging
from structlog.processors import JSONRenderer
from structlog.stdlib import add_log_level


def configure_logging(log_level: str = "INFO", json_format: bool = True):
    processors = [
        structlog.contextvars.merge_contextvars,
        add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if json_format:
        processors.append(JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    log_level_value = getattr(logging, log_level.upper(), logging.INFO)

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(log_level_value),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


class AgentLogger:
    def __init__(self, agent_name: str):
        self.logger = structlog.get_logger(agent_name)
        self.agent_name = agent_name

    def log_task_start(self, task: str, context: dict = None) -> None:
        self.logger.info("task_started", task=task, context=context or {})

    def log_task_complete(
        self, task: str, duration_ms: float, result: dict = None
    ) -> None:
        self.logger.info(
            "task_completed",
            task=task,
            duration_ms=round(duration_ms, 2),
            result=result or {},
        )

    def log_llm_call(
        self,
        model: str,
        prompt_length: int,
        response_length: int,
        latency_ms: float,
    ) -> None:
        # prompt_length / response_length may arrive as character counts (int)
        # or as pre-computed token counts. Estimate tokens only from strings.
        def _to_tokens(val: int) -> int:
            return val // 4 if isinstance(val, int) else len(str(val)) // 4

        self.logger.info(
            "llm_call",
            model=model,
            prompt_tokens=_to_tokens(prompt_length),
            response_tokens=_to_tokens(response_length),
            latency_ms=round(latency_ms, 2),
        )

    def log_tool_call(
        self,
        tool: str,
        args: dict,
        success: bool,
        duration_ms: float,
    ) -> None:
        self.logger.info(
            "tool_call",
            tool=tool,
            args=args,
            success=success,
            duration_ms=round(duration_ms, 2),
        )

    def log_error(self, error: Exception, context: dict = None) -> None:
        self.logger.error(
            "error_occurred",
            error_type=type(error).__name__,
            error_message=str(error),
            context=context or {},
        )
