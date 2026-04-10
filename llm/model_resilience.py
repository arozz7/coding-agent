from enum import Enum
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from datetime import datetime, timedelta
import time
import structlog
import httpx

logger = structlog.get_logger()


class ModelStatus(Enum):
    AVAILABLE = "available"
    OFFLOADED = "offloaded"
    LOADING = "loading"
    ERROR = "error"
    UNKNOWN = "unknown"


class ModelAvailabilityError(Exception):
    pass


class RateLimitError(Exception):
    def __init__(self, message: str, retry_after: Optional[int] = None):
        super().__init__(message)
        self.retry_after = retry_after


@dataclass
class ModelHealthStatus:
    model_name: str
    status: ModelStatus
    is_available: bool
    last_checked: datetime
    latency_ms: Optional[float] = None
    error_message: Optional[str] = None
    retry_after: Optional[int] = None
    metadata: Dict[str, Any] = None


class OllamaModelManager:
    def __init__(self, endpoint: str = "http://127.0.0.1:11434"):
        self.endpoint = endpoint
        self._client = httpx.Client(timeout=10.0)
        self.logger = logger.bind(component="ollama_manager")
    
    def _make_request(self, method: str, path: str, **kwargs) -> Dict[str, Any]:
        url = f"{self.endpoint}{path}"
        try:
            response = self._client.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json() if response.text else {}
        except httpx.HTTPStatusError as e:
            self.logger.error("ollama_http_error", status=e.response.status_code, detail=e.response.text)
            raise
        except Exception as e:
            self.logger.error("ollama_request_error", error=str(e))
            raise
    
    def list_models(self) -> List[Dict[str, Any]]:
        try:
            result = self._make_request("GET", "/api/tags")
            return result.get("models", [])
        except Exception:
            return []
    
    def get_model_status(self, model_name: str) -> ModelHealthStatus:
        start_time = time.time()
        
        try:
            result = self._make_request("POST", "/api/show", json={"name": model_name})
            latency = (time.time() - start_time) * 1000
            
            return ModelHealthStatus(
                model_name=model_name,
                status=ModelStatus.AVAILABLE,
                is_available=True,
                last_checked=datetime.utcnow(),
                latency_ms=round(latency, 2),
                metadata=result,
            )
            
        except httpx.HTTPStatusError as e:
            error_text = e.response.text.lower()
            latency = (time.time() - start_time) * 1000
            
            if "not found" in error_text:
                return ModelHealthStatus(
                    model_name=model_name,
                    status=ModelStatus.OFFLOADED,
                    is_available=False,
                    last_checked=datetime.utcnow(),
                    latency_ms=round(latency, 2),
                    error_message=f"Model not found - may be offloaded: {e.response.status_code}",
                )
            
            return ModelHealthStatus(
                model_name=model_name,
                status=ModelStatus.ERROR,
                is_available=False,
                last_checked=datetime.utcnow(),
                latency_ms=round(latency, 2),
                error_message=str(e),
            )
            
        except Exception as e:
            latency = (time.time() - start_time) * 1000
            
            return ModelHealthStatus(
                model_name=model_name,
                status=ModelStatus.UNKNOWN,
                is_available=False,
                last_checked=datetime.utcnow(),
                latency_ms=round(latency, 2),
                error_message=str(e),
            )
    
    def is_model_available(self, model_name: str) -> bool:
        status = self.get_model_status(model_name)
        return status.is_available
    
    def load_model(self, model_name: str) -> bool:
        try:
            self._make_request("POST", "/api/generate", json={"model": model_name, "prompt": "", "stream": False})
            return True
        except Exception as e:
            self.logger.error("load_model_failed", model=model_name, error=str(e))
            return False
    
    def check_ollama_running(self) -> bool:
        try:
            self._make_request("GET", "/")
            return True
        except Exception:
            return False
    
    def get_server_status(self) -> Dict[str, Any]:
        is_running = self.check_ollama_running()
        
        return {
            "server_available": is_running,
            "endpoint": self.endpoint,
            "timestamp": datetime.utcnow().isoformat(),
        }


class CloudRateLimitHandler:
    def __init__(self):
        self._rate_limits: Dict[str, Dict[str, Any]] = {}
        self.logger = logger.bind(component="rate_limit_handler")
    
    def parse_rate_limit_error(self, response: httpx.Response) -> Optional[RateLimitError]:
        retry_after = response.headers.get("retry-after")
        
        if response.status_code == 429:
            message = f"Rate limit exceeded: {response.text[:200]}"
            
            if retry_after:
                try:
                    wait_seconds = int(retry_after)
                    return RateLimitError(message, retry_after=wait_seconds)
                except ValueError:
                    pass
            
            return RateLimitError(message, retry_after=None)
        
        if response.status_code == 403:
            if "quota" in response.text.lower() or "limit" in response.text.lower():
                return RateLimitError(f"Usage limit/quota exceeded: {response.text[:200]}", retry_after=None)
        
        return None
    
    def register_rate_limit(self, model_name: str, retry_after: int) -> None:
        reset_time = datetime.utcnow() + timedelta(seconds=retry_after)
        
        self._rate_limits[model_name] = {
            "reset_time": reset_time,
            "retry_after": retry_after,
            "last_updated": datetime.utcnow(),
        }
        
        self.logger.warning(
            "rate_limit_registered",
            model=model_name,
            retry_after=retry_after,
            reset_time=reset_time.isoformat(),
        )
    
    def get_wait_time(self, model_name: str) -> Optional[int]:
        if model_name not in self._rate_limits:
            return None
        
        rate_limit_info = self._rate_limits[model_name]
        reset_time = rate_limit_info["reset_time"]
        
        if datetime.utcnow() >= reset_time:
            del self._rate_limits[model_name]
            return None
        
        remaining = (reset_time - datetime.utcnow()).total_seconds()
        return int(remaining)
    
    def is_rate_limited(self, model_name: str) -> bool:
        wait_time = self.get_wait_time(model_name)
        return wait_time is not None and wait_time > 0
    
    def clear_expired(self) -> int:
        now = datetime.utcnow()
        expired = [
            name for name, info in self._rate_limits.items()
            if now >= info["reset_time"]
        ]
        
        for name in expired:
            del self._rate_limits[name]
        
        return len(expired)
    
    def get_status(self) -> Dict[str, Any]:
        return {
            "rate_limited_models": list(self._rate_limits.keys()),
            "count": len(self._rate_limits),
        }


class ModelResilienceManager:
    def __init__(
        self,
        ollama_endpoint: str = "http://127.0.0.1:11434",
        fallback_models: Optional[List[str]] = None,
    ):
        self.ollama_manager = OllamaModelManager(ollama_endpoint)
        self.rate_limit_handler = CloudRateLimitHandler()
        self.fallback_models = fallback_models or []
        self.logger = logger.bind(component="model_resilience")
        
        self._model_status_cache: Dict[str, ModelHealthStatus] = {}
        self._cache_ttl_seconds = 30
    
    def check_model_health(self, model_name: str, force_refresh: bool = False) -> ModelHealthStatus:
        if not force_refresh and model_name in self._model_status_cache:
            cached = self._model_status_cache[model_name]
            age = (datetime.utcnow() - cached.last_checked).total_seconds()
            if age < self._cache_ttl_seconds:
                return cached
        
        status = self.ollama_manager.get_model_status(model_name)
        self._model_status_cache[model_name] = status
        
        return status
    
    def is_model_available(self, model_name: str) -> bool:
        status = self.check_model_health(model_name)
        return status.is_available
    
    def find_available_model(self, preferred_models: List[str]) -> Optional[str]:
        for model in preferred_models:
            if self.is_model_available(model):
                return model
        
        for model in self.fallback_models:
            if self.is_model_available(model):
                return model
        
        all_models = self.ollama_manager.list_models()
        for model_info in all_models:
            model_name = model_info.get("name", "")
            if self.is_model_available(model_name):
                return model_name
        
        return None
    
    def find_working_fallback(
        self,
        primary_model: str,
        cloud_models: Optional[List[str]] = None,
    ) -> tuple[Optional[str], Optional[str]]:
        local_available = self.is_model_available(primary_model)
        
        if local_available:
            return primary_model, "local"
        
        self.logger.warning(
            "primary_model_unavailable",
            model=primary_model,
            status=self._model_status_cache.get(primary_model),
        )
        
        if cloud_models:
            for cloud_model in cloud_models:
                if not self.rate_limit_handler.is_rate_limited(cloud_model):
                    return cloud_model, "cloud"
        
        if self.fallback_models:
            fallback = self.find_available_model(self.fallback_models)
            if fallback:
                return fallback, "local"
        
        return None, None
    
    def handle_request_error(
        self,
        model_name: str,
        error: Exception,
    ) -> Dict[str, Any]:
        error_message = str(error).lower()
        
        if isinstance(error, httpx.HTTPStatusError):
            rate_limit_error = self.rate_limit_handler.parse_rate_limit_error(error.response)
            if rate_limit_error:
                if rate_limit_error.retry_after:
                    self.rate_limit_handler.register_rate_limit(model_name, rate_limit_error.retry_after)
                
                return {
                    "action": "retry_later",
                    "model": model_name,
                    "retry_after": rate_limit_error.retry_after,
                    "message": str(rate_limit_error),
                }
        
        if "offload" in error_message or "not found" in error_message:
            self._model_status_cache[model_name] = ModelHealthStatus(
                model_name=model_name,
                status=ModelStatus.OFFLOADED,
                is_available=False,
                last_checked=datetime.utcnow(),
                error_message=str(error),
            )
            
            return {
                "action": "fallback",
                "model": model_name,
                "reason": "model_offloaded",
                "message": "Local model may be offloaded, attempting fallback",
            }
        
        if "connection" in error_message or "timeout" in error_message:
            return {
                "action": "retry",
                "model": model_name,
                "reason": "connection_error",
                "message": "Connection to model failed, will retry",
            }
        
        return {
            "action": "fail",
            "model": model_name,
            "reason": "unknown",
            "message": str(error),
        }
    
    def get_diagnostics(self) -> Dict[str, Any]:
        ollama_status = self.ollama_manager.get_server_status()
        rate_limit_status = self.rate_limit_handler.get_status()
        
        cached_models = {
            name: {
                "status": status.status.value,
                "available": status.is_available,
                "last_checked": status.last_checked.isoformat(),
                "error": status.error_message,
            }
            for name, status in self._model_status_cache.items()
        }
        
        return {
            "ollama_server": ollama_status,
            "rate_limits": rate_limit_status,
            "cached_models": cached_models,
            "fallback_models": self.fallback_models,
        }
    
    def clear_cache(self) -> None:
        self._model_status_cache.clear()
        self.logger.info("model_status_cache_cleared")


def create_resilience_manager(
    ollama_endpoint: str = "http://127.0.0.1:11434",
    fallback_models: Optional[List[str]] = None,
) -> ModelResilienceManager:
    return ModelResilienceManager(ollama_endpoint, fallback_models)