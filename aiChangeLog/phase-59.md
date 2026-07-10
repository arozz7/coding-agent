# Phase 59 — Cross-platform criteria: don't rely on shell portability

## Problem

logs/api-20260709-111051.log showed real damage from a portability gap: the
acceptance criterion `command exits 0: find src-tauri/db/ -type f -name
'*.sql' | grep -q .` was generated despite `RequirementsExtractor`'s own
system prompt explicitly banning `find`/`grep` as "Unix-only... they do not
exist on Windows and will always fail." Small local models don't reliably
follow prompt-only rules (the same lesson behind Phase 4's hard task-count
cap) — the criterion was generated anyway, and because cmd.exe's *built-in*
`find` searches file *content* rather than paths, the command couldn't pass
on Windows no matter what the agent did. Chasing it degraded real work: the
agent had already built a correct migration
(`src-tauri/db/migrations/0001_init.sql`, `expenses`/`categories` tables,
verifier score 9/10) and then, trying to satisfy the unpassable check,
created a second conflicting migration (`src-tauri/db/001_create_payments.sql`,
a `payments` table, wrong directory) — tanking the score to 3/10.

The user asked for this to work correctly on both Windows and Linux, not
just Windows — the fix needed to be platform-agnostic, not a Windows-only
patch.

## Change

**Root fix — enforce portability at criteria-generation, not the prompt**
(`agent/orchestration/criterion_evaluator.py`, new `normalize_criterion()`):
a `find <dir> -type f -name '<pattern>'` shape is really asking "does a file
matching this pattern exist" — which the codebase already answers with the
`file exists: <glob>` auto-check, a pure-Python `Path.glob()` call with zero
shell/OS dependency, identical behavior on Windows and Linux. `find ...`
criteria are rewritten into that form. Any other `command exits 0` criterion
whose command starts with a Unix text/file utility with no reliable
cross-platform behavior (`grep`, `ls`, `cat`, `wc`, `sed`, `awk`, `xargs`,
`head`, `tail`, bare `find`) is dropped rather than kept and left to fail
unconditionally on one OS or the other. Wired into both criteria-generation
call sites — `RequirementsExtractor._parse()` (acceptance criteria) and
`PlannerAgent._generate_criteria()` (completion criteria) — so this can't be
bypassed by either code path.

**Defense in depth — `find` translation in the shell layer**
(`agent/tools/shell_tool.py`): `_translate_unix_to_windows()` already
translates `grep`→`findstr`, `ls`→`dir`, etc. for commands the agent issues
directly (not just criteria), but had no `find` handling — it fell through
unchanged, which is exactly the untranslated-`find` failure seen in the log
(`"original"` and `"translated"` were identical). Added a `find <dir>
[-type f] -name '<pattern>'` → `dir /s /b "<dir>\<pattern>"` translation.
Unix stays untouched — translation only runs when `IS_WINDOWS`, matching the
existing pattern for every other verb in that function.

## Files

- `agent/orchestration/criterion_evaluator.py` — `normalize_criterion()`
- `agent/orchestration/requirements_extractor.py` — apply `normalize_criterion` to parsed acceptance criteria
- `agent/agents/planner_agent.py` — apply `normalize_criterion` to parsed completion criteria
- `agent/tools/shell_tool.py` — `find` → `dir /s /b` translation for Windows
- `tests/unit/test_criterion_normalizer.py` — new, 14 tests
- `tests/unit/test_requirements_extractor.py` — 2 new tests (find rewrite, unportable-command drop)
- `tests/unit/test_planner_agent.py` — 1 new test (find rewrite in completion criteria)
- `tests/unit/test_shell_tool.py` — 4 new tests for `find` translation

## Tests

`pytest -q` — 622 passed (601 prior + 21 new). `ruff check` clean.

## Known remaining gap (not fixed this phase)

While reviewing this log, a second issue surfaced: the verifier quality
gate's stagnation-stop (added in Phase 56) doesn't fully halt the loop when
`completion_criteria` and `acceptance_criteria` are both present — a
stagnation "stop" only skips injecting *that* gate's own fix; the separate
acceptance-criterion loop can still trigger further full verification
passes afterward, and `_prev_verifier_score` isn't updated on a stopped
round, so the next comparison uses a stale baseline. In this run it was
implicitly bounded by the acceptance-fix budget (5) rather than by design.
Flagged for a follow-up phase, not addressed here.
