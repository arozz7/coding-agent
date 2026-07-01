# Phase 42 — fix(model_router): account-level circuit breaker for OpenRouter free tier

## Root cause

`logs/api-20260630-212838.log` showed every evaluator call (7/7) hitting
`openrouter_rate_limited` on the first attempt:

```
qwen/qwen3-coder:free          -> 429 (retry_after 7s)  -> fallback to local
google/gemma-4-26b-a4b-it:free -> 429 (retry_after 0s)  -> fallback to local
qwen/qwen3-coder:free          -> 429 (retry_after 22s) -> fallback to local
...
```

`get_evaluator_model()` blacklists only the specific model ID that got
rate-limited, then re-selects a *different* free model next call. But
OpenRouter's free tier shares one rate-limit bucket account-wide across all
`:free` models, so the "different" model hits the same wall immediately.
Every evaluator call was paying a wasted HTTP round-trip (models list fetch
+ a rate-limited generate call) before falling back to the same local model
it would have used anyway — and each cache invalidation (`_evaluator_cache =
None` on every 429) forced a fresh remote attempt on the very next call.

## Fix

`llm/model_router.py`:
- Added `_openrouter_rate_limit_hits` (rolling window of hit timestamps),
  `_openrouter_cooldown_until` (monotonic breaker deadline), and the
  `_record_openrouter_rate_limit()` / `_openrouter_cooldown_active()` helpers.
- `_record_openrouter_rate_limit()` is called from the shared
  `_OpenRouterRateLimitError` handler (so it accounts for 429s from any
  OpenRouter call, not just the evaluator path). After 2 hits within a 10 min
  window, it trips a 30 min cooldown.
- `get_evaluator_model()` now checks `_openrouter_cooldown_active()` before
  fetching `/v1/models` or attempting a remote pick, returning the local
  fallback immediately while the breaker is tripped. This also stabilizes
  which model acts as evaluator during a run (no more per-call judge
  flip-flopping between whatever fallback slot got hit).

`tests/integration/test_model_fallback.py`:
- `_make_router_with_configs()` was missing the evaluator-cache/blacklist
  attributes added in earlier phases, which meant `TestOpenRouterRateLimitFallback`
  (4 tests) has been silently broken (AttributeError) since that phase shipped.
  Completed the fixture with those fields plus the new breaker fields — this
  fixes those 4 pre-existing failures as a side effect.
- Added `TestOpenRouterAccountBreaker`: verifies the breaker trips after the
  threshold and that `get_evaluator_model()` skips the remote fetch entirely
  during cooldown.

## Verification

- `pytest tests/integration/test_model_fallback.py -k "not KeywordClassifier"`
  — 12/12 pass (previously 6 passed / 4 failed with AttributeError).
- `pytest tests/unit tests/integration` — diffed FAILED lines against the
  pre-change baseline via `git stash`: identical except the 4 tests fixed
  above. No new failures introduced.

## Follow-up not addressed here

The `visual: a native desktop application window is open...` criterion in
the same log still fails every round (`screenshot: false` always) because
`AppProbe` launches via `npm start` with `port: null` and has no path to
screenshot a native Tauri/Electron window. That's a separate gap (tracked
for a follow-up), not fixed by this change.
