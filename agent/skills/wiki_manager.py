"""
WikiManager - File I/O for the .agent-wiki knowledge base.

Follows the Karpathy LLM Wiki pattern:
  - index.md  : catalog of all entries (append-only table)
  - log.md    : chronological record of compilations
  - <category>/<name>.md : individual knowledge entries

Each wiki is scoped to its workspace directory.  Entries are tagged with
``project:<name>`` so the query layer can enforce strict per-project isolation:
  - At workspace root (project_name=""): only untagged (workspace-level) entries surface
  - In a project subdir: only entries tagged for that project surface

Usage:
    wiki = WikiManager(workspace_path, project_name="my-project")
    context = wiki.query(["authentication", "jwt"])   # pre-task
    wiki.compile("jwt-auth", content)                 # post-task
"""
from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
import structlog

logger = structlog.get_logger()

CATEGORIES = ("tech-patterns", "bugs", "decisions", "api-usage", "synthesis")

_PROJECT_TAG_RE = re.compile(r"\bproject:[a-z0-9_.-]+\b", re.IGNORECASE)

_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "bugs": ["bug", "fix", "error", "crash", "broken", "workaround", "patch"],
    "decisions": ["decided", "decision", "chose", "architecture", "adr", "trade-off"],
    "api-usage": ["api", "endpoint", "sdk", "client", "http", "rest", "graphql"],
    "tech-patterns": [],  # default
}


def _detect_category(text: str) -> str:
    text_lower = text.lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            return category
    return "tech-patterns"


def _slug(name: str) -> str:
    """Convert a title to a safe filename slug."""
    return re.sub(r"[^a-z0-9-]", "-", name.lower().strip()).strip("-")[:60]


class WikiManager:
    """Read/write the .agent-wiki knowledge base inside a workspace.

    Args:
        workspace_path: Absolute path to the active workspace directory.
        project_name:   Name of the active sub-project (empty string when
                        operating from the workspace root).  Controls entry
                        tagging and query filtering for strict isolation.
    """

    def __init__(self, workspace_path: str, project_name: str = ""):
        self.wiki_root = Path(workspace_path) / ".agent-wiki"
        self.project_name = project_name.strip()
        self.logger = logger.bind(component="wiki_manager", project=project_name or "<root>")

    def _ensure_dirs(self) -> None:
        self.wiki_root.mkdir(parents=True, exist_ok=True)
        for cat in CATEGORIES:
            (self.wiki_root / cat).mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # Query (pre-task context injection)
    # ------------------------------------------------------------------

    def query(self, terms: List[str], max_entries: int = 4) -> str:
        """Return relevant wiki content for the given search terms.

        Reads index.md, finds lines matching any term, and enforces project
        scope isolation:
          - In a project (project_name set): return entries tagged for this
            project OR entries with no project tag (workspace-level knowledge).
          - At root (project_name empty): return only untagged entries; skip
            any entry that carries a ``project:`` tag for a specific project.
        """
        index_path = self.wiki_root / "index.md"
        if not index_path.exists():
            return ""

        try:
            index_lines = index_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return ""

        terms_lower = [t.lower() for t in terms if len(t) > 2]
        matched_paths: list[str] = []

        for line in index_lines:
            if not any(t in line.lower() for t in terms_lower):
                continue
            if not self._line_in_scope(line):
                continue
            # Extract relative path: `| path/to/entry.md | ...`
            parts = [p.strip() for p in line.split("|")]
            for part in parts:
                if part.endswith(".md") and "/" in part:
                    matched_paths.append(part)
                    break

        if not matched_paths:
            return ""

        context_parts = ["**From Agent Wiki:**"]
        for rel_path in matched_paths[:max_entries]:
            full_path = self.wiki_root.parent / rel_path
            if full_path.exists():
                try:
                    entry = full_path.read_text(encoding="utf-8")
                    # Strip YAML frontmatter, keep body
                    if entry.startswith("---"):
                        end = entry.find("---", 3)
                        entry = entry[end + 3:].strip() if end > 0 else entry
                    context_parts.append(f"\n### {rel_path}\n{entry[:600]}")
                except OSError:
                    pass

        return "\n".join(context_parts) if len(context_parts) > 1 else ""

    # ------------------------------------------------------------------
    # Compile (post-task knowledge persistence)
    # ------------------------------------------------------------------

    def compile(
        self,
        title: str,
        content: str,
        category: Optional[str] = None,
        tags: Optional[List[str]] = None,
        confidence: str = "medium",
        scope: str = "project",
    ) -> str:
        """Write a wiki entry, update index.md, append log.md.

        Args:
            title:      Human-readable title for the entry.
            content:    Markdown body of the entry.
            category:   One of CATEGORIES; auto-detected from title+content if None.
            tags:       List of tag strings.
            confidence: 'high' | 'medium' | 'speculative'
            scope:      'project' (default) or 'workspace'.  'workspace' marks
                        the entry as cross-project knowledge and surfaces it at
                        every project level.

        Returns:
            Relative path of the written file (e.g. '.agent-wiki/bugs/jwt-fix.md')
        """
        self._ensure_dirs()

        if category is None or category not in CATEGORIES:
            category = _detect_category(title + " " + content)

        slug = _slug(title)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        # Auto-tag with project scope so query filtering works correctly
        base_tags: List[str] = list(tags or [])
        if scope == "workspace" or not self.project_name:
            base_tags = [t for t in base_tags if not t.startswith("project:")]
        elif self.project_name:
            project_tag = f"project:{self.project_name}"
            if project_tag not in base_tags:
                base_tags.append(project_tag)

        tags_str = ", ".join(base_tags)

        frontmatter = (
            f"---\ntitle: {title}\ntags: [{tags_str}]\n"
            f"created: {timestamp}\nconfidence: {confidence}\n---\n\n"
        )
        entry_content = frontmatter + content.strip() + "\n"

        entry_path = self.wiki_root / category / f"{slug}.md"
        entry_path.write_text(entry_content, encoding="utf-8")

        rel_path = str(entry_path.relative_to(self.wiki_root.parent)).replace("\\", "/")
        self._update_index(category, slug, title, base_tags, rel_path, timestamp)
        self._append_log(f"compiled: [{title}]({rel_path}) ({category}) — {timestamp}")

        self.logger.info("wiki_entry_written", path=str(entry_path), category=category)
        return rel_path

    # ------------------------------------------------------------------
    # Status (wiki summary)
    # ------------------------------------------------------------------

    def status(self) -> dict:
        """Return a summary of wiki health: entry counts, project breakdown."""
        index_path = self.wiki_root / "index.md"
        if not index_path.exists():
            return {"total": 0, "by_category": {}, "by_project": {}, "wiki_root": str(self.wiki_root)}

        try:
            lines = [l for l in index_path.read_text(encoding="utf-8").splitlines()
                     if l.startswith("|") and ".md" in l and "Path" not in l]
        except OSError:
            lines = []

        by_category: dict[str, int] = {}
        by_project: dict[str, int] = {}

        for line in lines:
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 4:
                category = parts[3] if len(parts) > 3 else "unknown"
                by_category[category] = by_category.get(category, 0) + 1
                tags_col = parts[4] if len(parts) > 4 else ""
                match = _PROJECT_TAG_RE.search(tags_col)
                proj = match.group(0).split(":", 1)[1] if match else "<workspace>"
                by_project[proj] = by_project.get(proj, 0) + 1

        last_log = ""
        log_path = self.wiki_root / "log.md"
        if log_path.exists():
            try:
                log_lines = [l for l in log_path.read_text(encoding="utf-8").splitlines() if l.startswith("- ")]
                last_log = log_lines[-1][2:] if log_lines else ""
            except OSError:
                pass

        return {
            "total": len(lines),
            "by_category": by_category,
            "by_project": by_project,
            "wiki_root": str(self.wiki_root),
            "current_project": self.project_name or "<root>",
            "last_entry": last_log,
        }

    # ------------------------------------------------------------------
    # Clean (remove stale cross-project entries)
    # ------------------------------------------------------------------

    def clean(self) -> dict:
        """Remove index entries that are out of scope for the current project.

        At root (project_name=""): removes all entries with a ``project:`` tag.
        In a project: removes entries tagged for a *different* project.

        Entry files are NOT deleted — only the index row is removed so the
        content is recoverable.  Returns a summary dict.
        """
        index_path = self.wiki_root / "index.md"
        if not index_path.exists():
            return {"removed": 0, "kept": 0}

        try:
            raw = index_path.read_text(encoding="utf-8")
        except OSError:
            return {"removed": 0, "kept": 0}

        kept_lines: list[str] = []
        removed = 0

        for line in raw.splitlines(keepends=True):
            is_data_row = line.startswith("|") and ".md" in line and "Path" not in line
            if is_data_row and not self._line_in_scope(line):
                removed += 1
                self.logger.info("wiki_clean_removed", line=line.strip()[:80])
            else:
                kept_lines.append(line)

        if removed:
            index_path.write_text("".join(kept_lines), encoding="utf-8")
            self._append_log(f"clean: removed {removed} out-of-scope entries — {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")

        return {"removed": removed, "kept": sum(1 for l in kept_lines if l.startswith("|") and ".md" in l and "Path" not in l)}

    # ------------------------------------------------------------------
    # Migrate (move entries to a project's own wiki)
    # ------------------------------------------------------------------

    def migrate_to(self, project_name: str, target_workspace_path: str) -> dict:
        """Move entries tagged with ``project_name`` into the target project's wiki.

        Copies each matching entry file into the target `.agent-wiki`, updates
        both indexes, and removes the migrated rows from this index.
        Idempotent: already-migrated entries are skipped.

        Returns a summary dict with ``moved`` count.
        """
        target_wiki = WikiManager(target_workspace_path, project_name=project_name)
        target_wiki._ensure_dirs()

        index_path = self.wiki_root / "index.md"
        if not index_path.exists():
            return {"moved": 0}

        try:
            raw = index_path.read_text(encoding="utf-8")
        except OSError:
            return {"moved": 0}

        project_tag = f"project:{project_name.lower()}"
        kept_lines: list[str] = []
        moved = 0

        for line in raw.splitlines(keepends=True):
            is_data_row = line.startswith("|") and ".md" in line and "Path" not in line
            if not is_data_row:
                kept_lines.append(line)
                continue

            if project_tag not in line.lower():
                kept_lines.append(line)
                continue

            # This row belongs to the target project — migrate it
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 5:
                kept_lines.append(line)
                continue

            rel_path = parts[1]   # e.g. .agent-wiki/synthesis/my-entry.md
            src_file = self.wiki_root.parent / rel_path
            if not src_file.exists():
                kept_lines.append(line)
                continue

            # Derive the destination path inside the target wiki
            # rel_path is like ".agent-wiki/<category>/<slug>.md"
            path_parts = Path(rel_path).parts
            if len(path_parts) >= 3:
                dst_file = target_wiki.wiki_root / path_parts[-2] / path_parts[-1]
            else:
                dst_file = target_wiki.wiki_root / path_parts[-1]

            if not dst_file.exists():
                dst_file.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src_file, dst_file)
                dst_rel = str(dst_file.relative_to(target_wiki.wiki_root.parent)).replace("\\", "/")
                # Re-use the same index fields
                title = parts[2] if len(parts) > 2 else dst_file.stem
                category = parts[3] if len(parts) > 3 else "synthesis"
                tags_raw = parts[4] if len(parts) > 4 else ""
                timestamp = parts[5].strip() if len(parts) > 5 else datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
                tags_list = [t.strip() for t in tags_raw.split(",") if t.strip()]
                target_wiki._update_index(category, dst_file.stem, title, tags_list, dst_rel, timestamp)
                target_wiki._append_log(f"migrated-in: [{title}]({dst_rel}) — {timestamp}")
                moved += 1
                self.logger.info("wiki_migrated", src=str(src_file), dst=str(dst_file))
            # Either way, remove from source index
            # (don't keep the row in kept_lines)

        if moved:
            index_path.write_text("".join(kept_lines), encoding="utf-8")
            self._append_log(f"migrate: moved {moved} entries to {project_name} — {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')}")

        return {"moved": moved}

    # ------------------------------------------------------------------
    # Lint (wiki health check)
    # ------------------------------------------------------------------

    def lint(self) -> str:
        """Scan .agent-wiki/ for orphans and missing cross-references.

        Returns a markdown health report.
        """
        if not self.wiki_root.exists():
            return "Wiki directory does not exist yet — nothing to lint."

        all_entries = list(self.wiki_root.rglob("*.md"))
        all_entries = [e for e in all_entries if e.name not in ("index.md", "log.md")]

        if not all_entries:
            return "Wiki is empty."

        # Build inbound-link map
        inbound: dict[str, int] = {str(e.stem): 0 for e in all_entries}
        for entry in all_entries:
            try:
                text = entry.read_text(encoding="utf-8")
                for link in re.findall(r"\[\[([^\]]+)\]\]", text):
                    key = _slug(link)
                    if key in inbound:
                        inbound[key] += 1
            except OSError:
                pass

        orphans = [k for k, v in inbound.items() if v == 0]

        report_lines = ["## Wiki Health Report\n"]
        if orphans:
            report_lines.append("### Orphan Pages (no inbound links)")
            report_lines.extend(f"- {p}" for p in orphans)
        else:
            report_lines.append("### Orphan Pages\nNone found.")

        report_lines.append(f"\n**Total entries:** {len(all_entries)}")
        return "\n".join(report_lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _line_in_scope(self, index_line: str) -> bool:
        """Return True if this index row is visible under the current project scope.

        Rules:
          - At root (project_name=""): only rows with NO ``project:`` tag.
          - In a project: rows tagged ``project:<this>`` OR rows with no project tag.
        """
        match = _PROJECT_TAG_RE.search(index_line)
        if not match:
            return True  # untagged = workspace-level, always visible
        if not self.project_name:
            return False  # at root: hide any project-specific entry
        return match.group(0).lower() == f"project:{self.project_name.lower()}"

    def _update_index(
        self,
        category: str,
        slug: str,
        title: str,
        tags: List[str],
        rel_path: str,
        timestamp: str,
    ) -> None:
        """Upsert a row in index.md — update the existing row for rel_path if
        present, append a new row otherwise.  Prevents duplicate index entries
        when wiki-compile is called multiple times for the same entry.
        """
        index_path = self.wiki_root / "index.md"
        header = "# Agent Wiki Index\n\n| Path | Title | Category | Tags | Updated |\n|------|-------|----------|------|----------|\n"
        if not index_path.exists():
            index_path.write_text(header, encoding="utf-8")

        new_row = f"| {rel_path} | {title} | {category} | {', '.join(tags)} | {timestamp} |\n"

        existing = index_path.read_text(encoding="utf-8")
        lines = existing.splitlines(keepends=True)

        # Replace existing row that references this rel_path.
        updated = False
        for i, line in enumerate(lines):
            if f"| {rel_path} |" in line:
                lines[i] = new_row
                updated = True
                break

        if updated:
            index_path.write_text("".join(lines), encoding="utf-8")
        else:
            with open(index_path, "a", encoding="utf-8") as f:
                f.write(new_row)

    def _append_log(self, message: str) -> None:
        log_path = self.wiki_root / "log.md"
        if not log_path.exists():
            log_path.write_text("# Agent Wiki Log\n\n", encoding="utf-8")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(f"- {message}\n")


__all__ = ["WikiManager", "CATEGORIES"]
