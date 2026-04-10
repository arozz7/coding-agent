from typing import Optional, List, AsyncIterator
from pathlib import Path
import yaml
import structlog

from .config import ModelConfig
from .ollama_client import OllamaClient
from .cloud_api_client import CloudAPIClient
from .cost_tracker import CostTracker
from .rate_limiter import RateLimiter, RateLimitExceeded
from .health import HealthChecker, CircuitBreakerOpenError

logger = structlog.get_logger()


class ModelRouter:
    def __init__(self, config_path: str = "config/models.yaml"):
        self.configs: List[ModelConfig] = []
        self.config_by_name: dict[str, ModelConfig] = {}
        self.ollama = OllamaClient()
        self.cloud = CloudAPIClient()
        self.cost_tracker = CostTracker()
        self.rate_limiter = RateLimiter()
        self.health_checker = HealthChecker(self)
        self.logger = logger.bind(component="model_router")
        self._load_configs(config_path)

    def _configure_ollama_endpoint(self, config: ModelConfig) -> None:
        if config.endpoint and config.type == "local":
            self.ollama.set_base_url(config.endpoint)
            self.logger.info(
                "ollama_endpoint_configured",
                model=config.name,
                url=config.endpoint,
            )

    def _load_configs(self, path: str) -> None:
        config_file = Path(path)
        if not config_file.exists():
            self.logger.warning("config_not_found", path=path)
            return

        with open(config_file) as f:
            data = yaml.safe_load(f)

        for m in data.get("models", []):
            config = ModelConfig(**m)
            if config.api_key_env:
                import os
                config.api_key = os.environ.get(config.api_key_env)
            self.configs.append(config)
            self.config_by_name[config.name] = config
            self.rate_limiter.configure(config.name, config.rate_limit_rpm)
            if config.type == "local" and config.endpoint:
                self._configure_ollama_endpoint(config)

        self.logger.info("configs_loaded", count=len(self.configs))

    def get_model(self, purpose: str = "general") -> Optional[ModelConfig]:
        if purpose == "coding":
            for config in self.configs:
                if config.is_coding_optimized:
                    return config
        return self.configs[0] if self.configs else None

    def get_config(self, name: str) -> Optional[ModelConfig]:
        return self.config_by_name.get(name)

    async def generate(
        self,
        prompt: str,
        config: ModelConfig,
        max_retries: int = 3,
    ) -> str:
        await self.rate_limiter.acquire(config.name)

        for attempt in range(max_retries):
            try:
                if config.type == "local":
                    result = await self.ollama.generate(prompt, config.name)
                else:
                    result = await self.cloud.generate(prompt, config)

                self.cost_tracker.track_usage(config, prompt, result)
                self.health_checker.record_success(config.name)
                return result

            except RateLimitExceeded as e:
                self.logger.warning(
                    "rate_limit_exceeded",
                    model=config.name,
                    attempt=attempt,
                    wait=e.retry_after,
                )
                import asyncio
                await asyncio.sleep(e.retry_after)
                self.health_checker.record_rate_limit(config.name)

            except CircuitBreakerOpenError:
                self.logger.warning("circuit_breaker_open", model=config.name)
                raise

            except Exception as e:
                self.logger.error(
                    "llm_error",
                    model=config.name,
                    error=str(e),
                    attempt=attempt,
                )
                self.health_checker.record_failure(config.name)
                if attempt == max_retries - 1:
                    raise
                import asyncio
                await asyncio.sleep(2**attempt)

        raise LLMError(f"All {max_retries} retries exhausted")

    async def generate_stream(
        self,
        prompt: str,
        config: ModelConfig,
    ) -> AsyncIterator[str]:
        await self.rate_limiter.acquire(config.name)

        if config.type == "local":
            async for chunk in self.ollama.stream_generate(prompt, config.name):
                yield chunk
        else:
            async for chunk in self.cloud.stream_generate(prompt, config):
                yield chunk

    async def health_check(self, config: ModelConfig) -> bool:
        return await self.health_checker.check(config)

    def get_cost_summary(self) -> dict:
        return self.cost_tracker.get_summary()

    def get_healthy_models(self) -> list[str]:
        return self.health_checker.get_healthy_models()


class LLMError(Exception):
    pass
