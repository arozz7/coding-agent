# Loop Engineering Review — Recommendations

Source reviewed: [cobusgreyling/loop-engineering](https://github.com/cobusgreyling/loop-engineering)

That repo isn't a code library to import — it's a design framework for turning ad-hoc agent
sessions into scheduled, bounded, self-auditing "loops." This doc maps its concepts onto what
this project already has (`agent/orchestration/`, `agent/agents/fix_loop.py`,
`agent/subagent/`, `mcp/`) and lists concrete gaps worth closing.

## What we already have (no action needed)

| Loop-engineering building block | Our equivalent |
|---|---|
| Sub-agents (maker/checker split) | `verifier_coordinator.py`, `verifier_agent.py`, `acceptance_tester_agent.py` — separate from `developer_agent.py` |
| Skills (persistent project knowledge) | `.agents/skills/`, `skills/` |
| Plugins & Connectors (MCP) | `mcp/server.py`, `mcp/tools/filesystem_server.py`, `mcp/tools/git_server.py` |
| Worktrees for isolation | `.claude/worktrees/` already used for isolated fix attempts |
| Attempt capping in fix loops | `fix_loop.py` has `MAX_FIX_ITERATIONS` + cycling detection (identical error on consecutive attempts aborts early) |
| Cost/model governance | `llm/cost_tracker.py`, `llm/rate_limiter.py`, `llm/circuit_breaker.py`, `llm/model_router.py` |

## Gaps worth closing

### 1. No durable, human-readable loop state file
We have scattered state (`.state/active_project`, `data/*.db`, `data/criterion_scores.json`) but
nothing like the framework's `STATE.md` / `LOOP.md` convention — a single markdown file per
recurring workflow that a human can open and read without querying a DB. If we start running
any automation on a schedule (see #3), give each one its own `STATE.md` with a pruning rule
(drop closed/merged items every run) instead of letting a shared file grow unbounded.

### 2. `MAX_FIX_ITERATIONS=50` is high for an unattended cap
The framework's failure-mode catalog calls out "Infinite Fix Loop" as the #1 risk and recommends
a hard cap around 3 attempts before escalating to a human with full context. 50 is fine for an
interactive session where the user can Ctrl-C, but if `fix_loop.py` is ever invoked from a
scheduled/unattended context (see #3), that ceiling should drop sharply and escalate (not just
abort silently on cycling) — e.g. write a flagged entry to a state file or notify, rather than
just stopping.

### 3. No scheduled/unattended loop patterns yet
`CronCreate`/`CronList` exist as harness tools but nothing in this repo defines a scheduled
pattern (daily triage, PR babysitter, dependency sweeper, etc.). If we want the agent to do
proactive work between sessions, adopt the framework's phased rollout instead of jumping straight
to automation:
- **L1 (report-only)** — e.g. a daily triage that summarizes failing tests / stale PRs into a
  state file, no code changes.
- **L2 (assisted)** — propose a fix on a worktree branch, require human merge.
- **L3 (unattended)** — only after L1/L2 have run long enough to trust the signal.

### 4. Verifier isn't clearly a *different* model/instruction set
`verifier_coordinator.py` and `verifier_agent.py` separate the *role* of verifier from
`developer_agent.py`, which is good — but check whether the verifier actually runs at a different
(higher) reasoning effort or with an explicit "default to rejection" instruction, versus just
being a second pass with the same model config. The framework's "Verifier Theater" anti-pattern
is specifically about verifiers that don't run tests/lint themselves or that share blind spots
with the implementer because they're the same model under different framing.

### 5. No explicit path allowlist/denylist for autonomous edits
Nothing in `agent/tools/` or the skills appears to enforce a denylist of paths agents shouldn't
touch unattended (secrets, CI config, migrations). The framework treats this as mandatory before
any auto-merge or write-scope MCP connector is enabled. Worth adding as a config
(`config/environment.yaml` already exists as a natural home) before any auto-merge capability is
built.

### 6. No token budget / kill switch per workflow
`llm/cost_tracker.py` tracks cost, but there's no per-loop budget file or documented pause
criteria (the framework's `loop-budget.md` + "no kill switch" anti-pattern). If item #3 is
pursued, pair each scheduled pattern with a budget cap and an explicit stop condition.

### 7. No run log for autonomous actions
If any unattended loop is added, keep an append-only `loop-run-log.md` (or equivalent) separate
from application logs — the framework flags "no run log" as a top anti-pattern because it makes
past autonomous decisions unauditable. Our `logs/` directory is app-level logging, not a
loop-decision audit trail.

## Bottom line

The two biggest wins if we ever move from "agent runs when I invoke it" to "agent runs on a
schedule" are: (1) a hard, low attempt cap with human escalation instead of the current
interactive-friendly cap, and (2) adopting the L1→L2→L3 phased rollout instead of granting
write/merge access on day one. Everything else (sub-agent separation, MCP tools, worktrees, cost
tracking) is already structurally in place and just needs the scheduling layer wrapped around it
carefully.
