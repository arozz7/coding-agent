"""Unit tests for RunLedger."""
import json

from agent.orchestration.run_ledger import RunLedger


class TestRunLedger:
    def test_record_appends_jsonl_line(self, tmp_path):
        ledger_path = tmp_path / "run_ledger.jsonl"
        ledger = RunLedger(ledger_path)
        ledger.record(objective="build a game", task_type="develop", verifier_score=8, verifier_passed=True,
                       tasks_completed=5, tasks_failed=0, criterion_fix_count=0, acceptance_fix_count=0,
                       duration_seconds=42.5)

        lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        entry = json.loads(lines[0])
        assert entry["objective"] == "build a game"
        assert entry["verifier_score"] == 8
        assert entry["verifier_passed"] is True
        assert entry["tasks_completed"] == 5

    def test_record_creates_parent_directory(self, tmp_path):
        ledger_path = tmp_path / "nested" / "dir" / "run_ledger.jsonl"
        ledger = RunLedger(ledger_path)
        ledger.record(objective="x", task_type="develop", verifier_score=5, verifier_passed=False,
                       tasks_completed=1, tasks_failed=0, criterion_fix_count=0, acceptance_fix_count=0,
                       duration_seconds=1.0)
        assert ledger_path.exists()

    def test_multiple_records_append_not_overwrite(self, tmp_path):
        ledger_path = tmp_path / "run_ledger.jsonl"
        ledger = RunLedger(ledger_path)
        for i in range(3):
            ledger.record(objective=f"task {i}", task_type="develop", verifier_score=i, verifier_passed=False,
                           tasks_completed=1, tasks_failed=0, criterion_fix_count=0, acceptance_fix_count=0,
                           duration_seconds=1.0)
        lines = ledger_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 3

    def test_objective_truncated(self, tmp_path):
        ledger_path = tmp_path / "run_ledger.jsonl"
        ledger = RunLedger(ledger_path)
        ledger.record(objective="x" * 500, task_type="develop", verifier_score=5, verifier_passed=False,
                       tasks_completed=1, tasks_failed=0, criterion_fix_count=0, acceptance_fix_count=0,
                       duration_seconds=1.0)
        entry = json.loads(ledger_path.read_text(encoding="utf-8").strip())
        assert len(entry["objective"]) <= 200

    def test_record_never_raises_on_write_failure(self, tmp_path):
        # Point the ledger at a path whose parent can't be created (a file, not a dir)
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory")
        ledger = RunLedger(blocker / "run_ledger.jsonl")
        # Should not raise
        ledger.record(objective="x", task_type="develop", verifier_score=5, verifier_passed=False,
                       tasks_completed=1, tasks_failed=0, criterion_fix_count=0, acceptance_fix_count=0,
                       duration_seconds=1.0)

    def test_entry_includes_timestamp(self, tmp_path):
        ledger_path = tmp_path / "run_ledger.jsonl"
        ledger = RunLedger(ledger_path)
        ledger.record(objective="x", task_type="develop", verifier_score=5, verifier_passed=False,
                       tasks_completed=1, tasks_failed=0, criterion_fix_count=0, acceptance_fix_count=0,
                       duration_seconds=1.0)
        entry = json.loads(ledger_path.read_text(encoding="utf-8").strip())
        assert "timestamp" in entry
