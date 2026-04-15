import os
import re
from typing import Optional, List, AsyncIterator
from pathlib import Path
import yaml
import structlog

from .config import ModelConfig
from .ollama_client import OllamaClient, ModelNotReadyError
from .cloud_api_client import CloudAPIClient, _OpenRouterRateLimitError
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
        """Expand ${VAR} and ${VAR:-default} references using os.environ.

        Syntax:
          ${VAR}          — replaced by env value; left as-is if unset
          ${VAR:-default} — replaced by env value; falls back to *default* if unset

        Using ${VAR:-default} in config files means the system works with no
        .env file — the explicit default is used and no URL stays unexpanded.
        """
        if not value or "${" not in value:
            return value

        def _replacer(m: re.Match) -> str:
            spec = m.group(1)
            if ":-" in spec:
                var, default = spec.split(":-", 1)
                return os.environ.get(var.strip(), default)
            return os.environ.get(spec, m.group(0))  # leave placeholder if unset

        return re.sub(r"\$\{([^}]+)\}", _replacer, value)

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

    def _get_local_fallback(self, exclude_name: str) -> Optional[ModelConfig]:
        """Return the first healthy local model that is not *exclude_name*."""
        for cfg in self.configs:
            if cfg.type == "local" and cfg.name != exclude_name:
                return cfg
        # If nothing else, use any local model (even the same name — better than failing)
        for cfg in self.configs:
            if cfg.type == "local":
                return cfg
        return None

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        """Return True if *exc* is a 429 response from a remote API."""
        msg = str(exc)
        return "429" in msg and ("Too Many Requests" in msg or "rate" in msg.lower())

    # How long to wait between retries when the local model is not loaded.
    # A 35B model at 3-bit can take 3–8 minutes to reload from disk.
    # Three attempts × 120 s each = up to ~6 minutes of patience before
    # we give up and try a fallback model.
    _MODEL_RELOAD_WAIT_SECS = 120
    _MODEL_RELOAD_MAX_RETRIES = 3

    async def generate(
        self,
        prompt: str,
        config: ModelConfig,
        max_retries: int = 3,
        _is_fallback: bool = False,
        timeout: float = 600.0,
    ) -> str:
        import asyncio
        await self.rate_limiter.acquire(config.name)

        model_reload_attempts = 0

        for attempt in range(max_retries):
            try:
                if config.type == "local":
                    result = await self.ollama.generate(
                        prompt,
                        config.name,
                        enable_thinking=config.enable_thinking,
                        timeout=timeout,
                    )
                else:
                    result = await self.cloud.generate(prompt, config)

                self.cost_tracker.track_usage(config, prompt, result)
                self.health_checker.record_success(config.name)
                return result

            except ModelNotReadyError as e:
                # The local model was evicted (TTL expiry) and LM Studio hasn't
                # reloaded it yet.  Use a long wait so the model has time to
                # re-initialise before we retry.
                model_reload_attempts += 1
                self.health_checker.record_failure(config.name)

                if model_reload_attempts <= self._MODEL_RELOAD_MAX_RETRIES:
                    self.logger.warning(
                        "model_not_ready_waiting",
                        model=config.name,
                        reload_attempt=model_reload_attempts,
                        wait_secs=self._MODEL_RELOAD_WAIT_SECS,
                        error=str(e)[:120],
                    )
                    await asyncio.sleep(self._MODEL_RELOAD_WAIT_SECS)
                    # Reset the fast-retry loop counter so we get a fresh set
                    # of attempts after the wait.
                    attempt = 0  # noqa: PLW2901 — intentional loop-var reset
                    continue

                # Reload retries exhausted — try a fallback model.
                if not _is_fallback:
                    fallback = self._get_local_fallback(config.name)
                    if fallback:
                        self.logger.warning(
                            "model_reload_exhausted_using_fallback",
                            primary=config.name,
                            fallback=fallback.name,
                        )
                        return await self.generate(
                            prompt, fallback, max_retries=max_retries, _is_fallback=True, timeout=timeout
                        )
                raise LLMError(
                    f"Model {config.name!r} failed to reload after "
                    f"{self._MODEL_RELOAD_MAX_RETRIES} attempts and no fallback available"
                ) from e

            except RateLimitExceeded as e:
                self.logger.warning(
                    "rate_limit_exceeded",
                    model=config.name,
                    attempt=attempt,
                    wait=e.retry_after,
                )
                await asyncio.sleep(e.retry_after)
                self.health_checker.record_rate_limit(config.name)

            except CircuitBreakerOpenError:
                self.logger.warning("circuit_breaker_open", model=config.name)
                raise

            except _OpenRouterRateLimitError as e:
                # OpenRouter 429 — respect Retry-After, then fall back to local
                self.logger.warning(
                    "openrouter_rate_limited",
                    model=config.name,
                    retry_after=e.retry_after,
                )
                self.health_checker.record_rate_limit(config.name)
                if not _is_fallback:
                    fallback = self._get_local_fallback(config.name)
                    if fallback:
                        self.logger.info("using_local_fallback", fallback=fallback.name)
                        return await self.generate(prompt, fallback, max_retries=max_retries, _is_fallback=True)
                raise LLMError(f"OpenRouter model {config.name!r} is rate-limited and no local fallback available") from e

            except Exception as e:
                # Generic 429 string match (other remote providers)
                if self._is_rate_limit_error(e) and config.type != "local" and not _is_fallback:
                    self.logger.warning(
                        "remote_rate_limited_falling_back",
                        model=config.name,
                        error=str(e)[:120],
                    )
                    self.health_checker.record_rate_limit(config.name)
                    fallback = self._get_local_fallback(config.name)
                    if fallback:
                        self.logger.info("using_local_fallback", fallback=fallback.name)
                        return await self.generate(prompt, fallback, max_retries=max_retries, _is_fallback=True)
                    raise LLMError(f"Remote model {config.name!r} is rate-limited and no local fallback available") from e

                self.logger.error(
                    "llm_error",
                    model=config.name,
                    error=str(e),
                    attempt=attempt,
                )
                self.health_checker.record_failure(config.name)
                if attempt == max_retries - 1:
                    raise
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
