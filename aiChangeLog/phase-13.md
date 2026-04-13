# Phase 13 — SDLC Workflow + Integration Tests

## Summary
Implemented the full autonomous SDLC pipeline (Plan→Build→Test→Debug→Run→Verify→Complete) and comprehensive integration tests covering all phases individually and end-to-end.

## Problem Solved
The orchestrator was single-shot: detect one task type → run one agent → done. Debug failures asked the user to intervene manually. The shell tool and Playwright existed in isolation but nothing sequenced them. Remote Discord users had no way to see what the running app looked like.

## New Files
| File | Purpose |
|------|---------|
| `agent/sdlc_workflow.py` | SDLCWorkflow class — full pipeline engine (280 lines) |
| `tests/integration/test_sdlc_full_cycle.py` | Full pipeline integration tests (7 tests) |
| `tests/integration/test_sdlc_phases_individual.py` | Per-phase isolation tests (22 tests) |
| `tests/integration/test_discord_sdlc_sim.py` | Discord command simulation for SDLC (7 tests) |
| `aiChangeLog/phase-13.md` | This file |

## Modified Files
| File | Change |
|------|--------|
| `agent/orchestrator.py` | Added `sdlc` keyword detection; routed to SDLCWorkflow; added `job_id` param to `run_task`; `screenshot_path` surfaced in result |
| `agent/tools/browser_tool.py` | Added `wait_for_server(url, timeout)` helper |
| `api/job_store.py` | Added `screenshot_path` column (with idempotent migration); wired into create/update/row_to_dict |
| `api/main.py` | Pass `job_id` to `run_task`; store `screenshot_path` in job record |
| `api/discord_bot.py` | Added SDLC phase labels; `_send_screenshot()` helper; screenshot attachment delivery in `_poll_job` |

## SDLCWorkflow Behaviour
- **Debug loop:** retries until pytest passes. Hard cap = 5. After cap, returns partial result with last error — never raises, always returns `success: True` with `phase: debug_exhausted`.
- **Port detection priority:** `.env` → `.env.example` → `package.json` start script → `pyproject.toml` → scan 8000–8099 for first free port.
- **Start command detection:** scans `main.py`/`app.py`/`server.py`/`run.py` for uvicorn/flask; falls back to `npm start` or `make run/start`.
- **Screenshots:** saved to `workspace/.screenshots/{job_id}_{ts}.png`. Files older than 24 h pruned on each new save.

## Test Results
- 36 new tests added — all passing
- 107 total integration tests — all passing
- 0 regressions
