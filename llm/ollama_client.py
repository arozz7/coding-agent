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
        """Inner coroutine — executed inside asyncio.wait_for by generate()."""
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                payload: dict[str, Any] = {
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 8192,
                }
                if enable_thinking is False:
                    # LM Studio / vLLM Qwen3 extension — disables the <think> phase
                    # so the model always returns a direct content response.
                    payload["enable_thinking"] = False

                response = await client.post(url, json=payload)
                response.raise_for_status()
                data = response.json()

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
        except asyncio.CancelledError:
            # Propagate cancellation (from asyncio.wait_for) without wrapping.
            raise
        except Exception as e:
            self.logger.error("ollama_error", error=str(e), error_type=type(e).__name__)
            raise

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

    async def health_check(self, model: str) -> bool:
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
