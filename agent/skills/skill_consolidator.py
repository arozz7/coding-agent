"""SkillConsolidator — lifecycle management for agent-wiki skills.

Operations:
  find_conflicts() — group skills by overlapping trigger, flag contradictions
  merge(group)     — LLM-merges a conflict group into one authoritative entry
  prune()          — archives zero-use skills older than 30 days
  promote()        — lifts project skills to global when seen across 3+ projects
"""
from __future__ import annotations

import re
import shutil
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Optional

import structlog

if TYPE_CHECKING:
    from llm import ModelRouter

logger = structlog.get_logger()

_DAYS_BEFORE_PRUNE = 30


class SkillConsolidator:
    """Manages the full lifecycle of skills in .agent-wiki/skills/."""

    def __init__(self, workspace_path: str, model_router: Optional["ModelRouter"] = None):
        self.skills_root = Path(workspace_path) / ".agent-wiki" / "skills"
        self.model_router = model_router
        self.logger = logger.bind(component="skill_consolidator")

    # ------------------------------------------------------------------
    # Conflict detection
    # ------------------------------------------------------------------

    def find_conflicts(self) -> list[dict]:
        """Return groups of skills with overlapping triggers that may conflict."""
        all_skills = self._load_all_skills()
        groups: dict[str, list[dict]] = {}
        for skill in all_skills:
            key = skill.get("trigger", "")[:20].lower().strip()
            if not key:
                continue
            groups.setdefault(key, []).append(skill)

        conflicts = [
            {"trigger_prefix": k, "skills": v}
            for k, v in groups.items()
            if len(v) >= 2
        ]
        self.logger.info("conflicts_found", count=len(conflicts))
        return conflicts

    # ------------------------------------------------------------------
    # Prune
    # ------------------------------------------------------------------

    def prune(self) -> dict:
        """Archive zero-use skills that haven't been touched in 30+ days."""
        archive_dir = self.skills_root / "_archive"
        archive_dir.mkdir(parents=True, exist_ok=True)
        today = date.today()
        pruned = 0

        for skill_file in self.skills_root.rglob("*.md"):
            if "_archive" in skill_file.parts:
                continue
            try:
                content = skill_file.read_text(encoding="utf-8", errors="ignore")
                use_count_m = re.search(r"use_count:\s*(\d+)", content)
                last_used_m = re.search(r"last_used:\s*([\d-]+)", content)
                if not use_count_m or int(use_count_m.group(1)) > 0:
                    continue
                if last_used_m:
                    age = (today - date.fromisoformat(last_used_m.group(1))).days
                    if age < _DAYS_BEFORE_PRUNE:
                        continue
                dst = archive_dir / skill_file.name
                shutil.move(str(skill_file), str(dst))
                pruned += 1
                self.logger.info("skill_pruned", file=skill_file.name)
            except Exception as exc:
                self.logger.warning("prune_error", file=str(skill_file), error=str(exc))

        return {"pruned": pruned}

    # ------------------------------------------------------------------
    # Promote
    # ------------------------------------------------------------------

    def promote(self, project_names: list[str]) -> dict:
        """Promote project-scoped skills to global when their trigger matches 3+ projects."""
        global_dir = self.skills_root / "global"
        global_dir.mkdir(parents=True, exist_ok=True)
        project_dir = self.skills_root / "project"
        if not project_dir.exists():
            return {"promoted": 0}

        promoted = 0
        for skill_file in project_dir.glob("*.md"):
            try:
                content = skill_file.read_text(encoding="utf-8", errors="ignore")
                trigger_m = re.search(r'trigger:\s*"?([^"\n]+)"?', content)
                if not trigger_m:
                    continue
                trigger = trigger_m.group(1).strip()
                match_count = sum(
                    1 for p in project_names if self._trigger_found_in_project(trigger, p)
                )
                if match_count >= 3:
                    dst = global_dir / skill_file.name
                    if not dst.exists():
                        shutil.copy2(str(skill_file), str(dst))
                        new_content = re.sub(r"scope:\s*\S+", "scope: global", content)
                        dst.write_text(new_content, encoding="utf-8")
                        promoted += 1
                        self.logger.info("skill_promoted", skill=skill_file.name, projects=match_count)
            except Exception as exc:
                self.logger.warning("promote_error", file=str(skill_file), error=str(exc))

        return {"promoted": promoted}

    # ------------------------------------------------------------------
    # Merge (LLM-assisted)
    # ------------------------------------------------------------------

    async def merge(self, conflict_group: dict) -> Optional[str]:
        """LLM-merge a conflict group into one authoritative skill. Returns slug or None."""
        if not self.model_router:
            return None
        skills = conflict_group.get("skills", [])
        if len(skills) < 2:
            return None

        skill_texts = "\n\n---\n\n".join(
            f"### {s['path']}\n{s['content'][:600]}" for s in skills
        )
        system = (
            "You are a skill curator. Merge the following similar skills into one authoritative entry. "
            "Preserve the most specific trigger pattern, combine fix steps keeping only unique ones, "
            "and set confidence: high. Return ONLY the merged skill in this exact format:\n\n"
            "---\ntitle: <title>\ntrigger: \"<trigger>\"\nagent_types: [<types>]\n"
            "scope: <scope>\nuse_count: <sum of use_counts>\nsuccess_rate: <weighted avg>\n"
            f"last_used: {date.today().isoformat()}\nconfidence: high\n---\n\n<body>"
        )
        try:
            model = self.model_router.get_model("coding")
            merged_text = await self.model_router.generate(
                f"Merge these skills:\n\n{skill_texts}", model, system_prompt=system
            )
            if not merged_text or "---" not in merged_text:
                return None

            title_m = re.search(r"title:\s*(.+)", merged_text)
            title = title_m.group(1).strip() if title_m else "merged-skill"
            slug = re.sub(r"[^a-z0-9-]", "-", title.lower()).strip("-")[:60]

            global_dir = self.skills_root / "global"
            global_dir.mkdir(parents=True, exist_ok=True)
            out_path = global_dir / f"{slug}.md"
            out_path.write_text(merged_text.strip() + "\n", encoding="utf-8")

            archive_dir = self.skills_root / "_archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            for s in skills:
                src = Path(s["path"])
                if src.exists():
                    shutil.move(str(src), str(archive_dir / src.name))

            self.logger.info("skills_merged", output=str(out_path), count=len(skills))
            return slug
        except Exception as exc:
            self.logger.warning("merge_failed", error=str(exc))
            return None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load_all_skills(self) -> list[dict]:
        skills = []
        for skill_file in self.skills_root.rglob("*.md"):
            if "_archive" in skill_file.parts:
                continue
            try:
                content = skill_file.read_text(encoding="utf-8", errors="ignore")
                trigger_m = re.search(r'trigger:\s*"?([^"\n]+)"?', content)
                use_count_m = re.search(r"use_count:\s*(\d+)", content)
                skills.append({
                    "path": str(skill_file),
                    "content": content,
                    "trigger": trigger_m.group(1).strip() if trigger_m else "",
                    "use_count": int(use_count_m.group(1)) if use_count_m else 0,
                })
            except Exception:
                pass
        return skills

    def _trigger_found_in_project(self, trigger: str, project_name: str) -> bool:
        project_dir = self.skills_root / "project"
        if not project_dir.exists():
            return False
        for f in project_dir.glob("*.md"):
            try:
                if trigger.lower() in f.read_text(encoding="utf-8", errors="ignore").lower():
                    return True
            except Exception:
                pass
        return False


__all__ = ["SkillConsolidator"]
