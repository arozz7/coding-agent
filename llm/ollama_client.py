import httpx
from typing import AsyncIterator, Optional, Any
import structlog

logger = structlog.get_logger()

DEFAULT_OLLAMA_URL = "http://localhost:11434"


class OllamaClient:
    def __init__(self, base_url: Optional[str] = None):
        self.base_url = base_url or DEFAULT_OLLAMA_URL
        self.logger = logger.bind(component="ollama_client")

    def set_base_url(self, url: str) -> None:
        self.base_url = url
        self.logger.info("base_url_updated", url=url)

    def _get_chat_endpoint(self) -> str:
        return f"{self.base_url}/v1/chat/completions"

    async def generate(self, prompt: str, model: str) -> str:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                self._get_chat_endpoint(),
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 2048,
                },
            )
            response.raise_for_status()
            data = response.json()
            message = data.get("choices", [{}])[0].get("message", {})
            content = message.get("content", "")
            if not content:
                content = message.get("reasoning_content", "")
            return content

    async def stream_generate(
        self, prompt: str, model: str
    ) -> AsyncIterator[str]:
        async with httpx.AsyncClient(timeout=120.0) as client:
            async with client.stream(
                "POST",
                self._get_chat_endpoint(),
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 2048,
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
