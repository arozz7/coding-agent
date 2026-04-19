# Phase 25 — Per-Project Wiki Isolation & Wiki Management Commands

## Problem solved

The workspace root `.agent-wiki` accumulated entries from all past projects (e.g. "Shadows-of-Eldoria" entries contaminated a new deep-research task, causing the architect agent to name an output file `architecture-design-shadows-of-eldoria.md`). Root cause: single shared wiki with no project scoping, and the planner could assign `architect` agent type to research synthesis tasks.

## Changes

### `agent/skills/wiki_manager.py`
- `WikiManager(workspace_path, project_name="")` — new `project_name` parameter (empty when at workspace root)
- `compile()` — auto-tags every entry with `project:<name>` when in a project; workspace-level entries (`scope="workspace"` or at root) are left untagged
- `query()` — strict scope filter: at root returns only untagged entries; in a project returns entries tagged for that project plus untagged entries; foreign-project entries are silently skipped
- New `status()` — returns total count, breakdown by category and project, last compiled entry
- New `clean()` — removes out-of-scope index rows (files preserved on disk)
- New `migrate_to(project_name, target_path)` — moves tagged entries from source wiki to correct project wiki, updates both indexes

### `agent/orchestrator.py`
- Derives `_project_name` by comparing `AGENT_EFFECTIVE_WORKSPACE` against `WORKSPACE_PATH`; passes it to `WikiManager`

### `api/main.py`
- `GET /wiki/status` — wiki health summary
- `GET /wiki/query?terms=` — project-scoped search
- `POST /wiki/clean` — strips out-of-scope index rows
- `POST /wiki/migrate` — moves project-tagged entries to their project wiki

### `api/discord_bot.py`
- `!wiki` command with `status`, `query <terms>`, `clean`, `migrate <project>` sub-actions

### `agent/agents/planner_agent.py`
- `_RESEARCH_SAFE_TYPES` constant excludes `architect` (it writes files)
- `_enforce_research_types()` remaps any `architect` task to `documenter` in research plans
- `_strategy_hint("research")` explicitly forbids `architect` in the planning prompt

### `agent/agents/architect_agent.py`
- `_sanitize_path()` strips unsafe characters and `..` segments from LLM-generated `FILE:` paths before writing

### `tests/unit/test_wiki_manager.py` (new)
- 18 tests covering: compile tagging, query isolation (project / root / cross-project), clean, migrate, status

## Migration guide for existing deployments

If the workspace root wiki has stale project entries:

```
!wiki migrate <project-name>   # move tagged entries to project wiki
!wiki clean                    # remove remaining stale tagged rows
!wiki status                   # confirm root wiki is clean
```

## Files changed

| File | Change type |
|------|-------------|
| `agent/skills/wiki_manager.py` | Modified — project isolation |
| `agent/orchestrator.py` | Modified — project_name wiring |
| `agent/agents/planner_agent.py` | Modified — research type enforcement |
| `agent/agents/architect_agent.py` | Modified — path sanitization |
| `api/main.py` | Modified — wiki endpoints |
| `api/discord_bot.py` | Modified — !wiki command |
| `tests/unit/test_wiki_manager.py` | New — 18 tests |
| `README.md` | Updated |
| `docs/user-manual.md` | Updated |
