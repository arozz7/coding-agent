import asyncio
import httpx
from typing import AsyncIterator, Optional, Any
import structlog

logger = structlog.get_logger()

DEFAULT_OLLAMA_URL = "http://localhost:11434"

# Strings in an HTTP error body that indicate the model was evicted / not loaded.
# LM Studio uses these when a TTL-expired model gets a new request.
_MODEL_NOT_READY_HINTS = (
    "no model is currently loaded",
    "no models loaded",
    "model not loaded",
    "model_not_found",
    "failed to load",
    "model is loading",
    "not available",
    "crashed",
)


class ModelNotReadyError(RuntimeError):
    """Raised when the local inference server has no model loaded.

    Distinct from generic RuntimeError so callers can apply a longer
    wait/retry strategy rather than the normal 1-2 s backoff.
    """


class OllamaClient:
    def __init__(self, base_url: Optional[str] = None):
        self.base_url = base_url or DEFAULT_OLLAMA_URL
        self.logger = logger.bind(component="ollama_client")

    def set_base_url(self, url: str) -> None:
        self.base_url = url
        self.logger.info("base_url_updated", url=url)

    def _get_chat_endpoint(self) -> str:
        return f"{self.base_url}/v1/chat/completions"

    async def generate(
        self,
        prompt: str,
        model: str,
        enable_thinking: Optional[bool] = None,
        timeout: float = 600.0,
    ) -> str:
        """Generate a completion.

        Args:
            prompt: The user prompt.
            model: Model name as served by LM Studio / Ollama.
            enable_thinking: Pass False to disable extended reasoning on
                Qwen3/DeepSeek-R1 thinking models.  When these models return
                an empty ``content`` field (reasoning only, no actual response),
                we raise RuntimeError so the caller's retry loop handles it.
        """
        url = self._get_chat_endpoint()
        self.logger.info("ollama_generate_start", model=model, prompt_len=len(prompt), url=url)

        # Pre-flight: check the LM Studio model state before committing to a
        # 600-second httpx timeout.  If the model is not "loaded" we raise
        # ModelNotReadyError immediately so model_router applies its 120s
        # patience retry loop instead of burning the full timeout window
        # (~10 min) per attempt.  check_model_state has a 5-second timeout so
        # it adds negligible overhead on the happy path.
        state = await self.check_model_state(model)
        if state is not None and state != "loaded":
            raise ModelNotReadyError(
                f"Model {model!r} is not loaded (state={state!r}); "
                "will retry after LM Studio reload"
            )

        # Use asyncio.wait_for as a hard wall-clock guard.  httpx's per-operation
        # timeout resets whenever LM Studio sends a byte (e.g. chunked keepalives
        # while reloading a TTL-expired model), so it alone cannot prevent multi-hour
        # hangs.  asyncio.wait_for cancels the coroutine at the deadline regardless.
        try:
            return await asyncio.wait_for(
                self._do_generate(url, model, prompt, enable_thinking, timeout),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            self.logger.error("ollama_hard_timeout", url=url, timeout=timeout)
            raise RuntimeError(f"LLM hard timeout after {timeout:.0f}s for model {model!r}")

    async def _do_generate(
        self,
        url: str,
        model: str,
        prompt: str,
        enable_thinking: Optional[bool],
        timeout: float,
    ) -> str:
        """Inner coroutine — executed inside asyncio.wait_for by generate().

        Runs the blocking httpx.Client call in a thread via asyncio.to_thread so
        that asyncio.wait_for can reliably cancel it on Python 3.12+.

        Background: asyncio.wait_for cannot cancel an httpx.AsyncClient request
        on Python 3.12 because the event loop waits for the coroutine to
        acknowledge cancellation, which it never does while mid-read.  Running
        the same request synchronously in a thread allows wait_for to cancel the
        awaitable wrapper immediately; the background thread finishes on its own
        within httpx's own timeout window.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 8192,
        }
        if enable_thinking is False:
            # LM Studio / vLLM Qwen3 extension — disables the <think> phase
            # so the model always returns a direct content response.
            payload["enable_thinking"] = False

        def _sync_post() -> dict[str, Any]:
            """Synchronous httpx POST — safe to run in a thread pool."""
            with httpx.Client(timeout=timeout) as client:
                response = client.post(url, json=payload)
                response.raise_for_status()
                return response.json()

        try:
            data = await asyncio.to_thread(_sync_post)
        except httpx.TimeoutException:
            self.logger.error("ollama_timeout", url=url, timeout=timeout)
            raise RuntimeError(f"Ollama timeout after {timeout:.0f}s for model {model!r}")
        except httpx.HTTPStatusError as e:
            body = e.response.text
            self.logger.error(
                "ollama_http_error",
                status=e.response.status_code,
                body=body[:500],
            )
            # Detect model-not-loaded conditions so the caller can apply a
            # longer retry wait rather than the normal 1-2 s backoff.
            lower_body = body.lower()
            if e.response.status_code in (503, 404, 400) and any(
                hint in lower_body for hint in _MODEL_NOT_READY_HINTS
            ):
                raise ModelNotReadyError(
                    f"Model {model!r} is not loaded (HTTP {e.response.status_code}): {body[:200]}"
                )
            raise RuntimeError(f"Ollama HTTP {e.response.status_code}: {body[:200]}")
        except Exception as e:
            self.logger.error("ollama_error", error=str(e), error_type=type(e).__name__)
            raise

        choices = data.get("choices", [])
        if not choices:
            raise RuntimeError(f"No choices in response: {data}")

        message = choices[0].get("message", {})
        content = message.get("content", "")

        if not content:
            # Thinking models sometimes produce only reasoning_content
            # with an empty content field.  Returning the raw thinking
            # trace as a response breaks all downstream parsing, so we
            # raise instead — the retry loop in model_router will retry
            # or fall back to another model.
            reasoning = message.get("reasoning_content", "")
            if reasoning:
                self.logger.warning(
                    "empty_content_with_reasoning",
                    model=model,
                    reasoning_preview=reasoning[:120],
                )
                raise RuntimeError(
                    f"Model {model!r} returned empty content (reasoning-only response). "
                    "Set enable_thinking: false in models.yaml for this model."
                )
            raise RuntimeError(f"Model {model!r} returned empty content and no reasoning")

        self.logger.info("ollama_generate_success", content_len=len(content))
        return content

    async def stream_generate(
        self, prompt: str, model: str
    ) -> AsyncIterator[str]:
        async with httpx.AsyncClient(timeout=600.0) as client:
            async with client.stream(
                "POST",
                self._get_chat_endpoint(),
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 8192,
                    "stream": True,
                },
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        if line == "data: [DONE]":
                            break
                        import json
                        try:
                            data = json.loads(line[5:])
                            if content := data.get("choices", [{}])[0].get(
                                "delta", {}
                            ).get("content"):
                                yield content
                        except json.JSONDecodeError:
                            continue

    async def list_all_models(self) -> list[dict]:
        """Return all models known to LM Studio via /api/v0/models.

        Each entry has at least ``id`` and ``state`` ("loaded" | "not-loaded").
        Returns an empty list when the endpoint is unavailable.
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self.base_url}/api/v0/models")
                if r.status_code != 200:
                    return []
                return r.json().get("data", [])
        except Exception:
            return []

    async def check_model_state(self, model: str) -> Optional[str]:
        """Return the LM Studio model state string ("loaded", "not-loaded", etc.).

        Uses the /api/v0/models endpoint which includes a ``state`` field.
        Returns None when the endpoint is unavailable (plain Ollama or older
        LM Studio that doesn't expose /api/v0/models).
        """
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                r = await client.get(f"{self.base_url}/api/v0/models")
                if r.status_code != 200:
                    return None
                for m in r.json().get("data", []):
                    if m.get("id") == model:
                        return m.get("state")  # "loaded" | "not-loaded" | None
                return None
        except Exception:
            return None

    async def warmup(self, model: str) -> bool:
        """Probe the primary model on startup; return True if it is loaded.

        Checks the LM Studio state endpoint first for a fast answer.
        If the state is "loaded", returns True immediately.
        If the state is "not-loaded" (or the endpoint is unavailable), sends a
        minimal inference request — this either confirms the model is ready or
        triggers auto-load if LM Studio has that feature enabled.
        Never raises; failures are logged as warnings.
        """
        state = await self.check_model_state(model)
        if state == "loaded":
            self.logger.info("model_warmup_already_loaded", model=model)
            return True

        if state is not None:
            # State endpoint available but model is not loaded.
            self.logger.warning(
                "model_not_loaded_in_lm_studio",
                model=model,
                state=state,
                hint="Load the model in LM Studio before submitting tasks.",
            )

        # Send a tiny inference ping.  This will:
        #   • Confirm the model is ready if it somehow loaded since the state check.
        #   • Trigger auto-load if LM Studio has auto-load enabled.
        #   • Fail immediately with a clear error if neither applies.
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": "hi"}],
                    "max_tokens": 1,
                }
                r = await client.post(self._get_chat_endpoint(), json=payload)
                if r.status_code == 200:
                    self.logger.info("model_warmup_ping_ok", model=model)
                    return True
                body = r.text
                lower = body.lower()
                if any(hint in lower for hint in _MODEL_NOT_READY_HINTS):
                    self.logger.warning(
                        "model_warmup_not_ready",
                        model=model,
                        hint="Open LM Studio and load the model before sending tasks.",
                    )
                else:
                    self.logger.warning("model_warmup_ping_failed", model=model, status=r.status_code, body=body[:200])
                return False
        except Exception as e:
            self.logger.error("model_warmup_error", model=model, error=str(e))
            return False

    async def health_check(self, model: str) -> bool:
        """Return True only when the model is confirmed loaded in LM Studio.

        Prefers the /api/v0/models ``state`` field (accurate) over the plain
        /v1/models list (which returns all available models, loaded or not).
        Falls back to /v1/models existence check when the state endpoint is
        unavailable (plain Ollama).
        """
        state = await self.check_model_state(model)
        if state is not None:
            return state == "loaded"

        # Fallback: plain Ollama /v1/models existence check.
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{self.base_url}/v1/models")
                if response.status_code == 200:
                    models = response.json().get("data", [])
                    return any(m.get("id") == model for m in models)
                return False
        except Exception as e:
            self.logger.error("health_check_failed", error=str(e))
            return False
