"""
WikiManager - File I/O for the .agent-wiki knowledge base.

Follows the Karpathy LLM Wiki pattern:
  - index.md  : catalog of all entries (append-only table)
  - log.md    : chronological record of compilations
  - <category>/<name>.md : individual knowledge entries

Usage:
    wiki = WikiManager(workspace_path)
    context = wiki.query(["authentication", "jwt"])   # pre-task
    wiki.compile("tech-patterns", "jwt-auth", content, "Discovered JWT pattern")  # post-task
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
import structlog

logger = structlog.get_logger()

CATEGORIES = ("tech-patterns", "bugs", "decisions", "api-usage", "synthesis")

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
    """Read/write the .agent-wiki knowledge base inside a workspace."""

    def __init__(self, workspace_path: str):
        self.wiki_root = Path(workspace_path) / ".agent-wiki"
        self.logger = logger.bind(component="wiki_manager")

    def _ensure_dirs(self) -> None:
        self.wiki_root.mkdir(parents=True, exist_ok=True)
        for cat in CATEGORIES:
            (self.wiki_root / cat).mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # Query (pre-task context injection)
    # ------------------------------------------------------------------

    def query(self, terms: List[str], max_entries: int = 4) -> str:
        """Return relevant wiki content for the given search terms.

        Reads index.md, finds lines matching any term, reads those entry
        files, and returns a formatted context block.
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
            if any(t in line.lower() for t in terms_lower):
                # Extract relative path from index line: `| path/to/entry.md | ...`
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
    ) -> str:
        """Write a wiki entry, update index.md, append log.md.

        Args:
            title:      Human-readable title for the entry.
            content:    Markdown body of the entry.
            category:   One of CATEGORIES; auto-detected from title+content if None.
            tags:       List of tag strings.
            confidence: 'high' | 'medium' | 'speculative'

        Returns:
            Relative path of the written file (e.g. '.agent-wiki/bugs/jwt-fix.md')
        """
        self._ensure_dirs()

        if category is None or category not in CATEGORIES:
            category = _detect_category(title + " " + content)

        slug = _slug(title)
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tags_str = ", ".join(tags or [])

        frontmatter = (
            f"---\ntitle: {title}\ntags: [{tags_str}]\n"
            f"created: {timestamp}\nconfidence: {confidence}\n---\n\n"
        )
        entry_content = frontmatter + content.strip() + "\n"

        entry_path = self.wiki_root / category / f"{slug}.md"
        entry_path.write_text(entry_content, encoding="utf-8")

        rel_path = str(entry_path.relative_to(self.wiki_root.parent)).replace("\\", "/")
        self._update_index(category, slug, title, tags or [], rel_path, timestamp)
        self._append_log(f"compiled: [{title}]({rel_path}) ({category}) — {timestamp}")

        self.logger.info("wiki_entry_written", path=str(entry_path), category=category)
        return rel_path

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
