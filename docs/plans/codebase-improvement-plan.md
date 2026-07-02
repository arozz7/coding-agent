# Codebase Review & Improvement Plan

**Date:** 2026-07-02
**Branch reviewed:** `phase-33/orchestrator-refactor`
**Scope:** full repo — `agent/`, `api/`, `llm/`, `mcp/`, tests, packaging, repo hygiene

---

## 1. Snapshot

| Metric | Value |
|---|---|
| Production Python (agent/api/llm/mcp/etc.) | ~31,800 lines across ~120 files |
| Test suite | **501 passed, 16 failed, 48 errors** (~21 s) |
| Files over the 600-line hard limit | 6 (`developer_agent.py` 906, `model_router.py` 776, `verifier_coordinator.py` 731, `task_loop.py` 725, `research_agent.py` 693, `orchestrator.py` 602) |
| `except Exception` / bare `except` | 298 (61 of them silently `pass`/`continue`) |
| CI workflows | **None** (only a CodeQL config, no `.github/workflows/`) |
| Lint/type tooling | Configured in `pyproject.toml` but `ruff` not installed in the active environment; no `.pre-commit-config.yaml` |

### What's working well

The phase-33 refactor is genuinely good architecture work and should be kept:

- `api/main.py` went from ~1,470 lines to **167**, split into `api/routes/*` and `api/deps.py` (shared `AppState`).
- `api/discord_bot.py` went from ~1,640 lines to **31**, split into `api/discord/` (client, poller, commands).
- `agent/orchestration/` cleanly extracts `TaskLoop`, `ContextBuilder`, `VerifierCoordinator`, `TaskRouter`, `SubagentManager`, `agent_factory` from the old monolithic orchestrator.
- `_TaskExecCtx` in `task_loop.py` is a textbook unification of the two previously duplicated persistence paths.
- The `llm/` package has real resilience engineering: circuit breaker, rate limiter, cost tracker, health checker, account-level OpenRouter breaker.
- 500+ passing tests is a strong testing culture for a solo project.

The problems below are mostly *finishing the refactor properly*, not redoing it.

---

## 2. Findings by priority

### P0 — The test suite is red on this branch (64 broken tests)

The refactor moved code but the tests still `mock.patch` the **old** import locations. Per the project's own refactoring protocol ("Run tests AGAIN — if any test fails, you have broken behavior"), this must be fixed before the branch merges.

Root causes, confirmed by running the suite:

1. **`tests/integration/test_sessions.py` (8 errors)** — patches `api.main._orchestrator`, which no longer exists. The orchestrator now lives on `api.deps.app_state.orchestrator`.
2. **`tests/integration/test_model_fallback.py` (16 errors)** — patches `agent.orchestrator.DeveloperAgent`, which is no longer imported there. Agent construction moved to `agent/orchestration/agent_factory.py`.
3. **`test_task_loop.py` (7), `test_project_delete.py` (4), `test_verifier_coordinator_criteria.py` (2), `test_research_local_dirs.py` (1), `test_search.py` (2)** — same class of stale-patch-target failures (verify each; the two `test_search` Google failures may instead be live-network tests, see P2-6).

**Fix pattern.** For the API tests, stop patching module globals and use the shared state object (or better, FastAPI's `dependency_overrides` once deps are injectable):

```python
# BEFORE (breaks whenever api.main is reorganized)
with patch("api.main._orchestrator", mock_orch):
    ...

# AFTER — set state on the object the routes actually read
from api.deps import app_state

@pytest.fixture
def client(mock_orch):
    app_state.orchestrator = mock_orch
    yield TestClient(app)
    app_state.orchestrator = None
```

For agent-construction tests, patch the factory, not a transitive import:

```python
# BEFORE
patch("agent.orchestrator.DeveloperAgent", MockDeveloperAgent)

# AFTER — patch where the class is looked up
patch("agent.orchestration.agent_factory.DeveloperAgent", MockDeveloperAgent)
```

**Longer-term rule to adopt:** *patch where it's used, not where it's defined* — and prefer constructor injection (which this codebase already supports via `TaskLoopDeps` / `create_agents`) over `mock.patch` entirely. The 64 broken tests are the direct cost of patch-by-string-path coupling.

---

### P1 — No CI

The project mandates `pytest`, lint, and per-phase change logs, but nothing enforces them — which is exactly how a branch with 64 red tests happened. Add a minimal workflow:

```yaml
# .github/workflows/ci.yml
name: CI
on:
  push: { branches: [main] }
  pull_request:
jobs:
  test:
    runs-on: windows-latest   # matches the dev/runtime platform
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.11" }
      - run: pip install -e ".[dev]"
      - run: ruff check agent api llm mcp observability
      - run: python -m pytest tests -q
```

Mark live-network tests (`test_search.py` web calls, anything hitting LM Studio/Ollama) with `@pytest.mark.external` and exclude them in CI (`-m "not external"`), so CI is deterministic.

---

### P1 — The env-var "CodeQL taint laundering" pattern is a real concurrency bug, not just a smell

`agent/orchestrator.py:554` and four sites in `api/routes/workspace.py` write user input to a **process-global** env var and immediately read it back to break CodeQL's taint chain:

```python
os.environ["_CODEQL_SAFE_PROJECT"] = _project_name      # request A writes
_safe_project_name = os.getenv("_CODEQL_SAFE_PROJECT")  # …but request B may have written in between
```

FastAPI runs sync handlers in a **threadpool**, so two concurrent requests can interleave between the write and the read. Both values have passed validation, so the traversal guard still holds — but request A can end up operating on request B's project. In `delete_project` that is a **wrong-project data-loss race**.

Replace every laundering site with the inline pattern that is already proven to satisfy CodeQL in this repo (per prior CodeQL work: the inline `(base / input).resolve()` + `is_relative_to(base)` form passes; a shared `resolve_within()` helper does *not*):

```python
_ws_root = Path(self.workspace_path).resolve()
_project_dir = (_ws_root / _project_name).resolve()   # inline, no env round-trip
if not _project_dir.is_relative_to(_ws_root):
    raise ValueError(f"project_name {project_name!r} is outside workspace root")
```

If CodeQL still flags a specific sink after that, suppress with a scoped inline comment justifying it rather than laundering through global state. (Note: past experience in this repo says `# lgtm` comments are ignored by CodeQL — use the config file's `query-filters`/`paths-ignore` in `.github/codeql/codeql-config.yml` for targeted exclusions instead.)

---

### P1 — `AgentOrchestrator` uses two different workspace paths

In `agent/orchestrator.py:44-66`, `self.workspace_path` is resolved from env vars (`AGENT_EFFECTIVE_WORKSPACE` / `WORKSPACE_PATH`), **but every tool is constructed from the ignored `workspace_path` parameter**:

```python
_ws = _effective if _effective else os.getenv("WORKSPACE_PATH", "./workspace")
self.workspace_path = _ws                      # ← env-derived
self.fs_tool = FileSystemTool(workspace_path)  # ← constructor param
self.shell_tool = ShellTool(workspace_path)    # ← constructor param
```

If the env value and the parameter ever disagree (e.g., a `!project switch` that updates one but not the other), sessions/memory record one workspace while file/shell tools operate in another. Pick one source of truth — resolve once at the top and use the same value everywhere:

```python
_ws = os.getenv("AGENT_EFFECTIVE_WORKSPACE", "").strip() or os.getenv("WORKSPACE_PATH", "./workspace")
self.workspace_path = _ws
self.fs_tool = FileSystemTool(_ws)
self.shell_tool = ShellTool(_ws)
# … and so on for pytest_tool, browser_tool, tool_executor, wiki_manager, mcp_server
```

If the parameter is truly dead, deprecate it explicitly (`workspace_path: str | None = None` + a warning when passed) so callers stop believing it does something.

---

### P2 — Finish decomposing the six files over the 600-line hard limit

The project's own rule: soft limit 300, hard limit 600. Apply the existing refactoring protocol (tests green → move one chunk → tests green):

| File | Lines | Suggested split |
|---|---|---|
| `agent/agents/developer_agent.py` | 906 | Extract the block-format parsing (the seven `_*_BLOCK_RE` regexes + appliers for `FILE:`/`EDIT:`/`REPLACE:`/`APPEND:`/`SKILL:`/`SCRIPT:`) into `agent/agents/output_blocks.py`; extract the run-and-fix loop (npm-missing detection, error-file extraction, fix budget) into `agent/agents/fix_loop.py`. The agent class keeps only prompting + orchestration of those helpers. |
| `llm/model_router.py` | 776 | Extract evaluator-model selection (dynamic free-model discovery, blacklist, OpenRouter account breaker — lines ~53-72 plus their methods) into `llm/evaluator_selector.py`; extract config loading/env expansion into `llm/config_loader.py`. |
| `agent/orchestration/verifier_coordinator.py` | 731 | Split criterion evaluation (auto-checks: `file_exists` / `file_contains` / `command_exits_0`) from LLM-judged evaluation and report formatting. |
| `agent/orchestration/task_loop.py` | 725 | `_TaskExecCtx` and `TaskLoopDeps` can move to their own module; the criterion-driven fix-loop section is a natural second seam. |
| `agent/agents/research_agent.py` | 693 | Separate query decomposition / gap-fill passes from synthesis & file output. |
| `agent/orchestrator.py` | 602 | See next item — the dispatch table plus moving `delete_project` shrinks it below the limit. |

### P2 — Replace the if/elif agent dispatch with the registry you already have

`_run_specialized_agent` (orchestrator.py:250-269) hand-routes ten task types even though `create_agents()` already returns a dict. Keep the dict:

```python
# __init__ — instead of 14 self.developer_agent = _agents["developer"] lines:
self.agents = _agents
_ALIASES = {"plan": "plan", "review": "reviewer", "test": "tester",
            "security": "red_team", "researcher": "research"}

# _run_specialized_agent — the whole elif chain becomes:
agent = self.agents.get(_ALIASES.get(task_type, task_type), self.agents["developer"])
return await agent.run(task, context)
```

This also removes the need for `agent/orchestration/agent_factory.py` consumers to stay in sync with a hand-maintained attribute list. Keep individual attributes only for the few agents referenced elsewhere (or migrate those references to `self.agents[...]`).

### P2 — `delete_project` belongs in a service, not the orchestrator

`AgentOrchestrator.delete_project` (orchestrator.py:540-602) is 60 lines of storage-layer coordination with `import shutil` inline. Move it to `agent/project_lifecycle.py` taking `(session_memory, task_store, codebase_memory, workspace_path)` — it has no reasoning-layer logic and is independently testable. This is also where the P1 laundering fix lands.

### P2 — Exception-handling policy

298 broad catches, 61 of which silently swallow. Many are legitimate (skill post-hooks, phase-callback guards), but the volume hides real failures — e.g. `run_task`'s callback guard and handover failures degrade silently. Adopt three tiers and sweep the codebase toward them:

```python
# Tier 1 — boundary (API route, Discord command, task loop top): catch broad, log, convert
except Exception as ex:
    self.logger.error("task_failed", error=str(ex))
    return {"success": False, "error": str(ex)}

# Tier 2 — best-effort side effect (notifications, post-skills): catch broad but ALWAYS log
except Exception as ex:
    self.logger.warning("post_skill_failed", skill=name, error=str(ex))

# Tier 3 — everything else: catch the specific exception or let it propagate
except (OSError, json.JSONDecodeError) as ex: ...
```

Concretely: ban naked `except Exception: pass` (there are 61) — minimum bar is a `logger.debug` with a stable event name. `ruff`'s `S110`/`BLE001` rules can enforce this once ruff runs in CI.

### P2 — Packaging cleanup (`pyproject.toml`)

- **Two divergent dependency lists.** `[project.dependencies]` and `[tool.poetry.dependencies]` have drifted: the poetry list is missing `fastapi`, `uvicorn`, `discord.py`, `playwright`, `requests`, `ddgs` extras, etc. Since the build backend is `poetry-core` but the `[project]` table is the modern standard, **delete the `[tool.poetry.dependencies]` / dev-group duplicates** and keep only `[project]` + `[tool.poetry] packages` (or move to hatchling and drop poetry entirely).
- The console script is `ferrite = "local_coding_agent.__main__:main"` — a leftover from the decoy naming. Rename to `coding-agent` (or whatever the CLI is actually invoked as).
- `packages = [{ include = "local_coding_agent" }]` omits `agent`, `api`, `llm`, `mcp`, `observability` — an actual `pip install` of this project would ship only the CLI shim. List all top-level packages.
- Placeholder author `Developer <dev@example.com>`.
- Add `pytest-timeout` to dev deps (a hung LLM call currently hangs the whole suite) and install `ruff` into the working environment.

### P2 — Repo hygiene

- **Delete `nul`** (Windows redirection accident, tracked in the worktree since April).
- **`git rm results.sarif`** (135 KB scan artifact) and add `*.sarif`, `codeql-db/`, `.codeqldb/` to `.gitignore` — both CodeQL database dirs sit untracked in the tree now.
- Move root-level `test_ollama.py` into `tests/` or delete it (it's a 4-line smoke script).
- `CLAUDE.md` describes **Ferrite, a Rust storage-diagnostics tool** — not this repo. Any agent (or contributor) reading it gets architecture rules for the wrong project (`thiserror`, crate naming, cargo gates). If keeping it as a decoy is intentional, isolate the real instructions in `AGENTS.md` and make `CLAUDE.md` point at it; otherwise replace it with the actual project brief (three-layer rule, `pytest`/`ruff` gates, aiChangeLog convention).
- `aiChangeLog/` starts at phase-03; phases 00-02 exist only inside a stale worktree under `.claude/worktrees/`. Copy them over if they're wanted, and note `.claude/worktrees/` is correctly gitignored.

### P3 — Smaller improvements

1. **FastAPI lifespan** — `api/main.py:152` uses deprecated `@app.on_event("startup")`; migrate to the `lifespan=` context manager before FastAPI removes it.
2. **Session-ID collisions** — `session_%Y%m%d_%H%M%S` collides when two tasks arrive in the same second (plausible with Discord + API + subagents). Append entropy: `f"session_{ts}_{uuid4().hex[:6]}"`.
3. **Typing ratchet** — `disallow_untyped_defs = false` makes mypy nearly decorative. Ratchet per-package: start with `llm/` (already well-typed), add `[[tool.mypy.overrides]] module = "llm.*" disallow_untyped_defs = true`, expand outward.
4. **Pre-commit** — `pre-commit` is in dev deps but there's no `.pre-commit-config.yaml`. Add ruff + black + isort + `check-added-large-files` (would have caught `results.sarif`).
5. **README restructure** — 29 KB single-page feature dump reads like a changelog. Keep a ~10-bullet feature summary + quick start; move the rest into `docs/capabilities.md` (which already exists for this purpose).
6. **`ChainRunner(self)` / `SDLCWorkflow(self)`** pass the whole orchestrator as a god-object dependency. When each is next touched, narrow to an explicit deps dataclass like `TaskLoopDeps` did.
7. **Prompt-guard expectations** — the regex blocklist in `agent/security/prompt_guard.py` is fine as defense-in-depth for a local tool, but document that it is trivially bypassable and that the real boundaries are workspace scoping + the shell-path guard, so future work hardens those instead of growing the regex list.

---

## 3. Phased implementation plan

Follows the house convention: each phase ends with green `pytest`, a `aiChangeLog/phase-XX.md`, and a commit.

### Phase A — Stabilize (do before merging `phase-33/orchestrator-refactor`)
1. Fix the 64 stale-patch tests (P0) — mechanical, file-by-file; suite fully green.
2. Mark live-network tests `@pytest.mark.external`.
3. Repo hygiene sweep: `nul`, `results.sarif`, CodeQL dirs in `.gitignore`, `test_ollama.py`.
4. Add `.github/workflows/ci.yml` (P1) so regressions can't land silently again.

### Phase B — Correctness hardening
1. Replace all five env-var taint-laundering sites with inline `resolve()` + `is_relative_to()` (P1 race).
2. Unify the orchestrator workspace-path source of truth (P1).
3. Session-ID entropy fix.
4. Packaging cleanup: dedupe pyproject, fix `packages`, rename script entry, add `pytest-timeout`.

### Phase C — Structural debt (one file per task, refactoring protocol each time)
1. Agent-dispatch registry + move `delete_project` to `project_lifecycle.py` → `orchestrator.py` under 500 lines.
2. Split `developer_agent.py` (output blocks + fix loop).
3. Split `model_router.py` (evaluator selector + config loader).
4. Split `verifier_coordinator.py`, then `task_loop.py`, then `research_agent.py`.

### Phase D — Quality ratchet (ongoing, low urgency)
1. Exception-policy sweep with ruff `BLE001`/`S110` enforcement.
2. mypy strictness per-package, starting with `llm/`.
3. Pre-commit hooks; FastAPI lifespan migration.
4. README restructure; CLAUDE.md/AGENTS.md reconciliation.

---

## 4. Appendix — evidence

- Test run: `python -m pytest tests -q` → `16 failed, 501 passed, 3 warnings, 48 errors in 21.43s`.
- Example error: `AttributeError: <module 'api.main'> does not have the attribute '_orchestrator'` (moved to `api.deps.app_state` in the phase-33 split).
- Example error: `AttributeError: <module 'agent.orchestrator'> does not have the attribute 'DeveloperAgent'` (moved to `agent/orchestration/agent_factory.py`).
- Laundering sites: `agent/orchestrator.py:554`, `api/routes/workspace.py:38, 73, 145, 198`.
- Oversize files: `wc -l` — developer_agent 906, model_router 776, verifier_coordinator 731, task_loop 725, research_agent 693, orchestrator 602.
- Broad catches: 298 `except Exception`/bare across `agent api llm mcp`; 61 immediately `pass`/`continue`.
