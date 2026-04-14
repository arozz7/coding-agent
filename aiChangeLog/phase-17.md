# Phase 17 — Codex-Inspired Agent Improvements + Reliability Fixes

## Summary

Four workstreams shipped in this phase:

1. **CodeQL false-positive resolution** — Dismissed 6 new path-injection alerts
   introduced by the Phase 16 workspace endpoints. All were false positives
   (paths sourced from env vars we control or validated before use). The
   `# lgtm` inline suppression is no longer honoured by CodeQL 2.25.x; the
   GitHub alert-dismissal API is the correct mechanism.

2. **Task misclassification + workspace blindness** — Content-writing tasks
   ("flush out the narrative docs") were routed to `chat` instead of `develop`
   because the classifier only recognised code-writing as develop. Fixed in
   both the LLM classifier prompt and the keyword fallback. Added a workspace
   file listing to every agent's enriched context so agents see what files
   actually exist before responding.

3. **Four Codex-inspired agent improvements** — Based on a research pass over
   the OpenAI Codex CLI architecture (github.com/openai/codex):
   - Subagent depth limit lowered 3 → 2 (prevent runaway nesting)
   - Prompt order fixed: static content first, current task last (cache-friendly)
   - Structured `## DONE` marker so developer agent emits a clean completion summary
   - Lazy skill content loading: SKILL.md text is read on first use, not at startup

4. **Shell tool PATH auto-discovery** — `subprocess.run()` commands ("npm not
   found") were failing because the API server inherits a minimal PATH from its
   launcher. The shell tool now builds an augmented PATH at module load by
   scanning common install directories (nvm, volta, fnm, Homebrew, Cargo, etc.)
   and merges them with the subprocess environment. An optional `EXTRA_PATH`
   env var covers non-standard installs.

---

## Modified Files

| File | Change |
|------|--------|
| `api/main.py` | Added `# lgtm` suppression comments to 4 new path-injection lines; 6 total alerts dismissed via GitHub API |
| `agent/orchestrator.py` | (1) Added content-writing keywords to `_detect_task_type_keyword` — "flush out", "flesh out", "fill in", "draft the", etc. (2) Added workspace file listing block to `_build_enriched_context` so all agents see real directory tree |
| `config/task_classifier.yaml` | Expanded `develop` description to include document/narrative/content writing tasks |
| `agent/subagent/spawner.py` | Lowered `max_depth` default 3 → 2 |
| `agent/agents/developer_agent.py` | (1) Prompt order: enriched_context before task. (2) Added `## DONE / Files created / Summary` block to system prompt. (3) Parses DONE block to extract `completion_summary` and reconcile `files_created` |
| `agent/skills/skill_loader.py` | `Skill.content` is now a lazy `@property` — reads SKILL.md on first access only. `_load_skill()` reads frontmatter only at startup |
| `agent/tools/shell_tool.py` | Added `_build_tool_env()` — builds augmented PATH by probing ~15 common install directories per OS. Stored in module-level `_TOOL_ENV` passed to all `subprocess.run()` calls. Startup log shows which tools were found |
| `tests/unit/test_subagent.py` | Tests that exercise 3-level traversal now pass explicit `max_depth=3` |

---

## Architecture — Task Classifier Fix

```
User: "flush out the narrative docs"
                ↓
_detect_task_type_keyword(task)
  checks _DEVELOP keywords → "flush out" matches → returns "develop"
                ↓
_run_task_loop → DeveloperAgent
  receives enriched_context with workspace file listing
  reads existing docs → writes content → emits ## DONE
```

Previously this fell through all keyword groups and returned `"chat"`,
routing to `ChatAgent` which has no file tools and replied
"I don't have visibility into your docs folder."

---

## Architecture — Shell PATH Discovery

```
Module load:
  _build_tool_env()
    path_parts = current os.environ["PATH"]
    + scan ~15 common dirs (nvm, volta, fnm, Homebrew, Cargo …)
    + EXTRA_PATH from .env (user override, comma-separated)
    → _TOOL_ENV["PATH"] = merged PATH

ShellTool.run():
  subprocess.run(..., env=_TOOL_ENV)   ← always uses augmented PATH

ShellTool.__init__():
  logs {npm, node, python, git, cargo} → resolved paths (or NOT FOUND)
```

On the dev machine: npm resolved via nvm (`C:\Users\arozz\AppData\Local\nvm\v22.15.0\npm.CMD`).

---

## Architecture — Skill Lazy Loading

```
Startup (discover_skills):
  for each SKILL.md:
    read raw text  ← ONLY to extract frontmatter
    Skill(name, description, path, triggers)  ← no content stored

On first trigger (execute_pre / get_skill):
  skill.content  ← property reads path.read_text() and caches result
```

Reduces startup I/O from N file reads to N metadata reads.
Full content is only loaded for the 1–2 skills actually triggered per task.

---

## Codex Patterns Adopted / Skipped

| Pattern | Decision |
|---|---|
| Subagent depth limit | ✅ Adopted — max_depth 3→2 |
| Prompt order (static → dynamic) | ✅ Adopted — task moved to end |
| Structured completion signal | ✅ Adopted — `## DONE` marker |
| Skill progressive disclosure | ✅ Adopted — lazy content load |
| Zero Data Retention / stateless | ⛔ Skipped — local single-user, not needed |
| MCP server integration | ⛔ Skipped — own tool executor pattern |
| Encrypted context compaction | ⛔ Skipped — context bridge covers this |
| Full sandbox + approval workflow | ⛔ Skipped — overkill for local setup |

---

## Test Results
- All 219 unit tests pass
- PR #5 CodeQL check: passing (6 alerts dismissed as false positives)
