"""Unit tests for project deletion — storage layer and orchestrator."""
import shutil
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from agent.memory.session_memory import SessionMemory
from api.task_store import TaskStore


# ---------------------------------------------------------------------------
# SessionMemory — list / delete by project
# ---------------------------------------------------------------------------

class TestSessionMemoryProjectOps:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.db = str(Path(self.tmp) / "memory.db")
        self.mem = SessionMemory(self.db)

    def teardown_method(self):
        self.mem.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_sessions_by_project_exact_match(self):
        self.mem.create_session("s1", "/ws/proj-a")
        self.mem.create_session("s2", "/ws/proj-b")
        self.mem.create_session("s3", "/ws/proj-a")

        ids = self.mem.list_sessions_by_project("/ws/proj-a")
        assert sorted(ids) == ["s1", "s3"]

    def test_list_sessions_by_project_no_match(self):
        self.mem.create_session("s1", "/ws/proj-a")
        assert self.mem.list_sessions_by_project("/ws/proj-x") == []

    def test_list_sessions_normalises_separators(self):
        self.mem.create_session("s1", r"C:\ws\proj-a")
        ids = self.mem.list_sessions_by_project("C:/ws/proj-a")
        assert ids == ["s1"]

    def test_delete_sessions_by_project_cascades(self):
        self.mem.create_session("s1", "/ws/proj-a")
        self.mem.save_message("s1", "user", "hello")
        self.mem.create_session("s2", "/ws/proj-b")

        count = self.mem.delete_sessions_by_project("/ws/proj-a")

        assert count == 1
        # Session and its messages gone
        assert self.mem.get_session_summary("s1")["session_id"] == "s1"  # returns stub
        conn = sqlite3.connect(self.db)
        row = conn.execute("SELECT COUNT(*) FROM messages WHERE session_id='s1'").fetchone()
        conn.close()
        assert row[0] == 0

    def test_delete_sessions_by_project_leaves_others(self):
        self.mem.create_session("s1", "/ws/proj-a")
        self.mem.create_session("s2", "/ws/proj-b")

        self.mem.delete_sessions_by_project("/ws/proj-a")

        remaining = self.mem.list_sessions_by_project("/ws/proj-b")
        assert remaining == ["s2"]

    def test_delete_sessions_by_project_returns_zero_when_none(self):
        assert self.mem.delete_sessions_by_project("/ws/no-such") == 0


# ---------------------------------------------------------------------------
# TaskStore — count / delete by session IDs
# ---------------------------------------------------------------------------

class TestTaskStoreProjectOps:
    def setup_method(self):
        self.tmp = tempfile.mkdtemp()
        self.db = str(Path(self.tmp) / "jobs.db")
        # JobStore creates the `jobs` table; TaskStore creates `agent_tasks`.
        # Both share the same file, so initialise JobStore first.
        from api.job_store import JobStore
        self._job_store = JobStore(self.db)
        self.store = TaskStore(self.db)
        # Insert jobs rows directly (TaskStore shares the DB with JobStore)
        self.store._conn.execute(
            "INSERT INTO jobs (job_id, session_id, task, status, phase, task_type, "
            "files_created, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("j1", "s1", "task1", "done", "done", "chat", "[]", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        self.store._conn.execute(
            "INSERT INTO jobs (job_id, session_id, task, status, phase, task_type, "
            "files_created, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("j2", "s1", "task2", "done", "done", "chat", "[]", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        self.store._conn.execute(
            "INSERT INTO jobs (job_id, session_id, task, status, phase, task_type, "
            "files_created, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("j3", "s2", "task3", "done", "done", "chat", "[]", "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        self.store._conn.execute(
            "INSERT INTO agent_tasks (task_id, job_id, sequence, description, agent_type, "
            "status, result, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("t1", "j1", 1, "do stuff", "develop", "done", None, "2026-01-01T00:00:00", "2026-01-01T00:00:00"),
        )
        self.store._conn.commit()

    def teardown_method(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_count_by_session_ids_correct(self):
        assert self.store.count_by_session_ids(["s1"]) == 2
        assert self.store.count_by_session_ids(["s2"]) == 1
        assert self.store.count_by_session_ids(["s1", "s2"]) == 3

    def test_count_by_session_ids_empty_list(self):
        assert self.store.count_by_session_ids([]) == 0

    def test_delete_by_session_ids_removes_jobs_and_tasks(self):
        deleted = self.store.delete_by_session_ids(["s1"])

        assert deleted == 2
        row = self.store._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE session_id='s1'"
        ).fetchone()
        assert row[0] == 0
        # agent_tasks for j1 also gone
        row = self.store._conn.execute(
            "SELECT COUNT(*) FROM agent_tasks WHERE job_id='j1'"
        ).fetchone()
        assert row[0] == 0

    def test_delete_by_session_ids_leaves_other_sessions(self):
        self.store.delete_by_session_ids(["s1"])
        row = self.store._conn.execute(
            "SELECT COUNT(*) FROM jobs WHERE session_id='s2'"
        ).fetchone()
        assert row[0] == 1

    def test_delete_by_session_ids_empty_list(self):
        assert self.store.delete_by_session_ids([]) == 0


# ---------------------------------------------------------------------------
# Orchestrator.delete_project — dry_run and full delete
# ---------------------------------------------------------------------------

class TestOrchestratorDeleteProject:
    def _make_orchestrator(self, tmp_path):
        """Build a minimal orchestrator with real in-memory stores."""
        from agent.orchestrator import AgentOrchestrator
        from unittest.mock import MagicMock

        model_router = MagicMock()
        model_router.register_switch_callback = MagicMock()

        mem_db = str(tmp_path / "memory.db")
        jobs_db = str(tmp_path / "jobs.db")
        chroma = str(tmp_path / "chroma")

        with (
            patch("agent.orchestrator.SkillManager"),
            patch("agent.orchestrator.WikiManager"),
            patch("agent.orchestrator.MemoryWiki"),
            patch("mcp.server.create_mcp_server"),
            patch("agent.orchestrator.DeveloperAgent"),
            patch("agent.orchestrator.PlanAgent"),
            patch("agent.orchestrator.TesterAgent"),
            patch("agent.orchestrator.ReviewerAgent"),
            patch("agent.orchestrator.ArchitectAgent"),
            patch("agent.orchestrator.ChatAgent"),
            patch("agent.orchestrator.ResearchAgent"),
            patch("agent.orchestrator.MapperAgent"),
            patch("agent.orchestrator.RedTeamAgent"),
            patch("agent.orchestrator.DocumenterAgent"),
            patch("agent.orchestrator.PlannerAgent"),
            patch("agent.orchestrator.PlanReviewerAgent"),
            patch("agent.orchestrator.ChainRunner"),
            patch("agent.orchestrator.SkillExecutor"),
            patch("agent.orchestrator.AgentLogger"),
            patch("os.getenv", side_effect=lambda k, d="": {
                "AGENT_EFFECTIVE_WORKSPACE": "",
                "WORKSPACE_PATH": str(tmp_path),
            }.get(k, d)),
        ):
            orch = AgentOrchestrator(
                workspace_path=str(tmp_path),
                model_router=model_router,
                session_db_path=mem_db,
                chroma_path=chroma,
            )
        # Override task_store with one pointing at the tmp DB.
        # JobStore must be initialised first so the `jobs` table schema exists.
        from api.job_store import JobStore
        from api.task_store import TaskStore
        JobStore(jobs_db)  # creates `jobs` table; instance not needed further
        orch.task_store = TaskStore(jobs_db)
        return orch

    def test_dry_run_returns_counts_without_deleting(self, tmp_path):
        proj_path = tmp_path / "my-proj"
        proj_path.mkdir()
        (proj_path / ".agent-wiki").mkdir()
        index = proj_path / ".agent-wiki" / "index.md"
        index.write_text(
            "# Agent Wiki Index\n\n| Path | Title | Category | Tags | Updated |\n"
            "|------|-------|----------|------|----------|\n"
            "| .agent-wiki/tech-patterns/foo.md | Foo | tech-patterns |  | 2026-01-01T00:00:00Z |\n"
        )

        orch = self._make_orchestrator(tmp_path)
        orch.session_memory.create_session("sess-1", str(proj_path))

        result = orch.delete_project(str(proj_path), dry_run=True)

        assert result["dry_run"] is True
        assert result["sessions"] == 1
        assert result["wiki_entries"] == 1
        # Wiki directory still exists
        assert (proj_path / ".agent-wiki").exists()
        # Session still exists
        assert orch.session_memory.list_sessions_by_project(str(proj_path)) == ["sess-1"]

    def test_full_delete_clears_all_stores(self, tmp_path):
        proj_path = tmp_path / "my-proj"
        proj_path.mkdir()
        wiki_dir = proj_path / ".agent-wiki"
        wiki_dir.mkdir()
        (wiki_dir / "index.md").write_text("# Agent Wiki Index\n\n")

        orch = self._make_orchestrator(tmp_path)
        orch.session_memory.create_session("sess-2", str(proj_path))

        result = orch.delete_project(str(proj_path), dry_run=False)

        assert result["dry_run"] is False
        assert result["deleted_sessions"] == 1
        # Session gone
        assert orch.session_memory.list_sessions_by_project(str(proj_path)) == []
        # Wiki directory removed
        assert not wiki_dir.exists()
        # Source files untouched
        assert proj_path.exists()

    def test_delete_project_no_wiki_dir(self, tmp_path):
        proj_path = tmp_path / "bare-proj"
        proj_path.mkdir()

        orch = self._make_orchestrator(tmp_path)
        result = orch.delete_project(str(proj_path), dry_run=False)

        assert result["wiki_entries"] == 0
        assert result["deleted_sessions"] == 0
