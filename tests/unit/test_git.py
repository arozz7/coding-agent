"""Unit tests for Git tool."""
import pytest
import tempfile
import subprocess
from pathlib import Path


@pytest.fixture
def git_repo(tmp_path):
    """Create a temporary git repository."""
    subprocess.run(["git", "init"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@test.com"],
        cwd=str(tmp_path),
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=str(tmp_path),
        capture_output=True,
    )
    # Make initial commit to create default branch
    (tmp_path / "README.txt").write_text("Initial")
    subprocess.run(["git", "add", "README.txt"], cwd=str(tmp_path), capture_output=True)
    subprocess.run(["git", "commit", "-m", "Initial"], cwd=str(tmp_path), capture_output=True)
    return tmp_path


class TestGitTool:
    def test_initialization_valid_repo(self, git_repo):
        from agent.tools import GitTool

        tool = GitTool(str(git_repo))
        assert tool.repo_path == git_repo.resolve()

    def test_initialization_invalid_repo(self, tmp_path):
        from agent.tools import GitTool, GitError

        with pytest.raises(GitError):
            GitTool(str(tmp_path))

    def test_status_clean(self, git_repo):
        from agent.tools import GitTool

        tool = GitTool(str(git_repo))
        result = tool.status()
        assert result["success"] is True
        assert result["clean"] is True
        assert result["files"] == []

    def test_status_with_changes(self, git_repo):
        from agent.tools import GitTool

        tool = GitTool(str(git_repo))
        
        (git_repo / "test.txt").write_text("Hello")
        result = tool.status()
        
        assert result["success"] is True
        assert result["clean"] is False
        assert len(result["files"]) == 1
        assert result["files"][0]["path"] == "test.txt"
        assert result["files"][0]["untracked"] is True

    def test_add(self, git_repo):
        from agent.tools import GitTool

        tool = GitTool(str(git_repo))
        (git_repo / "test.txt").write_text("Hello")
        
        result = tool.add(["test.txt"])
        assert result["success"] is True
        
        status = tool.status()
        assert status["files"][0]["staged"] is True

    def test_commit(self, git_repo):
        from agent.tools import GitTool

        tool = GitTool(str(git_repo))
        (git_repo / "test.txt").write_text("Hello")
        tool.add(["test.txt"])
        
        result = tool.commit("Initial commit")
        assert result["success"] is True

    def test_log(self, git_repo):
        from agent.tools import GitTool

        tool = GitTool(str(git_repo))
        (git_repo / "test.txt").write_text("Hello")
        tool.add(["test.txt"])
        tool.commit("Second commit")
        
        result = tool.log()
        assert result["success"] is True
        assert len(result["commits"]) >= 1
        assert "Second commit" in result["commits"][0]["message"]

    def test_branch(self, git_repo):
        from agent.tools import GitTool

        tool = GitTool(str(git_repo))
        result = tool.branch()
        
        assert result["success"] is True
        assert result["current"] is not None
        assert result["current"] in ["master", "main"]

    def test_diff_no_changes(self, git_repo):
        from agent.tools import GitTool

        tool = GitTool(str(git_repo))
        result = tool.diff()
        
        assert result["success"] is True
        assert result["has_changes"] is False

    def test_diff_with_changes(self, git_repo):
        from agent.tools import GitTool

        tool = GitTool(str(git_repo))
        (git_repo / "test.txt").write_text("Hello")
        tool.add(["test.txt"])
        
        result = tool.diff()
        assert result["success"] is True

    def test_restore(self, git_repo):
        from agent.tools import GitTool

        tool = GitTool(str(git_repo))
        (git_repo / "test.txt").write_text("Hello")
        tool.add(["test.txt"])
        
        result = tool.restore(["test.txt"])
        assert result["success"] is True
        
        status = tool.status()
        assert status["clean"] is False
