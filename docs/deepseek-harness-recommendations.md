# DeepSeek Harness Review — Recommendations

Source reviewed: [deepseek-ai/deepseek-harness](https://github.com/deepseek-ai/deepseek-harness) (`dsh`),
specifically `packages/guard/` (loop-hygiene), the session-log/turn-flow model in
`docs/architecture.md`, and the capability-seam pattern used for `packages/subagent/`.

It's a TypeScript plugin framework (Cordis), not something to import — but three of its
mechanisms map directly onto recurring failure modes logged in `docs/agent-evolution.md`
(oscillating fix loops, verifier theater, undiagnosable runs). This doc is the same kind of
gap-analysis as `loop-engineering-recommendations.md`, scoped to what's concretely portable.

## 1. Escalating advisory reminder before hard-abort (`packages/guard/repeat-tool-reminder`)

**What they do:** instead of hard-aborting on a repeated action, a loop-hygiene listener tracks
consecutive calls keyed on `(tool name, canonicalized arguments)` per agent, and injects an
*escalating* advisory message into context at configured thresholds (default `[3, 5, 8]`) — first
a short generic nudge, then a detailed one naming the tool, run length, and arguments. The model
stays in control of whether to change approach or conclude; nothing is blocked. Untracked
bookkeeping calls (e.g. `todo_write`) don't reset the counter, so they can't be used to launder a
loop.

**What we do today** (`agent/agents/fix_loop.py:128-151`): a single-shot hard abort — if the MD5
hash of the raw error text is identical two attempts in a row, the loop breaks immediately with no
chance for the model to see the pattern and self-correct on attempt 2. This has two weaknesses
their design avoids:
- **No middle ground.** One repeat and we're done; a model that would converge on a differently-
  worded fix at attempt 3 never gets there.
- **Keyed on error text, not on the model's action.** If the model re-emits the *same broken fix*
  but the compiler error text shifts by a line number or a reordered symbol, cycling detection
  never fires and the loop burns all `MAX_FIX_ITERATIONS` (currently 50) silently. Keying on the
  tool call (which file/command the model chose, canonicalized) is a more direct signal of
  "trying the same thing again" than keying on the tool's output.

**Recommendation:** replace the single MD5-hash-of-error-text check with an escalating counter
keyed on the *fix attempt's action signature* (files touched + command re-run), fed a nudge at
attempt 2 ("you tried this, it didn't work — read the error and change approach") and only
hard-aborting at a higher ceiling (e.g. 4-5) if the signature repeats past the nudges. This is a
small, local change to `fix_loop.py` and doesn't require the plugin architecture — just the same
"reminder before abort" shape. It directly targets the oscillation pattern documented repeatedly
in `agent-evolution.md` (`command-exits-0 prose-strip`, `file-append-prose-collision fix`,
`windows-shell-operator-passthrough fix` — all cases where several fix rounds burned before the
real cause was found, and a mid-loop nudge naming the repeated action might have surfaced the bug
class faster, or at minimum produced a clearer log line than "identical error, aborting").

## 2. Declared, cooperative per-capability timeouts (`packages/guard/timeout-policy`)

**What they do:** a tool declares its own `timeoutMs` on its definition; one generic
`tools/execute` listener reads that declared value and arms a cooperative abort signal — zero
config in the listener itself, budget lives with the capability that knows its own latency
profile. Tools that don't declare a budget (their `bash`/`read`/`write`/`edit`) get no timeout by
design, not by omission.

**What we do today:** per-role hardcoded generate-timeout constants that get bumped reactively —
`developer-role timeout to 1500s` (2026-08-19), then the identical fix copied to the tester role
five days later (`tester-role-timeout fix`, 2026-08-20) because it "was left on the model_router
default." That's exactly the failure mode the declared-budget pattern prevents: the timeout lived
in the wrong place (a per-role special case) instead of being a property of *what's being called*
(a `generate()` request with a given `max_tokens`/model combination), so fixing it for one caller
didn't fix it for the sibling caller.

**Recommendation:** if `model_router.py` doesn't already do this, make the generate-timeout a
function of the request's model + `max_tokens` budget (larger max_tokens under thinking-enabled
models legitimately need more wall-clock time), computed once in one place, rather than a
per-role constant duplicated at each call site. That structurally prevents the next role
(`architect_agent`, `reviewer_agent`, etc.) from silently inheriting a too-short default the way
`tester_agent` did.

## 3. Append-only, model-visible session log (`docs/architecture.md` § Session log)

**What they do:** hold the invariant "model-visible ⟺ logged" — anything that reaches a model
request must be reconstructable from a structured, append-only event log (`SessionEvent`), not
just present in application logs. Every debugging session starts from that log, not from grepping
raw stdout.

**What we do today:** debugging is done by grepping `logs/api-*.log` after the fact — every single
row in `agent-evolution.md`'s orchestration section cites a specific log filename and a
description of what was manually diagnosed from it ("4+ criterion_fix_injected rounds...",
"29 WinError 2 spawn failures..."). That works, but it's forensic: someone has to notice the
pattern in unstructured text and hand-write the diagnosis every time.

**Recommendation:** this is the highest-leverage item but also the biggest lift, so treat it as a
direction rather than a task — don't build a full event-sourced session log. The cheap partial
win: emit one structured (JSON-lines) event per orchestration-relevant decision point that's
*already* being logged as free text — fix-loop attempt start/end with the attempt's action
signature, criterion fix injections, verifier score deltas, model/blacklist rotations. That alone
would let `review-agent-run` (the meta-skill already built for exactly this diagnosis workflow)
grep structured fields instead of parsing prose, and would make oscillation patterns detectable
by a script instead of requiring a human/agent read-through of the whole log each time.

## 4. Verifier as a distinct seam, not a second pass (capability-seam pattern)

This reinforces gap #4 already flagged in `loop-engineering-recommendations.md` ("Verifier isn't
clearly a *different* model/instruction set"). DeepSeek Harness's capability-seam convention
(`docs/glossary.md#capability-seam`: Service Definition / Provider / Consumer, "never one role
alone") is the structural argument for why that gap matters: their subagent seam lets a
verifier-role provider be swapped independently (different model, different reasoning effort,
even a different product entirely) without touching the consumer that calls it. If
`verifier_coordinator.py` and `verifier_agent.py` currently just re-invoke the same
`model_router` path with a different prompt, the "verifier theater" risk is structural, not just a
config oversight — worth confirming the verifier actually runs at a distinct (higher) reasoning
effort or a default-to-rejection instruction, as the existing recommendation says, and worth
locking that distinction in as a routing-level guarantee rather than a prompt convention that a
future edit could quietly erode.

## Bottom line

Items 1 and 2 are small, concrete, single-file fixes with a direct line to bugs already logged in
`agent-evolution.md` — worth doing next. Item 3 is a direction for the logging layer, not a
sprint. Item 4 doesn't need new code, just verification that the existing separation is real.
