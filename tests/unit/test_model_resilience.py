"""Unit tests for model resilience."""
import pytest
from unittest.mock import Mock, patch
from datetime import datetime, timedelta

from llm.model_resilience import (
    ModelStatus,
    ModelHealthStatus,
    OllamaModelManager,
    CloudRateLimitHandler,
    ModelResilienceManager,
    RateLimitError,
)


class TestModelStatus:
    def test_status_values(self):
        assert ModelStatus.AVAILABLE.value == "available"
        assert ModelStatus.OFFLOADED.value == "offloaded"
        assert ModelStatus.LOADING.value == "loading"
        assert ModelStatus.ERROR.value == "error"
        assert ModelStatus.UNKNOWN.value == "unknown"


class TestRateLimitError:
    def test_initialization_without_retry(self):
        error = RateLimitError("Rate limited")
        assert str(error) == "Rate limited"
        assert error.retry_after is None
    
    def test_initialization_with_retry(self):
        error = RateLimitError("Rate limited", retry_after=60)
        assert error.retry_after == 60


class TestModelHealthStatus:
    def test_initialization(self):
        status = ModelHealthStatus(
            model_name="test-model",
            status=ModelStatus.AVAILABLE,
            is_available=True,
            last_checked=datetime.utcnow(),
        )
        
        assert status.model_name == "test-model"
        assert status.status == ModelStatus.AVAILABLE
        assert status.is_available is True


class TestOllamaModelManager:
    @patch("llm.model_resilience.httpx.Client")
    def test_list_models(self, mock_client_class):
        mock_response = Mock()
        mock_response.json.return_value = {"models": [{"name": "model1"}, {"name": "model2"}]}
        
        mock_client = Mock()
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client
        
        manager = OllamaModelManager()
        models = manager.list_models()
        
        assert len(models) == 2
        assert models[0]["name"] == "model1"
    
    @patch("llm.model_resilience.httpx.Client")
    def test_get_model_status_available(self, mock_client_class):
        mock_response = Mock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"model": "test-model", "parameters": {}}
        
        mock_client = Mock()
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client
        
        manager = OllamaModelManager()
        status = manager.get_model_status("test-model")
        
        assert status.status == ModelStatus.AVAILABLE
        assert status.is_available is True
    
    @patch("llm.model_resilience.httpx.Client")
    def test_check_ollama_running(self, mock_client_class):
        mock_response = Mock()
        mock_response.status_code = 200
        
        mock_client = Mock()
        mock_client.request.return_value = mock_response
        mock_client_class.return_value = mock_client
        
        manager = OllamaModelManager()
        assert manager.check_ollama_running() is True
    
    @patch("llm.model_resilience.httpx.Client")
    def test_check_ollama_not_running(self, mock_client_class):
        mock_client = Mock()
        mock_client.request.side_effect = Exception("Connection refused")
        mock_client_class.return_value = mock_client
        
        manager = OllamaModelManager()
        assert manager.check_ollama_running() is False


class TestCloudRateLimitHandler:
    def test_initialization(self):
        handler = CloudRateLimitHandler()
        
        assert len(handler._rate_limits) == 0
    
    def test_parse_rate_limit_429(self):
        handler = CloudRateLimitHandler()
        
        response = Mock()
        response.status_code = 429
        response.text = "Rate limit exceeded"
        response.headers = {"retry-after": "60"}
        
        error = handler.parse_rate_limit_error(response)
        
        assert error is not None
        assert isinstance(error, RateLimitError)
        assert error.retry_after == 60
    
    def test_parse_rate_limit_403_quota(self):
        handler = CloudRateLimitHandler()
        
        response = Mock()
        response.status_code = 403
        response.text = "quota exceeded"
        response.headers = {}
        
        error = handler.parse_rate_limit_error(response)
        
        assert error is not None
        assert "quota" in str(error).lower()
    
    def test_register_rate_limit(self):
        handler = CloudRateLimitHandler()
        
        handler.register_rate_limit("gpt-4", 60)
        
        assert "gpt-4" in handler._rate_limits
        assert handler._rate_limits["gpt-4"]["retry_after"] == 60
    
    def test_get_wait_time(self):
        handler = CloudRateLimitHandler()
        
        handler.register_rate_limit("gpt-4", 120)
        
        wait = handler.get_wait_time("gpt-4")
        
        assert wait is not None
        assert wait > 0
        assert wait <= 120
    
    def test_is_rate_limited_true(self):
        handler = CloudRateLimitHandler()
        
        handler.register_rate_limit("gpt-4", 60)
        
        assert handler.is_rate_limited("gpt-4") is True
    
    def test_is_rate_limited_false(self):
        handler = CloudRateLimitHandler()
        
        assert handler.is_rate_limited("gpt-4") is False
    
    def test_clear_expired(self):
        handler = CloudRateLimitHandler()
        
        handler.register_rate_limit("gpt-4", 0)
        
        import time
        time.sleep(0.1)
        
        cleared = handler.clear_expired()
        
        assert cleared >= 1
    
    def test_get_status(self):
        handler = CloudRateLimitHandler()
        
        handler.register_rate_limit("gpt-4", 60)
        
        status = handler.get_status()
        
        assert "gpt-4" in status["rate_limited_models"]
        assert status["count"] == 1


class TestModelResilienceManager:
    def test_initialization(self):
        manager = ModelResilienceManager(
            ollama_endpoint="http://localhost:11434",
            fallback_models=["fallback-model"],
        )
        
        assert manager.ollama_manager.endpoint == "http://localhost:11434"
        assert manager.fallback_models == ["fallback-model"]
    
    @patch("llm.model_resilience.OllamaModelManager")
    def test_is_model_available(self, mock_manager_class):
        mock_instance = Mock()
        mock_instance.get_model_status.return_value = ModelHealthStatus(
            model_name="test",
            status=ModelStatus.AVAILABLE,
            is_available=True,
            last_checked=datetime.utcnow(),
        )
        mock_manager_class.return_value = mock_instance
        
        manager = ModelResilienceManager()
        manager.ollama_manager = mock_instance
        
        available = manager.is_model_available("test")
        
        assert available is True
    
    @patch("llm.model_resilience.OllamaModelManager")
    def test_find_available_model(self, mock_manager_class):
        manager = ModelResilienceManager()
        
        manager._model_status_cache = {
            "unavailable": ModelHealthStatus(
                model_name="unavailable",
                status=ModelStatus.OFFLOADED,
                is_available=False,
                last_checked=datetime.utcnow(),
            ),
            "available-model": ModelHealthStatus(
                model_name="available-model",
                status=ModelStatus.AVAILABLE,
                is_available=True,
                last_checked=datetime.utcnow(),
            ),
        }
        
        found = manager.find_available_model(["unavailable", "available-model"])
        
        assert found == "available-model"
    
    @patch("llm.model_resilience.OllamaModelManager")
    def test_find_working_fallback_local_unavailable(self, mock_manager_class):
        mock_instance = Mock()
        
        call_count = [0]
        def mock_status(name):
            call_count[0] += 1
            if call_count[0] == 1:
                return ModelHealthStatus(
                    model_name=name,
                    status=ModelStatus.OFFLOADED,
                    is_available=False,
                    last_checked=datetime.utcnow(),
                )
            return ModelHealthStatus(
                model_name=name,
                status=ModelStatus.AVAILABLE,
                is_available=True,
                last_checked=datetime.utcnow(),
            )
        
        mock_instance.get_model_status.side_effect = mock_status
        mock_manager_class.return_value = mock_instance
        
        manager = ModelResilienceManager(fallback_models=["fallback-model"])
        manager.ollama_manager = mock_instance
        
        model, source = manager.find_working_fallback("primary", ["cloud-model"])
        
        assert model is not None
        assert source in ["local", "cloud"]
    
    def test_handle_request_error_rate_limit(self):
        import httpx
        
        manager = ModelResilienceManager()
        
        response = Mock(spec=httpx.Response)
        response.status_code = 429
        response.text = "Rate limit"
        response.headers = {"retry-after": "30"}
        
        error = httpx.HTTPStatusError("429", request=Mock(), response=response)
        
        result = manager.handle_request_error("test-model", error)
        
        assert result["action"] == "retry_later"
        assert result["retry_after"] == 30
    
    def test_handle_request_error_offload(self):
        manager = ModelResilienceManager()
        
        error = Exception("model not found - may be offloaded")
        
        result = manager.handle_request_error("test-model", error)
        
        assert result["action"] == "fallback"
        assert result["reason"] == "model_offloaded"
    
    def test_get_diagnostics(self):
        manager = ModelResilienceManager()
        
        diagnostics = manager.get_diagnostics()
        
        assert "ollama_server" in diagnostics
        assert "rate_limits" in diagnostics
        assert "cached_models" in diagnostics
    
    def test_clear_cache(self):
        manager = ModelResilienceManager()
        
        manager._model_status_cache["test"] = ModelHealthStatus(
            model_name="test",
            status=ModelStatus.AVAILABLE,
            is_available=True,
            last_checked=datetime.utcnow(),
        )
        
        manager.clear_cache()
        
        assert len(manager._model_status_cache) == 0