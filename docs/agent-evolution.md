# Agent Evolution

This document is the source of truth for *how the agent has grown over time* — not what it can generically do (see `capabilities.md`), but which specific agents, skills, and system improvements were added and why. It covers the deployed agent across all project types it runs on (coding, research, general-prompt).

Append a new row to the **Evolution Log** whenever a capability is added or a significant behavior change is made. Use `/log-capability` to do this consistently.

---

## Capability Registry

Current capabilities by type. The Evolution Log below shows when each was added.

### Runtime Agents (`agent/agents/`)

| Agent | Role |
|-------|------|
| `architect_agent` | High-level design and architecture decisions |
| `base_agent` | Shared base class; tools, prompt construction, LLM call |
| `chat_agent` | Conversational replies, Q&A |
| `developer_agent` | Code generation, file writes, fix rounds |
| `documenter_agent` | Documentation, changelogs, README updates |
| `mapper_agent` | Codebase mapping and dependency analysis |
| `plan_agent` | Plan/Build mode task decomposition |
| `planner_agent` | SDLC pipeline planning |
| `plan_reviewer_agent` | Critiques plans before execution |
| `red_team_agent` | Adversarial testing and security review |
| `research_agent` | Web search, local file analysis, synthesis |
| `reviewer_agent` | Code review and quality checks |
| `tester_agent` | Test generation and execution |
| `verifier_agent` | Scores task output against research and code rubrics |
| `acceptance_tester_agent` | Tests behavioral acceptance criteria post-task |

### Runtime Skills (`skills/`)

Invokable by the agent during task execution.

| Skill | Trigger Condition |
|-------|------------------|
| `architect-decision-engine` | Core system changes (DB, API, Auth) |
| `codebase-mapper` | Analyzing existing codebases |
| `handover` | End-of-session context bridge |
| `playwright-cli` | Browser automation, screenshots, scraping |
| `security-auditor` | Pre-PR or `.env` edits |
| `tdd-enforcer` | Feature implementations |
| `wiki-compile` | Compile wiki entries from research |
| `wiki-lint` | Validate wiki structure |
| `wiki-query` | Query the project wiki |
| `workspace-janitor` | Clutter identification and removal |

### Meta-Skills (`.claude/skills/` — Claude Code development workflow)

| Skill | Purpose |
|-------|---------|
| `handover` | Captures conversation state for cross-session handoff |
| `review-agent-run` | Reviews live run logs for oscillation bugs; implements fixes |
| `log-capability` | Appends a new entry to this file and commits it |

---

## Evolution Log

Append-only. One row per capability added or significant behavior change. Use `/log-capability` to add rows.

| Date | Type | Name | What It Does / Problem Solved | Triggered By |
|------|------|------|-------------------------------|--------------|
| 2026-04-10 | runtime-agent | `architect_agent`, `developer_agent`, `reviewer_agent`, `tester_agent`, `base_agent` | Initial four-role agent system: arch→dev→review→test pipeline | Initial MVP commit |
| 2026-04-10 | runtime-agent | `chat_agent`, `research_agent` | Conversational Q&A and iterative web+local research | Batch 6: multi-agent task routing |
| 2026-04-10 | runtime-skill | `architect-decision-engine`, `codebase-mapper`, `handover`, `playwright-cli`, `security-auditor`, `tdd-enforcer`, `wiki-compile`, `wiki-lint`, `wiki-query`, `workspace-janitor` | Initial skills system bundled with MCP server and RAG memory | Phase 10: Add MCP server, subagents, RAG memory, skills |
| 2026-04-11 | model-routing | OpenRouter 429 rate-limit fallback | On 429, fall back to local model immediately; no wasted retries | First OpenRouter integration showing rate-limit failures |
| 2026-04-12 | runtime-agent | `plan_agent` | Plan/Build mode task decomposition | Phase 11: Plan/Build mode |
| 2026-04-12 | runtime-agent | `planner_agent` | SDLC pipeline: full plan → build → test → review cycle | SDLC pipeline + agentic task manager |
| 2026-04-17 | runtime-agent | `documenter_agent`, `mapper_agent`, `plan_reviewer_agent`, `red_team_agent` | Documentation, codebase analysis, plan critique, adversarial testing | Phase 23: anchor-and-patch edits + new agent types |
| 2026-05-06 | runtime-agent | `verifier_agent` | Scores task output 0–10 against research quality and code quality rubrics | Phase 26: verification loop — runs were completing with no quality gate |
| 2026-05-09 | runtime-agent | `acceptance_tester_agent` | Tests behavioral acceptance criteria after task; launches app, takes screenshots | Phase 29: acceptance-test-driven fix loop |
| 2026-05-09 | orchestration | Criterion-driven fix loop | Verifier failures inject targeted fix tasks; loop continues until score threshold met | Phase 28: goal-driven planning — agent was completing tasks that didn't meet criteria |
| 2026-05-09 | orchestration | Acceptance-test-driven fix loop | Failed acceptance criteria inject fix tasks; loop continues until all pass | Phase 29: verifier passing but app not actually working |
| 2026-05-17 | model-routing | Per-model endpoint configuration | Configures per-model endpoint before each `generate()` call; while-loop retry slot fix | LM Studio endpoint mismatch causing silent routing failures |
| 2026-05-18 | model-routing | Dynamic free-model selection via OpenRouter `/v1/models` | Fetches live free-tier model list and picks best by context window; avoids hardcoded model names | Hardcoded evaluator model names going offline |
| 2026-05-18 | orchestration | APPEND blocks + Bayesian fix budgets + acceptance-loop skip | Documenter uses APPEND for safe multi-round edits; fix budget scales with verifier score; acceptance loop skipped when criteria already satisfied | Fix rounds corrupting existing files by overwriting instead of appending |
| 2026-05-21 | orchestration | Orchestrator modular split | Split 1607-line `orchestrator.py` into focused modules (task_loop, verifier_coordinator, etc.) | File hit hard 600-line limit; coupling made oscillation bugs hard to isolate |
| 2026-05-22 | orchestration | Misrouted auto-criteria fix | `file exists:`, `file contains:`, `command exits 0:` in acceptance_criteria routed to filesystem/shell evaluator instead of LLM | Live payment-tracker run: 4 acceptance fix loops on already-existing file; score 8→2→0 |
| 2026-05-22 | orchestration | Evidence-threading for behavioral criteria | Threads `combined_response` (all task outputs) into `AcceptanceTesterAgent`; provides 6 000-char excerpt when no screenshot is available | Same live run: behavioral criteria failed blindly with no evidence to evaluate against |
| 2026-05-22 | model-routing | OpenRouter 402 blacklist + immediate rotation | HTTP 402 (credits exhausted) raises distinct `_OpenRouterPaymentRequiredError`; model blacklisted, cache busted, next free model selected immediately | `deepseek/deepseek-v4-flash:free` exhausted mid-session; all LLM-evaluated criteria silently failed |
| 2026-05-23 | model-routing | 24 h TTL on evaluator blacklist | Blacklist entries expire after 86 400 s; exhausted models re-enter the free pool automatically the next day | 402 blacklist was permanent for the process lifetime; OpenRouter credits reset daily |
| 2026-05-25 | meta-skill | `review-agent-run` | Reviews live run logs for acceptance/criterion oscillation, diagnoses root causes, implements fixes, writes changelog | Recurring oscillation bugs in payment-tracker runs needed a repeatable diagnosis workflow |
| 2026-05-25 | meta-skill | `log-capability` | Appends a capability entry to this file and commits; keeps evolution log current as agent self-improves | Need to track agent self-improvement across all project types |
| 2026-05-25 | orchestration | `command-exits-0 prose-strip` | LLM was embedding '| End state:' and '| Constraint:' prose into command exits 0 criterion values causing the shell to pipe to a nonexistent program on every evaluation; fixed by stripping prose suffixes in evaluate_criteria and reframing the RequirementsExtractor prompt | api-20260525-193648.log — 4 acceptance_fix_injected loops, score 3→0 |
| 2026-05-25 | orchestration | `Bayesian-store poison reset` | Malformed command exits 0 criteria (phase-38 bug) recorded 53 failures poisoning command_exits_0 confidence to 0.018 and cutting the fix budget to 1; fixed by raising minimum attempt budget from 1 to 2 and resetting poisoned data | api-20260525-205156.log — loop abandoned npm run lint after 1 attempt, score stuck at 1/10 |
| 2026-05-26 | orchestration | `windows-cmd-exit-code-fix` | Fixed _check_single_criterion() in verifier_coordinator.py: bash '; echo __EXIT__$?' syntax never worked in cmd.exe on Windows; replaced with platform-aware '& echo __EXIT__%ERRORLEVEL%' sentinel eliminating all spurious command exits 0 failures | api-20260525-230750.log |
| 2026-06-30 | orchestration | `windows-shell-operator-passthrough fix` | Fixed ShellTool._resolve_args passing shell operators (&&, ||, 2>&1) as literal argv to real .exe tools (cargo, node) on Windows instead of routing through cmd.exe, which caused command-exits-0 criteria to fail unconditionally and the acceptance/criterion fix loops to oscillate forever | logs/api-20260628-141657.log |
| 2026-07-01 | model-routing | `openrouter-free-tier-circuit-breaker` | Added account-level circuit breaker that trips after repeated OpenRouter 429s within a 10-minute window and skips remote evaluator selection for 30 minutes, since the free tier shares one rate-limit bucket across all :free models and per-model blacklisting was cycling through IDs that all hit the same wall | logs/api-20260630-212838.log — 7/7 evaluator calls rate-limited on first attempt |
| 2026-07-01 | orchestration | `tauri-devurl-screenshot fix` | AppProbe now detects Tauri projects (src-tauri/), launches via npm run tauri dev, and reads the devUrl port from tauri.conf.json to screenshot the native webview's actual rendered content, since the native app visual criterion was always failing with zero screenshot evidence before this fix | logs/api-20260630-212838.log — visual criterion oscillated 3 fix rounds with screenshot:false every time |
| 2026-08-19 | model-routing | `reasoning-token-truncation fix` | Made max_tokens per-model configurable (was hardcoded 8192) and raised it for Qwen3.8-27B-Q4_K_S to 24576 with thinking left enabled instead of disabled, plus raised the developer role's generate timeout to 1500s, fixing a stall where every developer task failed with an empty-content reasoning-only response | logs/api-20260818-192257.log — 5 straight developer-role task failures, verify loop stuck 6 rounds over ~1h50m never converging |
| 2026-08-19 | orchestration | `file-append-prose-collision fix` | Bounded the FILE:/APPEND: block path regex to a single line ([^\n]+ instead of unbounded .+? under DOTALL) across output_blocks.py, architect_agent.py, mapper_agent.py, and tester_agent.py, since the model's own prose mentioning the marker word before the real block (e.g. "using an APPEND: block:") caused the regex to anchor on the prose and swallow the real marker line into a garbage path that failed the path-traversal guard, silently dropping the fix | logs/api-20260819-184755.log — 4+ criterion_fix_injected rounds on a trivial one-line append, 16 near-duplicate wiki entries showing this recurred across many prior runs |
| 2026-08-20 | config | `context-window-correction` | Corrected config/models.yaml's context_window from a stale 262144 to 65536 for the two turboquant-backed models, matching TurboQuantLoader's actual context_size=131072 split across --parallel 2 slots (131072/2=65536); context_builder/documenter_agent/research_agent all read this field to size prompt budgets, so the stale value let prompts grow up to 4x too large before the server rejected them | logs/api-20260819-201238.log — task_num 7 failed: "request (93676 tokens) exceeds the available context size (65536 tokens)" |
| 2026-08-20 | orchestration | `powershell-script-splitting fix` | Added is_powershell_script/split_shell_block to output_blocks.py so DeveloperRole._run_shell_blocks routes multi-statement PowerShell blocks (explicit fence or $var=/control-flow syntax) through a temp .ps1 file run as one process instead of splitting every fenced block by newline and running each line as an independent command; also fixes multi-line quoted arguments (python -c "...") getting torn apart at every newline | logs/api-20260819-201238.log — 29 WinError 2 spawn failures from one PowerShell block; third occurrence of the same line-splitting flaw across different logs |
