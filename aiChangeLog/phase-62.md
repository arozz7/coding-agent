# Phase 62 — Fix reasoning-truncation stall in the developer role

## Root cause

`logs/api-20260818-192257.log` showed the developer role failing every
single task from 00:49 to 02:19 (task_num 7–11) with the identical error:

```
Model 'Qwen3.8-27B-Q4_K_S' returned empty content (reasoning-only response).
```

`llm/ollama_client.py` hardcoded `max_tokens: 8192` on every request. With
thinking enabled (the model's default — `enable_thinking` was unset for this
entry in `config/models.yaml`), a non-trivial coding prompt's `<think>` trace
alone could exceed 8192 tokens, so the response was cut off before the model
ever reached its answer: `reasoning_content` populated, `content` empty.
The client treated this as a hard failure, and `task_loop` retried the whole
task from scratch each time — 6 outer verify rounds over ~1h50m
(`criteria_evaluated: 5/5` every round, `verify_code_complete` gaps stuck at
4–5, score oscillating 3↔4, never converging) with the process still polling
`/task/job_72c9d482fff6` at the point the log ends, unfinished.

Cross-checked against `TurboQuantLoader/logs/conversations.2026-08-19.jsonl`
(the raw model-server traffic): 18 requests in the same window show
`finish_reason: "length"`, `completion_tokens: 8192` exactly, and a
zero-length response — confirming the truncation happened at the model
layer, not just in how the client interpreted it.

The fix disables thinking (`enable_thinking: false`) as a workaround, but
that throws away the model's reasoning quality to route around a token-budget
bug rather than fixing the budget. Went with raising the budget instead.

## The fix

1. `llm/config.py` — added `max_tokens: int = 8192` to `ModelConfig`,
   following the existing per-model `enable_thinking` override pattern.
2. `llm/ollama_client.py` — `generate`/`_do_generate` accept `max_tokens`
   and use it in the request payload instead of the hardcoded `8192`. The
   truncation error now logs `finish_reason` and `reasoning_len` and points
   at raising `max_tokens` instead of disabling thinking.
3. `llm/model_router.py` — resolves `max_tokens` the same way as
   `enable_thinking` (call-site override → per-model config → default) and
   threads it through `generate()` and every fallback-chain call site.
4. `config/models.yaml` — `Qwen3.8-27B-Q4_K_S` now sets `max_tokens: 24576`
   (was the old hardcoded 8192). Thinking stays enabled.

## Follow-up: developer-role timeout

Re-running the same workload after the fix (`logs/api-20260818-223831.log`)
confirmed zero `empty_content_with_reasoning` failures and the run actually
completed (`task_loop_complete`, 14 tasks, criteria converged 5/5 → 6/6).
But it surfaced 5 `ollama_hard_timeout` events (task_num 4, 5, 10, 11, 12) —
with more token budget, some generations now legitimately run past the
`model_router` default 600s timeout instead of failing fast with empty
content, burning ~50 minutes total in retries.

5. `agent/agents/developer_agent.py` — added `_DEVELOPER_TIMEOUT_SECS = 1500.0`
   and passed it to all 3 `model_router.generate` calls in `DeveloperRole`
   (initial implementation, write-phase, force-run).
6. `agent/agents/fix_loop.py` — same constant (kept local to avoid a
   circular import back into `developer_agent.py`), passed to the fix-loop's
   generate call.

Only the developer role's timeout changed — planning, chat, research, etc.
keep the 600s default since they don't carry the larger `max_tokens` budget.

## Verification

- `pytest tests/unit -q` — 487 passed.
- `pytest tests/unit/test_developer_agent_fix_loop.py tests/unit/test_agents.py tests/unit/test_llm.py tests/integration/test_model_fallback.py -q` — 57 passed.
- Live confirmation in `TurboQuantLoader/logs/conversations.2026-08-19.jsonl`
  post-fix: a request at 23:34:51 ran to 19,706 completion tokens — well
  past the old 8192 cap — and finished with `finish_reason: "stop"` and real
  content (`resp_len: 2273`). Under the old config this exact call would
  have failed with the reasoning-only-truncation bug.
- `logs/api-20260819-184755.log` (live run, post `max_tokens` fix but before
  the timeout fix landed) shows 2 more `ollama_hard_timeout` events and zero
  `empty_content_with_reasoning` — consistent with the timeout being the
  next bottleneck, motivating the follow-up fix above.

## Files

- `llm/config.py` — `max_tokens` field on `ModelConfig`
- `llm/ollama_client.py` — configurable `max_tokens`, better truncation diagnostics
- `llm/model_router.py` — `max_tokens` threaded through `generate()` and fallback chain
- `config/models.yaml` — `Qwen3.8-27B-Q4_K_S` max_tokens raised to 24576
- `agent/agents/developer_agent.py` — developer-role timeout raised to 1500s
- `agent/agents/fix_loop.py` — same timeout applied to the fix-loop generate call
