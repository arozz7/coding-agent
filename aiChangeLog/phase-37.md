# Phase 37 — OpenRouter 402 Blacklist & Rotation Fix

## Goal
Prevent silent evaluation failures when a free-tier OpenRouter model returns `402 Payment Required` (credits exhausted). Previously the router retried the dead model up to 3 times, tripped the circuit breaker, then kept the exhausted model cached for up to one hour — every LLM-evaluated acceptance criterion silently failed for the rest of the session.

## Root Cause
`cloud_api_client.py` treated HTTP 402 identically to other API errors. No specific handling existed for credits-exhausted responses. `model_router.py` cached the chosen evaluator model with no mechanism to evict it on payment failure, and `get_evaluator_model()` had no blacklist — the same dead model was re-selected on every cache bust.

## Files Modified

### `llm/cloud_api_client.py`
- Added `_OpenRouterPaymentRequiredError` — a distinct exception raised on HTTP 402, separate from `_OpenRouterRateLimitError` (429). This gives callers a clean signal to rotate rather than retry.

### `llm/model_router.py` (two commits)

**Commit 859fd8d — Blacklist & immediate rotation:**
- `generate()`: catches `_OpenRouterPaymentRequiredError`, adds the exhausted model to `_evaluator_blacklist`, busts `_evaluator_cache` immediately, then falls back to the next available model with no retry — zero wasted requests.
- `get_evaluator_model()`: filters `_evaluator_blacklist` before scoring free models so the exhausted model is never re-selected after a cache bust.

**Commit 3b2096c — 24 h TTL on blacklist entries:**
- `_evaluator_blacklist` changed from `set[str]` to `dict[str, float]` (model → monotonic timestamp of blacklisting).
- `_EVALUATOR_BLACKLIST_TTL = 86400.0` (24 hours) — matches OpenRouter's daily credit reset cadence.
- `get_evaluator_model()`: expires stale blacklist entries before filtering, so a previously exhausted model is automatically re-eligible the next day without a process restart.

## Verification
- Before fix: `deepseek/deepseek-v4-flash:free` returned 402 → circuit breaker opened → all LLM-evaluated criteria returned `passed: false` silently for the remainder of the session.
- After fix: 402 triggers `openrouter_payment_required` log event → model blacklisted → cache busted → next free model selected → evaluation continues without interruption.
- Blacklist is self-healing: entries expire after 24 h; the model re-enters the free pool automatically.
