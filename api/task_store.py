"""SQLite-backed agent task store.

Each job can have an ordered list of tasks (a task list). The orchestrator
works through tasks sequentially, routing each to the right agent. Agents may
append new tasks dynamically as they discover more work.

Table: agent_tasks
  Lives in the same SQLite file as the job store (data/jobs.db) for simplicity.
"""

import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import structlog

logger = structlog.get_logger()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_tasks (
    task_id     TEXT PRIMARY KEY,
    job_id      TEXT NOT NULL,
    sequence    INTEGER NOT NULL,
    description TEXT NOT NULL,
    agent_type  TEXT NOT NULL DEFAULT 'develop',
    status      TEXT NOT NULL DEFAULT 'pending',
    result      TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_agent_tasks_job_id ON agent_tasks (job_id);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class AgentTask:
    """Lightweight value object for a single agent task."""

    __slots__ = (
        "task_id", "job_id", "sequence", "description",
        "agent_type", "status", "result", "created_at", "updated_at",
    )

    def __init__(
        self,
        task_id: str,
        job_id: str,
        sequence: int,
        description: str,
        agent_type: str,
        status: str,
        result: Optional[str],
        created_at: str,
        updated_at: str,
    ):
        self.task_id = task_id
        self.job_id = job_id
        self.sequence = sequence
        self.description = description
        self.agent_type = agent_type
        self.status = status
        self.result = result
        self.created_at = created_at
        self.updated_at = updated_at

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "job_id": self.job_id,
            "sequence": self.sequence,
            "description": self.description,
            "agent_type": self.agent_type,
            "status": self.status,
            "result": self.result,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @property
    def is_terminal(self) -> bool:
        return self.status in ("done", "failed", "skipped")


class TaskStore:
    """Thread-safe SQLite task store for agent task lists.

    Shares the same DB file as JobStore. Initialise once at startup and
    pass the instance wherever the task loop needs it.
    """

    def __init__(self, db_path: str = "data/jobs.db"):
        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        logger.info("task_store_initialized", db_path=db_path)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def create_task(
        self,
        job_id: str,
        description: str,
        agent_type: str = "develop",
        sequence: Optional[int] = None,
    ) -> AgentTask:
        """Create and persist a single task. Auto-assigns sequence if not provided."""
        if sequence is None:
            sequence = self.next_sequence(job_id)
        task_id = f"task_{uuid.uuid4().hex[:12]}"
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                """INSERT INTO agent_tasks
                   (task_id, job_id, sequence, description, agent_type,
                    status, result, created_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (task_id, job_id, sequence, description, agent_type,
                 "pending", None, now, now),
            )
            self._conn.commit()
        task = AgentTask(task_id, job_id, sequence, description, agent_type,
                         "pending", None, now, now)
        logger.info("task_created", task_id=task_id, job_id=job_id,
                    sequence=sequence, agent_type=agent_type)
        return task

    def create_tasks(
        self, job_id: str, task_specs: List[Dict[str, str]]
    ) -> List[AgentTask]:
        """Bulk-create an ordered list of tasks for a job.

        task_specs: [{"description": "...", "agent_type": "develop"}, ...]
        """
        tasks = []
        for i, spec in enumerate(task_specs, start=1):
            task = self.create_task(
                job_id=job_id,
                description=spec["description"],
                agent_type=spec.get("agent_type", "develop"),
                sequence=i,
            )
            tasks.append(task)
        return tasks

    def update_task(
        self,
        task_id: str,
        status: str,
        result: Optional[str] = None,
    ) -> None:
        """Update a task's status and optional result summary."""
        now = _now_iso()
        with self._lock:
            self._conn.execute(
                "UPDATE agent_tasks SET status=?, result=?, updated_at=? WHERE task_id=?",
                (status, result, now, task_id),
            )
            self._conn.commit()
        logger.info("task_updated", task_id=task_id, status=status)

    def delete_job_tasks(self, job_id: str) -> int:
        """Remove all tasks for a job. Called when the parent job is pruned."""
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM agent_tasks WHERE job_id=?", (job_id,)
            )
            self._conn.commit()
        return cursor.rowcount

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_task(self, task_id: str) -> Optional[AgentTask]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
        return self._row_to_task(row) if row else None

    def get_next_pending(self, job_id: str) -> Optional[AgentTask]:
        """Return the lowest-sequence pending task for this job, or None."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM agent_tasks "
                "WHERE job_id=? AND status='pending' "
                "ORDER BY sequence LIMIT 1",
                (job_id,),
            ).fetchone()
        return self._row_to_task(row) if row else None

    def list_tasks(self, job_id: str) -> List[AgentTask]:
        """Return all tasks for a job ordered by sequence."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM agent_tasks WHERE job_id=? ORDER BY sequence",
                (job_id,),
            ).fetchall()
        return [self._row_to_task(r) for r in rows]

    def all_done(self, job_id: str) -> bool:
        """True if every task is in a terminal state (none pending or running)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM agent_tasks "
                "WHERE job_id=? AND status IN ('pending','running')",
                (job_id,),
            ).fetchone()
        return row[0] == 0

    def next_sequence(self, job_id: str) -> int:
        """Return the next available sequence number for this job."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(sequence) FROM agent_tasks WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return (row[0] or 0) + 1

    def task_counts(self, job_id: str) -> Dict[str, int]:
        """Return {status: count} for all tasks in a job."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) FROM agent_tasks WHERE job_id=? GROUP BY status",
                (job_id,),
            ).fetchall()
        return {row[0]: row[1] for row in rows}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _row_to_task(self, row: sqlite3.Row) -> AgentTask:
        return AgentTask(
            task_id=row["task_id"],
            job_id=row["job_id"],
            sequence=row["sequence"],
            description=row["description"],
            agent_type=row["agent_type"],
            status=row["status"],
            result=row["result"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
