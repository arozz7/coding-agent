"""ProjectScriptsTool — manages reusable helper scripts created by agents.

Agents emit SCRIPT: blocks; this tool saves them to scripts/ and surfaces
them to future planners and developers via list_scripts().
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()


class ProjectScriptsTool:
    """Saves and retrieves agent-created helper scripts under scripts/."""

    def __init__(self, workspace_path: str):
        self.scripts_dir = Path(workspace_path) / "scripts"
        self.logger = logger.bind(component="project_scripts")

    def save_script(self, name: str, content: str) -> Optional[str]:
        """Write a script to scripts/<name>. Makes it executable on Unix."""
        if not name or not content:
            return None
        safe_name = Path(name).name  # strip any directory traversal
        if not safe_name or ".." in safe_name:
            return None
        try:
            self.scripts_dir.mkdir(parents=True, exist_ok=True)
            script_path = self.scripts_dir / safe_name
            script_path.write_text(content, encoding="utf-8")
            if os.name != "nt":
                script_path.chmod(
                    script_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
                )
            self.logger.info("script_saved", path=str(script_path))
            return str(script_path)
        except Exception as exc:
            self.logger.warning("script_save_failed", name=name, error=str(exc))
            return None

    def list_scripts(self) -> str:
        """Return a formatted list of available helper scripts for agent context injection."""
        if not self.scripts_dir.exists():
            return ""
        scripts = [s for s in sorted(self.scripts_dir.iterdir()) if s.is_file()]
        if not scripts:
            return ""
        lines = ["## Available project scripts (scripts/)"]
        for s in scripts:
            try:
                first_line = s.read_text(encoding="utf-8", errors="ignore").splitlines()[0]
                desc = (
                    first_line.lstrip("#! ").strip()[:80]
                    if first_line.startswith(("#", "//"))
                    else ""
                )
                lines.append(f"  - `scripts/{s.name}`{f' — {desc}' if desc else ''}")
            except Exception:
                lines.append(f"  - `scripts/{s.name}`")
        return "\n".join(lines)


__all__ = ["ProjectScriptsTool"]
