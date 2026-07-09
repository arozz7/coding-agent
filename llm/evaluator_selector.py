"""Dynamic free-evaluator-model selection + OpenRouter account-level circuit breaker.

Mixed into ModelRouter (via inheritance) rather than composed as a separate
object, because these methods read/write several ModelRouter instance
attributes initialized in ModelRouter.__init__ (_evaluator_cache,
_evaluator_blacklist, _openrouter_rate_limit_hits, _openrouter_cooldown_until,
plus the cache/breaker tuning constants). A composed object would either
duplicate that state or require every existing caller/test that pokes
router._evaluator_cache / router._openrouter_cooldown_until directly to be
rewritten to go through a nested attribute. Mixin inheritance keeps the flat
`self` attribute surface — and the public/private method names — unchanged.
"""
from __future__ import annotations

import os
import time

from .config import ModelConfig


class EvaluatorSelectorMixin:
    """ModelRouter methods for dynamic free-evaluator-model selection.

    Expects the including class's __init__ to set:
      _evaluator_cache, _EVALUATOR_CACHE_TTL,
      _evaluator_blacklist, _EVALUATOR_BLACKLIST_TTL,
      _openrouter_rate_limit_hits, _OPENROUTER_BREAKER_WINDOW,
      _OPENROUTER_BREAKER_THRESHOLD, _openrouter_cooldown_until,
      _OPENROUTER_BREAKER_COOLDOWN,
    plus `self.logger`, `self.get_config`, `self.get_model`, `self.rate_limiter`.
    """

    # Families known to follow instructions well enough for structured JSON eval.
    _EVAL_PREFERRED = ("gemma", "qwen", "llama", "mistral", "phi", "deepseek", "magistral")

    def _record_openrouter_rate_limit(self) -> None:
        """Record a 429 from OpenRouter and trip the breaker if it recurs.

        Called on every OpenRouter rate-limit response, not just the evaluator
        path, since the underlying quota is shared account-wide.
        """
        now = time.monotonic()
        self._openrouter_rate_limit_hits = [
            t for t in self._openrouter_rate_limit_hits if now - t < self._OPENROUTER_BREAKER_WINDOW
        ]
        self._openrouter_rate_limit_hits.append(now)
        if (
            len(self._openrouter_rate_limit_hits) >= self._OPENROUTER_BREAKER_THRESHOLD
            and now >= self._openrouter_cooldown_until
        ):
            self._openrouter_cooldown_until = now + self._OPENROUTER_BREAKER_COOLDOWN
            self.logger.warning(
                "openrouter_free_tier_circuit_tripped",
                hits=len(self._openrouter_rate_limit_hits),
                window_secs=self._OPENROUTER_BREAKER_WINDOW,
                cooldown_secs=self._OPENROUTER_BREAKER_COOLDOWN,
            )

    def _openrouter_cooldown_active(self) -> bool:
        return time.monotonic() < self._openrouter_cooldown_until

    @staticmethod
    def _is_rate_limit_error(exc: Exception) -> bool:
        """Return True if *exc* is a 429 response from a remote API."""
        msg = str(exc)
        return "429" in msg and ("Too Many Requests" in msg or "rate" in msg.lower())

    @staticmethod
    def _score_free_model(entry: dict) -> int:
        mid = entry.get("id", "").lower()
        ctx = int(entry.get("context_length") or 0)
        score = ctx
        if any(f in mid for f in EvaluatorSelectorMixin._EVAL_PREFERRED):
            score += 1_000_000
        return score

    async def get_evaluator_model(self) -> ModelConfig:
        """Return a free OpenRouter model suitable for lightweight pass/fail evaluation.

        Fetches GET /v1/models from OpenRouter, filters for zero-cost models, picks the
        best candidate by context window + family preference, and caches the result for
        one hour.  Falls back to the static 'openrouter/free' config entry on any error.
        """
        now = time.monotonic()
        if self._evaluator_cache is not None:
            cached_config, cached_at = self._evaluator_cache
            if now - cached_at < self._EVALUATOR_CACHE_TTL:
                return cached_config

        api_key = os.environ.get("OPENROUTER_API_KEY", "")
        fallback = self.get_config("openrouter/free") or self.get_model("coding")

        if not api_key:
            self.logger.warning("evaluator_no_api_key", hint="Set OPENROUTER_API_KEY to enable dynamic free-model selection")
            return fallback

        if self._openrouter_cooldown_active():
            self.logger.info(
                "evaluator_openrouter_cooldown_active",
                remaining_secs=round(self._openrouter_cooldown_until - time.monotonic()),
                fallback=fallback.name,
            )
            return fallback

        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://openrouter.ai/api/v1/models",
                    headers={"Authorization": f"Bearer {api_key}"},
                )
                resp.raise_for_status()
                models = resp.json().get("data", [])

            _now = time.monotonic()
            # Expire stale blacklist entries before filtering.
            self._evaluator_blacklist = {
                k: v for k, v in self._evaluator_blacklist.items()
                if _now - v < self._EVALUATOR_BLACKLIST_TTL
            }
            free = [
                m for m in models
                if str(m.get("pricing", {}).get("prompt", "1")) == "0"
                and str(m.get("pricing", {}).get("completion", "1")) == "0"
                and int(m.get("context_length") or 0) >= 8192
                and m.get("id", "") not in self._evaluator_blacklist
            ]

            if not free:
                self.logger.warning("evaluator_no_free_models_found", fallback=fallback.name)
                return fallback

            best = max(free, key=self._score_free_model)
            config = ModelConfig(
                name=best["id"],
                type="remote",
                endpoint="https://openrouter.ai/api/v1",
                api_key=api_key,
                context_window=int(best.get("context_length") or 32000),
                rate_limit_rpm=20,
                recommended_for=["evaluation"],
                enable_thinking=False,
                provider="openrouter",
            )
            self.rate_limiter.configure(config.name, config.rate_limit_rpm)
            self._evaluator_cache = (config, now)
            self.logger.info("evaluator_model_selected", model=config.name, context=config.context_window)
            return config

        except Exception as exc:
            self.logger.warning("evaluator_model_fetch_failed", error=str(exc), fallback=fallback.name)
            return fallback
