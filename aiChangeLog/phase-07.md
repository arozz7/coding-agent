# Phase 07 — Batch 5: Subagent Context + /metrics + tiktoken + MemoryWiki Population

**Date:** 2026-04-10
**Plan version:** v2.3
**Covers:** Batch 5 — four infrastructure fixes from Known Remaining Issues

---

## Summary

Four issues fixed in this batch:

1. **Subagent isolation was incomplete** — `spawn_subagent()` built an `isolated_context` missing `tool_executor` and `enriched_context`, so subagents had no tool access and no RAG/wiki enrichment.
2. **Prometheus `/metrics` endpoint not wired** — metrics were defined and collected but no route exposed them to a scraper.
3. **Token estimation used `len(text)//4`** — rough and model-agnostic. Now uses `tiktoken` with `cl100k_base` encoding when available, falling back to the old heuristic.
4. **MemoryWiki graph never populated** — NetworkX graph was fully implemented but `index_workspace()` never called the static analyzer to fill it.

---

## Fix 1 — Subagent Isolated Context (`agent/orchestrator.py`)

`spawn_subagent()` was missing two keys in `isolated_context`:

- `tool_executor` — without this, subagents had no way to call tools (file reads, shell, pytest, etc.)
- `enriched_context` — without this, subagents ran with no RAG context or skill instructions

### Changes

```python
# Before building isolated_context:
self.session_memory.get_or_create_session(subagent_id, self.workspace_path)
enriched_context = await self._build_enriched_context(task)

isolated_context = {
    "session_id": subagent_id,
    "parent_session_id": parent_session_id,
    "workspace_path": self.workspace_path,
    "model_router": self.model_router,
    "tool_executor": self._create_session_executor(subagent_id),  # NEW
    "enriched_context": enriched_context,                          # NEW
    "context_limits": context_limits or {},
    "is_subagent": True,
}
```

`get_or_create_session()` is called first because `EventEmittingExecutor` writes events to the session table immediately on creation, so the session row must exist before the executor is instantiated.

---

## Fix 2 — Prometheus `/metrics` Endpoint (`api/main.py`)

`observability/routes.py` had a standalone FastAPI sub-app for `/metrics`, but it was never mounted on the main app. Rather than mount a sub-app, the route is added directly:

```python
from fastapi import ..., Response
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

@app.get("/metrics", include_in_schema=False)
async def metrics():
    """Prometheus scrape endpoint."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
```

`include_in_schema=False` hides the route from the OpenAPI docs since it is a scrape endpoint, not a user-facing API.

---

## Fix 3 — tiktoken Token Estimation (`llm/cost_tracker.py`, `pyproject.toml`)

### `llm/cost_tracker.py`

Optional import at module level with graceful fallback:

```python
try:
    import tiktoken as _tiktoken
    _ENCODING = _tiktoken.get_encoding("cl100k_base")
except Exception:
    _ENCODING = None
```

`estimate_tokens()` updated:

```python
def estimate_tokens(self, text: str) -> int:
    if _ENCODING is not None:
        return len(_ENCODING.encode(text))
    return len(text) // 4
```

`cl100k_base` is used for all models (Qwen included) because it is the closest publicly available tokenizer and gives accurate-enough estimates for cost tracking.

The `len // 4` fallback in `observability/logging.py` (lines 60-61) was intentionally left unchanged — that site receives pre-counted integer lengths, not text, so tiktoken cannot be applied there.

### `pyproject.toml`

Added to both `[project]` dependencies and `[tool.poetry.dependencies]`:
- `tiktoken>=0.7.0` / `tiktoken = "^0.7"`

---

## Fix 4 — MemoryWiki Population (`agent/orchestrator.py`)

### Import

```python
from agent.memory.memory_wiki import MemoryWiki
```

### Initialization

```python
self.memory_wiki = MemoryWiki(project_id=Path(workspace_path).name)
```

### `index_workspace()` rewrite

After the RAG index pass, `index_workspace()` now:

1. Clears the existing wiki graph (`self.memory_wiki.clear()`)
2. Finds all `*.py` files under the workspace via `Path.rglob`
3. Calls `self.code_analyzer.analyze_file(path)` for each file
4. Populates the graph:
   - `add_file(rel_path, file_type="source", language="python")`
   - `add_function(...)` for each function in the analysis result
   - `add_class(...)` for each class (with method names)
   - `add_import(...)` for each import where `module` is non-empty
5. Logs final wiki statistics and returns them merged into the result dict

Errors from individual files are caught and counted (`wiki_errors`) so a single bad file doesn't abort the entire index.

---

## Known Remaining Issues (Batch 6+)

- `memory_wiki.py` `lint()` only checks orphans — contradiction detection not yet implemented
- Prometheus `/metrics` uses the default process-level registry; per-request counters defined in `observability/metrics.py` are used only if callers increment them (no auto-instrumentation middleware)
- Token estimation: `observability/logging.py` still uses `len // 4` (receives int, not text — cannot apply tiktoken without upstream changes)
- Subagent results not merged back into parent RAG index after completion
