"""Unit tests for real-usage extraction added to the OpenAI-compatible
generate() paths in cloud_api_client.py and ollama_client.py (Phase 1 of
docs/plans/real-token-usage-plan.md).
"""
import pytest
from unittest.mock import AsyncMock, Mock, patch

from llm.config import ModelConfig


def _make_async_client_mock(json_return, status_code=200):
    mock_response = Mock()
    mock_response.status_code = status_code
    mock_response.text = "{}"
    mock_response.raise_for_status = Mock()
    mock_response.json.return_value = json_return

    mock_client = AsyncMock()
    mock_client.post.return_value = mock_response

    mock_class = Mock()
    mock_class.return_value.__aenter__ = AsyncMock(return_value=mock_client)
    mock_class.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_class


class TestCloudAPIClientUsageExtraction:
    @pytest.mark.asyncio
    async def test_openai_generate_populates_usage_out_when_present(self):
        from llm.cloud_api_client import CloudAPIClient

        payload = {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        }
        mock_class = _make_async_client_mock(json_return=payload)
        config = ModelConfig(name="m", type="remote", endpoint="https://api.openai.com/v1")

        with patch("llm.cloud_api_client.httpx.AsyncClient", mock_class):
            client = CloudAPIClient()
            usage_out: dict = {}
            content = await client.generate("prompt", config, usage_out=usage_out)

        assert content == "hi"
        assert usage_out == {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}

    @pytest.mark.asyncio
    async def test_openai_generate_leaves_usage_out_empty_when_absent(self):
        """Regression guard: a backend that doesn't return `usage` must not
        break the call — usage_out simply stays empty, same as passing None."""
        from llm.cloud_api_client import CloudAPIClient

        payload = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
        mock_class = _make_async_client_mock(json_return=payload)
        config = ModelConfig(name="m", type="remote", endpoint="https://api.openai.com/v1")

        with patch("llm.cloud_api_client.httpx.AsyncClient", mock_class):
            client = CloudAPIClient()
            usage_out: dict = {}
            content = await client.generate("prompt", config, usage_out=usage_out)

        assert content == "hi"
        assert usage_out == {}

    @pytest.mark.asyncio
    async def test_openai_generate_without_usage_out_unchanged(self):
        """Callers that don't pass usage_out (the default) see zero behavior
        change — this is the existing call pattern used everywhere today."""
        from llm.cloud_api_client import CloudAPIClient

        payload = {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        }
        mock_class = _make_async_client_mock(json_return=payload)
        config = ModelConfig(name="m", type="remote", endpoint="https://api.openai.com/v1")

        with patch("llm.cloud_api_client.httpx.AsyncClient", mock_class):
            client = CloudAPIClient()
            content = await client.generate("prompt", config)

        assert content == "hi"


def _make_sync_client_mock(json_return):
    mock_response = Mock()
    mock_response.json.return_value = json_return
    mock_response.raise_for_status = Mock()

    mock_client = Mock()
    mock_client.post.return_value = mock_response

    mock_class = Mock()
    mock_class.return_value.__enter__ = Mock(return_value=mock_client)
    mock_class.return_value.__exit__ = Mock(return_value=False)
    return mock_class


class TestOllamaClientUsageExtraction:
    @pytest.mark.asyncio
    async def test_do_generate_populates_usage_out_when_present(self):
        """This is the load-bearing path: TurboQuantLoader-served local models
        (config.type == "local") go through OllamaClient, not CloudAPIClient —
        this is where turboquant traffic's real usage actually gets captured."""
        from llm.ollama_client import OllamaClient

        payload = {
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1471, "completion_tokens": 14118, "total_tokens": 15589},
        }
        mock_class = _make_sync_client_mock(json_return=payload)

        with patch("llm.ollama_client.httpx.Client", mock_class):
            client = OllamaClient(base_url="http://127.0.0.1:7432")
            usage_out: dict = {}
            content = await client._do_generate(
                "http://127.0.0.1:7432/v1/chat/completions",
                "Qwen3.8-27B-Q4_K_S",
                "prompt",
                None,
                None,
                60.0,
                usage_out=usage_out,
            )

        assert content == "hi"
        assert usage_out == {"prompt_tokens": 1471, "completion_tokens": 14118, "total_tokens": 15589}

    @pytest.mark.asyncio
    async def test_do_generate_leaves_usage_out_empty_when_absent(self):
        from llm.ollama_client import OllamaClient

        payload = {"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
        mock_class = _make_sync_client_mock(json_return=payload)

        with patch("llm.ollama_client.httpx.Client", mock_class):
            client = OllamaClient(base_url="http://127.0.0.1:7432")
            usage_out: dict = {}
            content = await client._do_generate(
                "http://127.0.0.1:7432/v1/chat/completions",
                "Qwen3.8-27B-Q4_K_S",
                "prompt",
                None,
                None,
                60.0,
                usage_out=usage_out,
            )

        assert content == "hi"
        assert usage_out == {}
