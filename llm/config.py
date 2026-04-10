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
