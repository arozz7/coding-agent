# Real Token Usage Plan — Replace Char-Count Estimates with API Ground Truth

**Date:** 2026-08-22
**Status:** Phase 1 implemented 2026-08-22 (see §3a for two corrections made
during implementation). Phase 2/3 not started.
**Scope:** `llm/cloud_api_client.py`, `llm/model_router.py`, `llm/cost_tracker.py`,
`agent/orchestration/context_builder.py`, `agent/orchestration/session_memory.py` (or
equivalent), plus `llm/ollama_client.py` for parity.
**Trigger:** TurboQuantLoader's streaming `/v1/chat/completions` now returns a real
`usage` object (`prompt_tokens`/`completion_tokens`/`total_tokens`) when the request
sets `stream_options.include_usage` — see TurboQuantLoader `aiChangeLog` around
2026-08-22. Investigating whether pi's better context-size display than ours was
caused by this same gap led to finding it's not just a pi problem — two systems in
this repo already estimate tokens from scratch instead of ever using it.

---

## 1. Findings

### 1a. `cost_tracker.py` estimates every call, even when real usage is available

`ModelRouter.generate()` calls `self.cost_tracker.track_usage(config, prompt, result)`
at a single, centralized site (`llm/model_router.py:365`), passing only the raw prompt
and response **strings**. `CostTracker.track_usage()` then re-derives token counts via
its own `estimate_tokens()` (tiktoken `cl100k_base` if installed, else `len(text)//4`):

```python
# llm/cost_tracker.py
def estimate_tokens(self, text: str) -> int:
    if _ENCODING is not None:
        return len(_ENCODING.encode(text))
    return len(text) // 4

def track_usage(self, config, prompt: str, response: str) -> None:
    prompt_tokens = self.estimate_tokens(prompt)
    completion_tokens = self.estimate_tokens(response)
    ...
```

This is doubly wrong for local models: `cl100k_base` is **OpenAI's** tokenizer, not
Qwen's — so even the "good" branch of the estimate uses the wrong vocabulary for every
`provider: turboquant` / `provider: ollama` call, which is most of this deployment's
traffic. Every hosted-API call (OpenRouter, Anthropic) already returns real usage in
its response and we throw it away.

**Fix is cheap**: `track_usage()` is called from exactly one place. If
`ModelRouter.generate()` has real usage in hand after the `self.cloud.generate(...)` /
`self.ollama.generate(...)` call, it can pass it straight through — zero changes needed
at any of the ~30 `model_router.generate()` call sites across `agent/`.

### 1b. `context_builder.py`'s budget check is a pure char-count heuristic

```python
# agent/orchestration/context_builder.py
def estimate_tokens(self, session_id: str, task: str) -> int:
    history = self.build_events_context(session_id)
    char_count = len(history) + len(task)
    return char_count // 4 + 4_500  # overhead: system prompt + enriched context

def check_budget(self, session_id: str, task: str) -> str:
    ...
    estimated = self.estimate_tokens(session_id, task)
    ratio = estimated / config.context_window
    if ratio >= 0.82: return "bridge"
    if ratio >= 0.75: return "warn"
    return "ok"
```

`check_budget()` (single caller: `agent/orchestrator.py:312`) runs **before** the next
LLM call, to decide whether to trigger a handover/context-bridge — so it can never be
fully replaced by real usage (there's no response yet to measure). But right now it has
*zero* ground truth to calibrate against: every session's estimate drifts on pure
`chars/4` math with no correction, which is a known-bad approximation for code-heavy
content specifically (the majority of what this agent sends).

**Fix**: after each primary agent-turn call completes, record the real
`total_tokens` it reported, keyed by `session_id`. `estimate_tokens()` then bases its
estimate on *that* real number plus only the incremental new content since, instead of
re-deriving the entire history from characters every time.

---

## 2. Design constraints

- **`ModelRouter.generate()` / `.stream_generate()` public return types must not
  change.** ~30 call sites across `agent/agents/*.py`, `agent/orchestration/*.py`, and
  `agent/multi_agent/workflow.py` all expect a plain `str` (non-streaming) or
  `AsyncIterator[str]` (streaming). Changing this is a large, risky ripple for no
  reason — Phase 1 below doesn't need it.
- **`CloudAPIClient.generate()` / `.stream_generate()` have exactly one caller each**
  (`ModelRouter`, `llm/model_router.py:353`/`363` and `:637`/`:640`) — free to change
  their return shape without touching anything outside `model_router.py`.
- **Real usage isn't available from every provider path today.** Confirmed available:
  OpenAI-compatible JSON `usage` (TurboQuantLoader non-streaming passthrough; streaming
  now too, gated on `stream_options.include_usage`), and hosted OpenRouter (real API,
  always returns it). Not yet confirmed/likely absent: `_anthropic_generate`/`_stream`
  (different response schema — `usage.input_tokens`/`output_tokens`, not
  `prompt_tokens`/`completion_tokens` — needs its own extraction, not covered by
  Phase 1). Treat "no usage available" as the default case everywhere — never make a
  provider path *require* usage to function.

---

## 3a. Implementation notes (corrections made while implementing Phase 1)

Two corrections against the plan as written above — noted here so this doc and
what actually shipped don't quietly diverge:

1. **`provider: turboquant` traffic goes through `llm/ollama_client.py`, not
   `llm/cloud_api_client.py`.** `config.type == "local"` for turboquant models
   (see `config/models.yaml`), so `ModelRouter.generate()` calls
   `self.ollama.generate()`, never `self.cloud.generate()`. Section 3 above
   claims Phase 1 "covers every `provider: turboquant`... call for free" but
   then defers the `OllamaClient` extraction to Phase 3 (section 5) — those two
   statements contradict each other, and following the step-by-step as written
   would have shipped a Phase 1 that silently misses the traffic it was
   motivated by. Folded the `ollama_client.py._do_generate` usage extraction
   into Phase 1 instead (same `usage_out` pattern as `_openai_generate`);
   Phase 3 no longer needs an ollama_client bullet for the non-streaming path.
2. **Non-streaming `generate()` uses the `usage_out` output-parameter, not a
   `tuple[str, Optional[UsageInfo]]` return.** The plan's section 2 justified a
   return-type change on "single caller, free to change" grounds, but existing
   tests (`tests/unit/test_model_router_timeout.py`,
   `tests/integration/test_model_fallback.py`) mock
   `router.ollama.generate = AsyncMock(return_value="ok")` / same for
   `router.cloud.generate` — a tuple return breaks those call sites' `result`
   usage in `model_router.py` and would have required updating both test
   files. The output-param pattern (mirrors what section 3 already prescribes
   for streaming) needed zero test-mock changes and keeps non-streaming and
   streaming consistent with each other. `UsageInfo` still exists as designed
   (`llm/usage.py`) — it's just built via `UsageInfo(**usage_out)` in
   `model_router.py` instead of unpacked from a tuple.

`generate_stream()` / `OllamaClient.stream_generate()` were deliberately left
untouched in this pass — it doesn't call `track_usage` today (streaming has
never fed cost tracking, so there's no regression), and it has exactly one
caller (`agent/orchestrator.py:518`). Wiring `usage_out` through it now would
be plumbing with no consumer until Phase 2 needs it for
`context_builder.py`'s per-session anchor — add it there instead.

---

## 3. Phase 1 — Real usage into cost tracking (no call-site ripple)

**Goal:** `cost_tracker.py` uses real token counts whenever the backend provided them,
falls back to today's estimate otherwise. Covers every `provider: turboquant` and
`provider: openrouter` call for free, since `track_usage()` is centralized.

1. **`llm/cloud_api_client.py`** — `_openai_generate` currently does:
   ```python
   data = response.json()
   message = data["choices"][0]["message"]
   content = message.get("content", "")
   ...
   return content
   ```
   Change `_openai_generate` (and the `generate()` dispatcher above it) to return
   `tuple[str, Optional[UsageInfo]]` instead of bare `str`, where `UsageInfo` is a
   small new dataclass (`llm/config.py` or a new `llm/usage.py`):
   ```python
   @dataclass
   class UsageInfo:
       prompt_tokens: int
       completion_tokens: int
       total_tokens: int
   ```
   Extract it from `data.get("usage")` when present (mirror the empty-content guard's
   existing `message.get("reasoning_content", "")` pattern already added for the
   TurboQuantLoader reasoning fix). `_anthropic_generate` / `_openrouter_generate`
   return `(content, None)` for now — real extraction for those is Phase 3.

2. **`llm/cloud_api_client.py`** — `_openai_stream` similarly needs to surface usage
   from the final SSE chunk. Since it's a generator (`AsyncIterator[str]`), the
   cleanest option without changing the yield type is an **optional output parameter**:
   ```python
   async def _openai_stream(
       self, prompt, config, system_prompt, usage_out: Optional[dict] = None
   ) -> AsyncIterator[str]:
       ...
       async for line in response.aiter_lines():
           ...
           if usage := data.get("usage"):
               if usage_out is not None:
                   usage_out["prompt_tokens"] = usage.get("prompt_tokens", 0)
                   usage_out["completion_tokens"] = usage.get("completion_tokens", 0)
                   usage_out["total_tokens"] = usage.get("total_tokens", 0)
           if content := data["choices"][0].get("delta", {}).get("content"):
               yield content
   ```
   `usage_out` defaults to `None` — existing callers that don't pass it see zero
   behavior change. `generate()`/`stream_generate()` dispatchers thread it through to
   whichever provider method is selected; non-`openai` paths just ignore it.

   Also send the request payload with `"stream_options": {"include_usage": True}` —
   TurboQuantLoader's proxy now honors it (opt-in, matches the fix on that side); other
   OpenAI-compatible backends either honor it too or safely ignore an unknown field.

3. **`llm/model_router.py`** — at the two call sites:
   ```python
   # non-streaming, :363
   result, usage = await self.cloud.generate(prompt, config, system_prompt=system_prompt)
   ...
   self.cost_tracker.track_usage(config, prompt, result, usage=usage)

   # streaming, :640
   usage_out: dict = {}
   async for chunk in self.cloud.stream_generate(prompt, config, system_prompt, usage_out=usage_out):
       yield chunk
   self.cost_tracker.track_usage(config, prompt, accumulated_result, usage=usage_out or None)
   ```
   `OllamaClient.generate()`/`.stream_generate()` keep returning bare values for now
   (Phase 3 covers parity) — `model_router.py` passes `usage=None` on that branch,
   identical to today's behavior.

4. **`llm/cost_tracker.py`** — `track_usage()` gains an optional `usage: Optional[UsageInfo] = None`
   param; prefers it over `estimate_tokens()` when present:
   ```python
   def track_usage(self, config, prompt: str, response: str, usage: Optional[UsageInfo] = None) -> None:
       if usage is not None:
           prompt_tokens, completion_tokens = usage.prompt_tokens, usage.completion_tokens
       else:
           prompt_tokens = self.estimate_tokens(prompt)
           completion_tokens = self.estimate_tokens(response)
       ...
   ```
   Everything downstream (`CostRecord`, `get_summary()`) is unchanged — same shape,
   just more accurate numbers when available. Consider logging when `usage is None`
   for a `provider: turboquant`/`openrouter` call (should be rare post-Phase-1; a
   persistent `None` there means something upstream broke).

**Tests to add**: `track_usage` with and without `usage=`; `_openai_generate` /
`_openai_stream` usage extraction (mock response with/without a `usage` field —
confirm graceful `None`/empty fallback, matching the existing empty-content-with-
reasoning test pattern already added for the reasoning_content fix).

---

## 4. Phase 2 — Real usage into the context-budget check

**Goal:** `context_builder.estimate_tokens()` stops re-deriving the entire session
history from characters every call; anchors to the last real `total_tokens` instead.

1. **Session-keyed storage.** Whatever already persists per-session state
   (`session_memory` — confirm exact module/class name in `agent/`) gets a small
   addition: `record_token_usage(session_id: str, total_tokens: int) -> None` and
   `get_last_token_usage(session_id: str) -> Optional[int]`. Simple in-memory dict is
   fine if `session_memory` doesn't already persist to disk/DB — check before adding a
   new storage mechanism, prefer extending what's there.

2. **Report real usage after the primary agent-turn call**, not every `generate()`
   call. Scope this to the call sites that represent *the* growing session
   conversation — likely `developer_agent.py`'s main turn call(s) and
   `chat_agent.py` — not the many one-shot utility calls (`criterion_evaluator.py`,
   `objective_resolver.py`, `plan_reviewer_agent.py`, etc.) that run against their own
   short-lived prompts, not the accumulating history `check_budget()` cares about.
   Confirm this list against `agent/orchestration/task_loop.py`'s actual per-turn flow
   before implementing — the grep in section 1 lists every `model_router.generate()`
   call site as a starting point.

   Use the same `usage_out`-style optional param pattern from Phase 1, now threaded
   one layer further — `ModelRouter.generate()` gains `usage_out: Optional[dict] = None`
   (default `None`, so the other ~28 call sites need zero changes), populated the same
   way `cost_tracker.track_usage` gets its data.

3. **`context_builder.estimate_tokens()`** becomes:
   ```python
   def estimate_tokens(self, session_id: str, task: str) -> int:
       last_real = self.session_memory.get_last_token_usage(session_id)
       if last_real is not None:
           # Anchor to ground truth; estimate only what's new since then.
           new_chars = len(task)  # + any events appended since the last real total
           return last_real + new_chars // 4
       # No real usage yet for this session — fall back to today's full estimate.
       history = self.build_events_context(session_id)
       char_count = len(history) + len(task)
       return char_count // 4 + 4_500
   ```
   Exact "what's new since" accounting needs care — `build_events_context` pages the
   last 20 events, so it isn't a strict "since the last call" delta today. Simplest
   correct version for Phase 2: track *whether* any real usage exists for the session
   and, if so, just log the estimate-vs-real drift at `check_budget()` time without
   changing the threshold math yet — gather real drift data for a few sessions before
   deciding how aggressively to trust the anchor. Promote to the full anchor-and-delta
   version once the drift is characterized.

**Tests to add**: `estimate_tokens()` with and without a stored real usage;
`check_budget()` behavior unchanged when no real usage exists (regression guard).

---

## 5. Phase 3 — Parity across remaining providers (stretch)

Once Phase 1/2 are stable for `turboquant`/`openai`-kind endpoints:

- `_anthropic_generate` / `_anthropic_stream`: Anthropic's response schema uses
  `usage.input_tokens` / `usage.output_tokens` (no `total_tokens` field — sum them).
  Streaming reports usage across `message_start` (input) and `message_delta` (output)
  events, not one final object — needs its own extraction path, not a copy of the
  OpenAI one.
- `_openrouter_generate` / `_openrouter_stream`: same `usage` shape as OpenAI-compatible
  (OpenRouter proxies that convention) — should be a near-identical copy of the Phase 1
  OpenAI extraction.
- `ollama_client.py`: `_do_generate` already reads `message.get("reasoning_content")`
  for the empty-content guard but never extracts `usage` for cost tracking. Same
  pattern as Phase 1's `_openai_generate` change. `stream_generate` doesn't either —
  lower priority since precedent in this file already treats streaming usage as
  out of scope (matches the asymmetry TurboQuantLoader's own fix followed).

---

## 6. Risks / things to verify before starting

- **Confirm `session_memory`'s actual module path and class name** — this plan
  references it by the name used in `orchestrator.py`'s constructor call
  (`self.session_memory.get_or_create_session(...)`), but the exact file wasn't
  inspected as part of this plan. Locate it first.
- **Concurrency**: this agent runs sub-agents concurrently
  (`subagent_manager.py`, `multi_agent/workflow.py`). Any session-keyed real-usage
  store must be keyed correctly per-session and not clobbered by concurrent turns in
  *different* sessions sharing a `ModelRouter` instance — the `usage_out`
  output-parameter pattern (caller-owned dict, not a shared mutable attribute on
  `ModelRouter`) avoids this by construction; don't swap it for a shared
  `self.last_usage` attribute on `ModelRouter` or `CloudAPIClient`, which would race.
- **`--parallel`/streaming edge case**: if a streamed generation errors out or is
  cancelled before the final usage chunk arrives, `usage_out` stays empty —
  `track_usage()` and `estimate_tokens()` must both already handle "no usage" as the
  normal case (they do, by design, in Phases 1–2 above), not treat it as an error.
- **Verify TurboQuantLoader is actually sending the usage chunk in practice** before
  relying on it — the fix landed 2026-08-22 but hasn't been exercised end-to-end with
  `stream_options.include_usage` from a real client yet at time of writing this plan.
