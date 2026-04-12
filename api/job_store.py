"""SQLite-backed job store with in-memory hot cache.

Write-through pattern:
  - All reads use the in-memory dict (_cache) for O(1) polling.
  - All writes hit SQLite immediately so jobs survive API restarts.
  - On startup, load() restores the cache from the DB and marks any
    stale "running" jobs as "failed" (the coroutine died with the process).
  - Expired jobs (older than TTL_HOURS) are pruned on startup and on
    every new job creation so the DB never grows unboundedly.
"""

import json
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog

logger = structlog.get_logger()

TTL_HOURS = 24
_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id       TEXT PRIMARY KEY,
    status       TEXT NOT NULL DEFAULT 'pending',
    phase        TEXT NOT NULL DEFAULT 'pending',
    task_type    TEXT NOT NULL DEFAULT 'chat',
    summary      TEXT,
    files_created TEXT NOT NULL DEFAULT '[]',
    error        TEXT,
    session_id   TEXT NOT NULL,
    task         TEXT NOT NULL,
    full_response TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _expiry_cutoff() -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=TTL_HOURS)).isoformat()


class JobStore:
    """Thread-safe SQLite job store with in-memory read cache."""

    def __init__(self, db_path: str = "data/jobs.db"):
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._cache: Dict[str, dict] = {}
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute(_SCHEMA)
        self._conn.commit()
        logger.info("job_store_initialized", db_path=db_path)

    def load(self) -> None:
        """Load all jobs from SQLite into the in-memory cache.

        Called once at startup. Marks stale running jobs as failed and
        prunes expired records.
        """
        with self._lock:
            self._prune_expired_locked()
            # Mark any jobs that were still running when the process died
            self._conn.execute(
                "UPDATE jobs SET status='failed', phase='failed', "
                "error='API restarted while job was running', updated_at=? "
                "WHERE status IN ('pending', 'running')",
                (_now_iso(),),
            )
            self._conn.commit()
            rows = self._conn.execute("SELECT * FROM jobs").fetchall()
            for row in rows:
                self._cache[row["job_id"]] = self._row_to_dict(row)
        logger.info("jobs_loaded", count=len(self._cache))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def create(self, job_id: str, session_id: str, task: str, task_type: str, phase: str) -> dict:
        """Create a new job record and return its dict."""
        now = _now_iso()
        record: Dict[str, Any] = {
            "job_id": job_id,
            "status": "pending",
            "phase": phase,
            "task_type": task_type,
            "summary": None,
            "files_created": [],
            "error": None,
            "session_id": session_id,
            "task": task,
            "_full_response": None,
            "created_at": now,
            "updated_at": now,
        }
        with self._lock:
            self._prune_expired_locked()
            self._conn.execute(
                """INSERT INTO jobs
                   (job_id, status, phase, task_type, summary, files_created, error,
                    session_id, task, full_response, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    job_id, "pending", phase, task_type, None, "[]", None,
                    session_id, task, None, now, now,
                ),
            )
            self._conn.commit()
            self._cache[job_id] = record
        return record

    def get(self, job_id: str) -> Optional[dict]:
        """Return job dict from cache, or None if not found."""
        return self._cache.get(job_id)

    def update(self, job_id: str, **fields: Any) -> None:
        """Merge *fields* into the cached record and persist to SQLite."""
        with self._lock:
            record = self._cache.get(job_id)
            if record is None:
                logger.warning("job_update_missing", job_id=job_id)
                return
            record.update(fields)
            record["updated_at"] = _now_iso()
            self._conn.execute(
                """UPDATE jobs SET
                   status=?, phase=?, task_type=?, summary=?, files_created=?,
                   error=?, full_response=?, updated_at=?
                   WHERE job_id=?""",
                (
                    record["status"],
                    record["phase"],
                    record["task_type"],
                    record["summary"],
                    json.dumps(record.get("files_created") or []),
                    record["error"],
                    record.get("_full_response"),
                    record["updated_at"],
                    job_id,
                ),
            )
            self._conn.commit()

    def list_jobs(self, limit: int = 50, offset: int = 0) -> List[dict]:
        """Return a page of jobs ordered by creation time descending."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        result = []
        for row in rows:
            d = self._row_to_dict(row)
            d.pop("_full_response", None)
            result.append(d)
        return result

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        # Deserialize JSON-encoded list
        raw_files = d.get("files_created") or "[]"
        try:
            d["files_created"] = json.loads(raw_files)
        except (json.JSONDecodeError, TypeError):
            d["files_created"] = []
        # Rename DB column → internal key used by the rest of the API
        d["_full_response"] = d.pop("full_response", None)
        return d

    def _prune_expired_locked(self) -> None:
        """Delete jobs older than TTL_HOURS. Caller must hold _lock."""
        cutoff = _expiry_cutoff()
        cursor = self._conn.execute(
            "DELETE FROM jobs WHERE created_at < ?", (cutoff,)
        )
        pruned = cursor.rowcount
        self._conn.commit()
        if pruned:
            # Evict from cache too
            expired_ids = [jid for jid, j in self._cache.items()
                           if j.get("created_at", "") < cutoff]
            for jid in expired_ids:
                self._cache.pop(jid, None)
            logger.info("jobs_pruned", count=pruned)
