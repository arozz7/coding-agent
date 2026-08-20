# Phase 63 — Fix prose-collision bug in FILE:/APPEND: block parsing

## Root cause

`logs/api-20260819-184755.log` showed a live run stuck oscillating on one
criterion (`file contains: src-tauri/db/migrations/20260622000000_initial.sql`)
across 4 `criterion_fix_injected` attempts and a subsequent holistic-verifier
retry, `criteria_evaluated` stuck at `4/5 passed` the whole time. The task —
appending a `-- sqlx-migrate` marker comment — is trivial, and the model was
doing exactly the right thing every attempt. `.agent-wiki/tech-patterns/`
had accumulated 16 near-duplicate entries about this exact same marker
(`required-sqlx-migration-marker-comment.md`, `sqlx-migration-marker-append-fix.md`,
`sqlx-migration-marker-in-initial-file.md`, ...) — the agent had been
rediscovering and "fixing" this same append across many prior runs, and it
never actually stuck.

Pulled the model's actual response from `TurboQuantLoader/logs/conversations.2026-08-19.jsonl`:

```
...Now I'll append the required `-- sqlx-migrate` marker using an APPEND: block (not FILE:):

APPEND: src-tauri/db/migrations/20260622000000_initial.sql
```sql
-- sqlx-migrate
```
```

The model's own system prompt encourages phrasing like "using an APPEND:
block", so its response often mentions the marker word in prose *before*
the real block. `agent/agents/output_blocks.py`'s `APPEND_BLOCK_RE` was:

```python
re.compile(r'APPEND:\s*(.+?)\n```\w*\n(.*?)```', re.DOTALL)
```

Unanchored to line start, with an unbounded `.+?` path group under
`re.DOTALL` (so `.` matches newlines). `re.findall` starts matching at the
*first* literal `"APPEND:"` it finds — which was inside the prose sentence,
not the real marker line. The non-greedy `.+?` then swallowed everything
up to the next code fence, including the real `"APPEND: <path>"` line,
producing a garbage path:

```
"block (not FILE:):\n\nAPPEND: src-tauri/db/migrations/20260622000000_initial.sql"
```

`file_system_tool` correctly rejected this as `path_traversal_attempt`
(confirmed in the log at 23:46:13), so the append silently never landed —
every single time this task came up, across every prior run.

## The fix

Restricted the path capture group to `[^\n]+` (cannot match a newline even
under `DOTALL`, unlike `.+?`) in every occurrence of this pattern:

- `agent/agents/output_blocks.py` — `APPEND_BLOCK_RE` and `extract_file_writes`'s `FILE:` pattern
- `agent/agents/architect_agent.py` — `_extract_file_writes`'s `FILE:` pattern
- `agent/agents/mapper_agent.py` — inline `FILE:` pattern in `execute()`
- `agent/agents/tester_agent.py` — `_extract_file_writes`'s `FILE:` pattern

With the path bounded to a single line, a false match starting inside prose
(e.g. "...using an APPEND: block (not FILE:):") can no longer find a code
fence immediately after that same line, so the match attempt fails outright
and the regex engine backtracks to the real marker line instead.

`agent/agents/documenter_agent.py`'s `_extract_file_blocks`/`_extract_append_blocks`
already split the response on `(?m)^(?=FILE:)` / `(?m)^(?=APPEND:)` before
matching — line-start markers only — so they were already immune to this
class of bug; verified with the same adversarial input. `agent/skills/skill_writer.py`'s
`SKILL_BLOCK_RE` and `output_blocks.py`'s `EDIT_BLOCK_RE`/`REPLACE_BLOCK_RE`/
`SCRIPT_BLOCK_RE` already bound their path groups to `[^\n]+`/`\S+`, so they
were not vulnerable.

## Verification

- New regression tests reproduce the exact bug string from the log and
  confirm it now parses correctly:
  - `tests/unit/test_output_blocks.py` (new file) — 5 tests covering
    `extract_file_appends`/`extract_file_writes` prose-collision cases plus
    plain-case regressions.
  - `tests/unit/test_agents.py` — added prose-collision tests for
    `ArchitectRole._extract_file_writes` and `TesterRole._extract_file_writes`.
- Confirmed each new test fails against the pre-fix `.+?` pattern and passes
  against the fixed `[^\n]+` pattern (checked interactively before committing).
- `pytest tests/unit -q` — 494 passed (487 prior + 5 new in
  `test_output_blocks.py` + 2 new in `test_agents.py`).
- `mapper_agent.py`'s fix has no dedicated unit test (the pattern is inline
  in `execute()`, not a separate method) — verified correct by construction,
  identical change to the three tested occurrences.

## Files

- `agent/agents/output_blocks.py` — `APPEND_BLOCK_RE`, `extract_file_writes` path groups bounded
- `agent/agents/architect_agent.py` — `_extract_file_writes` path group bounded
- `agent/agents/mapper_agent.py` — inline `FILE:` path group bounded
- `agent/agents/tester_agent.py` — `_extract_file_writes` path group bounded
- `tests/unit/test_output_blocks.py` — new regression tests
- `tests/unit/test_agents.py` — new regression tests for architect/tester roles
