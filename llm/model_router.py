import os
import re
from typing import Optional, List, AsyncIterator
from pathlib import Path
import yaml
import structlog

from .config import ModelConfig
from .ollama_client import OllamaClient
from .cloud_api_client import CloudAPIClient
from .cost_tracker import CostTracker
from .rate_limiter import RateLimiter, RateLimitExceeded
from .health import HealthChecker
from .circuit_breaker import CircuitBreakerOpenError

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
        self._defaults: dict = {}
        self._active_model_name: Optional[str] = None
        self._load_configs(config_path)

    def _configure_ollama_endpoint(self, config: ModelConfig) -> None:
        if config.endpoint and config.type == "local":
            self.ollama.set_base_url(config.endpoint)
            self.logger.info(
                "ollama_endpoint_configured",
                model=config.name,
                url=config.endpoint,
            )

    @staticmethod
    def _expand_env(value: Optional[str]) -> Optional[str]:
        """Expand ${VAR} references in a string using os.environ.

        Unknown variables are left as-is so misconfigured names are visible
        in logs rather than silently becoming empty strings.
        """
        if not value or "${" not in value:
            return value
        return re.sub(
            r"\$\{([^}]+)\}",
            lambda m: os.environ.get(m.group(1), m.group(0)),
            value,
        )

    def _load_configs(self, path: str) -> None:
        config_file = Path(path)
        if not config_file.exists():
            self.logger.error(
                "config_not_found",
                path=str(config_file.resolve()),
                hint="Check that config/models.yaml exists at the project root",
            )
            return

        with open(config_file) as f:
            data = yaml.safe_load(f)

        self._defaults = data.get("defaults", {})

        for m in data.get("models", []):
            try:
                # Expand ${ENV_VAR} references before constructing the config
                m_expanded = {
                    k: (self._expand_env(v) if isinstance(v, str) else v)
                    for k, v in m.items()
                }
                config = ModelConfig(**m_expanded)
            except Exception as e:
                self.logger.error("model_config_invalid", entry=m, error=str(e))
                continue
            if config.api_key_env:
                config.api_key = os.environ.get(config.api_key_env)
            self.configs.append(config)
            self.config_by_name[config.name] = config
            self.rate_limiter.configure(config.name, config.rate_limit_rpm)
            if config.type == "local" and config.endpoint:
                self._configure_ollama_endpoint(config)

        # Honour the defaults.coding_model setting as the initial active model
        default_name = self._defaults.get("coding_model")
        if default_name and default_name in self.config_by_name:
            self._active_model_name = default_name

        self.logger.info(
            "configs_loaded",
            count=len(self.configs),
            active=self._active_model_name,
            path=str(config_file.resolve()),
        )

    def get_model(self, purpose: str = "general") -> Optional[ModelConfig]:
        """Return the model to use for *purpose*.

        Priority:
          1. Explicitly set active model (_active_model_name)
          2. Default from models.yaml [defaults] section for this purpose
          3. First model with is_coding_optimized = true (for coding purposes)
          4. First model in the list
        """
        if not self.configs:
            return None

        # 1. Explicit active model
        if self._active_model_name and self._active_model_name in self.config_by_name:
            return self.config_by_name[self._active_model_name]

        # 2. Purpose-specific default from YAML
        purpose_key = f"{purpose}_model"
        default_name = self._defaults.get(purpose_key)
        if default_name and default_name in self.config_by_name:
            return self.config_by_name[default_name]

        # 3. First coding-optimized model for coding purposes
        if purpose == "coding":
            for config in self.configs:
                if config.is_coding_optimized:
                    return config

        # 4. First available
        return self.configs[0]

    def get_config(self, name: str) -> Optional[ModelConfig]:
        return self.config_by_name.get(name)

    def set_active_model(self, name: str) -> ModelConfig:
        """Set the model that will be used for all requests until changed.

        Raises ValueError if *name* is not in the loaded config.
        """
        if name not in self.config_by_name:
            available = list(self.config_by_name.keys())
            raise ValueError(f"Unknown model '{name}'. Available: {available}")
        self._active_model_name = name
        config = self.config_by_name[name]
        if config.type == "local" and config.endpoint:
            self._configure_ollama_endpoint(config)
        self.logger.info("active_model_changed", model=name)
        return config

    def get_active_model_name(self) -> Optional[str]:
        """Return the name of the currently active model, or None if using defaults."""
        return self._active_model_name

    def clear_active_model(self) -> None:
        """Revert to the default model selection from models.yaml."""
        self._active_model_name = self._defaults.get("coding_model")
        self.logger.info("active_model_reset", model=self._active_model_name)

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
