"""Unit tests for the TaskStore."""
import os
import tempfile
import pytest

from api.task_store import TaskStore, AgentTask


@pytest.fixture
def store(tmp_path):
    db = str(tmp_path / "test_tasks.db")
    return TaskStore(db_path=db)


class TestTaskStoreCreate:
    def test_create_task_returns_agent_task(self, store):
        task = store.create_task("job1", "Run npm start", "develop", sequence=1)
        assert isinstance(task, AgentTask)
        assert task.task_id.startswith("task_")
        assert task.job_id == "job1"
        assert task.sequence == 1
        assert task.description == "Run npm start"
        assert task.agent_type == "develop"
        assert task.status == "pending"
        assert task.result is None

    def test_create_task_auto_sequence(self, store):
        t1 = store.create_task("job1", "First task", "develop")
        t2 = store.create_task("job1", "Second task", "develop")
        assert t2.sequence == t1.sequence + 1

    def test_create_tasks_bulk(self, store):
        specs = [
            {"description": "Research project", "agent_type": "research"},
            {"description": "Fix the bug", "agent_type": "develop"},
            {"description": "Run tests", "agent_type": "test"},
        ]
        tasks = store.create_tasks("job2", specs)
        assert len(tasks) == 3
        assert tasks[0].sequence == 1
        assert tasks[1].sequence == 2
        assert tasks[2].sequence == 3
        assert tasks[0].agent_type == "research"

    def test_create_tasks_default_agent_type(self, store):
        specs = [{"description": "Do something"}]
        tasks = store.create_tasks("job3", specs)
        assert tasks[0].agent_type == "develop"


class TestTaskStoreRead:
    def test_get_task_by_id(self, store):
        created = store.create_task("job1", "desc", "develop", 1)
        fetched = store.get_task(created.task_id)
        assert fetched is not None
        assert fetched.task_id == created.task_id
        assert fetched.description == "desc"

    def test_get_task_missing(self, store):
        assert store.get_task("nonexistent") is None

    def test_get_next_pending_order(self, store):
        store.create_task("job1", "Task 3", "develop", sequence=3)
        store.create_task("job1", "Task 1", "develop", sequence=1)
        store.create_task("job1", "Task 2", "develop", sequence=2)
        next_task = store.get_next_pending("job1")
        assert next_task is not None
        assert next_task.sequence == 1

    def test_get_next_pending_skips_done(self, store):
        t1 = store.create_task("job1", "Task 1", "develop", sequence=1)
        store.create_task("job1", "Task 2", "develop", sequence=2)
        store.update_task(t1.task_id, "done")
        next_task = store.get_next_pending("job1")
        assert next_task is not None
        assert next_task.sequence == 2

    def test_get_next_pending_none_when_all_done(self, store):
        t1 = store.create_task("job1", "Only task", "develop", sequence=1)
        store.update_task(t1.task_id, "done")
        assert store.get_next_pending("job1") is None

    def test_list_tasks_ordered(self, store):
        store.create_task("job1", "C", "develop", sequence=3)
        store.create_task("job1", "A", "develop", sequence=1)
        store.create_task("job1", "B", "develop", sequence=2)
        listed = store.list_tasks("job1")
        assert [t.sequence for t in listed] == [1, 2, 3]

    def test_list_tasks_different_jobs(self, store):
        store.create_task("job1", "For job1", "develop", 1)
        store.create_task("job2", "For job2", "develop", 1)
        job1_tasks = store.list_tasks("job1")
        assert len(job1_tasks) == 1
        assert job1_tasks[0].job_id == "job1"


class TestTaskStoreUpdate:
    def test_update_status_and_result(self, store):
        task = store.create_task("job1", "Run app", "develop", 1)
        store.update_task(task.task_id, "done", "App started successfully")
        fetched = store.get_task(task.task_id)
        assert fetched.status == "done"
        assert fetched.result == "App started successfully"

    def test_update_to_failed(self, store):
        task = store.create_task("job1", "Run app", "develop", 1)
        store.update_task(task.task_id, "failed", "npm: command not found")
        fetched = store.get_task(task.task_id)
        assert fetched.status == "failed"


class TestTaskStoreAllDone:
    def test_all_done_false_when_pending(self, store):
        store.create_task("job1", "Task", "develop", 1)
        assert store.all_done("job1") is False

    def test_all_done_true_when_all_terminal(self, store):
        t1 = store.create_task("job1", "T1", "develop", 1)
        t2 = store.create_task("job1", "T2", "develop", 2)
        store.update_task(t1.task_id, "done")
        store.update_task(t2.task_id, "skipped")
        assert store.all_done("job1") is True

    def test_all_done_false_when_running(self, store):
        t1 = store.create_task("job1", "T1", "develop", 1)
        store.update_task(t1.task_id, "running")
        assert store.all_done("job1") is False

    def test_all_done_true_for_empty_job(self, store):
        # A job with no tasks is trivially done
        assert store.all_done("nonexistent-job") is True


class TestTaskStoreNextSequence:
    def test_next_sequence_starts_at_1(self, store):
        assert store.next_sequence("new-job") == 1

    def test_next_sequence_increments(self, store):
        store.create_task("job1", "T1", "develop", 1)
        store.create_task("job1", "T2", "develop", 2)
        assert store.next_sequence("job1") == 3


class TestTaskStoreCounts:
    def test_task_counts(self, store):
        t1 = store.create_task("job1", "T1", "develop", 1)
        t2 = store.create_task("job1", "T2", "develop", 2)
        t3 = store.create_task("job1", "T3", "develop", 3)
        store.update_task(t1.task_id, "done")
        store.update_task(t2.task_id, "failed")
        counts = store.task_counts("job1")
        assert counts.get("done", 0) == 1
        assert counts.get("failed", 0) == 1
        assert counts.get("pending", 0) == 1


class TestAgentTaskProperties:
    def test_is_terminal_done(self, store):
        t = store.create_task("job1", "T", "develop", 1)
        store.update_task(t.task_id, "done")
        fetched = store.get_task(t.task_id)
        assert fetched.is_terminal is True

    def test_is_terminal_pending(self, store):
        t = store.create_task("job1", "T", "develop", 1)
        assert t.is_terminal is False

    def test_to_dict_has_all_keys(self, store):
        t = store.create_task("job1", "T", "develop", 1)
        d = t.to_dict()
        expected_keys = {
            "task_id", "job_id", "sequence", "description",
            "agent_type", "status", "result", "created_at", "updated_at",
        }
        assert expected_keys == set(d.keys())
