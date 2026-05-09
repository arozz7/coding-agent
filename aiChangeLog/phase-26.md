# Phase 26 — Extended Loop, Web Detection & Self-Learning Skills

## Summary

Five phases of improvements to the developer agent loop, verifier, and the
skill system infrastructure.

## Phase 1 — Extended Fix Loop

**`agent/agents/verifier_agent.py`**
- Added `test_output: str = ""` field to `VerifierResult` (also in `to_dict()`)
- `verify_code()` now populates `test_output` on the returned result
- Web-game note added to LLM scoring prompt (HTML truncation → score 0 for tests)

**`agent/orchestrator.py`**
- Per-task-type verifier round caps:
  - `develop`/`sdlc` → `DEVELOP_VERIFIER_ROUNDS` env var, default **6**
  - `research` → `RESEARCH_VERIFIER_ROUNDS` env var, default **3**
- Added `_plateau_count` tracking; stagnation now requires:
  - Score drops ≥ 2 pts (hard stop), OR
  - Score ≥ 5 AND plateau for **2 consecutive rounds** (soft stop)
  - Scores < 5 always run to the full round budget
- `_verifier_snapshot_files` tracks which files were added since last verifier round
- Both job_id and non-persistence paths updated consistently

**`agent/orchestration/verifier_coordinator.py`**
- `make_fix_specs()` gains `files_changed_this_round: list | None = None`
- Fix tasks are now **phase-aware**:
  - Round 1 → `syntax` (run node --check, fix all syntax first)
  - Round 2 → `runtime` (fix undefined refs, missing imports)
  - Round 3+ → `functionality` (implement features, ensure visible output)
- Fix task descriptions embed: verifier test output (up to 800 chars) + changed files

## Phase 2 — Web Project Completion Detection

**`agent/agents/verifier_agent.py` — `_run_nodejs_checks()`**
- Detects `public/index.html` and validates it is complete (`</html>` present)
- Warns when no `serve`/`http-server`/`live-server` script is in `package.json`
- Outputs `[web-game]` lines in the test output block surfaced to the LLM verifier

## Phase 3 — Skill System Foundation

**`agent/skills/wiki_manager.py`**
- Added `"skills"` to `CATEGORIES`
- `_ensure_dirs()` creates `skills/global/`, `skills/project/`, `skills/agents/`
- New `query_skills(terms, agent_type, max_entries)` — three-tier scope lookup ranked by `use_count`
- New `record_skill_use(slug, success)` — updates `use_count`, `success_rate`, `last_used`

**New `agent/skills/skill_writer.py`**
- `SkillWriter.write_fix_skill()` — creates a skill from an error→fix transition
- `SkillWriter.parse_and_write_skill_blocks()` — parses `SKILL: <title>` blocks from agent responses
- Routes to `global/`, `project/`, or `agents/<type>/` based on scope

**`agent/orchestration/context_builder.py`**
- Section 6 added to `build()`: loads matching skills from `.agent-wiki/skills/` via `query_skills()`
- Scoped to the active agent_type; capped at 8 000 chars (4 % of context budget)

**`agent/agents/developer_agent.py`**
- Added `_SKILL_BLOCK_RE` and `_SCRIPT_BLOCK_RE` module-level regexes
- After each task response, SKILL: blocks are parsed and saved via `SkillWriter`
- SCRIPT: blocks are parsed and saved via `ProjectScriptsTool`

## Phase 4 — Skill Lifecycle

**New `agent/skills/skill_consolidator.py`**
- `find_conflicts()` — groups skills by trigger prefix, flags overlapping entries
- `prune()` — archives `use_count=0` skills older than 30 days
- `promote()` — lifts project-scope skills to global when trigger appears in 3+ projects
- `merge(conflict_group)` — LLM-merges a conflict group into one authoritative entry, archives originals

## Phase 5 — Agent-Created Project Scripts

**New `agent/tools/project_scripts.py`**
- `ProjectScriptsTool.save_script(name, content)` — writes to `scripts/`, chmod +x on Unix
- `ProjectScriptsTool.list_scripts()` — formatted listing for agent context injection
- Path-traversal guard on script names

## File Map

| File | Status |
|------|--------|
| `agent/agents/verifier_agent.py` | Modified |
| `agent/orchestrator.py` | Modified |
| `agent/orchestration/verifier_coordinator.py` | Modified |
| `agent/orchestration/context_builder.py` | Modified |
| `agent/agents/developer_agent.py` | Modified |
| `agent/skills/wiki_manager.py` | Modified |
| `agent/skills/skill_writer.py` | **New** |
| `agent/skills/skill_consolidator.py` | **New** |
| `agent/tools/project_scripts.py` | **New** |
