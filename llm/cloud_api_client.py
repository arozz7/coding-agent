import httpx
from typing import AsyncIterator, TYPE_CHECKING
import json
import structlog

if TYPE_CHECKING:
    from .config import ModelConfig

logger = structlog.get_logger()


class CloudAPIClient:
    def __init__(self):
        self.logger = logger.bind(component="cloud_api_client")

    async def generate(self, prompt: str, config: "ModelConfig") -> str:
        if "anthropic" in (config.endpoint or ""):
            return await self._anthropic_generate(prompt, config)
        elif "openai" in (config.endpoint or ""):
            return await self._openai_generate(prompt, config)
        else:
            raise ValueError(f"Unsupported cloud API: {config.endpoint}")

    async def stream_generate(
        self, prompt: str, config: "ModelConfig"
    ) -> AsyncIterator[str]:
        if "anthropic" in (config.endpoint or ""):
            async for chunk in self._anthropic_stream(prompt, config):
                yield chunk
        elif "openai" in (config.endpoint or ""):
            async for chunk in self._openai_stream(prompt, config):
                yield chunk

    async def _anthropic_generate(self, prompt: str, config: "ModelConfig") -> str:
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
        self, prompt: str, config: "ModelConfig"
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

    async def _openai_generate(self, prompt: str, config: "ModelConfig") -> str:
        headers = {
            "Authorization": f"Bearer {config.api_key or ''}",
            "content-type": "application/json",
        }
        payload = {
            "model": config.name,
            "messages": [{"role": "user", "content": prompt}],
        }

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{config.endpoint}/chat/completions",
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
            return data["choices"][0]["message"]["content"]

    async def _openai_stream(
        self, prompt: str, config: "ModelConfig"
    ) -> AsyncIterator[str]:
        headers = {
            "Authorization": f"Bearer {config.api_key or ''}",
            "content-type": "application/json",
        }
        payload = {
            "model": config.name,
            "messages": [{"role": "user", "content": prompt}],
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

    async def health_check(self, endpoint: str) -> bool:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(f"{endpoint}/models")
                return response.status_code == 200
        except Exception:
            return False
