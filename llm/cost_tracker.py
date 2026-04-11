from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Dict, List
from collections import defaultdict
import structlog

try:
    import tiktoken as _tiktoken
    _ENCODING = _tiktoken.get_encoding("cl100k_base")
except Exception:  # tiktoken not installed or encoding unavailable
    _ENCODING = None

logger = structlog.get_logger()


@dataclass
class CostRecord:
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost: float
    timestamp: datetime


class CostTracker:
    API_COSTS = {
        "anthropic": {"input": 0.003, "output": 0.015},
        "openai": {"input": 0.0005, "output": 0.0015},
    }

    def __init__(self):
        self.records: List[CostRecord] = []
        self.model_totals: Dict[str, Dict[str, int | float]] = defaultdict(
            lambda: {"prompt": 0, "completion": 0, "cost": 0.0}
        )
        self.logger = logger.bind(component="cost_tracker")

    def estimate_tokens(self, text: str) -> int:
        if _ENCODING is not None:
            return len(_ENCODING.encode(text))
        return len(text) // 4

    def track_usage(
        self, config, prompt: str, response: str
    ) -> None:
        prompt_tokens = self.estimate_tokens(prompt)
        completion_tokens = self.estimate_tokens(response)

        if config.type == "local":
            cost = 0.0
        elif hasattr(config, "cost_per_1k_input"):
            if config.cost_per_1k_input:
                cost = (
                    prompt_tokens * config.cost_per_1k_input / 1000
                    + completion_tokens * (config.cost_per_1k_output or 0) / 1000
                )
            else:
                cost = 0.0
        else:
            cost = 0.0

        record = CostRecord(
            model=config.name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=cost,
            timestamp=datetime.utcnow(),
        )
        self.records.append(record)

        totals = self.model_totals[config.name]
        totals["prompt"] += prompt_tokens
        totals["completion"] += completion_tokens
        totals["cost"] += cost

        self.logger.debug(
            "usage_tracked",
            model=config.name,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost=round(cost, 6),
        )

    def get_summary(self) -> dict:
        total_cost = sum(t["cost"] for t in self.model_totals.values())
        total_prompt = sum(int(t["prompt"]) for t in self.model_totals.values())
        total_completion = sum(
            int(t["completion"]) for t in self.model_totals.values()
        )

        return {
            "total_cost": round(total_cost, 6),
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_tokens": total_prompt + total_completion,
            "by_model": {k: dict(v) for k, v in self.model_totals.items()},
            "record_count": len(self.records),
        }

    def get_daily_costs(self, days: int = 30) -> List[dict]:
        daily = defaultdict(lambda: {"cost": 0.0, "tokens": 0})

        cutoff = datetime.utcnow() - timedelta(days=days)
        for record in self.records:
            if record.timestamp >= cutoff:
                date = record.timestamp.date().isoformat()
                daily[date]["cost"] += record.cost
                daily[date]["tokens"] += (
                    record.prompt_tokens + record.completion_tokens
                )

        return [{"date": k, **v} for k, v in sorted(daily.items())]
