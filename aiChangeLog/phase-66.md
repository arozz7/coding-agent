# Phase 66 — Fix-loop cycling detection, verifier evidence bar, per-model timeout budget

## Root causes

Three related problems surfaced from reviewing a live oscillating run
(`job_a4c1b587e385`, round 3: `prev_score=5, new_score=4, regressed=true` after
a 23-minute, 22k-token developer turn plus two no-progress retries):

1. **Cycling detection was text-hash based** (`hashlib.md5(raw_errors)`), so it
   only caught a model repeating the *exact same* error output. A model that
   keeps touching the same file with a slightly different (but still broken)
   fix each time — error text shifting on a line number or reordered symbol —
   evaded it entirely and burned the full iteration budget.
2. **Verifier prompts had no explicit tie-breaking rule for ambiguous
   evidence**, so a plausible-looking but incomplete response could pass a
   criterion it shouldn't have, letting a round "succeed" without actually
   fixing anything (masking the regression that triggered this investigation).
3. **Timeout budget was duplicated per agent role**
   (`developer_agent._DEVELOPER_TIMEOUT_SECS`, `tester_agent._TESTER_TIMEOUT_SECS`,
   `fix_loop._DEVELOPER_TIMEOUT_SECS`, all hand-copied to `1500.0`) instead of
   living on the model. The tester constant was only added five days after the
   identical developer-role timeout bug, reactively, because nothing else
   would get the fix by default — a role added later with the same model would
   hit the same bug again.

## The fix

- **`agent/agents/fix_loop.py`** — cycling detection now keys on the *set of
  files touched per attempt*, not error text. At `_CYCLE_NUDGE_STREAK` (1)
  consecutive repeats it injects an advisory nudge into the next prompt
  ("you've modified X N times without resolving this — try a different fix");
  only at `_CYCLE_ABORT_STREAK` (3) does it give up. Both are env-overridable
  (`FIX_LOOP_CYCLE_NUDGE_STREAK`, `FIX_LOOP_CYCLE_ABORT_STREAK`).
- **`agent/agents/verifier_agent.py`** — all three verifier system prompts
  (coverage/depth, file-format check, code-correctness) gained "when evidence
  is ambiguous or incomplete, fail/answer no rather than assuming it passed."
  Also routes through `model_router.get_model("verify")` instead of
  `"coding"`, so verification can be pointed at a different/stronger model
  than development without code changes (`defaults.verify_model` in
  `config/models.yaml`, currently unset — falls through to the same
  coding-optimized default).
- **`llm/config.py`** — `ModelConfig.timeout_secs: float = 600.0`, a per-model
  property. **`llm/model_router.py`** — `generate()` resolves
  `effective_timeout = timeout if timeout is not None else config.timeout_secs`
  (explicit call-site override still wins). **`config/models.yaml`** —
  `Qwen3.8-27B-Q4_K_S` gets `timeout_secs: 1500`. `developer_agent.py`,
  `tester_agent.py`, and `fix_loop.py` no longer pass `timeout=` at all — every
  role, present and future, now gets the right budget for whichever model it
  calls, not just the roles patched reactively after a live failure.
- **Observability**: `task_loop.py` now logs `verifier_score_delta` (round,
  prev/new score, `regressed` bool) on every round — this is what made the
  round-3 regression visible in the first place. `evaluator_selector.py` logs
  `evaluator_blacklist_changed` on TTL expiry/rate-limit/payment-required.
  `task_loop_cycles.py`/`verifier_coordinator.py` thread `phase` through fix
  spec logging.

## Verification

- `tests/unit/test_model_router_timeout.py` (new) — model-declared timeout
  used by default, falls back to 600s, explicit override still wins.
- `tests/unit/test_agents.py` — updated: `tester_agent` no longer passes
  `timeout=` at all (asserts its absence from `generate()` kwargs).
- `tests/unit/test_developer_agent_fix_loop.py` — updated: cycling test now
  drives 4 same-file fix attempts, asserts the abort message names the file
  and that the pre-abort attempt carried the nudge text.
- `pytest tests/unit tests/integration -q` — 653 passed.

## Files

- `agent/agents/developer_agent.py`, `tester_agent.py`, `fix_loop.py` — drop
  per-role timeout constants
- `agent/agents/verifier_agent.py` — ambiguous-evidence tie-break, `"verify"`
  model purpose
- `agent/orchestration/task_loop.py` — `verifier_score_delta` logging
- `agent/orchestration/task_loop_cycles.py`, `verifier_coordinator.py` —
  `phase` threaded into fix-spec logging
- `llm/config.py`, `llm/model_router.py`, `config/models.yaml` —
  `timeout_secs` per-model config + resolution
- `llm/evaluator_selector.py` — `evaluator_blacklist_changed` logging
- `tests/unit/test_model_router_timeout.py` (new), `test_agents.py`,
  `test_developer_agent_fix_loop.py`
