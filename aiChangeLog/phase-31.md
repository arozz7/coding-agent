# Phase 31 — Agent Quality: Evaluator Model, Screenshot, Criteria, Context Fixes

**Date:** 2026-05-19  
**Commits:** `9a988d0` → `fe3c357` (15 commits)

## Objective

A full session of systemic fixes driven by end-to-end rolling-car canvas tests and a failed payment-tracker planning run. Covers eight independent problem areas: criteria evaluation bias, dynamic free-model selection, criterion format quality, search library migration, JSON parse bugs, trailing test-task injection, static deliverable screenshots, and Discord context contamination.

---

## 1 — Criteria Pipeline Fixes (7 commits)

### Problems

Multiple interconnected failures in the criterion evaluation loop across `fix(criteria)` × 3, `refactor(planner,verifier)`, `fix(verifier,developer)`, `fix(greenfield,verifier)`, `fix(planner,orchestrator)` commits:

- Glob patterns in `file exists:` and `file contains:` criteria were not expanding — only exact path checks ran.
- Filename fallback logic (try exact path → fallback to `**/*.<ext>` glob) was missing for `file contains`.
- Behavioral criteria were not gating the fix loop — all criteria evaluated regardless of type.
- Verifier score defaulted to 0 on greenfield tasks (no existing code), blocking all fix rounds.
- Canvas parallax direction was wrong (background scrolling rightward).
- Scenario-specific patches in planner and verifier were masking root causes.
- `dependency-dir` exclusion missing from recursive glob so `node_modules` contents matched criteria unintentionally.

### Changes

**`agent/orchestration/verifier_coordinator.py`**
- `_glob_filtered()` helper: runs `workspace.rglob(pattern)` and excludes `node_modules/`, `.git/`, `dist/`, `build/`, `__pycache__/` from results.
- `file exists:` and `file contains:` criterion checks now expand glob patterns via `_glob_filtered()` before falling back to exact path.
- `_extract_truncated_file_info()`: reads the last 30 lines of a truncated file for targeted HTML truncation recovery.
- `make_fix_specs()` HTML branch: generates APPEND: instructions to close missing tags instead of generic "split into modules".
- `run_acceptance_tests()`: returns `[]` (skip) for static deliverables with no server entry point instead of returning all-failed.

**`agent/orchestration/requirements_extractor.py`**
- System prompt updated: no longer generates `file exists: index.html` convention criteria. Generates behavioral criteria about what the running app does.

**`agent/agents/verifier_agent.py`**
- Score floor: greenfield tasks (no pre-existing files) start at 3/10, not 0/10.

**`agent/agents/developer_agent.py`**
- Parallax canvas direction rule added to system prompt: background objects scroll LEFT when car drives rightward.

**`agent/agents/planner_agent.py`** / **`agent/orchestrator.py`**
- Removed scenario-specific patches. Restored generic planning and orchestration logic.

---

## 2 — APPEND Blocks, Bayesian Fix Budgets, Acceptance-Loop Skip

**Commit:** `724eebc feat(agent): APPEND blocks, Bayesian fix budgets, acceptance-loop skip`

### Problem

The TQL output ceiling of 8192 tokens caused HTML files over ~200 lines to be silently truncated (the closing ``` of a FILE: block was never written). Tasks 2+ of a chunked plan overwrote task 1's output using FILE: instead of appending. Fix-loop burned all 5 rounds on static HTML with no server.

### Changes

**`agent/agents/developer_agent.py`**
- `_APPEND_BLOCK_RE`, `_extract_file_appends()`, `_execute_append()`: full APPEND: block support — reads existing file, concatenates new content, writes result.
- System prompt updated with APPEND: format example and parallax direction rule.

**`agent/agents/planner_agent.py`**
- CREATION strategy: complex output (>150 lines) generates a 3-task chunked plan. Tasks 2+ descriptions start with `⚠️ APPEND ONLY — do NOT use FILE:`.
- Removed "verify" from closing task descriptions (was triggering tester agent).

**`agent/orchestration/criterion_score_store.py`** *(new file)*
- Bayesian confidence scoring: `(successes + 1) / (attempts + 2)` (Laplace smoothing).
- Persists to `data/criterion_scores.json`.
- Normalises criteria to 4 pattern types: `file_exists`, `file_contains`, `command_exits_0`, `behavioral`.
- `attempt_budget()` returns 1–4 based on historical fix rate per pattern type.

**`agent/orchestration/__init__.py`**
- Exports `CriterionScoreStore`.

**`agent/orchestrator.py`**
- Hardcoded `< 3` fix budget replaced with `criterion_score_store.attempt_budget(criterion)`.
- Outcome recorded at all terminal states (success / budget-exhausted / abandoned).
- Acceptance loop breaks with "skipped" message instead of burning fix budget when `acc_results` is empty.

---

## 3 — Dynamic Free Evaluator Model via OpenRouter

**Commit:** `d850796 feat(evaluator): dynamic free-model selection via OpenRouter /v1/models`

### Problem

The Qwen model that wrote the code was also judging whether it passed acceptance criteria — same weights, same blind spots. Hardcoding specific free model names in `models.yaml` required manual maintenance as OpenRouter's free tier changed.

### Changes

**`llm/model_router.py`**
- `get_evaluator_model()`: fetches `GET https://openrouter.ai/api/v1/models`, filters for `pricing.prompt == "0"` and `pricing.completion == "0"` with `context_length >= 8192`, scores candidates by context window + family preference (gemma/qwen/llama/mistral/phi/deepseek/magistral), builds a `ModelConfig` on-the-fly, caches for 1 hour.
- Falls back to static `openrouter/free` config entry on any fetch failure.
- `_score_free_model()`: static scoring helper.
- `_evaluator_cache` + `_EVALUATOR_CACHE_TTL` fields on `ModelRouter.__init__`.

**`agent/orchestration/verifier_coordinator.py`**
- `_llm_eval_criterion()`: calls `await self.model_router.get_evaluator_model()` instead of `get_model("coding")`.

---

## 4 — /goal-Style Criterion Structure

**Commit:** `b8e3916 feat(criteria): /goal-style criterion structure in requirements_extractor`

### Problem

The requirements extractor generated vague behavioral descriptions like `"the canvas shows a moving car"` that the evaluator model could pass on a static grey box. No stated check or constraint meant the evaluator had to guess.

### Changes

**`agent/orchestration/requirements_extractor.py`**
- System prompt rewritten to enforce three-part structure per criterion: (1) one measurable end state, (2) a stated check, (3) a constraint if relevant.
- New `visual:` prefix format for UI checks — forces precise enough description for yes/no from a screenshot.
- Rules against vague criteria and file-exists-by-convention.
- Concrete examples included so the model produces falsifiable, evaluator-ready strings.

---

## 5 — Search Library Migration + Brave Error Logging

**Commit:** `5eb934c fix(search): drop duckduckgo_search legacy import, surface Brave failure reason`

### Problem

`duckduckgo_search` package was renamed to `ddgs` (installed: 9.14.4). Legacy fallback import triggered `RuntimeWarning` on every search. Brave failure log `brave_search_failed_fallback_ddg` carried no reason, making root cause (401/429/5xx) undiagnosable.

### Changes

**`agent/tools/web_tool.py`**
- `_search_duckduckgo()`: removed `duckduckgo_search` fallback import. Only `from ddgs import DDGS`.
- `search()`: Brave failure log now includes `reason=err` (the error string from `_search_brave()`).

---

## 6 — JSON Parse Fixes + Trailing Verify Task Ban

**Commit:** `8150195 fix(eval,planner): raw_decode for JSON parsing + ban trailing verify tasks`

### Problems

1. deepseek/deepseek-v4-flash:free returned two JSON objects on separate lines. The greedy regex `\{[\s\S]*\}` matched from first `{` to last `}`, combining both, causing `json.loads()` to fail with "Extra data". All behavioral criteria defaulted to `passed=False`.
2. Planner kept generating a 5th task with "verify" or "open in browser" in the description, triggering the tester agent which wrote a 17KB test file for a static HTML deliverable. Pytest then ran against it, scoring the verifier at 1/10.

### Changes

**`agent/orchestration/verifier_coordinator.py`**
- `_llm_eval_criterion()`: replaced greedy regex + `json.loads()` with `json.JSONDecoder().raw_decode()` — stops at first valid JSON object, ignores trailing content.

**`agent/agents/planner_agent.py`**
- `_generate_criteria()`: same `raw_decode` fix.
- `_strategy_hint()`: all three strategies (CREATION/DEBUGGING/MODIFICATION) now end with: `"NEVER add a trailing 'verify', 'test', 'review', or 'open in browser' task — the orchestrator runs verification automatically."`

---

## 7 — Static HTML Screenshot to Discord

**Commit:** `10b6882 feat(screenshot): capture and post static HTML deliverables to Discord`

### Problem

For static HTML deliverables (canvas animations, games), `AppProbe.launch()` returned `None` (no server entry point). No screenshot was taken and `_last_acceptance_screenshot` in the orchestrator was always `None`. Discord never received a preview image for any `!dev` task.

Two bugs compounding:
- Static path: no screenshot attempted.
- Server path: screenshot was taken inside `run_acceptance_tests()` but the path was local to that function and never surfaced back to the orchestrator.

### Changes

**`agent/orchestration/app_probe.py`**
- `screenshot_file(html_path)`: takes a Playwright screenshot of any HTML file via `file://` URL. Uses `--wait-for-timeout 2000` so JS animations render a first frame before capture. Saves to `.screenshots/static_<timestamp>.png`.

**`agent/orchestration/verifier_coordinator.py`**
- `self.last_screenshot_path`: new attribute set in both `run_acceptance_tests()` code paths.
- Static HTML path: finds largest `*.html` in workspace, calls `screenshot_file()`, stores result.
- Server path: stores result from existing `screenshot()` call.

**`agent/orchestrator.py`**
- Both acceptance loop paths (job_id and no-persistence) read `self.verifier_coordinator.last_screenshot_path` after each `run_acceptance_tests()` call and write it into `_last_acceptance_screenshot` (fix-loop context) and `screenshot_path` (final job result → Discord attachment).

---

## 8 — Discord Attachment Reading + Episodic Memory Contamination

**Commit:** `fe3c357 fix(discord,context): read file attachments + strip code from episodic memory`

### Problems

1. Discord `!dev`/`!ask` received only `task: str` (message text). File attachments (e.g. `message.txt` with 6KB of payment tracker requirements) were silently dropped. The planner saw only `"Save this prompt..."` with no idea what "this prompt" was, and hallucinated using recent rolling-car context.

2. Episodic memory stored full task result summaries including `FILE: car-simulation.html\n\`\`\`html...` blocks. When injected into a payment-tracker planning call, the model followed the embedded car code rather than the new objective, writing a car game plan into the payment tracker workspace.

### Changes

**`api/discord_bot.py`**
- `_submit_task()`: reads all text file attachments (txt/md/json/yaml/yml/toml/py/js/ts/html/css/rs, <200KB) and appends them to the task string as `--- Attachment: filename ---\n<content>` before submitting to the agent.

**`agent/orchestration/context_builder.py`**
- `_clean_episodic_summary()`: strips `FILE:`/`APPEND:` blocks and fenced code from episodic result summaries using regex before the summary is injected into task context. Only plain-text descriptions survive.
- `_EPISODIC_CODE_RE`: compiled pattern matching both block types.
- Applied in the episodic memory assembly loop.

---

## Modified Files Summary

| File | Change |
|---|---|
| `agent/agents/developer_agent.py` | APPEND: blocks; parallax direction rule |
| `agent/agents/planner_agent.py` | 3-task chunked CREATION; no trailing verify tasks; raw_decode |
| `agent/agents/verifier_agent.py` | Score floor for greenfield tasks |
| `agent/orchestration/verifier_coordinator.py` | Glob expansion; HTML truncation recovery; last_screenshot_path; raw_decode; get_evaluator_model() |
| `agent/orchestration/requirements_extractor.py` | /goal-style criterion structure with visual: format |
| `agent/orchestration/criterion_score_store.py` | New — Bayesian fix budgets |
| `agent/orchestration/app_probe.py` | screenshot_file() for file:// URLs |
| `agent/orchestration/__init__.py` | Export CriterionScoreStore |
| `agent/orchestrator.py` | Dynamic budgets; outcome recording; screenshot propagation |
| `agent/tools/web_tool.py` | Drop duckduckgo_search; surface Brave error |
| `llm/model_router.py` | get_evaluator_model() with 1h cache |
| `api/discord_bot.py` | Read file attachments; |
| `agent/orchestration/context_builder.py` | _clean_episodic_summary() strips code from episodic memory |
