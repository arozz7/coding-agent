# Phase 32 — Criterion Fix-Spec Quality: Targeted Instructions for All Auto-Check Types

**Date:** 2026-05-20  
**Commits:** `9201ffb` → `7bdfd66` (4 commits, landed after PR #15 merge)

## Objective

Three consecutive payment-tracker runs revealed that the criterion-driven fix loop was generating wrong instructions for every auto-checkable criterion type (`file contains`, `file exists`, `command exits 0`). The `_detect_fix_phase()` round-number fallback was firing for all of them, producing instructions like "Runtime is stable. Fix the failing tests" for a build failure and "Run the application" for a missing line of text. This phase replaces all three with targeted, criterion-aware instructions.

---

## Problems

### 1 — `file contains` fix spec: wrong instruction every round

**Criterion:** `file contains: PROJECT_PLAN.md:complete`  
**What happened:** `make_targeted_fix_spec()` called `_detect_fix_phase()`. No syntax/runtime/test signals in the detail, so the round-number fallback fired: round 1 → "run syntax checks", round 2 → "run the application", round 3 → "make the application do what the objective requires." Two wasted rounds before round 3 accidentally appended the needed text.  
**Fix:** `make_targeted_fix_spec()` detects `file contains:` before calling `_detect_fix_phase()` and generates: "Read `path`, then APPEND: the text `substring`." If the file is missing entirely (detail starts with "read error" or "missing:"), it generates: "Create `path` containing `substring`."

### 2 — `file exists` glob fix spec: literal wildcard confuses the agent

**Criterion:** `file exists: PHASE1_*.md`  
**What happened:** Fix spec said "Create the missing file `PHASE1_*.md`." The agent saw the `*` and either created nothing valid or placed the file in a subdirectory. The root-level glob `ws.glob("PHASE1_*.md")` then missed it. Two wasted rounds.  
**Fixes:**
- `make_targeted_fix_spec()`: for glob patterns, substitutes `*` → `COMPLETE` to give a concrete example filename. Instruction becomes "Create a file matching `PHASE1_*.md` — for example `PHASE1_COMPLETE.md` — at the project root."
- `_eval_one()` `file exists` branch: when root-level glob returns nothing, falls back to recursive `ws.glob(f"**/{pattern}")` before declaring failure.

### 3 — `command exits 0` fix spec: "fix the tests" for a build error

**Criterion:** `command exits 0: npm run build`  
**What happened:** `_detect_fix_phase()` found "failed" in the gaps text, matched `_TEST_FAILURE_SIGNALS`, and returned "Runtime is stable. Fix the failing tests." Three rounds, all wrong.  
**Fix:** `make_targeted_fix_spec()` detects `command exits 0:` and generates: "Run `{cmd}`, read its complete output, fix ALL errors, re-run to confirm."

### 4 — Requirements extractor still generating documentation-file criteria

**What happened:** `file contains: PROJECT_PLAN.md:complete`, `file contains: PROJECT_PLAN.md:# Role & Objective`, and `file exists: PHASE1_*.md` were all generated as acceptance criteria. The system prompt banned `file exists` by convention but not `file contains` on doc files or `PHASE*.md` patterns.  
**Fix:** Extended the ban to cover `file contains` against documentation/planning files and added explicit list: `PROJECT_PLAN.md, README.md, CHANGELOG.md, ARCHITECTURE.md, STACK.md, PHASE*.md, ROADMAP.md`. Also fixed example format from `|` separator (wrong) to `:` separator (what the parser expects).

---

## Changes

**`agent/orchestration/verifier_coordinator.py`**
- `make_targeted_fix_spec()`: new branches for `file contains:`, `file exists:` (with glob handling), and `command exits 0:` before the `_detect_fix_phase()` fallback. Glob patterns replaced with concrete example filenames via `re.sub(r"[*?]+", "COMPLETE", rel)`.
- `_eval_one()` `file exists` branch: recursive `**/` fallback when root-level glob returns no results.

**`agent/orchestration/requirements_extractor.py`**
- System prompt: corrected `file contains` format example from `|` separator to `:` separator.
- Added explicit ban on criteria targeting documentation/planning files (`PROJECT_PLAN.md`, `README.md`, `CHANGELOG.md`, `ARCHITECTURE.md`, `STACK.md`, `PHASE*.md`, `ROADMAP.md`).

**`README.md`**
- Added phase-31 features: dynamic evaluator model, APPEND: blocks, Bayesian fix budgets, /goal-style criteria, static HTML screenshots, Discord attachments, episodic memory decontamination.
- Updated project structure: added `criterion_score_store.py`, `requirements_extractor.py`, `app_probe.py`; corrected `__init__.py` and `model_router.py` descriptions.

---

## Modified Files Summary

| File | Change |
|---|---|
| `agent/orchestration/verifier_coordinator.py` | Targeted fix specs for all 3 auto-check criterion types; recursive glob fallback |
| `agent/orchestration/requirements_extractor.py` | Format fix (`|` → `:`); extended documentation-file ban |
| `README.md` | Phase-31 feature documentation + project structure update |
