"""Tests for WikiManager project isolation, tagging, clean, and migrate."""
import pytest
from pathlib import Path
import tempfile

from agent.skills.wiki_manager import WikiManager


@pytest.fixture
def tmp_workspace(tmp_path):
    """Workspace root with two project sub-dirs."""
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "alpha").mkdir()
    (root / "beta").mkdir()
    return root


# ---------------------------------------------------------------------------
# Compile tagging
# ---------------------------------------------------------------------------

class TestCompileTagging:
    def test_project_tag_added_when_project_active(self, tmp_workspace):
        wiki = WikiManager(str(tmp_workspace / "alpha"), project_name="alpha")
        wiki.compile("Test Entry", "Some content", category="synthesis")
        index = (tmp_workspace / "alpha" / ".agent-wiki" / "index.md").read_text()
        assert "project:alpha" in index

    def test_no_project_tag_at_root(self, tmp_workspace):
        wiki = WikiManager(str(tmp_workspace), project_name="")
        wiki.compile("Root Entry", "Root content", category="synthesis")
        index = (tmp_workspace / ".agent-wiki" / "index.md").read_text()
        assert "project:" not in index

    def test_workspace_scope_strips_project_tag(self, tmp_workspace):
        wiki = WikiManager(str(tmp_workspace / "alpha"), project_name="alpha")
        wiki.compile("Global Pattern", "Content", category="tech-patterns", scope="workspace")
        index = (tmp_workspace / "alpha" / ".agent-wiki" / "index.md").read_text()
        assert "project:" not in index

    def test_existing_project_tag_not_duplicated(self, tmp_workspace):
        wiki = WikiManager(str(tmp_workspace / "alpha"), project_name="alpha")
        wiki.compile("Entry", "Content", category="synthesis", tags=["project:alpha", "api"])
        index = (tmp_workspace / "alpha" / ".agent-wiki" / "index.md").read_text()
        assert index.count("project:alpha") == 1


# ---------------------------------------------------------------------------
# Query isolation
# ---------------------------------------------------------------------------

class TestQueryIsolation:
    def _seed_entry(self, workspace_path, project_name, title, content):
        wiki = WikiManager(str(workspace_path), project_name=project_name)
        wiki.compile(title, content, category="synthesis", tags=["auth"])

    def test_project_query_returns_own_entries(self, tmp_workspace):
        self._seed_entry(tmp_workspace / "alpha", "alpha", "Alpha Auth", "jwt token handling")
        wiki = WikiManager(str(tmp_workspace / "alpha"), project_name="alpha")
        result = wiki.query(["auth"])
        assert result != ""  # something returned
        assert "alpha-auth" in result  # slug appears in path header

    def test_project_query_excludes_other_project(self, tmp_workspace):
        self._seed_entry(tmp_workspace / "beta", "beta", "Beta Auth", "oauth2 flow")
        # Copy beta's wiki into alpha so alpha can see it (simulates root contamination)
        import shutil
        shutil.copytree(
            tmp_workspace / "beta" / ".agent-wiki",
            tmp_workspace / "alpha" / ".agent-wiki",
        )
        wiki = WikiManager(str(tmp_workspace / "alpha"), project_name="alpha")
        result = wiki.query(["auth"])
        # beta's entry tagged project:beta should not appear for alpha
        assert "Beta Auth" not in result

    def test_root_query_excludes_project_entries(self, tmp_workspace):
        # Seed a project entry directly into the root wiki
        root_wiki = WikiManager(str(tmp_workspace), project_name="alpha")
        root_wiki.compile("Alpha Work", "specific to alpha", category="synthesis")
        # Query from root (no project)
        root_anon = WikiManager(str(tmp_workspace), project_name="")
        result = root_anon.query(["alpha"])
        assert "Alpha Work" not in result

    def test_root_query_returns_untagged_entries(self, tmp_workspace):
        wiki = WikiManager(str(tmp_workspace), project_name="")
        wiki.compile("Global Pattern", "reusable across projects", category="tech-patterns")
        result = wiki.query(["pattern"])
        assert result != ""
        assert "global-pattern" in result  # slug appears in path header

    def test_project_query_returns_workspace_level_entries(self, tmp_workspace):
        # Workspace-level entry (no project tag) written to alpha's wiki
        wiki_write = WikiManager(str(tmp_workspace / "alpha"), project_name="alpha")
        wiki_write.compile("Shared Pattern", "reusable", category="tech-patterns", scope="workspace")
        wiki_read = WikiManager(str(tmp_workspace / "alpha"), project_name="alpha")
        result = wiki_read.query(["pattern"])
        assert result != ""
        assert "shared-pattern" in result


# ---------------------------------------------------------------------------
# Clean
# ---------------------------------------------------------------------------

class TestClean:
    def test_clean_removes_foreign_project_entries_at_root(self, tmp_workspace):
        # Write an entry tagged project:alpha into root wiki
        wiki = WikiManager(str(tmp_workspace), project_name="alpha")
        wiki.compile("Alpha Entry", "content", category="synthesis")
        # Now clean from root perspective (no project)
        root_wiki = WikiManager(str(tmp_workspace), project_name="")
        result = root_wiki.clean()
        assert result["removed"] == 1
        index = (tmp_workspace / ".agent-wiki" / "index.md").read_text()
        assert "project:alpha" not in index

    def test_clean_keeps_own_project_entries(self, tmp_workspace):
        wiki = WikiManager(str(tmp_workspace / "alpha"), project_name="alpha")
        wiki.compile("Alpha Entry", "content", category="synthesis")
        result = wiki.clean()
        assert result["removed"] == 0
        assert result["kept"] == 1

    def test_clean_removes_different_project_entries(self, tmp_workspace):
        # Seed alpha and beta into alpha's wiki (beta snuck in via contamination)
        wiki_a = WikiManager(str(tmp_workspace / "alpha"), project_name="alpha")
        wiki_a.compile("Alpha Entry", "content", category="synthesis")
        wiki_b = WikiManager(str(tmp_workspace / "alpha"), project_name="beta")
        wiki_b.compile("Beta Entry", "content", category="synthesis")
        # Clean from alpha's perspective
        alpha = WikiManager(str(tmp_workspace / "alpha"), project_name="alpha")
        result = alpha.clean()
        assert result["removed"] == 1
        assert result["kept"] == 1


# ---------------------------------------------------------------------------
# Migrate
# ---------------------------------------------------------------------------

class TestMigrate:
    def test_migrate_moves_project_entries_to_target(self, tmp_workspace):
        # Write alpha entries into root wiki
        wiki = WikiManager(str(tmp_workspace), project_name="alpha")
        wiki.compile("Alpha Feature", "jwt pattern", category="tech-patterns")
        # Migrate alpha entries to alpha's own wiki
        root_wiki = WikiManager(str(tmp_workspace), project_name="")
        result = root_wiki.migrate_to("alpha", str(tmp_workspace / "alpha"))
        assert result["moved"] == 1
        # Target wiki should now have the entry
        target_index = (tmp_workspace / "alpha" / ".agent-wiki" / "index.md").read_text()
        assert "Alpha Feature" in target_index

    def test_migrate_removes_entries_from_source(self, tmp_workspace):
        wiki = WikiManager(str(tmp_workspace), project_name="alpha")
        wiki.compile("Alpha Feature", "content", category="synthesis")
        root_wiki = WikiManager(str(tmp_workspace), project_name="")
        root_wiki.migrate_to("alpha", str(tmp_workspace / "alpha"))
        src_index = (tmp_workspace / ".agent-wiki" / "index.md").read_text()
        assert "Alpha Feature" not in src_index

    def test_migrate_does_not_touch_other_projects(self, tmp_workspace):
        wiki_a = WikiManager(str(tmp_workspace), project_name="alpha")
        wiki_a.compile("Alpha Feature", "content", category="synthesis")
        wiki_b = WikiManager(str(tmp_workspace), project_name="beta")
        wiki_b.compile("Beta Feature", "content", category="synthesis")
        # Migrate only alpha
        root_wiki = WikiManager(str(tmp_workspace), project_name="")
        root_wiki.migrate_to("alpha", str(tmp_workspace / "alpha"))
        src_index = (tmp_workspace / ".agent-wiki" / "index.md").read_text()
        # Beta entry should still be there
        assert "Beta Feature" in src_index


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

class TestStatus:
    def test_status_empty_wiki(self, tmp_workspace):
        wiki = WikiManager(str(tmp_workspace), project_name="")
        result = wiki.status()
        assert result["total"] == 0

    def test_status_counts_entries(self, tmp_workspace):
        wiki = WikiManager(str(tmp_workspace / "alpha"), project_name="alpha")
        wiki.compile("Entry 1", "content", category="synthesis")
        wiki.compile("Entry 2", "content", category="bugs")
        result = wiki.status()
        assert result["total"] == 2
        assert result["by_category"].get("synthesis") == 1
        assert result["by_category"].get("bugs") == 1

    def test_status_project_breakdown(self, tmp_workspace):
        wiki = WikiManager(str(tmp_workspace / "alpha"), project_name="alpha")
        wiki.compile("Alpha Entry", "content", category="synthesis")
        result = wiki.status()
        assert "alpha" in result["by_project"]
