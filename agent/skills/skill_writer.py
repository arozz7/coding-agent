"""SkillWriter — extracts and persists reusable fix patterns as agent-wiki skills.

Called by the developer agent after a successful fix (error → no error).
Also parses SKILL: blocks emitted explicitly by agents.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Optional

import structlog

from agent.skills.wiki_manager import WikiManager

logger = structlog.get_logger()

_SKILL_TEMPLATE = """\
---
title: {title}
trigger: "{trigger}"
agent_types: [{agent_types}]
scope: {scope}
use_count: 0
success_rate: 1.0
last_used: {today}
confidence: medium
---

{body}
"""

_SKILL_BLOCK_RE = re.compile(
    r"SKILL:\s*(?P<title>[^\n]+)\n(?P<body>.*?)(?=\nSKILL:|\nFILE:|\nEDIT:|\nREPLACE:|\Z)",
    re.DOTALL,
)


class SkillWriter:
    """Writes agent-learned patterns to .agent-wiki/skills/."""

    def __init__(self, workspace_path: str, project_name: str = ""):
        self.wiki = WikiManager(workspace_path, project_name=project_name)
        self.workspace_path = workspace_path
        self.logger = logger.bind(component="skill_writer")

    def write_fix_skill(
        self,
        error_signature: str,
        fix_description: str,
        fix_steps: list[str],
        agent_type: str = "developer",
        scope: str = "project",
    ) -> Optional[str]:
        """Write a fix recipe skill from a successful error→fix transition.

        Returns relative path of the written skill file, or None on failure.
        """
        if not error_signature or not fix_steps:
            return None

        title = f"Fix: {error_signature[:50]}"
        body = f"## When to apply\nWhen output contains: `{error_signature}`\n\n"
        body += "## Fix steps\n" + "\n".join(f"{i+1}. {s}" for i, s in enumerate(fix_steps))
        if fix_description:
            body += f"\n\n## Context\n{fix_description[:300]}"

        return self._write_skill(
            title=title,
            trigger=re.escape(error_signature[:80]),
            body=body,
            agent_type=agent_type,
            scope=scope,
        )

    def parse_and_write_skill_blocks(
        self, agent_response: str, agent_type: str = ""
    ) -> list[str]:
        """Parse SKILL: blocks from an agent response and write each one.

        Returns list of written skill paths.
        """
        written: list[str] = []
        for match in _SKILL_BLOCK_RE.finditer(agent_response):
            title = match.group("title").strip()
            body = match.group("body").strip()
            if not title or not body:
                continue
            trigger = ""
            trigger_m = re.search(r"trigger:\s*(.+)", body, re.IGNORECASE)
            if trigger_m:
                trigger = trigger_m.group(1).strip().strip("\"'")
            path = self._write_skill(
                title=title,
                trigger=trigger or title,
                body=body,
                agent_type=agent_type,
                scope="project",
            )
            if path:
                written.append(path)
        return written

    def _write_skill(
        self,
        title: str,
        trigger: str,
        body: str,
        agent_type: str,
        scope: str,
    ) -> Optional[str]:
        try:
            self.wiki._ensure_dirs()
            slug = re.sub(r"[^a-z0-9-]", "-", title.lower()).strip("-")[:60]
            if scope == "global" or not agent_type:
                sub = "global"
            else:
                sub = f"agents/{agent_type}"
            skill_dir = Path(self.workspace_path) / ".agent-wiki" / "skills" / sub
            skill_dir.mkdir(parents=True, exist_ok=True)
            content = _SKILL_TEMPLATE.format(
                title=title,
                trigger=trigger,
                agent_types=agent_type or "",
                scope=scope,
                today=date.today().isoformat(),
                body=body,
            )
            skill_path = skill_dir / f"{slug}.md"
            skill_path.write_text(content, encoding="utf-8")
            self.logger.info("skill_written", path=str(skill_path), title=title, scope=scope)
            return str(skill_path.relative_to(Path(self.workspace_path)))
        except Exception as exc:
            self.logger.warning("skill_write_failed", error=str(exc))
            return None


__all__ = ["SkillWriter"]
