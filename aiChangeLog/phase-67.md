# Phase 67 — Real provider token usage for cost tracking (Phase 1 of real-token-usage-plan)

## Root cause

`CostTracker.track_usage()` re-derived every call's token counts from raw
prompt/response text via `estimate_tokens()` (tiktoken `cl100k_base` if
installed, else `len(text)//4`) even when the provider's response already
carried real `usage.prompt_tokens`/`completion_tokens`/`total_tokens`. `cl100k_base`
is OpenAI's tokenizer, not Qwen's — wrong vocabulary for every
`provider: turboquant`/`ollama` call, which is most of this deployment's
traffic. Full design doc: `docs/plans/real-token-usage-plan.md`.

## The fix

- **`llm/usage.py`** (new) — `UsageInfo` dataclass
  (`prompt_tokens`/`completion_tokens`/`total_tokens`).
- **`llm/cost_tracker.py`** — `track_usage()` gained `usage: Optional[UsageInfo] = None`;
  uses it verbatim when present, falls back to `estimate_tokens()` otherwise
  (zero behavior change for callers that omit it). Logs
  `usage_estimated_fallback` when a `turboquant`/`openrouter` call still had
  no real usage — should be rare post-fix; a persistent occurrence means
  something upstream broke.
- **`llm/cloud_api_client.py`** — `_openai_generate()` (+ its `generate()`
  dispatcher) gained an opt-in `usage_out: Optional[dict]` output-parameter,
  populated from the response's `usage` field when present. Anthropic/
  OpenRouter accept the param but don't populate it yet (their usage schemas
  differ — `usage.input_tokens`/`output_tokens` for Anthropic, no
  `total_tokens` — real extraction is Phase 3).
- **`llm/ollama_client.py`** — same `usage_out` pattern on
  `generate()`/`_do_generate()`. **This is the load-bearing path**:
  `provider: turboquant` models have `type: local`, so they're routed through
  `OllamaClient`, not `CloudAPIClient` — the plan document's Phase 1 section
  initially missed this and would have shipped without fixing the traffic it
  was motivated by (corrected in `docs/plans/real-token-usage-plan.md` §3a).
- **`llm/model_router.py`** — both branches of `generate()` build a
  caller-owned `usage_out: dict = {}` per call (not a shared attribute —
  sub-agents run concurrent `generate()` calls, and a shared mutable
  `last_usage` would race), pass it to whichever client is used, and feed the
  result to `cost_tracker.track_usage()`.

Chose the output-parameter pattern for non-streaming `generate()` too, rather
than the plan's originally-proposed `tuple[str, Optional[UsageInfo]]` return —
existing tests (`test_model_router_timeout.py`, `test_model_fallback.py`) mock
`router.ollama.generate = AsyncMock(return_value="ok")`, which a tuple return
would have broken. Output-param needed zero test-mock changes and keeps
non-streaming and streaming consistent with each other (see plan §3a for the
full rationale). `generate_stream()` deliberately left untouched — it doesn't
call `track_usage` today (streaming has never fed cost tracking) and has
exactly one caller (`agent/orchestrator.py:518`); Phase 2 adds `usage_out`
there when `context_builder.py` actually needs it.

## Verification

- `tests/unit/test_usage_extraction.py` (new) — `_openai_generate`/`_do_generate`
  populate `usage_out` when the response has a `usage` field, leave it empty
  when absent, and are unaffected when the caller omits `usage_out` entirely.
- `tests/unit/test_llm.py` — `track_usage()` prefers real `UsageInfo` over the
  text estimate; regression guard confirms the no-`usage` path is unchanged.
- `pytest tests/unit tests/integration -q` — 653 passed, no regressions in
  the pre-existing model-router/fallback test suites.

## Not done (see plan doc)

- Phase 2 — anchor `context_builder.estimate_tokens()`'s handover-budget
  check to real per-session usage instead of pure `chars/4`. Needs
  `session_memory`'s exact module/class located first (flagged as an open
  risk in the plan, not yet done).
- Phase 3 — Anthropic/OpenRouter real usage extraction, streaming usage
  parity.

## Files

- `llm/usage.py` (new)
- `llm/cost_tracker.py`, `llm/cloud_api_client.py`, `llm/ollama_client.py`,
  `llm/model_router.py`
- `tests/unit/test_usage_extraction.py` (new), `tests/unit/test_llm.py`
- `docs/plans/real-token-usage-plan.md` (new)
