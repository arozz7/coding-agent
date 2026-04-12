# Phase 05 — Batch 3: Skill Execution + Context Pipeline Repair

**Date:** 2026-04-10
**Plan version:** v2.3
**Covers:** Batch 3 — skill execution, WikiManager, SkillExecutor, context pipeline fix

---

## Summary

Batch 2 introduced a regression: deleting `_run_general_agent()` left `_detect_skills()`,
`_get_skill_context()`, `_load_wiki_context()`, and `_load_rag_context()` as dead code —
no code path called them, so wiki context and RAG were completely disconnected.

Batch 3 fixes the regression, replaces the four dead methods with a single
`_build_enriched_context()`, adds real skill execution (not just text injection), and
implements the wiki knowledge base (compile, query, lint).

---

## New Files

### `agent/skills/wiki_manager.py`

Handles all `.agent-wiki/` I/O. Follows the Karpathy LLM Wiki pattern.

- `query(terms, max_entries)` — reads `index.md`, finds matching entries, returns content as context string
- `compile(title, content, category, tags, confidence)` — writes wiki entry, updates `index.md`, appends `log.md`
- `lint()` — scans for orphan pages (no inbound wikilinks), returns markdown health report
- `_detect_category(text)` — auto-detects category from keyword matching
- Auto-creates `.agent-wiki/{tech-patterns,bugs,decisions,api-usage,synthesis}/` on first write

Wiki structure maintained:
```
.agent-wiki/
├── index.md      ← append-only catalog table
├── log.md        ← append-only compilation log
├── tech-patterns/
├── bugs/
├── decisions/
├── api-usage/
└── synthesis/
```

### `agent/skills/skill_executor.py`

Dispatches pre/post skill execution. Replaces the text-only `_get_skill_context()` pattern.

**Pre-execution skills** (return context string to inject into prompt):
- `wiki-query` → calls `WikiManager.query()` with terms extracted from task
- `security-auditor` → runs `scan_secrets.py` via `importlib`, injects findings
- All others → inject SKILL.md content as prompt instructions

**Post-execution skills** (take action after task completes):
- `wiki-compile` → calls LLM to synthesize a structured wiki entry; parses TITLE/TAGS/CATEGORY/CONFIDENCE headers; writes entry via `WikiManager.compile()`; falls back to template if no model available
- `wiki-lint` → calls `WikiManager.lint()`, returns health report
- `handover` → returns instruction to run `/handover` in new session

---

## Modified Files

### `agent/skills/__init__.py`

Exports `WikiManager` and `SkillExecutor` alongside existing `SkillManager`/`Skill`.

### `agent/orchestrator.py` — Context Pipeline Repair + Skill Wiring

**Regression fix — four dead methods replaced:**
- Deleted: `_detect_skills()`, `_get_skill_context()`, `_load_wiki_context()`, `_load_rag_context()`
- Added: `_build_enriched_context(task)` — async method that runs all three enrichment steps in sequence:
  1. `wiki-query` pre-skill → persistent knowledge from `.agent-wiki/`
  2. RAG — `codebase_memory.get_relevant_context()` (was always called but result was discarded)
  3. Pre-execution skill instructions (tdd-enforcer, security-auditor, etc.)

**Skill trigger tables promoted to class constants** (`_PRE_TRIGGERS`, `_POST_TRIGGERS`):
- `_detect_skill_names(task, phase)` replaces `_detect_skills()`

**`_run_specialized_agent()` updated:**
- Calls `_build_enriched_context(task)` before agent dispatch
- Passes `enriched_context` string in context dict to all agents

**Post-execution skill dispatch added in `run_task()`:**
- After successful result, calls `_detect_skill_names(task, "post")`
- Executes each triggered post-skill via `skill_executor.execute_post()`
- Appends `skill_reports` list to the returned result dict

**`WikiManager` and `SkillExecutor` initialized in `__init__()`:**
```python
self.wiki_manager = WikiManager(workspace_path)
self.skill_executor = SkillExecutor(self.wiki_manager, self.skill_manager)
```

### All four agent roles — enriched context injection

Each role (`developer`, `architect`, `reviewer`, `tester`) now reads
`context.get("enriched_context", "")` and appends it to the LLM prompt.
This was the missing final step — the context string was built but never
included in the actual LLM call.

---

## Flow After Batch 3

```
run_task(task)
  ├─ _build_enriched_context(task)
  │    ├─ wiki-query: reads .agent-wiki/index.md → relevant entry content
  │    ├─ RAG: vector search → top-3 code chunks
  │    └─ pre-skills: tdd-enforcer/security-auditor SKILL.md injected as instructions
  │
  ├─ _run_specialized_agent(task, task_type, session_id)
  │    └─ agent.run(task, context={..., enriched_context: "..."})
  │         └─ role.execute(context)
  │              └─ prompt includes wiki + RAG + skill instructions  ← NEW
  │
  └─ post-skills (if triggered):
       └─ wiki-compile: LLM synthesizes entry → WikiManager.compile() → index.md + log.md
```

---

## Known Remaining Issues (Batch 4+)

- `OllamaModelManager` uses blocking `httpx.Client` — blocks event loop (Batch 4)
- Two circuit breaker implementations not consolidated (Batch 4)
- Git tools in MCP not registered — `repo_path` never passed (Batch 4)
- Subagent spawning does not pass `tool_executor` or `enriched_context` into isolated context (Batch 5)
- Token estimation uses `len(text)//4` — switch to `tiktoken` (Batch 5)
- `memory_wiki.py` NetworkX graph not implemented (Batch 5)
- Prometheus `/metrics` endpoint not implemented (Batch 5)
- Wiki `lint()` only checks orphans — contradiction detection not yet implemented (future)
