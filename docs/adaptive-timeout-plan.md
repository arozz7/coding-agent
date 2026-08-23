# Follow-up plan: adaptive per-model timeout from observed tokens/sec

Status: **design only — not implemented.** Follow-up to `docs/deepseek-harness-recommendations.md`
item 2 (`ModelConfig.timeout_secs`, `llm/config.py` + `config/models.yaml`), which replaced
duplicated per-role timeout constants with a single static value per model. This plan replaces
that static value with one derived from real throughput, while keeping the static value as the
safety floor/fallback.

## Problem

`timeout_secs` is a hand-tuned guess (1500s for `Qwen3.8-27B-Q4_K_S`, picked after two live
failures). It doesn't account for:
- Different hardware running the same `models.yaml` (a laptop vs. a workstation GPU).
- TurboQuantLoader's `--parallel 2` splitting one model across 2 slots — concurrent requests to
  the same model see materially lower per-request throughput than a solo request.
- Model swaps at the same config name (someone re-points `Qwen3.8-27B-Q4_K_S`'s `.gguf` to a
  different quantization) silently invalidating the tuned constant.

## Data source (already available, currently unused)

`agent/agents/../llm/ollama_client.py:_do_generate` calls the OpenAI-compatible
`/v1/chat/completions` endpoint and today reads only `choices[0].message.content`
(`ollama_client.py:183-218`). Every response from an OpenAI-compatible backend (LM Studio, vLLM,
llama.cpp server — all three backends this project targets) also includes a `usage` object with
`completion_tokens`, which nothing in the codebase currently reads (confirmed via grep — zero
hits for `usage` in `llm/`). Combined with wall-clock elapsed time already available around the
`_sync_post()` call, `completion_tokens / elapsed_secs` is a real observed tokens/sec sample, at
zero extra network cost — no new health-check round trip needed, despite the original framing of
"use the health endpoint." The health endpoint (`HealthChecker.check()` → `ollama.health_check()`)
is a lightweight up/down ping with no generation, so it can't measure throughput; real `generate()`
calls are the only honest source.

## Storage

Extend `HealthChecker` (`llm/health.py`), which already tracks a rolling per-model latency window
in `self.successes[model]` (list of `(timestamp, latency_ms)`, pruned to the last hour in
`_record_success`, averaged in `_calculate_avg_latency`). Add a parallel
`self.throughput_samples: Dict[str, list[tuple[datetime, float]]]` of `(timestamp, tokens_per_sec)`,
pruned the same way, with a `_calculate_avg_throughput(model)` mirroring
`_calculate_avg_latency`. Reuse the existing pattern instead of introducing a new tracking
subsystem — `ModelResilienceManager` (`llm/model_resilience.py`) looks like a separate, currently
unwired legacy path (nothing in `model_router.py` calls it); don't extend that one.

## Computation

In `ModelRouter.generate()` (`llm/model_router.py`), where `effective_timeout` is currently
resolved (`timeout if timeout is not None else config.timeout_secs`), add a third tier in between:

1. Explicit call-site `timeout=` override (unchanged, highest priority).
2. **New:** if `health_checker` has enough throughput samples for this model (see minimum-sample
   guard below), compute `effective_max_tokens / observed_tokens_per_sec * SAFETY_FACTOR`, clamped
   to `[config.timeout_secs, some hard ceiling]` — i.e. the observed value can only ever *raise*
   the effective timeout above the static floor, never lower it below what's already been proven
   safe by hand-tuning. `SAFETY_FACTOR` (e.g. 1.5–2.0) absorbs the gap between "average" and
   "worst observed" throughput.
3. `config.timeout_secs` (today's static value) — the fallback when there isn't enough data yet.

## Guardrails (the actual design work here)

This project already hit the exact failure class this data feed risks — `criterion_score_store`'s
Bayesian confidence got poisoned by 53 malformed-criterion failures and cut a fix budget to 1
attempt (`Bayesian-store poison reset` in `docs/agent-evolution.md`, 2026-05-25). A single bad
throughput sample (e.g. a call that raced a model reload, or landed while both TurboQuant slots
were busy) must not be able to poison the estimate the same way:

- **Minimum-sample floor.** Don't trust the observed average until at least N samples (e.g. 5)
  exist for that model; below that, use the static `timeout_secs` unconditionally — mirrors the
  "raised minimum attempt budget from 1 to 2" fix from the same incident.
- **Per-sample sanity clamp.** Reject a sample before admitting it to the rolling window if
  `elapsed_secs <= 0`, `completion_tokens <= 0`, or the resulting tokens/sec falls outside a
  plausible range (e.g. absurdly high from a near-instant cached/degenerate response) — don't let
  one clock artifact set the baseline.
- **Median, not mean, or a trimmed mean.** TurboQuant's 2-slot concurrency means throughput is
  bimodal (solo vs. contended), not normally distributed; a median or a rolling window with
  outlier trimming resists a run of concurrent-slot samples dragging the estimate down (or a lucky
  solo run dragging it up past what's typical) more than a plain mean would.
- **The static value is a floor, never bypassed downward.** As above — this makes the feature
  strictly additive risk (can only *avoid* a timeout that would have fired unnecessarily), not a
  replacement that could make an already-correct static value too aggressive.
- **Same TTL-style decay as the evaluator blacklist.** Reuse the existing 1-hour pruning window
  already in `HealthChecker._record_success` rather than an unbounded history, so a stale sample
  set from before a hardware/model swap ages out on its own.

## Rollout

1. Add `throughput_samples` tracking to `HealthChecker` and wire the `usage.completion_tokens` /
   elapsed-time capture into `ollama_client._do_generate` (or have `model_router.generate()`
   measure elapsed time around the call it already makes, to keep `ollama_client` a thin HTTP
   layer) — data collection only, no behavior change yet. Ship this alone first and let it
   accumulate samples across normal runs before wiring in step 2.
2. Add the three-tier `effective_timeout` resolution in `model_router.generate()`, gated behind
   the minimum-sample floor so it's a no-op until real data exists.
3. Unit tests: extend `tests/unit/test_model_router_timeout.py` (added alongside the static
   `timeout_secs` work) with cases for below-floor sample count (falls through to static),
   above-floor with a clean sample set (computes a raised timeout), and a poisoned-sample-mix
   scenario (clamp keeps the estimate sane).
4. Manually verify against one real local run's logs (the project's own convention per
   `docs/agent-evolution.md`) before trusting this in the fix loop — confirm the observed
   tokens/sec for `Qwen3.8-27B-Q4_K_S` roughly explains why 1500s was the number that stopped the
   original timeouts, as a sanity check that the formula and the hand-tuned value agree.

## Open question to resolve before implementing

Whether to key the rolling throughput window purely by model name (current `HealthChecker`
granularity) or also by "was this call concurrent with another request to the same model" —
TurboQuant's `--parallel 2` means the honest answer may need two buckets (solo vs. contended
throughput) rather than one blended average. Concurrency count isn't currently tracked anywhere
in `model_router.py`; adding it is a prerequisite if the blended-average approach above proves too
noisy in practice. Recommend starting with the single blended bucket (simpler, matches the
existing `avg_latency_ms` pattern) and only splitting by concurrency if real samples show it's
necessary — no need to build the more complex version speculatively.
