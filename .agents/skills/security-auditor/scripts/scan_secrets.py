import os
import re
import json
from pathlib import Path
from typing import List, Dict, Any

# Common regex patterns for secrets
PATTERNS: Dict[str, str] = {
    "Generic Secret": r'(?i)secret|token|password|auth|api_key',
    "AWS Key": r'AKIA[0-9A-Z]{16}',
    "Potential Private Key": r'-----BEGIN [A-Z ]+ PRIVATE KEY-----'
}

def scan_files(root_dir: str = ".") -> List[Dict[str, str]]:
    """
    Scans the directory for potential secrets using regex patterns.

    Args:
        root_dir: The root directory to start scanning from. Defaults to current directory.

    Returns:
        List[Dict[str, str]]: A list of dictionaries containing finding details (file path and type).
    """
    findings: List[Dict[str, str]] = []
    
    # Convert string path to Path object
    root_path = Path(root_dir)
    
    # Define exclusions
    exclusions = {".git", "node_modules", ".agent", "__pycache__", ".venv"}

    for path in root_path.rglob("*"):
        # Skip if any part of the path is in exclusions
        if any(part in exclusions for part in path.parts):
            continue
            
        if not path.is_file():
            continue

        try:
            # excessive 5MB limit to avoid freezing on large binaries
            if path.stat().st_size > 5 * 1024 * 1024:
                continue

            content = path.read_text(encoding='utf-8', errors='ignore')
            for name, pattern in PATTERNS.items():
                if re.search(pattern, content):
                    findings.append({"file": str(path), "type": name})
        except (OSError, UnicodeDecodeError):
            continue
            
    return findings

if __name__ == "__main__":
    print(json.dumps(scan_files(), indent=2))