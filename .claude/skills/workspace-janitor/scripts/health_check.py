import json
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Union

def get_workspace_health() -> Dict[str, Any]:
    """
    Analyzes the workspace for potential issues like uncommitted changes, clutter, and empty directories.

    Returns:
        Dict[str, Any]: A report containing status, issues, suggestions, and metrics.
    """
    report: Dict[str, Any] = {
        "status": "healthy",
        "issues": [],
        "suggestions": [],
        "metrics": {
            "large_files_count": 0,
            "uncommitted_changes": False
        }
    }

    # 1. Check for uncommitted changes (Git)
    try:
        # Check if inside a git work tree
        subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], 
                       check=True, 
                       stdout=subprocess.DEVNULL, 
                       stderr=subprocess.DEVNULL)
        
        # Check for status
        result = subprocess.run(["git", "status", "--porcelain"], 
                                capture_output=True, 
                                text=True, 
                                check=True)
        
        if result.stdout.strip():
            report["issues"].append("Uncommitted changes detected.")
            report["metrics"]["uncommitted_changes"] = True
            report["suggestions"].append({
                "action": "notify",
                "path": "git repo",
                "reason": "You have uncommitted changes. Consider committing before cleanup."
            })
    except (subprocess.CalledProcessError, FileNotFoundError):
        report["issues"].append("Not a git repository or git not installed.")

    # 2. Check for Clutter and Large Files
    clutter_extensions = {'.log', '.tmp', '.bak', '.pyc', '.swp'}
    size_threshold = 5 * 1024 * 1024  # 5MB

    # Use pathlib for iteration
    root_path = Path(".")
    
    # Define directories to ignore (including those starting with .)
    ignored_dirs = {'node_modules', '__pycache__', '.venv', '.agent', '.git'}

    for path in root_path.rglob("*"):
        # Skip if any parent directory is in ignored_dirs
        if any(part in ignored_dirs for part in path.parts):
            continue
            
        if not path.is_file():
            continue
            
        try:
            # Check extension
            if path.suffix.lower() in clutter_extensions:
                report["suggestions"].append({
                    "action": "delete",
                    "path": str(path),
                    "reason": f"Clutter file ({path.suffix})"
                })
                report["status"] = "cluttered"

            # Check size
            file_size = path.stat().st_size
            if file_size > size_threshold:
                size_mb = round(file_size / (1024 * 1024), 2)
                report["suggestions"].append({
                    "action": "delete",
                    "path": str(path),
                    "reason": f"Large file ({size_mb}MB)"
                })
                report["metrics"]["large_files_count"] += 1
                report["status"] = "cluttered"

        except OSError:
            continue

    # 3. Check for empty directories
    # os.walk is still reliable for bottom-up directory traversal to find empty ones
    for path in root_path.rglob("*"):
        if not path.is_dir():
             continue
             
        # Skip ignored dirs
        if any(part in ignored_dirs for part in path.parts):
            continue
            
        try:
            # Check if directory is empty (no files or subdirs)
            if not any(path.iterdir()):
                 report["suggestions"].append({
                    "action": "remove_directory",
                    "path": str(path),
                    "reason": "Empty directory"
                })
                 report["status"] = "cluttered"
        except OSError:
            continue

    return report

if __name__ == "__main__":
    # The agent reads this JSON output to decide next steps
    print(json.dumps(get_workspace_health(), indent=2))