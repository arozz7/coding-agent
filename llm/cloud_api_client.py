import httpx
from typing import AsyncIterator, TYPE_CHECKING, Optional
import json
import structlog

if TYPE_CHECKING:
    from .config import ModelConfig

logger = structlog.get_logger()


_OPENROUTER_BASE = "https://openrouter.ai/api/v1"
_OPENROUTER_REFERER = "http://localhost"  # satisfies OpenRouter's HTTP-Referer requirement
_OPENROUTER_TITLE = "local-coding-agent"


class _OpenRouterRateLimitError(Exception):
    """Raised when OpenRouter responds with 429. Carries retry_after seconds."""
    def __init__(self, retry_after: int = 0):
        self.retry_after = retry_after
        super().__init__(f"429 Too Many Requests (retry after {retry_after}s)")


class _OpenRouterPaymentRequiredError(Exception):
    """Raised when OpenRouter responds with 402 — model is out of free credits."""
    pass


class CloudAPIClient:
    def __init__(self):
        self.logger = logger.bind(component="cloud_api_client")

    def _endpoint_type(self, config: "ModelConfig") -> str:
        ep = (config.endpoint or "").lower()
        if "anthropic" in ep:
            return "anthropic"
        if "openrouter.ai" in ep:
            return "openrouter"
        if "openai" in ep:
            return "openai"
        return "openai"  # default to OpenAI-compatible for unknown endpoints

    async def generate(
        self,
        prompt: str,
        config: "ModelConfig",
        system_prompt: Optional[str] = None,
        usage_out: Optional[dict] = None,
    ) -> str:
        """``usage_out``, if given, is filled in-place with the provider's real
        token usage when the response includes one. Only the OpenAI-compatible
        path populates it today (Phase 1) — anthropic/openrouter accept the
        param but leave it empty for now (Phase 3 adds their extraction)."""
        kind = self._endpoint_type(config)
        if kind == "anthropic":
            return await self._anthropic_generate(prompt, config, system_prompt)
        if kind == "openrouter":
            return await self._openrouter_generate(prompt, config, system_prompt)
        return await self._openai_generate(prompt, config, system_prompt, usage_out=usage_out)

    async def stream_generate(
        self, prompt: str, config: "ModelConfig", system_prompt: Optional[str] = None
    ) -> AsyncIterator[str]:
        kind = self._endpoint_type(config)
        if kind == "anthropic":
            async for chunk in self._anthropic_stream(prompt, config, system_prompt):
                yield chunk
        elif kind == "openrouter":
            async for chunk in self._openrouter_stream(prompt, config, system_prompt):
                yield chunk
        else:
            async for chunk in self._openai_stream(prompt, config, system_prompt):
                yield chunk

    async def _anthropic_generate(self, prompt: str, config: "ModelConfig", system_prompt: Optional[str]) -> str:
        headers = {
            "x-api-key": config.api_key or "",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": config.name,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system_prompt:
            payload["system"] = [
                {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
            ]

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                config.endpoint,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("content", [{}])[0].get("text", "")

    async def _anthropic_stream(
        self, prompt: str, config: "ModelConfig", system_prompt: Optional[str]
    ) -> AsyncIterator[str]:
        headers = {
            "x-api-key": config.api_key or "",
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        payload = {
            "model": config.name,
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
        }
        if system_prompt:
            payload["system"] = [
                {"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}
            ]

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                config.endpoint,
                headers=headers,
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        data = json.loads(line[5:])
                        if content_block := data.get("content_block"):
                            if text := content_block.get("text"):
                                yield text
                        elif data.get("type") == "message_stop":
                            break

    async def _openai_generate(
        self,
        prompt: str,
        config: "ModelConfig",
        system_prompt: Optional[str],
        usage_out: Optional[dict] = None,
    ) -> str:
        headers = {
            "Authorization": f"Bearer {config.api_key or ''}",
            "content-type": "application/json",
        }
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": config.name,
            "messages": messages,
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{config.endpoint}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            message = data["choices"][0]["message"]
            content = message.get("content", "")

            if usage_out is not None and (usage := data.get("usage")):
                usage_out["prompt_tokens"] = usage.get("prompt_tokens", 0)
                usage_out["completion_tokens"] = usage.get("completion_tokens", 0)
                usage_out["total_tokens"] = usage.get("total_tokens", 0)

            if not content:
                # Same failure mode ollama_client.py already guards against:
                # a reasoning model spent its whole max_tokens budget on the
                # <think> trace and never reached an answer. Returning the
                # empty string here would silently break downstream parsing,
                # so raise instead — the caller's retry loop handles it.
                reasoning = message.get("reasoning_content", "")
                finish_reason = data["choices"][0].get("finish_reason")
                if reasoning:
                    self.logger.warning(
                        "empty_content_with_reasoning",
                        model=config.name,
                        finish_reason=finish_reason,
                        reasoning_len=len(reasoning),
                        reasoning_preview=reasoning[:120],
                    )
                    raise RuntimeError(
                        f"Model {config.name!r} returned empty content (reasoning-only "
                        f"response, finish_reason={finish_reason!r}, {len(reasoning)} "
                        "reasoning chars hit the max_tokens cap before an answer was "
                        "produced). Raise max_tokens in models.yaml for this model."
                    )
                raise RuntimeError(f"Model {config.name!r} returned empty content and no reasoning")

            return content

    async def _openai_stream(
        self, prompt: str, config: "ModelConfig", system_prompt: Optional[str]
    ) -> AsyncIterator[str]:
        headers = {
            "Authorization": f"Bearer {config.api_key or ''}",
            "content-type": "application/json",
        }
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": config.name,
            "messages": messages,
            "stream": True,
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{config.endpoint}/chat/completions",
                headers=headers,
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        if line == "data: [DONE]":
                            break
                        data = json.loads(line[5:])
                        if content := data["choices"][0].get("delta", {}).get(
                            "content"
                        ):
                            yield content

    async def _openrouter_generate(self, prompt: str, config: "ModelConfig", system_prompt: Optional[str]) -> str:
        headers = {
            "Authorization": f"Bearer {config.api_key or ''}",
            "HTTP-Referer": _OPENROUTER_REFERER,
            "X-Title": _OPENROUTER_TITLE,
            "content-type": "application/json",
        }
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": config.name,
            "messages": messages,
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{_OPENROUTER_BASE}/chat/completions",
                headers=headers,
                json=payload,
            )
            if response.status_code == 429:
                retry_after = int(response.headers.get("retry-after", 0))
                raise _OpenRouterRateLimitError(retry_after=retry_after)
            if response.status_code == 402:
                raise _OpenRouterPaymentRequiredError(f"Model {config.name!r} returned 402 — out of free credits")
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]

    async def _openrouter_stream(
        self, prompt: str, config: "ModelConfig", system_prompt: Optional[str]
    ) -> AsyncIterator[str]:
        headers = {
            "Authorization": f"Bearer {config.api_key or ''}",
            "HTTP-Referer": _OPENROUTER_REFERER,
            "X-Title": _OPENROUTER_TITLE,
            "content-type": "application/json",
        }
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": config.name,
            "messages": messages,
            "stream": True,
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                f"{_OPENROUTER_BASE}/chat/completions",
                headers=headers,
                json=payload,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data:"):
                        if line.strip() == "data: [DONE]":
                            break
                        try:
                            data = json.loads(line[5:])
                            if content := data["choices"][0].get("delta", {}).get("content"):
                                yield content
                        except (json.JSONDecodeError, KeyError, IndexError):
                            pass

    async def health_check(self, endpoint: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{endpoint}/models")
                return response.status_code == 200
        except Exception:
            return False
