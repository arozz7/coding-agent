"""Unit tests for session memory."""
import pytest
import tempfile
import os
from pathlib import Path


class TestSessionMemory:
    def test_create_session(self, tmp_path):
        from agent.memory import SessionMemory

        db_path = tmp_path / "test.db"
        memory = SessionMemory(str(db_path))
        session_id = memory.create_session("test_session", str(tmp_path))
        assert session_id == "test_session"
        memory.close()

    def test_save_and_get_messages(self, tmp_path):
        from agent.memory import SessionMemory

        db_path = tmp_path / "test.db"
        memory = SessionMemory(str(db_path))
        memory.create_session("test_session")
        memory.save_message("test_session", "user", "Hello")
        memory.save_message("test_session", "assistant", "Hi there")
        history = memory.get_conversation_history("test_session")
        assert len(history) == 2
        assert history[0]["content"] == "Hello"
        assert history[1]["content"] == "Hi there"
        memory.close()

    def test_update_task_status(self, tmp_path):
        from agent.memory import SessionMemory

        db_path = tmp_path / "test.db"
        memory = SessionMemory(str(db_path))
        memory.create_session("test_session")
        memory.update_task_status("test_session", "Test task", "completed")
        summary = memory.get_session_summary("test_session")
        assert "completed" in summary["tasks"]
        memory.close()
