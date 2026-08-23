from dataclasses import dataclass
from typing import Optional


@dataclass
class UsageInfo:
    """Real token counts reported by a provider's API response.

    Distinct from CostTracker's estimate_tokens() fallback — this is only
    ever built from a provider's own `usage` field, never derived from
    character counts.
    """
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    @classmethod
    def from_openai_usage(cls, usage: Optional[dict]) -> Optional["UsageInfo"]:
        """Build from an OpenAI-compatible `usage` object, or None if absent/empty."""
        if not usage:
            return None
        return cls(
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
        )
