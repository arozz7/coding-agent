"""
Skill Loader - Discovers and manages Claude Skills.

Loads skills from the skills/ directory based on:
- Keyword triggers (from skill description)
- User invocation (explicit skill name)
- Pre/post execution hooks
"""
import re
from pathlib import Path
from typing import Dict, List, Optional, Any
import structlog
import yaml

logger = structlog.get_logger()


class Skill:
    """Represents a loaded skill."""
    
    def __init__(self, name: str, description: str, path: Path, content: str, 
                 user_invocable: bool = False, triggers: List[str] = None,
                 allowed_tools: List[str] = None):
        self.name = name
        self.description = description
        self.path = path
        self.content = content
        self.user_invocable = user_invocable
        self.triggers = triggers or []
        self.allowed_tools = allowed_tools or []
    
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
        """Load a skill from SKILL.md file."""
        content = path.read_text(encoding="utf-8")
        
        # Extract frontmatter
        frontmatter = {}
        if content.startswith("---"):
            end_idx = content.find("---", 3)
            if end_idx > 0:
                fm_text = content[3:end_idx].strip()
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
            content=content,
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


__all__ = ["SkillManager", "Skill"]