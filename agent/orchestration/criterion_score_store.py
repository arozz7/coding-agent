"""Persistent Bayesian confidence scores for auto-checkable criterion patterns.

Tracks how often each criterion *type* gets fixed across tasks so the fix-loop
can allocate its attempt budget intelligently — giving up faster on patterns that
historically never succeed, and staying patient with ones that usually do.

Confidence formula (Laplace smoothing):
    confidence = (successes + 1) / (attempts + 2)

Starts at 0.5 with no data, converges toward true fix rate as evidence accumulates.
"""

import json
from pathlib import Path

import structlog

logger = structlog.get_logger()

# Maps criterion prefixes to normalized pattern keys used for scoring.
_PATTERN_MAP = (
    ("file exists:", "file_exists"),
    ("file contains:", "file_contains"),
    ("command exits 0:", "command_exits_0"),
)


def _normalize(criterion: str) -> str:
    """Return the pattern key for a criterion string."""
    lower = criterion.lower().strip()
    for prefix, key in _PATTERN_MAP:
        if lower.startswith(prefix):
            return key
    return "behavioral"


class CriterionScoreStore:
    """Persists per-pattern fix-success rates to a JSON file.

    Data shape: {"file_exists": {"successes": 4, "attempts": 5}, ...}

    Usage::

        store = CriterionScoreStore()
        budget = store.attempt_budget(criterion)   # dynamic, replaces hardcoded 3
        store.record(criterion, succeeded=True)    # call at terminal states
    """

    _SKIP_THRESHOLD = 0.35
    _MIN_ATTEMPTS_FOR_SKIP = 4  # need at least this many data points before cutting budget

    def __init__(self, store_path: str = "data/criterion_scores.json"):
        self._path = Path(store_path)
        self._data: dict[str, dict[str, int]] = self._load()
        self.logger = logger.bind(component="criterion_score_store")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, criterion: str, succeeded: bool) -> None:
        """Record the outcome of one fix attempt for the given criterion."""
        key = _normalize(criterion)
        entry = self._data.setdefault(key, {"successes": 0, "attempts": 0})
        entry["attempts"] += 1
        if succeeded:
            entry["successes"] += 1
        self._save()
        self.logger.info(
            "criterion_score_recorded",
            pattern=key,
            succeeded=succeeded,
            confidence=round(self.get_confidence(criterion), 3),
            total_attempts=entry["attempts"],
        )

    def get_confidence(self, criterion: str) -> float:
        """Return Bayesian confidence (0–1) that this criterion type can be fixed."""
        key = _normalize(criterion)
        entry = self._data.get(key, {})
        s = entry.get("successes", 0)
        n = entry.get("attempts", 0)
        return (s + 1) / (n + 2)

    def attempt_budget(self, criterion: str) -> int:
        """Dynamic per-criterion fix-attempt budget based on historical confidence.

        Returns 1–4:
          - No data yet (< _MIN_ATTEMPTS_FOR_SKIP): use default of 3
          - confidence < 0.35 (historically unfixable): 1 — give up fast
          - confidence < 0.50: 2
          - confidence >= 0.50: 3 (default)
          - confidence >= 0.70 (reliably fixable): 4 — stay patient
        """
        key = _normalize(criterion)
        n = self._data.get(key, {}).get("attempts", 0)
        if n < self._MIN_ATTEMPTS_FOR_SKIP:
            return 3
        conf = self.get_confidence(criterion)
        if conf < self._SKIP_THRESHOLD:
            return 1
        if conf < 0.50:
            return 2
        if conf >= 0.70:
            return 4
        return 3

    def stats(self) -> dict:
        """Return all patterns with their confidence scores — for diagnostics."""
        return {
            k: {
                **v,
                "confidence": round((v["successes"] + 1) / (v["attempts"] + 2), 3),
            }
            for k, v in self._data.items()
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _load(self) -> dict[str, dict[str, int]]:
        try:
            if self._path.exists():
                return json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        except Exception as exc:
            self.logger.warning("criterion_score_save_failed", error=str(exc))
