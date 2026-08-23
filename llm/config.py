from pydantic import BaseModel, Field
from typing import Optional, List


class ModelConfig(BaseModel):
    name: str
    type: str = Field(description="local or remote")
    endpoint: Optional[str] = None
    api_key: Optional[str] = None
    api_key_env: Optional[str] = None
    context_window: int = 32000
    is_coding_optimized: bool = False
    rate_limit_rpm: int = 60
    cost_per_1k_input: Optional[float] = None
    cost_per_1k_output: Optional[float] = None
    recommended_for: List[str] = Field(default_factory=list)
    # Set to False for Qwen3/DeepSeek-R1 thinking models that return empty content
    # when extended reasoning is enabled.  Passes enable_thinking=false to the API.
    enable_thinking: Optional[bool] = None
    # Response token budget passed as max_tokens. Thinking models spend part of
    # this budget on their <think> trace before the actual answer, so models
    # with enable_thinking left on (the default) need enough headroom for both.
    max_tokens: int = 8192
    # Per-call generate() timeout in seconds. Lives on the model, not the
    # caller: it's the model's own generation speed plus its max_tokens/
    # thinking budget that determines how long a call legitimately takes,
    # not which agent role is calling it. A role-specific override still
    # wins if a caller passes generate(..., timeout=...) explicitly.
    timeout_secs: float = 600.0
    # Inference backend for local models.  Used to decide whether programmatic
    # load/unload via the LM Studio REST API is available.
    # Values: "lmstudio" | "ollama" | "llama_cpp" | "turboquant"
    provider: str = "lmstudio"
