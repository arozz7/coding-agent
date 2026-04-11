# Phase 06 — Batch 4: MCP Git Registration + Circuit Breaker Consolidation + Async HTTP

**Date:** 2026-04-10
**Plan version:** v2.3
**Covers:** Batch 4 — three infrastructure fixes

---

## Summary

Three infrastructure issues fixed in this batch:

1. **MCP git tools never activated** — `create_mcp_server()` accepted `repo_path` but orchestrator never passed it, so all git tools were silently skipped on every invocation.
2. **Duplicate circuit breaker** — `health.py` contained its own inline circuit breaker (with a bug) alongside the correct implementation in `circuit_breaker.py`.
3. **Blocking HTTP client** — `OllamaModelManager` kept a persistent `httpx.Client` on `self._client`, blocking the event loop on every Ollama API call.

---

## Fix 1 — MCP Git Auto-Detection (`mcp/server.py`)

`create_mcp_server()` now auto-detects the git repo root when `repo_path` is not explicitly supplied:

```python
if repo_path is None:
    from pathlib import Path as _Path
    if (_Path(workspace_path) / ".git").exists():
        repo_path = workspace_path
        logger.info("git_repo_detected", path=workspace_path)
```

Git tools (`git_status`, `git_diff`, `git_diff_staged`, `git_commit`, `git_log`, `git_branch`, `git_add`) are now registered in every workspace that contains a `.git` directory — no caller changes needed.

---

## Fix 2 — Circuit Breaker Consolidation

### Bug in `health.py` (removed)

`HealthChecker.check_circuit_breaker()` used `failure_counts[model] >= 5` to decide whether to transition OPEN → HALF_OPEN. Since a model must have ≥ 5 failures to become OPEN, this condition was always `True`, so the method always transitioned to HALF_OPEN and never actually blocked requests. The circuit breaker was effectively a no-op.

### Changes

**`llm/circuit_breaker.py`**
- Added `CircuitBreakerOpenError = CircuitBreakerError` alias for backward compatibility.

**`llm/health.py`** — rewritten
- Removed duplicate `CircuitState` enum.
- Removed duplicate `CircuitBreakerOpenError` class (now re-exported as alias from `circuit_breaker`).
- Removed inline `circuit_states: Dict[str, CircuitState]` and `failure_counts: Dict[str, int]` dicts.
- `HealthChecker` now owns a `CircuitBreakerManager` instance (`self._cb_manager`).
- `_record_success` / `_record_failure` delegate to `cb._on_success()` / `cb._on_failure()`.
- `check_circuit_breaker()` now uses `cb.state` — the `state` property auto-transitions OPEN → HALF_OPEN via time-based recovery (`_should_attempt_reset()`), fixing the bug.
- `get_healthy_models()` checks `cb._failure_count < 3` and `cb.state != CircuitState.OPEN` via manager.
- `record_failure()` no longer duplicates the HALF_OPEN → OPEN transition (handled by `_on_failure()`).

**`llm/model_router.py`**
- Import line changed from `from .health import HealthChecker, CircuitBreakerOpenError` to two separate imports: `from .health import HealthChecker` and `from .circuit_breaker import CircuitBreakerOpenError`.

---

## Fix 3 — Async OllamaModelManager (`llm/model_resilience.py`)

`OllamaModelManager` previously stored a persistent `httpx.Client(timeout=10.0)` as `self._client`, making every Ollama API call blocking. This blocked the asyncio event loop during health checks and model status queries.

### Changes

**`OllamaModelManager`**
- Removed `self._client = httpx.Client(timeout=10.0)`.
- `_make_request()` converted to `async def` using a per-request `async with httpx.AsyncClient(timeout=10.0) as client:`.
- All methods made `async`: `list_models`, `get_model_status`, `load_model`, `check_ollama_running`, `get_server_status`.

**`ModelResilienceManager`**
- All methods that call `OllamaModelManager` made `async`: `check_model_health`, `is_model_available`, `find_available_model`, `find_working_fallback`, `get_diagnostics`.

**`api/main.py`**
- Line 541: `diagnostics = resilience.get_diagnostics()` → `diagnostics = await resilience.get_diagnostics()`.

---

## Known Remaining Issues (Batch 5+)

- Subagent spawning does not pass `tool_executor` or `enriched_context` into isolated context
- Token estimation uses `len(text)//4` — switch to `tiktoken`
- `memory_wiki.py` NetworkX graph not implemented
- Prometheus `/metrics` endpoint not implemented
- Wiki `lint()` only checks orphans — contradiction detection not yet implemented
