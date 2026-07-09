# Phase 47 — Fix CI: lint gate + a real health-check bug

The PR's CI check (added in Phase 44) was failing: `ruff check` had never
actually been run against this codebase before (ruff wasn't installed in the
dev environment used for earlier phases), so turning the lint gate on in CI
was the first real signal — 105 pre-existing violations across 40+ files.
Also fixed a self-inflicted bug caught by the same run.

## Self-inflicted bug (caught before it shipped)

`agent/orchestrator.py::run_task` had a local variable named `new_session_id`
(from `bridge_text, new_session_id = await self.context_builder.build_handover(...)`)
in the same function where Phase 45 added a call to the module-level
`new_session_id()` helper. Because Python treats a name as local to the whole
function once it's assigned anywhere in that function, the earlier call at
`if not session_id: session_id = new_session_id()` would have raised
`UnboundLocalError` at runtime — ruff's F823 caught it. Renamed the local to
`bridge_session_id`.

## Real bugs found via the F841 (unused-variable) sweep

Several "unused variable" warnings turned out to be genuine bugs — a
discarded computation is often a sign the result was supposed to be used:

- **`llm/health.py::HealthChecker.check()`** — the actual health-check result
  (`available = await self.router.ollama.health_check(...)`) was computed and
  then **completely ignored**; the code unconditionally reported
  `available=True` and returned `True` regardless of what the check actually
  said. A model reported as "not loaded" (a legitimate `False` return, not an
  exception) was being marked healthy system-wide. Fixed: `False` now raises
  and is routed through the existing failure path. Added two regression
  tests (`tests/unit/test_llm.py::TestHealthChecker`) since this method had
  zero prior coverage beyond construction.
- **`agent/tools/shell_tool.py::run` (streaming path)** — computed
  `CREATE_NEW_PROCESS_GROUP` on Windows but never passed it to
  `create_subprocess_exec`. Investigated whether this mattered: the actual
  timeout-kill mechanism uses `taskkill /F /T /PID` (`_kill_process_tree`),
  which walks Windows' own parent-PID tree and doesn't need process-group
  membership. Confirmed dead, not a live bug — removed.
- **`agent/tools/tool_executor.py::_take_screenshot`** — extracted a `url`
  from the tool call input but called `browser_tool.run_and_screenshot()`
  with no arguments, silently ignoring whatever URL/port the caller
  requested and always screenshotting port 8080. Now parses the port out of
  the supplied URL and passes it through.
- **`agent/agents/verifier_agent.py::verify_research`** — the LLM's one-
  sentence `feedback` explanation (from the coverage/depth scoring call) was
  extracted and then dropped; `_format_research_report` never received it.
  Threaded it through as an optional parameter and added a `Feedback:` line
  to the report when present.

## Cosmetic/dead-code fixes (no behavior change)

- `agent/agents/red_team_agent.py`, `agent/agents/reviewer_agent.py` —
  removed genuinely-unused `tool_executor` extractions (copy-paste
  boilerplate; neither agent calls any tool).
- `agent/tools/code_chunker.py::_chunk_python` — removed `in_class`/
  `indent_stack`, vestigial from an earlier indent-aware chunking approach
  that was simplified; nothing referenced them.
- `agent/tools/file_system_tool.py` — removed a dead `PathTraversalError`
  import from `agent.security.paths` that claimed via a stale
  `# noqa: F401 – re-exported` comment to be re-exported, but was
  immediately shadowed by a same-named local `FileOperationError` subclass
  two lines later. Confirmed nothing in the codebase catches the
  security-module version expecting it to come from this module.
- Ambiguous single-letter loop variables (`l` → `ln`/`lat`, easily confused
  with `1`/`I`) in `agent/skills/wiki_manager.py` (×3) and `llm/health.py`
  (×2).
- Bare `except:` → `except Exception:` in `agent/tools/browser_tool.py`
  (was swallowing `KeyboardInterrupt`/`SystemExit` too).
- `E402` (import not at top of file): reordered a regex constant and a
  `import tempfile` that were sandwiched between import blocks in
  `agent/orchestrator.py` and `agent/platform.py`.
- Two `F821` (undefined name in a string type-hint) resolved by adding the
  referenced classes (`TaskRouter`, `EventEmittingExecutor`) under
  `TYPE_CHECKING` blocks instead of leaving unresolvable forward references.
- 68 unused imports/f-string-without-placeholders auto-fixed via
  `ruff check --fix` across 39 files (mechanical, reviewed by diff).

## CodeQL: default setup was conflicting with the Phase 46 workflow

The PR's CodeQL check also failed — SARIF upload was rejected with
`"CodeQL analyses from advanced configurations cannot be processed when the
default setup is enabled."` Confirmed via
`gh api repos/arozz7/coding-agent/code-scanning/default-setup` that default
setup was active (`state: configured`). Disabled it
(`state: not-configured`) via the same API so the custom
`.github/workflows/codeql.yml` (added in Phase 46, which wires in the
`py/path-injection` exclusion) becomes the sole scanner. This is reversible
via the same endpoint.

## Verification

- `ruff check agent api llm mcp observability` → **0 errors** (was 105).
- `python -m pytest tests -q` → **567 passed** (was 565; +2 for the new
  `HealthChecker` regression tests).
