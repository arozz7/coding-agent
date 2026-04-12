# Phase 12 — Plan/Build Mode, Discord Fixes, Wiki Initialization, Project Directories

## Goals
Fix four user-reported issues: Discord `!result` truncation, workspace project
directory creation, Plan/Build interactive mode, and agent wiki initialization.

---

## 12.0 — Discord Rate-Limit Fix

### `api/discord_bot.py`
- `_poll_job()` (line ~222): added `await asyncio.sleep(0.3)` after each chunk send in the
  inline result loop. Without this delay, Discord's rate limiter silently drops messages when
  multiple chunks are sent in rapid succession.
- `!result` command (line ~347): same `await asyncio.sleep(0.3)` fix after each chunk send.
- Added `"planning"` to `_PHASE_LABELS` dict so the status message shows **"Building plan"**
  while a plan task runs.
- Added `"plan"` to `_INLINE_TYPES` set so plan responses are streamed inline (like chat/
  research), not summarised as file output.

---

## 12.1 — Workspace Project Directory Rule

### `agent/agents/developer_agent.py`
- `DeveloperRole.get_system_prompt()` — added a **PROJECT DIRECTORY RULE** block (marked
  CRITICAL) that instructs the developer to:
  1. Infer a short, lowercase, hyphenated project name from the task
  2. Create ALL project files under `<project-name>/` subdirectory
  3. Never dump files into the workspace root for a new project
  4. When continuing an existing project, stay in the same subdirectory
  - Includes concrete examples: "Build an RPG game" → `rpg-game/`

---

## 12.2 — Plan / Build Interactive Mode

### `agent/agents/plan_agent.py` (new)
- `PlanRole` — extends `AgentRole`; system prompt instructs the LLM to produce a
  structured markdown plan document with: Project Overview, Tech Stack, Architecture,
  File Structure, Implementation Phases, Key Decisions, Risks & Mitigations.
- The role explicitly forbids file writes (no `FILE:` syntax, no shell commands).
- Plan always ends with: *"Reply 'build it' to start implementation, or request changes."*
- `PlanAgent` wrapper class (analogous to the other agent wrappers).

### `config/task_classifier.yaml`
- Added `plan` as the first valid type with higher priority than `develop`:
  - Triggers on: "plan first", "solid plan", "show me a plan", "want to plan",
    "planning phase", "let's plan", "before we build", "roadmap", etc.
- LLM classifier prompt updated with `plan` description and example phrases.

### `agent/orchestrator.py`
- `_detect_task_type_keyword()` — added `plan` as priority-0 check (before develop).
  Keyword list: "plan first", "solid plan", "show me a plan", "want to plan",
  "want first work on a", "planning phase", "let's plan", "lets plan", "before we build",
  "before building", "before implementing", "roadmap", "outline the approach",
  "outline a plan", "create a plan", "work on a plan", "i want a plan".
- `_run_specialized_agent()` — added `elif task_type == "plan"` branch that routes to
  `self.plan_agent`.
- `_phase_labels` dict — added `"plan": "planning"`.
- `plan_agent` instantiated in `__init__` alongside other agents.
- `PlanAgent` imported at module top.

---

## 12.3 — AGENTS.md Global Instructions

### `AGENTS.md` (new, project root)
A CLAUDE.md-equivalent for the coding agent system. Injected into every agent's
context before each task. Defines:
- Core philosophy (SOLID, DRY, KISS)
- The Golden Rule (no code without plan approval for multi-file changes)
- Project directory rule
- Plan/Build workflow description
- File size limits (300 soft / 600 hard)
- Error handling, testing, security standards
- Shell/terminal conventions (PowerShell, Windows)
- Agent wiki summary

### `agent/orchestrator.py`
- `_build_enriched_context()` — loads `AGENTS.md` from the project root and prepends it
  to the enriched context as **step 0** (before wiki-query and RAG). Falls back silently
  if the file is missing.

---

## 12.4 — Wiki Initialization & Always-Run Compile

### `agent/orchestrator.py`
- `__init__()` — calls `self.wiki_manager._ensure_dirs()` immediately after creating the
  `WikiManager` instance. This creates the full `.agent-wiki/` directory structure
  (`tech-patterns/`, `bugs/`, `decisions/`, `api-usage/`, `synthesis/`) at agent startup
  instead of waiting for the first `wiki-compile` call.
- `run_task()` post-skill section — `wiki-compile` now runs after **every** successful
  task (not only when the task contains "compile", "save", etc. keywords). Implemented by
  building `_always_post = ["wiki-compile"]` before the keyword-matched post-skills.

---

## Files Changed

| File | Change |
|---|---|
| `api/discord_bot.py` | sleep fix in `_poll_job` and `!result`; "planning" phase label; "plan" in inline types |
| `agent/agents/developer_agent.py` | Project directory rule in system prompt |
| `agent/agents/plan_agent.py` | New — PlanRole and PlanAgent |
| `config/task_classifier.yaml` | Added "plan" type with description and example phrases |
| `agent/orchestrator.py` | Plan routing, plan keyword detection, AGENTS.md injection, wiki-compile always runs, wiki dirs initialized on startup |
| `AGENTS.md` | New — global coding agent instructions |
| `aiChangeLog/phase-12.md` | This file |
