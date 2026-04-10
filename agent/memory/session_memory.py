import sqlite3
import json
from datetime import datetime
from typing import List, Optional
from pathlib import Path
import structlog

logger = structlog.get_logger()


class SessionMemory:
    def __init__(self, db_path: str = "data/memory.db"):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._initialize_schema()
        self.logger = logger.bind(component="session_memory")

    def _initialize_schema(self) -> None:
        cursor = self.conn.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                project_path TEXT,
                status TEXT DEFAULT 'active',
                current_task TEXT,
                metadata TEXT
            )
        """
        )

        cursor.execute(
            """
            PRAGMA table_info(sessions)
        """
        )
        columns = [row[1] for row in cursor.fetchall()]
        if "metadata" not in columns:
            cursor.execute("ALTER TABLE sessions ADD COLUMN metadata TEXT")
        if "updated_at" not in columns:
            cursor.execute("ALTER TABLE sessions ADD COLUMN updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP")

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                tokens_used INTEGER DEFAULT 0,
                model_name TEXT,
                tool_calls TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
        """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                description TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                result TEXT,
                FOREIGN KEY (session_id) REFERENCES sessions(id)
            )
        """
        )

        self.conn.commit()

    def create_session(
        self, session_id: str, project_path: Optional[str] = None, metadata: Optional[dict] = None
    ) -> str:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO sessions (id, project_path, status, metadata, updated_at)
            VALUES (?, ?, 'active', ?, CURRENT_TIMESTAMP)
        """,
            (session_id, project_path, json.dumps(metadata) if metadata else None),
        )
        self.conn.commit()
        self.logger.info("session_created", session_id=session_id)
        return session_id

    def save_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tokens_used: int = 0,
        model_name: Optional[str] = None,
        tool_calls: Optional[List[dict]] = None,
    ) -> None:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO messages 
            (session_id, role, content, tokens_used, model_name, tool_calls)
            VALUES (?, ?, ?, ?, ?, ?)
        """,
            (
                session_id,
                role,
                content,
                tokens_used,
                model_name,
                json.dumps(tool_calls) if tool_calls else None,
            ),
        )
        cursor.execute(
            """
            UPDATE sessions SET updated_at = CURRENT_TIMESTAMP WHERE id = ?
        """,
            (session_id,),
        )
        self.conn.commit()

    def get_conversation_history(
        self, session_id: str, max_messages: int = 50
    ) -> List[dict]:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            SELECT role, content, tokens_used, model_name, timestamp
            FROM messages
            WHERE session_id = ?
            ORDER BY timestamp ASC
            LIMIT ?
        """,
            (session_id, max_messages),
        )
        messages = cursor.fetchall()
        return [
            {
                "role": r,
                "content": c,
                "tokens": t,
                "model": m,
                "timestamp": ts,
            }
            for r, c, t, m, ts in messages
        ]

    def update_task_status(
        self,
        session_id: str,
        task_desc: str,
        status: str,
        result: Optional[dict] = None,
    ) -> int:
        cursor = self.conn.cursor()

        cursor.execute(
            """
            SELECT id FROM tasks WHERE session_id = ? AND description = ?
        """,
            (session_id, task_desc),
        )
        existing = cursor.fetchone()

        if existing:
            task_id = existing[0]
            cursor.execute(
                """
                UPDATE tasks 
                SET status = ?, completed_at = CURRENT_TIMESTAMP, result = ?
                WHERE id = ?
            """,
                (status, json.dumps(result) if result else None, task_id),
            )
        else:
            cursor.execute(
                """
                INSERT INTO tasks (session_id, description, status, result)
                VALUES (?, ?, ?, ?)
            """,
                (session_id, task_desc, status, json.dumps(result) if result else None),
            )

        self.conn.commit()
        return cursor.rowcount

    def get_session_summary(self, session_id: str) -> dict:
        cursor = self.conn.cursor()

        cursor.execute(
            "SELECT COUNT(*) FROM messages WHERE session_id = ?",
            (session_id,),
        )
        message_count = cursor.fetchone()[0]

        cursor.execute(
            """
            SELECT status, COUNT(*) FROM tasks 
            WHERE session_id = ? GROUP BY status
        """,
            (session_id,),
        )
        task_counts = dict(cursor.fetchall())

        cursor.execute(
            """
            SELECT created_at, updated_at, project_path, status, metadata 
            FROM sessions WHERE id = ?
        """,
            (session_id,),
        )
        row = cursor.fetchone()

        if row:
            return {
                "session_id": session_id,
                "message_count": message_count,
                "tasks": task_counts,
                "created_at": row[0],
                "updated_at": row[1],
                "project_path": row[2],
                "status": row[3],
                "metadata": json.loads(row[4]) if row[4] else None,
            }
        return {"session_id": session_id, "message_count": 0, "tasks": {}}

    def list_sessions(self, limit: int = 20, status: Optional[str] = None) -> List[dict]:
        cursor = self.conn.cursor()
        
        query = """
            SELECT id, created_at, updated_at, project_path, status, metadata,
                   (SELECT COUNT(*) FROM messages WHERE session_id = s.id) as msg_count
            FROM sessions s
        """
        params = []
        
        if status:
            query += " WHERE status = ?"
            params.append(status)
        
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        
        cursor.execute(query, params)
        rows = cursor.fetchall()
        
        return [
            {
                "session_id": row[0],
                "created_at": row[1],
                "updated_at": row[2],
                "project_path": row[3],
                "status": row[4],
                "metadata": json.loads(row[5]) if row[5] else None,
                "message_count": row[6],
            }
            for row in rows
        ]

    def get_or_create_session(
        self, session_id: str, project_path: Optional[str] = None
    ) -> dict:
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT id FROM sessions WHERE id = ?",
            (session_id,),
        )
        existing = cursor.fetchone()
        
        if not existing:
            self.create_session(session_id, project_path)
        
        return self.get_session_summary(session_id)

    def update_session_status(self, session_id: str, status: str) -> None:
        cursor = self.conn.cursor()
        cursor.execute(
            """
            UPDATE sessions SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?
        """,
            (status, session_id),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
