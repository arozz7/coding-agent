import os
import subprocess
from pathlib import Path
from typing import Optional, List
import structlog

logger = structlog.get_logger()


class GitError(Exception):
    pass


class GitTool:
    def __init__(self, repo_path: str):  # noqa: ARG002  repo_path kept for API compat
        # Always resolve the workspace from the environment (trusted source only).
        # repo_path is accepted for backward-compatibility but never used in any
        # path operation — this ensures no user-supplied value reaches the filesystem.
        #
        # Priority:
        #   1. AGENT_EFFECTIVE_WORKSPACE — set by api/main.py to the fully-resolved
        #      effective path (WORKSPACE_PATH / PROJECT_DIR).  Preferred because it
        #      is set programmatically and is never stored in .env, so module reloads
        #      cannot cause double-appending of PROJECT_DIR.
        #   2. WORKSPACE_PATH + PROJECT_DIR — fallback for processes that don't go
        #      through api/main.py (e.g. tests, CLI usage).
        effective_env = os.environ.get("AGENT_EFFECTIVE_WORKSPACE", "").strip()
        if effective_env:
            workspace_env = effective_env
        else:
            workspace_env = os.environ.get("WORKSPACE_PATH", "").strip()
            if not workspace_env:
                raise GitError("WORKSPACE_PATH environment variable is required")
            project_dir = os.environ.get("PROJECT_DIR", "").strip()
            if project_dir:
                workspace_env = str(Path(workspace_env) / project_dir)
        self.repo_path = Path(workspace_env).resolve()
        self.logger = logger.bind(component="git_tool")
        self._verify_repo()

    def _verify_repo(self) -> None:
        if not self.repo_path.exists():
            raise GitError(f"Repository path does not exist: {self.repo_path}")

        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise GitError(f"Not a git repository: {self.repo_path}")

    def _run_git(self, args: List[str]) -> str:
        result = subprocess.run(
            ["git"] + args,
            cwd=str(self.repo_path),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise GitError(f"Git error: {result.stderr.strip()}")
        return result.stdout.strip()

    def status(self, short: bool = True) -> dict:
        try:
            if short:
                output = self._run_git(["status", "--porcelain"])
            else:
                output = self._run_git(["status"])
            
            files = []
            if short and output:
                for line in output.split("\n"):
                    if line:
                        staged = line[0] if len(line) > 0 else " "
                        unstaged = line[1] if len(line) > 1 else " "
                        path = line[3:].strip()
                        files.append({
                            "path": path,
                            "staged": staged not in " ?",
                            "modified": unstaged in "M",
                            "untracked": unstaged == "?"
                        })
            
            return {
                "success": True,
                "output": output,
                "files": files,
                "clean": len(files) == 0 if short else "clean" in output.lower(),
            }
        except Exception as e:
            self.logger.error("git_status_error", error=str(e))
            return {"success": False, "error": str(e)}

    def diff(self, file_path: Optional[str] = None) -> dict:
        try:
            args = ["diff"]
            if file_path:
                args.extend(["--", file_path])
            
            output = self._run_git(args)
            return {
                "success": True,
                "output": output,
                "has_changes": len(output) > 0,
            }
        except Exception as e:
            self.logger.error("git_diff_error", error=str(e))
            return {"success": False, "error": str(e)}

    def diff_staged(self, file_path: Optional[str] = None) -> dict:
        try:
            args = ["diff", "--cached"]
            if file_path:
                args.extend(["--", file_path])
            
            output = self._run_git(args)
            return {
                "success": True,
                "output": output,
                "has_changes": len(output) > 0,
            }
        except Exception as e:
            self.logger.error("git_diff_staged_error", error=str(e))
            return {"success": False, "error": str(e)}

    def commit(self, message: str, files: Optional[List[str]] = None) -> dict:
        try:
            if files:
                self._run_git(["add"] + files)
            
            output = self._run_git(["commit", "-m", message])
            return {
                "success": True,
                "output": output,
            }
        except Exception as e:
            self.logger.error("git_commit_error", error=str(e))
            return {"success": False, "error": str(e)}

    def log(self, n: int = 10) -> dict:
        try:
            output = self._run_git(["log", f"-{n}", "--oneline"])
            commits = []
            for line in output.split("\n"):
                if line:
                    parts = line.split(" ", 1)
                    if len(parts) == 2:
                        commits.append({
                            "hash": parts[0],
                            "message": parts[1],
                        })
            return {
                "success": True,
                "commits": commits,
            }
        except Exception as e:
            self.logger.error("git_log_error", error=str(e))
            return {"success": False, "error": str(e)}

    def branch(self, list_all: bool = True) -> dict:
        try:
            if list_all:
                output = self._run_git(["branch", "-a"])
            else:
                output = self._run_git(["branch"])
            
            branches = []
            current = None
            for line in output.split("\n"):
                line = line.strip()
                if line.startswith("*"):
                    current = line[1:].strip()
                    branches.append({"name": current, "current": True})
                elif line:
                    branches.append({"name": line, "current": False})
            
            return {
                "success": True,
                "branches": branches,
                "current": current,
            }
        except Exception as e:
            self.logger.error("git_branch_error", error=str(e))
            return {"success": False, "error": str(e)}

    def add(self, files: List[str]) -> dict:
        try:
            self._run_git(["add"] + files)
            return {"success": True}
        except Exception as e:
            self.logger.error("git_add_error", error=str(e))
            return {"success": False, "error": str(e)}

    def restore(self, files: List[str]) -> dict:
        try:
            self._run_git(["restore"] + files)
            return {"success": True}
        except Exception as e:
            self.logger.error("git_restore_error", error=str(e))
            return {"success": False, "error": str(e)}
