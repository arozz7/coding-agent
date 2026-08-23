import sys
import json
from pathlib import Path
from typing import List, Dict, Any, Union

def perform_cleanup(targets: List[Dict[str, str]]) -> Dict[str, Any]:
    """
    Performs cleanup actions on the specified targets.

    Args:
        targets: A list of dictionaries, each containing 'path' and 'action' ('delete' or 'remove_directory').

    Returns:
        Dict[str, Any]: A summary of the operations, including deleted paths and failures.
    """
    results: Dict[str, Any] = {
        "deleted": [],
        "failed": [],
        "summary": ""
    }

    for target in targets:
        path_str = target.get("path")
        action = target.get("action")

        if not path_str:
             results["failed"].append({"path": "N/A", "reason": "No path provided"})
             continue

        path = Path(path_str)

        if not path.exists():
            results["failed"].append({"path": str(path), "reason": "Path does not exist"})
            continue

        try:
            if action == "delete":
                if path.is_file() or path.is_symlink():
                    path.unlink()
                    results["deleted"].append(str(path))
                else:
                    results["failed"].append({"path": str(path), "reason": "Path is not a file"})
            elif action == "remove_directory":
                # Only removes if empty to prevent accidental data loss
                if path.is_dir():
                    path.rmdir()
                    results["deleted"].append(str(path))
                else:
                     results["failed"].append({"path": str(path), "reason": "Path is not a directory"})
            else:
                results["failed"].append({"path": str(path), "reason": f"Unknown action: {action}"})

        except Exception as e:
            results["failed"].append({"path": str(path), "reason": str(e)})

    results["summary"] = f"Successfully removed {len(results['deleted'])} items. {len(results['failed'])} failed."
    return results

if __name__ == "__main__":
    # The agent passes the list of approved files as a JSON string argument
    if len(sys.argv) < 2:
        print(json.dumps({"error": "No targets provided"}))
        sys.exit(1)

    try:
        approved_targets: List[Dict[str, str]] = json.loads(sys.argv[1])
        print(json.dumps(perform_cleanup(approved_targets), indent=2))
    except json.JSONDecodeError:
        print(json.dumps({"error": "Invalid input format"}))
