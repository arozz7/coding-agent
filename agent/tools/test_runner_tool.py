import subprocess
import json
from pathlib import Path
from typing import Optional, List, Dict
import structlog

logger = structlog.get_logger()


class TestRunnerError(Exception):
    pass


class PytestTool:
    def __init__(self, project_root: str):
        self.project_root = Path(project_root).resolve()
        self.logger = logger.bind(component="pytest_tool")

    def _run_pytest(self, args: List[str], capture_output: bool = True) -> subprocess.CompletedProcess:
        cmd = ["pytest"] + args
        result = subprocess.run(
            cmd,
            cwd=str(self.project_root),
            capture_output=capture_output,
            text=True,
        )
        return result

    def run(
        self,
        path: Optional[str] = None,
        markers: Optional[List[str]] = None,
        verbose: bool = False,
        collect_only: bool = False,
    ) -> dict:
        try:
            args = []
            
            if path:
                args.append(path)
            else:
                args.append("tests/")
            
            if collect_only:
                args.append("--collect-only")
            
            if verbose:
                args.append("-v")
            
            args.extend(["-q", "--tb=short"])
            
            if markers:
                for marker in markers:
                    args.extend(["-m", marker])
            
            result = self._run_pytest(args)
            
            output_lines = result.stdout.split("\n") if result.stdout else []
            summary_line = output_lines[-1] if output_lines else ""
            
            passed = 0
            failed = 0
            errors = 0
            skipped = 0
            
            if "passed" in summary_line:
                import re
                match = re.search(r"(\d+) passed", summary_line)
                if match:
                    passed = int(match.group(1))
            
            if "failed" in summary_line:
                import re
                match = re.search(r"(\d+) failed", summary_line)
                if match:
                    failed = int(match.group(1))
            
            if "error" in summary_line:
                import re
                match = re.search(r"(\d+) error", summary_line)
                if match:
                    errors = int(match.group(1))
            
            if "skipped" in summary_line:
                import re
                match = re.search(r"(\d+) skipped", summary_line)
                if match:
                    skipped = int(match.group(1))
            
            return {
                "success": result.returncode == 0 or (collect_only and result.returncode == 0),
                "returncode": result.returncode,
                "passed": passed,
                "failed": failed,
                "errors": errors,
                "skipped": skipped,
                "output": result.stdout,
                "errors_output": result.stderr if result.stderr else "",
            }
            
        except FileNotFoundError:
            self.logger.error("pytest_not_found")
            return {
                "success": False,
                "error": "pytest is not installed. Run: pip install pytest",
            }
        except Exception as e:
            self.logger.error("pytest_error", error=str(e))
            return {"success": False, "error": str(e)}

    def list_tests(self, path: Optional[str] = None) -> dict:
        return self.run(path=path, collect_only=True)

    def run_file(self, file_path: str) -> dict:
        return self.run(path=file_path)

    def run_by_marker(self, marker: str) -> dict:
        return self.run(markers=[marker])