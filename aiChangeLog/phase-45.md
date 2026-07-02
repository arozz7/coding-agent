# Phase 45 — Correctness Hardening (Improvement Plan Phase B)

Source: `docs/plans/codebase-improvement-plan.md`, Phase B ("Correctness hardening").

## Changes

### 1. Removed env-var taint-laundering (concurrency race fix)

Six sites wrote user-derived path/name input to a process-global `os.environ`
key and immediately read it back, on the theory that this broke CodeQL's
taint chain:

```python
os.environ["_CODEQL_SAFE_PATH"] = new_path
path = (workspace_root / os.getenv("_CODEQL_SAFE_PATH", "")).resolve()
```

Because FastAPI runs sync handlers in a threadpool, two concurrent requests
can interleave between the write and the read — both values have already
passed validation, so the traversal guard still holds, but request A can
read request B's freshly-written env value. In `delete_project` this was a
wrong-project data-loss race.

Replaced all six sites with the inline `(base / input).resolve()` +
`is_relative_to(base)` pattern (no `os.environ` round-trip) — the pattern
previously confirmed to satisfy CodeQL in this repo without the laundering
step:

- `agent/orchestrator.py` — `delete_project`
- `api/routes/workspace.py` — `set_workspace`, `read_workspace_file`,
  `set_project`, `wiki_migrate`, `take_screenshot`

Two adjacent sites (`set_workspace`, `set_project`) also wrote-then-read
`AGENT_EFFECTIVE_WORKSPACE` — that env var *is* a legitimate cross-process
source of truth (read by `agent/workspace_context.py`, `git_tool.py`,
`mcp/server.py`, `api/routes/tasks.py`), so the *write* stays, but the
redundant immediate read-back was replaced with direct use of the
already-validated local `Path` object.

**Verification gap, disclosed rather than hidden:** removing the laundering
was validated against the documented race (real bug, fixed) and against the
full test suite (565/565 green), but **not** against an actual CodeQL run —
the CodeQL CLI isn't installed in this environment, and the two databases at
`.codeqldb/` and `codeql-db/` are stale (2026-04-18, predate this branch).
Separately, `.github/codeql/codeql-config.yml` already excludes the
`py/path-injection` query entirely for this repo, but there is no
`.github/workflows/codeql.yml` wiring that config to an actual scan — so
whether it's even in effect is unconfirmed from the repo alone. **Before
trusting this as CodeQL-clean, run `codeql database analyze` (or trigger a
scan) against `agent/orchestrator.py` and `api/routes/workspace.py`
specifically.**

### 2. Unified the orchestrator's workspace-path source of truth

`AgentOrchestrator.__init__` computed `self.workspace_path` from env vars
(`AGENT_EFFECTIVE_WORKSPACE` / `WORKSPACE_PATH`) but built every tool
(`fs_tool`, `shell_tool`, `tool_executor`, `wiki_manager`, `memory_wiki`,
`mcp_server`, ...) from the separate, ignored `workspace_path` constructor
parameter. If the two ever disagreed, session/memory bookkeeping and actual
file/shell operations could point at different directories. All tool/manager
construction now uses the same resolved `_ws` value as `self.workspace_path`.

### 3. Session-ID collision fix

`session_<timestamp>` (second-level precision) could collide when two tasks
arrived in the same second — plausible with Discord + API + subagents
submitting concurrently. Added `agent/session_id.py::new_session_id(prefix)`
(timestamp + 6 hex chars from `uuid4`) and switched all five generation
sites to it: `agent/orchestrator.py` (×2), `agent/orchestration/context_builder.py`
(handover bridge sessions), `api/routes/tasks.py`, `agent/multi_agent/workflow.py`.

### 4. Packaging cleanup

- **Switched build backend from Poetry to Hatchling.** The repo has no
  `poetry.lock`, no `poetry`/`poetry-core` installed anywhere in the dev
  environment, and the README's documented install path is
  `python -m pip install -e .` — Poetry was never actually driving this
  project; `[tool.poetry.dependencies]` had drifted from
  `[project.dependencies]` (missing `fastapi`, `discord.py`, `playwright`,
  `requests`, `ddgs`, ...) purely because nothing ever exercised it. Removed
  `[tool.poetry]`, `[tool.poetry.dependencies]`, and
  `[tool.poetry.group.dev.dependencies]` entirely; `[project.dependencies]`
  is now the single source of truth. Verified with a real dry-run editable
  build (`pip install --no-build-isolation --no-deps -e . --dry-run`) —
  hatchling resolves metadata successfully.
- `[tool.hatch.build.targets.wheel].packages` now lists every real top-level
  package (`agent`, `api`, `llm`, `mcp`, `observability`,
  `local_coding_agent`) instead of just `local_coding_agent`.
- Renamed the console script from `ferrite` (leftover from the decoy
  `CLAUDE.md` naming) to `coding-agent`. Confirmed no docs referenced the old
  name.
- Replaced the placeholder `Developer <dev@example.com>` author with the
  actual maintainer name (no email, to avoid publishing one unnecessarily in
  package metadata).
- Removed a duplicate `python-dotenv>=1.0.0` entry from `[project.dependencies]`.
- Added `pytest-timeout>=2.3.0` to dev deps **and** set `timeout = 60` in
  `[tool.pytest.ini_options]` — listing the dependency alone doesn't fix
  anything; the ini key is what actually caps a hung LLM call from hanging
  the whole suite. Verified empirically that the ini key is silently ignored
  (a `PytestConfigWarning`, not a hard error) when the plugin isn't
  installed, so this doesn't break environments that skip the dev extra;
  installed the plugin locally and confirmed the full suite still passes
  with it active.

## Verification

`python -m pytest tests -q` → **565 passed**, run after each of the four
changes above and once more at the end.

## Not done in this phase

Phase C (structural file splits: `developer_agent.py`, `model_router.py`,
`verifier_coordinator.py`, `task_loop.py`, `research_agent.py`,
`orchestrator.py`) remains per `docs/plans/codebase-improvement-plan.md`.
The CodeQL re-verification noted above is an open item, not deferred to a
later phase — it should happen before this phase's P1 fix is considered
fully closed.
