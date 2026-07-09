# Phase 57 — Stop criterion-fix loop from gaming checks

## Problem

Two ways the criterion-fix loop satisfied a check without doing real work,
both observed directly in logs/api-20260705-233402.log:

1. `PlannerAgent._generate_criteria()` (completion criteria, distinct from
   `RequirementsExtractor.extract()`'s acceptance criteria) had no rule
   against generating `file contains: <file>.md:<word>` criteria.
   `RequirementsExtractor` already bans this for acceptance criteria
   ("markdown files are documentation artifacts, not evidence that code
   works") but the completion-criteria prompt never got the same rule,
   producing criteria like `file contains: NEXT_STEPS.md:Prioritized` and
   `file contains: NEXT_STEPS.md:Missing` — satisfied by appending a single
   word, proving nothing.

2. `make_targeted_fix_spec()` treated every `file contains: <path>:<sub>`
   failure the same way — "read the file, then APPEND the missing text."
   For `file contains: package.json:"@tauri-apps/cli"` this produced an
   instruction to append raw text to a JSON file, which corrupts the file's
   syntax and doesn't install anything. The log shows this exact criterion
   fail across two fix rounds (verifier score staying at 1-2/10 throughout)
   because the fix never actually added a working dependency.

## Change

**Criteria generation** (planner_agent.py `_generate_criteria`): added the
same markdown-exclusion rule `RequirementsExtractor` already has, plus a
rule that a `file contains` substring must be something the code/config
needs to function (import, dependency entry, function signature, config
key) — never an arbitrary label chosen to make the check pass.

**Fix-spec generation** (verifier_coordinator.py `make_targeted_fix_spec`):
added `_MANIFEST_INSTALL_CMDS`, mapping package.json/Cargo.toml/
pyproject.toml/requirements.txt to their install command template
(`npm install {pkg}`, `cargo add {pkg}`, `pip install {pkg}`). A `file
contains` failure against one of these files (that isn't itself missing)
now instructs running the package-manager command and explicitly forbids
hand-editing or APPEND-ing the manifest. Non-manifest files are unaffected —
the original read-then-APPEND instruction still applies where it's safe.

## Files

- `agent/agents/planner_agent.py` — criteria-generation prompt rules
- `agent/orchestration/verifier_coordinator.py` — `_MANIFEST_INSTALL_CMDS` + manifest-aware fix instruction
- `tests/unit/test_planner_agent.py` — 1 new test asserting the prompt bans markdown file-contains criteria
- `tests/unit/test_verifier_coordinator_criteria.py` — 3 new tests: package.json→npm install, Cargo.toml→cargo add, non-manifest files still use APPEND

## Tests

`pytest -q` — 591 passed (587 prior + 4 new). `ruff check` clean.
