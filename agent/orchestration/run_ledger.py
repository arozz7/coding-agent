"""RunLedger — append-only record of task-loop outcomes.

Phases 1-3 of the agent-quality improvement plan (objective grounding,
verifier-gated completion, anti-gaming criteria) change *how* the loop
behaves. This ledger exists so the effect is measurable run-over-run instead
of taken on faith — one JSON line per completed TaskLoop.run(), covering the
signals that mattered in the original diagnosis: verifier score, whether it
actually passed, and how many fix rounds it took to get there.

Never raises — a broken ledger write must not break the task loop it's
observing.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Union

import structlog

logger = structlog.get_logger()

_MAX_OBJECTIVE_CHARS = 200


class RunLedger:
    """Appends one JSON line per task-loop run to a JSONL file."""

    def __init__(self, path: Union[str, Path] = "data/run_ledger.jsonl") -> None:
        self.path = Path(path)
        self.logger = logger.bind(component="run_ledger")

    def record(
        self,
        *,
        objective: str,
        task_type: str,
        verifier_score: "int | None",
        verifier_passed: bool,
        tasks_completed: int,
        tasks_failed: int,
        criterion_fix_count: int,
        acceptance_fix_count: int,
        duration_seconds: float,
    ) -> None:
        entry = {
            "timestamp": time.time(),
            "objective": objective[:_MAX_OBJECTIVE_CHARS],
            "task_type": task_type,
            "verifier_score": verifier_score,
            "verifier_passed": verifier_passed,
            "tasks_completed": tasks_completed,
            "tasks_failed": tasks_failed,
            "criterion_fix_count": criterion_fix_count,
            "acceptance_fix_count": acceptance_fix_count,
            "duration_seconds": round(duration_seconds, 2),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(entry) + "\n")
        except Exception as exc:
            self.logger.warning("run_ledger_write_failed", error=str(exc))
