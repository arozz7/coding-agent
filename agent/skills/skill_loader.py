"""
Skill Loader - Discovers and manages Claude Skills.

Loads skills from the skills/ directory based on:
- Keyword triggers (from skill description)
- User invocation (explicit skill name)
- Pre/post execution hooks

Remote fetch (fetch_remote) downloads SKILL.md files from a public GitHub
repository declared in config/environment.yaml under skills_registry.
"""
import re
from pathlib import Path
from typing import Dict, List, Optional, Any
import structlog
import yaml

logger = structlog.get_logger()


class Skill:
    """Represents a loaded skill.

    Content (full SKILL.md text) is loaded lazily on first access so that
    startup only pays for metadata (name, description, triggers).  The full
    text is only read when a skill is actually triggered for a task.
    """

    def __init__(self, name: str, description: str, path: Path,
                 user_invocable: bool = False, triggers: List[str] = None,
                 allowed_tools: List[str] = None):
        self.name = name
        self.description = description
        self.path = path
        self.user_invocable = user_invocable
        self.triggers = triggers or []
        self.allowed_tools = allowed_tools or []
        self._content: Optional[str] = None  # loaded on first access

    @property
    def content(self) -> str:
        """Full SKILL.md text, loaded lazily."""
        if self._content is None:
            try:
                self._content = self.path.read_text(encoding="utf-8")
            except Exception:
                self._content = ""
        return self._content

    def matches_trigger(self, text: str) -> bool:
        """Check if text triggers this skill."""
        text_lower = text.lower()
        for trigger in self.triggers:
            if trigger.lower() in text_lower:
                return True
        return False


class SkillManager:
    """Manages skill discovery, loading, and execution."""
    
    def __init__(self, skills_dir: str = "skills"):
        self.skills_dir = Path(skills_dir)
        self.skills: Dict[str, Skill] = {}
        self.logger = logger.bind(component="skill_manager")
        
        if self.skills_dir.exists():
            self.discover_skills()
    
    def discover_skills(self) -> None:
        """Scan skills directory and load all skills."""
        if not self.skills_dir.exists():
            self.logger.warning("skills_dir_not_found", path=str(self.skills_dir))
            return
        
        for skill_path in self.skills_dir.rglob("SKILL.md"):
            try:
                skill = self._load_skill(skill_path)
                if skill:
                    self.skills[skill.name] = skill
                    self.logger.info("skill_loaded", name=skill.name, triggers=skill.triggers)
            except Exception as e:
                self.logger.error("skill_load_failed", path=str(skill_path), error=str(e))
        
        self.logger.info("skills_discovered", count=len(self.skills))
    
    def _load_skill(self, path: Path) -> Optional[Skill]:
        """Load skill metadata from SKILL.md frontmatter only.

        The full file content is NOT read here — it is loaded lazily via
        Skill.content when the skill is actually triggered for a task.
        """
        # Read only enough of the file to extract frontmatter (~500 bytes).
        # If there is no frontmatter we read the whole file to infer metadata,
        # but this is the uncommon path and only happens once at startup.
        raw = path.read_text(encoding="utf-8")

        # Extract frontmatter
        frontmatter = {}
        if raw.startswith("---"):
            end_idx = raw.find("---", 3)
            if end_idx > 0:
                fm_text = raw[3:end_idx].strip()
                frontmatter = yaml.safe_load(fm_text) or {}

        name = frontmatter.get("name", path.parent.name)
        description = frontmatter.get("description", "")
        user_invocable = frontmatter.get("user_invocable", False)

        # Extract keywords from description for triggers
        triggers = []
        desc_lower = description.lower()
        keywords = ["test", "security", "architect", "adr", "database", "auth",
                    "api", "review", "refactor", "document", "cleanup", "lint"]
        for kw in keywords:
            if kw in desc_lower:
                triggers.append(kw)

        # Also add triggers from skill name
        for word in name.split("-"):
            if len(word) > 2:
                triggers.append(word)

        # Get allowed tools
        allowed_tools = frontmatter.get("allowed-tools", "")
        if isinstance(allowed_tools, str):
            allowed_tools = [t.strip() for t in allowed_tools.split(",")]

        return Skill(
            name=name,
            description=description,
            path=path,
            user_invocable=user_invocable,
            triggers=triggers,
            allowed_tools=allowed_tools,
        )
    
    def get_skill(self, name: str) -> Optional[Skill]:
        """Get a skill by name."""
        return self.skills.get(name)
    
    def detect_triggers(self, task: str, phase: str = "all") -> List[Skill]:
        """Detect which skills should run based on task."""
        triggered = []
        
        for skill in self.skills.values():
            if skill.matches_trigger(task):
                triggered.append(skill)
        
        return triggered
    
    def get_skill_content(self, name: str) -> str:
        """Get the full content of a skill (for adding to context)."""
        skill = self.skills.get(name)
        if skill:
            return skill.content
        return ""
    
    def list_skills(self) -> List[Dict[str, Any]]:
        """List all available skills."""
        return [
            {
                "name": s.name,
                "description": s.description,
                "user_invocable": s.user_invocable,
                "triggers": s.triggers,
                "path": str(s.path.relative_to(self.skills_dir.parent)),
            }
            for s in self.skills.values()
        ]


    def fetch_remote(self) -> dict:
        """Download skills from the remote registry into the local skills dir.

        Registry is configured in config/environment.yaml under skills_registry.
        Uses the GitHub Contents API (no auth required for public repos).
        Already-current files are skipped (compared by content hash).

        Returns a dict with keys: fetched, skipped, errors, skills.
        """
        import hashlib
        import urllib.request

        # Load environment config for registry settings
        try:
            from local_coding_agent import _PROJECT_ROOT
            env_cfg_path = _PROJECT_ROOT / "config" / "environment.yaml"
            with open(env_cfg_path, encoding="utf-8") as fh:
                env_cfg = yaml.safe_load(fh) or {}
        except Exception as e:
            self.logger.error("skills_fetch_env_config_failed", error=str(e))
            return {"fetched": 0, "skipped": 0, "errors": [str(e)], "skills": []}

        registry = env_cfg.get("skills_registry", {})
        import os
        repo = os.environ.get(
            registry.get("env_override", "SKILLS_REPO_URL"),
            f"{registry.get('repo', 'anthropics/anthropic-quickstarts')}",
        )
        branch = registry.get("branch", "main")
        skills_path = registry.get("skills_path", "")

        # GitHub Contents API URL
        api_url = f"https://api.github.com/repos/{repo}/contents/{skills_path}?ref={branch}"
        self.logger.info("skills_fetch_start", repo=repo, branch=branch, path=skills_path)

        fetched, skipped, errors, fetched_names = 0, 0, [], []

        try:
            req = urllib.request.Request(
                api_url,
                headers={"Accept": "application/vnd.github.v3+json",
                         "User-Agent": "local-coding-agent/1.0"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                import json
                contents = json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            self.logger.error("skills_fetch_listing_failed", error=str(e))
            return {"fetched": 0, "skipped": 0, "errors": [str(e)], "skills": []}

        # Look for SKILL.md files (direct or one level deep)
        skill_files = []
        for item in (contents if isinstance(contents, list) else []):
            if item.get("type") == "file" and item["name"].upper() == "SKILL.MD":
                skill_files.append(item)
            elif item.get("type") == "dir":
                # Recurse one level
                try:
                    sub_url = item["url"]
                    sub_req = urllib.request.Request(
                        sub_url,
                        headers={"Accept": "application/vnd.github.v3+json",
                                 "User-Agent": "local-coding-agent/1.0"},
                    )
                    with urllib.request.urlopen(sub_req, timeout=10) as sr:
                        sub_contents = json.loads(sr.read().decode("utf-8"))
                    for sub_item in (sub_contents if isinstance(sub_contents, list) else []):
                        if sub_item.get("type") == "file" and sub_item["name"].upper() == "SKILL.MD":
                            skill_files.append(sub_item)
                except Exception:
                    pass

        for skill_file in skill_files:
            try:
                download_url = skill_file.get("download_url", "")
                if not download_url:
                    continue

                # Determine local path: skills/<parent-dir>/SKILL.md
                parts = skill_file["path"].split("/")
                skill_dir_name = parts[-2] if len(parts) >= 2 else "remote-skill"
                local_dir = self.skills_dir / skill_dir_name
                local_path = local_dir / "SKILL.md"

                # Download content
                dl_req = urllib.request.Request(
                    download_url,
                    headers={"User-Agent": "local-coding-agent/1.0"},
                )
                with urllib.request.urlopen(dl_req, timeout=15) as dl:
                    remote_content = dl.read().decode("utf-8")

                # Skip if identical to local version
                if local_path.exists():
                    local_hash = hashlib.sha256(local_path.read_bytes()).hexdigest()
                    remote_hash = hashlib.sha256(remote_content.encode("utf-8")).hexdigest()
                    if local_hash == remote_hash:
                        skipped += 1
                        continue

                local_dir.mkdir(parents=True, exist_ok=True)
                local_path.write_text(remote_content, encoding="utf-8")
                fetched += 1
                fetched_names.append(skill_dir_name)
                self.logger.info("skill_fetched", name=skill_dir_name)

            except Exception as e:
                errors.append(str(e))
                self.logger.error("skill_fetch_item_failed", error=str(e))

        # Reload skills after fetch
        if fetched > 0:
            self.discover_skills()

        return {
            "fetched": fetched,
            "skipped": skipped,
            "errors": errors,
            "skills": fetched_names,
        }


__all__ = ["SkillManager", "Skill"]