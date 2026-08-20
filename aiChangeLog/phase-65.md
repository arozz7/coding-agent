# Phase 65 — Extend the developer-role timeout fix to the tester role

## Root cause

`logs/api-20260819-201238.log` showed two `ollama_hard_timeout` failures
from `base_agent:tester` (`task_num 4` at 00:48:58, `task_num 6` at
01:28:17), both at the `model_router` default 600s — while
`developer_agent.py`'s calls, already raised to 1500s by phase-62, did not
hit this failure in the same run. Phase-62 only patched `developer_agent.py`
and `fix_loop.py`; `tester_agent.py` was left on the 600s default despite
generating full test files under the same raised `max_tokens: 24576`
budget that motivated the developer-role timeout increase in the first
place.

## The fix

`agent/agents/tester_agent.py` — added `_TESTER_TIMEOUT_SECS = 1500.0`
(same value and rationale as `developer_agent._DEVELOPER_TIMEOUT_SECS`) and
passed it to the role's single `model_router.generate` call.

## Verification

- New test `test_tester_execute_passes_extended_timeout` asserts the
  `generate` call receives `timeout=1500.0`.
- `pytest tests/unit -q` — 503 passed (502 prior + 1 new).

## Files

- `agent/agents/tester_agent.py` — `_TESTER_TIMEOUT_SECS` added, passed to `generate()`
- `tests/unit/test_agents.py` — new regression test
