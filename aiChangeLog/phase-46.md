# Phase 46 — Wire up CodeQL scanning

Follow-up to Phase 45's disclosed verification gap: the taint-laundering
removal (6 sites) was correct-by-construction for the concurrency race but
was never checked against a live CodeQL run, because `.github/codeql/codeql-config.yml`
(which excludes `py/path-injection`) wasn't referenced by any workflow file —
there was no `.github/workflows/codeql.yml` at all.

## Change

Added `.github/workflows/codeql.yml`: runs `github/codeql-action` on push to
`main`, on every PR, and weekly on a cron schedule, explicitly passing
`config-file: ./.github/codeql/codeql-config.yml` so the existing
`py/path-injection` exclusion actually takes effect.

Also worth noting: the exclusion config's own comment states *why* the
exclusion exists — "CodeQL tracks taint through os.getenv/instance-attributes
all the way to any path join and does not model our sanitizers." That
confirms the env-var write-then-read pattern removed in Phase 45 never
actually broke CodeQL's taint chain (taint flows through `os.getenv` too);
the query-filter exclusion was always the thing suppressing the 40
historical alerts, not the laundering. This raises confidence that removing
the laundering is CodeQL-neutral, assuming the exclusion is in effect.

## Known risk — needs a human check

**GitHub does not allow both "default setup" (configured via repo Settings →
Code security, no workflow file) and a custom/advanced CodeQL workflow to run
simultaneously.** If this repo currently has default setup enabled, this new
workflow's first run may fail with an error to that effect, and default
setup will need to be disabled in repo settings first. This can't be checked
or changed from within a coding session — it requires looking at
github.com/arozz7/coding-agent → Settings → Code security → Code scanning.

## Verification

`python -m pytest tests -q` → 565 passed (unaffected; workflow-only change).
The workflow itself has not run yet — first real signal comes from the
Actions tab / Security → Code scanning tab after this push.
