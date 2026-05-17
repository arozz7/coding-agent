# Phase 30 — TurboQuantLoader Integration + Documenter APPEND Blocks

**Date:** 2026-05-17
**Commit:** `303ccb3`

## Objective

Three independent fixes shipped together:

1. Support TurboQuantLoader (a local llama-server proxy) as a first-class inference backend, including its new auto-switch model-swapping protocol.
2. Replace full-file regeneration in documenter fix-rounds with incremental APPEND: blocks to stay within the 600s hard timeout.
3. Fix an `AttributeError` that caused every `/task/start` API call to return HTTP 500.

---

## 1 — TurboQuantLoader (`turboquant` provider)

### Problem

TurboQuantLoader (TQL) is a Rust proxy that wraps `llama-server` and exposes an OpenAI-compatible API on port 7432. When added to `models.yaml` as a local provider, three things broke:

- **ConnectError wrong path** — if TQL was unreachable, `httpx.ConnectError` fell into the generic `except Exception` handler which retried with 2^n backoff rather than attempting a model load.
- **Pre-flight killed in-progress loads** — our pre-flight called `GET /health`, and if TQL was running but the model was still loading (non-200), it sent `POST /v1/admin/load` — which kills the in-progress load and restarts from scratch (visible as VRAM spike → drop → spike).
- **Blind 120s wait** — `ModelNotReadyError` for non-lmstudio providers waited 120 seconds unconditionally, ignoring TQL's `Retry-After: 10` hint.

### TurboQuantLoader auto-switch (new in TQL)

TQL now maintains a `[[models]]` registry in its `config.toml` (name → GGUF path). Any `POST /v1/chat/completions` or `POST /v1/messages` whose `model` field differs from the currently-loaded model triggers an automatic background hot-swap. The triggering request (and all concurrent ones) receives `HTTP 503 + Retry-After: 10`. Once ready, retries succeed normally.

Name resolution order: exact registry match → substring match → `models_dir` file-stem scan → fall through to current model.

### Changes

**`llm/ollama_client.py`**

- `ModelNotReadyError` gains `retry_after: Optional[int] = None` — carries the server's `Retry-After` header value.
- All `HTTP 503` responses now unconditionally raise `ModelNotReadyError` (with `retry_after` extracted from the header). Previously, only 503s whose body matched `_MODEL_NOT_READY_HINTS` were treated this way. `404`/`400` still require a hint match.

**`llm/model_router.py`**

- Replaced `_check_turboquant_health()` + `_try_load_turboquant_model()` (which triggered `/v1/admin/load`) with a single lightweight `_turboquant_is_reachable()` — returns `True` for any HTTP response (even non-200), `False` only on `ConnectError`. TQL owns model loading; the agent never calls `/v1/admin/load`.
- Pre-flight at `attempt == 0`: if TQL is unreachable, logs `turboquant_unreachable` and lets inference proceed (it will fail with a clear error). If TQL responds but is not 200 (model warming up), does nothing — avoids interrupting an in-progress load.
- `ModelNotReadyError` handler for turboquant: waits `e.retry_after or 10` seconds then retries. No manual load trigger.

**`llm/config.py`**

- `model_path` field removed from `ModelConfig`. TQL's `[[models]]` registry owns name→path mapping; the agent only passes model names.

**`config/models.yaml`**

- Both turboquant entries cleaned up: `model_path` comments removed, provider comment updated to reflect auto-switch behavior.
- `Qwen3.6-27B-Q4_K_S` added as a distinct entry (was missing from the earlier session).
- `defaults.coding_model` and `defaults.planning_model` set to `Qwen3.6-27B-Q4_K_S` (the Q4 quant is the reliable default; Q6_K is available for explicit selection).

### Interaction model (steady state)

```
Agent sends POST /v1/chat/completions  {model: "Qwen3.6-27B-Q4_K_S", ...}
  ↓
TQL: model already loaded → 200 OK + response        (happy path)
TQL: different model loaded → 503 + Retry-After: 10  (switching)
  ↓
model_router: sleep(10), retry
  ↓ (after switch completes)
TQL: 200 OK + response
```

If TQL is completely unreachable (not running), `_turboquant_is_reachable()` logs a warning at attempt 0, then inference fails with `ConnectError` → generic exception → retry with backoff → `LLMError` → fallback chain.

---

## 2 — Documenter APPEND: Blocks

### Problem

Fix-round update-file specs in `verifier_coordinator.py` embedded the full existing file (up to 60 KB) and asked the model to regenerate the entire document. At 3–5 tok/s for a ~8 000-token output, this reliably exceeded the 600s hard timeout.

### Solution

The documenter now supports `APPEND:` blocks alongside `FILE:` blocks. Fix-round specs instruct the model to write **only new sections**, not the full file. The agent reads the existing file and merges.

**`agent/agents/documenter_agent.py`**

- `_extract_append_blocks(response)` — same nested-fence-safe parser (`rfind('\n```')`) as `_extract_file_blocks`, but splits on `APPEND:` boundaries.
- `DocumenterRole.get_system_prompt()` — documents both formats with examples.
- `DocumenterRole.execute()` — after processing `FILE:` blocks, loops over `APPEND:` blocks: reads the existing file, appends new content, writes combined result.

**`agent/orchestration/verifier_coordinator.py`**

- `make_fix_specs()` update-file path: replaced full `existing_content` embed with a headings-only extract (`existing_headings`, max 2 000 chars). Prompt instructs the model to use `APPEND:` blocks and write only the missing sections.
- Output token budget drops from ~8 000 to ~500–1 000 tokens, well within 600s at any local model speed.

**`tests/unit/test_documenter_file_blocks.py`**

- Import updated: `from agent.agents.documenter_agent import _extract_file_blocks, _extract_append_blocks`
- `TestExtractAppendBlocks` added: 6 test cases covering simple blocks, nested fences, empty result, multiple blocks, FILE+APPEND isolation, and content stripping.

### Format reference

```
# New file — full content
FILE: path/to/file.md
```markdown
# Full document
...
```

# Append to existing file — new sections only
APPEND: path/to/existing.md
```markdown
## New Section
content here
```
```

---

## 3 — `_detect_task_type_keyword()` on Orchestrator

### Problem

`api/main.py:400` called `_orchestrator._detect_task_type_keyword(request.task)` synchronously before launching the background job. The method did not exist on `AgentOrchestrator`, causing `AttributeError` → FastAPI returned HTTP 500 on every `POST /task/start` call.

### Fix

**`agent/orchestrator.py`**

Added `_detect_task_type_keyword(task: str) -> str` — delegates to `self.task_router._detect_keyword(task)`, which already implements a zero-latency keyword-only classifier (no LLM call). This satisfies the synchronous pre-classification the API endpoint needs before the full async `run_task()` path runs.

---

## Modified Files Summary

| File | Change |
|------|--------|
| `llm/ollama_client.py` | `ModelNotReadyError.retry_after`; all 503s raise it |
| `llm/model_router.py` | `_turboquant_is_reachable()`; simplified pre-flight; turboquant `ModelNotReadyError` waits `retry_after` |
| `llm/config.py` | Removed `model_path`; updated provider docstring |
| `config/models.yaml` | Both turboquant entries cleaned; `Qwen3.6-27B-Q4_K_S` added; defaults updated |
| `agent/agents/documenter_agent.py` | `_extract_append_blocks()`; APPEND: system prompt; execute() merge loop |
| `agent/orchestration/verifier_coordinator.py` | Fix-round specs use headings-only context + APPEND: instruction |
| `agent/orchestrator.py` | `_detect_task_type_keyword()` added |
| `tests/unit/test_documenter_file_blocks.py` | `TestExtractAppendBlocks` (6 tests) |
