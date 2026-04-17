import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional, List, AsyncIterator, Callable
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


@dataclass
class ModelSwitchEvent:
    """Emitted whenever the router gives up on a model and uses a fallback."""
    from_model: str
    to_model: str
    reason: str          # "load_timeout" | "load_failed" | "circuit_open" | "reload_exhausted"
    task_id: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.utcnow)


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
        # local_runtime defaults — overridden by models.yaml [local_runtime] section
        self._local_runtime: dict = {
            "single_model_only": True,
            "load_timeout_secs": 300,
            "load_poll_interval_secs": 10,
            "max_load_attempts": 2,
        }
        # Registered callbacks, fired on every model switch (local→fallback).
        self._switch_callbacks: list[Callable[[ModelSwitchEvent], None]] = []
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
        # Merge local_runtime overrides from YAML (nested under defaults)
        lr = self._defaults.get("local_runtime", {})
        if lr:
            self._local_runtime.update(lr)

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

    def register_switch_callback(self, fn: Callable[[ModelSwitchEvent], None]) -> None:
        """Register a callback fired whenever the router switches to a fallback model.

        The callback receives a :class:`ModelSwitchEvent` describing the switch.
        It is called synchronously inside the async generate loop, so it must be
        a plain (non-async) function — or a coroutine scheduled with
        ``asyncio.create_task`` inside the callback body.
        """
        self._switch_callbacks.append(fn)

    def _fire_switch_event(self, event: ModelSwitchEvent) -> None:
        """Call all registered switch callbacks, swallowing exceptions."""
        for fn in self._switch_callbacks:
            try:
                fn(event)
            except Exception as e:
                self.logger.warning("switch_callback_error", error=str(e))

    def _get_fallback_chain(self, exclude_name: str) -> list[ModelConfig]:
        """Return ordered fallback candidates: other locals first, then remotes.

        Excludes *exclude_name* from the list.  Remotes are only included when
        an api_key is configured (or api_key_env is set and the var is present).
        """
        locals_: list[ModelConfig] = []
        remotes: list[ModelConfig] = []
        for cfg in self.configs:
            if cfg.name == exclude_name:
                continue
            if cfg.type == "local":
                locals_.append(cfg)
            elif cfg.type == "remote":
                # Only include remotes that have a usable API key
                has_key = bool(cfg.api_key) or (
                    bool(cfg.api_key_env) and bool(os.environ.get(cfg.api_key_env or ""))
                )
                if has_key:
                    remotes.append(cfg)
        return locals_ + remotes

    async def _ensure_single_local_model(self, config: ModelConfig) -> None:
        """Unload any other loaded local models before loading *config*.

        Only runs when ``single_model_only`` is True in local_runtime config
        and the provider is 'lmstudio' (we can only programmatically unload via
        the LM Studio API).
        """
        if not self._local_runtime.get("single_model_only"):
            return
        if config.provider != "lmstudio":
            return
        try:
            loaded = await self.ollama.get_loaded_local_models()
            for model_id in loaded:
                if model_id != config.name:
                    self.logger.info(
                        "unloading_other_model",
                        model=model_id,
                        reason="single_model_only",
                    )
                    await self.ollama.unload_model(model_id)
        except Exception as e:
            self.logger.warning("ensure_single_model_error", error=str(e))

    async def _try_load_lmstudio_model(self, config: ModelConfig) -> bool:
        """Unload others (if single_model_only), trigger load, then poll until ready.

        Returns True when the model becomes loaded within the configured timeout.
        """
        await self._ensure_single_local_model(config)
        accepted = await self.ollama.load_model(config.name)
        if not accepted:
            return False
        return await self.ollama.poll_until_loaded(
            config.name,
            timeout=float(self._local_runtime.get("load_timeout_secs", 300)),
            interval=float(self._local_runtime.get("load_poll_interval_secs", 10)),
        )

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        """Return True if *exc* is a 429 response from a remote API."""
        msg = str(exc)
        return "429" in msg and ("Too Many Requests" in msg or "rate" in msg.lower())

    # Fallback wait used for non-LM Studio local backends (ollama, llama_cpp)
    # that don't support programmatic load.  A 35B model can take 3–8 min to
    # load, so we give 120 s between blind retries.
    _MODEL_RELOAD_WAIT_SECS = 120

    async def generate(
        self,
        prompt: str,
        config: ModelConfig,
        max_retries: int = 3,
        _is_fallback: bool = False,
        _fallback_chain: Optional[list] = None,
        timeout: float = 600.0,
        enable_thinking: bool | None = None,
        system_prompt: Optional[str] = None,
    ) -> str:
        """Generate a completion.

        ``enable_thinking`` overrides the per-model ``enable_thinking`` setting
        in models.yaml for this single call.  Pass ``False`` for lightweight
        classification calls where a thinking trace is wasteful (e.g. the task
        type classifier that only needs one word back).  Leave as ``None``
        (default) to use the model's own setting.
        """
        import asyncio
        await self.rate_limiter.acquire(config.name)

        # Resolve effective enable_thinking: call-site override wins, then
        # per-model config, then None (let the model decide).
        effective_thinking = enable_thinking if enable_thinking is not None else config.enable_thinking

        # Track how many times we've tried to load this specific model.
        model_load_attempts = 0
        max_load_attempts = int(self._local_runtime.get("max_load_attempts", 2))

        for attempt in range(max_retries):
            try:
                if config.type == "local":
                    result = await self.ollama.generate(
                        prompt,
                        config.name,
                        system_prompt=system_prompt,
                        enable_thinking=effective_thinking,
                        timeout=timeout,
                    )
                else:
                    result = await self.cloud.generate(prompt, config, system_prompt=system_prompt)

                self.cost_tracker.track_usage(config, prompt, result)
                self.health_checker.record_success(config.name)
                return result

            except ModelNotReadyError as e:
                self.health_checker.record_failure(config.name)
                model_load_attempts += 1

                if config.provider == "lmstudio" and model_load_attempts <= max_load_attempts:
                    # Use the LM Studio API to actively load the model, then poll.
                    self.logger.warning(
                        "model_not_ready_loading",
                        model=config.name,
                        load_attempt=model_load_attempts,
                        error=str(e)[:120],
                    )
                    loaded = await self._try_load_lmstudio_model(config)
                    if loaded:
                        self.logger.info("model_loaded_retrying", model=config.name)
                        attempt = 0  # noqa: PLW2901 — intentional loop-var reset
                        continue
                    # Load triggered but timed out — count as a failed attempt
                    self.logger.warning(
                        "model_load_timeout",
                        model=config.name,
                        load_attempt=model_load_attempts,
                    )
                elif config.provider != "lmstudio" and model_load_attempts <= max_load_attempts:
                    # Non-LM Studio backend: fall back to blind wait
                    self.logger.warning(
                        "model_not_ready_waiting",
                        model=config.name,
                        load_attempt=model_load_attempts,
                        wait_secs=self._MODEL_RELOAD_WAIT_SECS,
                        error=str(e)[:120],
                    )
                    await asyncio.sleep(self._MODEL_RELOAD_WAIT_SECS)
                    attempt = 0  # noqa: PLW2901
                    continue

                # Load attempts exhausted — walk the fallback chain.
                return await self._run_fallback_chain(
                    prompt=prompt,
                    exclude=config.name,
                    reason="load_timeout" if config.provider == "lmstudio" else "reload_exhausted",
                    chain=_fallback_chain,
                    timeout=timeout,
                    enable_thinking=enable_thinking,
                    original_error=e,
                    system_prompt=system_prompt,
                )

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
                if not _is_fallback:
                    return await self._run_fallback_chain(
                        prompt=prompt,
                        exclude=config.name,
                        reason="circuit_open",
                        chain=_fallback_chain,
                        timeout=timeout,
                        enable_thinking=enable_thinking,
                        original_error=None,
                        system_prompt=system_prompt,
                    )
                raise

            except _OpenRouterRateLimitError as e:
                self.logger.warning(
                    "openrouter_rate_limited",
                    model=config.name,
                    retry_after=e.retry_after,
                )
                self.health_checker.record_rate_limit(config.name)
                if not _is_fallback:
                    return await self._run_fallback_chain(
                        prompt=prompt,
                        exclude=config.name,
                        reason="rate_limited",
                        chain=_fallback_chain,
                        max_retries=max_retries,
                        timeout=timeout,
                        enable_thinking=enable_thinking,
                        original_error=e,
                        system_prompt=system_prompt,
                    )
                raise LLMError(f"OpenRouter model {config.name!r} is rate-limited and no fallback available") from e

            except Exception as e:
                # Generic 429 string match (other remote providers)
                if self._is_rate_limit_error(e) and config.type != "local" and not _is_fallback:
                    self.logger.warning(
                        "remote_rate_limited_falling_back",
                        model=config.name,
                        error=str(e)[:120],
                    )
                    self.health_checker.record_rate_limit(config.name)
                    return await self._run_fallback_chain(
                        prompt=prompt,
                        exclude=config.name,
                        reason="rate_limited",
                        chain=_fallback_chain,
                        max_retries=max_retries,
                        timeout=timeout,
                        enable_thinking=enable_thinking,
                        original_error=e,
                        system_prompt=system_prompt,
                    )

                self.logger.error(
                    "llm_error",
                    model=config.name,
                    error=str(e),
                    attempt=attempt,
                )
                self.health_checker.record_failure(config.name)
                # Timeout errors are not retryable: fail fast instead.
                if "hard timeout" in str(e) or "timeout" in str(e).lower():
                    raise
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(2**attempt)

        raise LLMError(f"All {max_retries} retries exhausted")

    async def _run_fallback_chain(
        self,
        prompt: str,
        exclude: str,
        reason: str,
        chain: Optional[list],
        max_retries: int,
        timeout: float,
        enable_thinking: Optional[bool],
        original_error: Optional[Exception],
        system_prompt: Optional[str] = None,
    ) -> str:
        """Try each model in the fallback chain in order.

        Fires a :class:`ModelSwitchEvent` for each switch so registered
        callbacks (e.g. the Discord bot) can notify the user.
        """
        if chain is None:
            chain = self._get_fallback_chain(exclude)

        if not chain:
            msg = (
                f"Model {exclude!r} failed ({reason}) and no fallback models are available. "
                "Add remote models with API keys to config/models.yaml."
            )
            if original_error:
                raise LLMError(msg) from original_error
            raise LLMError(msg)

        fallback = chain[0]
        remaining_chain = chain[1:]

        self.logger.warning(
            "model_switch_fallback",
            from_model=exclude,
            to_model=fallback.name,
            reason=reason,
            remaining_fallbacks=len(remaining_chain),
        )
        self._fire_switch_event(ModelSwitchEvent(
            from_model=exclude,
            to_model=fallback.name,
            reason=reason,
        ))

        return await self.generate(
            prompt,
            fallback,
            max_retries=max_retries,
            _fallback_chain=remaining_chain,
            timeout=timeout,
            enable_thinking=enable_thinking,
            system_prompt=system_prompt,
        )

    async def generate_stream(
        self,
        prompt: str,
        config: ModelConfig,
        system_prompt: Optional[str] = None,
    ) -> AsyncIterator[str]:
        await self.rate_limiter.acquire(config.name)

        if config.type == "local":
            async for chunk in self.ollama.stream_generate(prompt, config.name, system_prompt):
                yield chunk
        else:
            async for chunk in self.cloud.stream_generate(prompt, config, system_prompt):
                yield chunk

    async def health_check(self, config: ModelConfig) -> bool:
        return await self.health_checker.check(config)

    def get_cost_summary(self) -> dict:
        return self.cost_tracker.get_summary()

    def get_healthy_models(self) -> list[str]:
        return self.health_checker.get_healthy_models()


class LLMError(Exception):
    pass
