import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, AsyncIterator, Callable, Tuple
from pathlib import Path
import structlog

from .config import ModelConfig
from .config_loader import build_model_config, load_config_file
from .evaluator_selector import EvaluatorSelectorMixin
from .ollama_client import OllamaClient, ModelNotReadyError
from .cloud_api_client import CloudAPIClient, _OpenRouterRateLimitError, _OpenRouterPaymentRequiredError
from .cost_tracker import CostTracker
from .usage import UsageInfo
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
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ModelRouter(EvaluatorSelectorMixin):
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
        # Cache for the dynamically-resolved free evaluator model.
        # Tuple of (ModelConfig, monotonic timestamp); refreshed every _EVALUATOR_CACHE_TTL seconds.
        self._evaluator_cache: Optional[Tuple[ModelConfig, float]] = None
        self._EVALUATOR_CACHE_TTL: float = 3600.0
        # Models that returned 402 are blacklisted for _EVALUATOR_BLACKLIST_TTL seconds.
        # OpenRouter free-tier limits reset daily, so 24 h is a safe expiry.
        self._evaluator_blacklist: dict[str, float] = {}  # model_name -> blacklisted_at (monotonic)
        self._EVALUATOR_BLACKLIST_TTL: float = 86400.0
        # Account-level circuit breaker for OpenRouter's free-tier rate limit.
        # The free tier shares ONE rate-limit bucket across all `:free` models,
        # so per-model blacklisting just cycles to a different model ID that
        # hits the same wall (observed: qwen3-coder:free -> gemma:free ->
        # qwen3-coder:free, 429 every time). After repeated hits in a short
        # window, stop attempting remote evaluator models entirely for a
        # cooldown period and go straight to the local fallback.
        self._openrouter_rate_limit_hits: list[float] = []  # monotonic timestamps
        self._OPENROUTER_BREAKER_WINDOW: float = 600.0       # look back 10 min
        self._OPENROUTER_BREAKER_THRESHOLD: int = 2          # hits within window to trip
        self._openrouter_cooldown_until: float = 0.0         # monotonic; 0 = not tripped
        self._OPENROUTER_BREAKER_COOLDOWN: float = 1800.0    # 30 min cooldown
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
        data = load_config_file(path)
        if data is None:
            return

        self._defaults = data.get("defaults", {})
        # Merge local_runtime overrides from YAML (nested under defaults)
        lr = self._defaults.get("local_runtime", {})
        if lr:
            self._local_runtime.update(lr)

        for m in data.get("models", []):
            try:
                config = build_model_config(m)
            except Exception as e:
                self.logger.error("model_config_invalid", entry=m, error=str(e))
                continue
            self.configs.append(config)
            self.config_by_name[config.name] = config
            self.rate_limiter.configure(config.name, config.rate_limit_rpm)
            if config.type == "local" and config.endpoint:
                self._configure_ollama_endpoint(config)

        # Honour the defaults.coding_model setting as the initial active model.
        # Also configure its endpoint immediately — otherwise _configure_ollama_endpoint
        # leaves base_url pointing at whichever local model was processed last above.
        default_name = self._defaults.get("coding_model")
        if default_name and default_name in self.config_by_name:
            self._active_model_name = default_name
            self._configure_ollama_endpoint(self.config_by_name[default_name])

        self.logger.info(
            "configs_loaded",
            count=len(self.configs),
            active=self._active_model_name,
            path=str(Path(path).resolve()),
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

        # 3. First coding-optimized model for coding purposes. "verify" shares
        # this fallback so the verifier gets a sensible default even with no
        # verify_model override in models.yaml, but resolves independently
        # once one is set — the routing is a real seam, not an accident of
        # both purposes asking for "coding".
        if purpose in ("coding", "verify"):
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

    async def _turboquant_is_reachable(self, config: ModelConfig) -> bool:
        """Return True if TurboQuantLoader TCP socket is accepting connections.

        Any HTTP response (even non-200) counts as reachable — we only want to
        know whether TQL is running at all, not whether a model is ready.
        """
        import httpx
        url = (config.endpoint or "http://127.0.0.1:7432").rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.get(f"{url}/health")
                return True
        except httpx.ConnectError:
            return False
        except Exception:
            return True  # other errors (timeout, non-200) still mean TQL is up

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
        timeout: Optional[float] = None,
        enable_thinking: bool | None = None,
        system_prompt: Optional[str] = None,
        messages: Optional[List[dict]] = None,
        max_tokens: Optional[int] = None,
    ) -> str:
        """Generate a completion.

        ``enable_thinking`` overrides the per-model ``enable_thinking`` setting
        in models.yaml for this single call.  Pass ``False`` for lightweight
        classification calls where a thinking trace is wasteful (e.g. the task
        type classifier that only needs one word back).  Leave as ``None``
        (default) to use the model's own setting.

        ``max_tokens`` overrides the per-model ``max_tokens`` setting in
        models.yaml for this single call.  Leave as ``None`` (default) to use
        the model's own budget.
        """
        import asyncio
        await self.rate_limiter.acquire(config.name)

        # Resolve effective enable_thinking: call-site override wins, then
        # per-model config, then None (let the model decide).
        effective_thinking = enable_thinking if enable_thinking is not None else config.enable_thinking
        # Resolve effective max_tokens the same way.
        effective_max_tokens = max_tokens if max_tokens is not None else config.max_tokens
        # Resolve effective timeout: an explicit call-site override wins,
        # otherwise fall back to the model's own declared budget rather than
        # a flat constant — generation speed and max_tokens/thinking budget
        # are properties of the model, not of whichever role is calling it.
        effective_timeout = timeout if timeout is not None else config.timeout_secs

        # Track how many times we've tried to load / wait for this model.
        # Governed by max_load_attempts (local_runtime), not by max_retries.
        model_load_attempts = 0
        max_load_attempts = int(self._local_runtime.get("max_load_attempts", 20))

        # Use a while loop so that model-switching waits (ModelNotReadyError +
        # continue) do not consume a retry slot.  attempt only increments on
        # genuine request failures (network errors, bad responses, rate limits).
        attempt = 0
        while attempt < max_retries:
            # Turboquant pre-flight: warn early if TQL is completely unreachable.
            # TQL handles model switching automatically — we never trigger loads.
            if config.provider == "turboquant" and attempt == 0:
                if not await self._turboquant_is_reachable(config):
                    self.logger.warning(
                        "turboquant_unreachable",
                        model=config.name,
                        endpoint=config.endpoint,
                        hint="Ensure TurboQuantLoader is running and accessible",
                    )

            try:
                if config.type == "local":
                    # Always configure the endpoint for this specific model before
                    # calling generate — OllamaClient.base_url is shared state and
                    # gets clobbered during _load_configs() by whichever local model
                    # was processed last.  Setting it here ensures each model (and
                    # every fallback hop) uses its own URL.
                    self._configure_ollama_endpoint(config)
                    # Caller-owned dict, not a shared attribute on OllamaClient/
                    # ModelRouter — sub-agents run concurrent generate() calls,
                    # and a shared mutable last_usage would race between them.
                    usage_out: dict = {}
                    result = await self.ollama.generate(
                        prompt,
                        config.name,
                        system_prompt=system_prompt,
                        enable_thinking=effective_thinking,
                        timeout=effective_timeout,
                        messages=messages,
                        max_tokens=effective_max_tokens,
                        usage_out=usage_out,
                    )
                else:
                    usage_out = {}
                    result = await self.cloud.generate(
                        prompt, config, system_prompt=system_prompt, usage_out=usage_out
                    )

                usage = UsageInfo(**usage_out) if usage_out else None
                self.cost_tracker.track_usage(config, prompt, result, usage=usage)
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
                        continue  # don't increment attempt — retry immediately
                    self.logger.warning(
                        "model_load_timeout",
                        model=config.name,
                        load_attempt=model_load_attempts,
                    )
                elif config.provider == "turboquant" and model_load_attempts <= max_load_attempts:
                    # TQL auto-switches models and returns 503 + Retry-After: N.
                    # Wait the indicated time and retry without burning a retry slot.
                    wait_secs = getattr(e, "retry_after", None) or 10
                    self.logger.info(
                        "turboquant_switching_waiting",
                        model=config.name,
                        wait_secs=wait_secs,
                        switch_attempt=model_load_attempts,
                        max_switch_attempts=max_load_attempts,
                    )
                    await asyncio.sleep(wait_secs)
                    continue  # don't increment attempt
                elif config.provider not in ("lmstudio", "turboquant") and model_load_attempts <= max_load_attempts:
                    # Other backends (ollama, llama_cpp): blind wait.
                    self.logger.warning(
                        "model_not_ready_waiting",
                        model=config.name,
                        load_attempt=model_load_attempts,
                        wait_secs=self._MODEL_RELOAD_WAIT_SECS,
                        error=str(e)[:120],
                    )
                    await asyncio.sleep(self._MODEL_RELOAD_WAIT_SECS)
                    continue  # don't increment attempt

                # Load / switch attempts exhausted — walk the fallback chain.
                return await self._run_fallback_chain(
                    prompt=prompt,
                    exclude=config.name,
                    reason="load_timeout" if config.provider in ("lmstudio", "turboquant") else "reload_exhausted",
                    chain=_fallback_chain,
                    max_retries=max_retries,
                    timeout=effective_timeout,
                    enable_thinking=enable_thinking,
                    max_tokens=max_tokens,
                    original_error=e,
                    system_prompt=system_prompt,
                    messages=messages,
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
                        max_retries=max_retries,
                        timeout=effective_timeout,
                        enable_thinking=enable_thinking,
                        max_tokens=max_tokens,
                        original_error=None,
                        system_prompt=system_prompt,
                        messages=messages,
                    )
                raise

            except _OpenRouterPaymentRequiredError:
                # 402 means this model's free-tier credits are exhausted.
                # Blacklist it for 24 h (daily reset) so get_evaluator_model()
                # skips it on re-selection, then fall back immediately.
                self._evaluator_blacklist[config.name] = time.monotonic()
                self._evaluator_cache = None  # force re-selection next call
                self.logger.warning(
                    "openrouter_payment_required",
                    model=config.name,
                    blacklisted_for_hours=self._EVALUATOR_BLACKLIST_TTL / 3600,
                )
                self.logger.info(
                    "evaluator_blacklist_changed",
                    reason="payment_required",
                    models=[config.name],
                    remaining_blacklisted=len(self._evaluator_blacklist),
                )
                if not _is_fallback:
                    return await self._run_fallback_chain(
                        prompt=prompt,
                        exclude=config.name,
                        reason="payment_required",
                        chain=_fallback_chain,
                        max_retries=max_retries,
                        timeout=effective_timeout,
                        enable_thinking=enable_thinking,
                        max_tokens=max_tokens,
                        original_error=None,
                        system_prompt=system_prompt,
                        messages=messages,
                    )
                raise LLMError(f"OpenRouter model {config.name!r} is out of free credits and no fallback available") from None

            except _OpenRouterRateLimitError as e:
                self.logger.warning(
                    "openrouter_rate_limited",
                    model=config.name,
                    retry_after=e.retry_after,
                )
                self.health_checker.record_rate_limit(config.name)
                self._record_openrouter_rate_limit()
                # Blacklist this evaluator model for the retry_after window so
                # get_evaluator_model() picks a different free model next call
                # instead of returning the same cached rate-limited one.
                blacklist_ttl = max(e.retry_after, 120)
                self._evaluator_blacklist[config.name] = time.monotonic() - (self._EVALUATOR_BLACKLIST_TTL - blacklist_ttl)
                self._evaluator_cache = None
                self.logger.info(
                    "evaluator_blacklist_changed",
                    reason="rate_limited",
                    models=[config.name],
                    blacklist_ttl_secs=blacklist_ttl,
                    remaining_blacklisted=len(self._evaluator_blacklist),
                )
                if not _is_fallback:
                    return await self._run_fallback_chain(
                        prompt=prompt,
                        exclude=config.name,
                        reason="rate_limited",
                        chain=_fallback_chain,
                        max_retries=max_retries,
                        timeout=effective_timeout,
                        enable_thinking=enable_thinking,
                        max_tokens=max_tokens,
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
                        timeout=effective_timeout,
                        enable_thinking=enable_thinking,
                        max_tokens=max_tokens,
                        original_error=e,
                        system_prompt=system_prompt,
                        messages=messages,
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
                await asyncio.sleep(min(2 ** attempt, 30))

            attempt += 1

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
        messages: Optional[List[dict]] = None,
        max_tokens: Optional[int] = None,
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
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            messages=messages,
        )

    async def generate_stream(
        self,
        prompt: str,
        config: ModelConfig,
        system_prompt: Optional[str] = None,
        messages: Optional[List[dict]] = None,
    ) -> AsyncIterator[str]:
        await self.rate_limiter.acquire(config.name)

        if config.type == "local":
            async for chunk in self.ollama.stream_generate(prompt, config.name, system_prompt, messages):
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
