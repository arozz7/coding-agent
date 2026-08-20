# Phase 64 — Fix context_window mismatch and multi-line shell-block splitting

## 1. context_window mismatch

`logs/api-20260819-201238.log` (`task_num 7`) failed with:

```
Ollama HTTP 400: request (93676 tokens) exceeds the available context size (65536 tokens)
```

`config/models.yaml` declared `context_window: 262144` for the two
`provider: turboquant` model entries (`Qwen3.8-27B-Q4_K_S`,
`Qwen3.6-27B-Q6_K`). `context_window` isn't cosmetic — `context_builder.py`
(`char_budget`, `check_budget`), `documenter_agent.py`, and `research_agent.py`
all read it directly to size prompt/history budgets. With a config value 4x
too large, the budgeting logic under-trimmed context and let prompts grow
past what the server could actually accept.

Checked `TurboQuantLoader/config.toml`:

```toml
context_size = 131072   # "Halved from 262144" (comment) — VRAM headroom
extra_flags  = [..., "--parallel", "2"]
```

`llama-server` splits `context_size` across parallel slots, so the real
per-request ceiling is `131072 / 2 = 65536` — exactly matching the error.
The `/health` endpoint reports `context_size: 131072` (the backend total,
not the per-request slot), which is why the user's own health check and the
error message disagreed at first glance — the health total needs dividing
by the parallel slot count to get the safe per-request budget our config
should use.

**Fix:** `config/models.yaml` — `context_window: 262144 → 65536` for the two
`provider: turboquant` entries only. LM Studio and OpenRouter entries were
untouched (unrelated backends; a first pass with `replace_all` briefly hit
them too — reverted before committing).

## 2. Multi-line shell-block splitting

Same log, ~01:45:56 — the model wrote a multi-line PowerShell block
(`$root = "..."`, `$log = "..."`, `Start-Process ...`, a `while` loop) inside
one fenced block. `DeveloperRole._run_shell_blocks` split every fenced block
into individual lines and ran each as an independent shell command — so
`$root = "J:\..."` was executed as if it were its own program, failing with
`WinError 2` (29 times in this burst). This is the **third** occurrence of
the same underlying flaw in different logs: a hallucinated `find_files`
burst, a multi-line `python -c "..."` heredoc that got torn apart mid-quote,
and now a PowerShell script.

**Fix**, in `agent/agents/output_blocks.py`:

- `SHELL_BLOCK_RE` now captures the fence language (`lang` named group)
  alongside the content, so callers can tell `powershell`/`ps1` apart from
  `shell`/`bash`/`sh`/`cmd`.
- `is_powershell_script(language, content)` — true when the fence is
  explicitly `powershell`/`ps1`, or the content contains PowerShell
  variable-assignment (`$x = ...`) or control-flow (`if`/`while`/`for`/
  `foreach`/`function`/`try`/`switch`) syntax — signs of cross-line state
  that can't be split into independent commands.
- `split_shell_block(content)` — quote-aware line join for the remaining
  (non-script) case: merges lines while a quote opened on an earlier line
  is still unclosed, so a multi-line quoted argument (`python -c "\n...\n"`)
  stays one command instead of being torn apart at every newline. Plain
  command sequences (`npm install` / `npm run build`) split exactly as
  before.

`agent/agents/developer_agent.py`'s `_run_shell_blocks`: for a block
`is_powershell_script` flags, the whole block is written to a temp
`.agent-tmp/ps-script-<uuid8>.ps1` via the existing `file_write` tool call,
then run as a single `powershell -NoProfile -ExecutionPolicy Bypass -File
<path>` command — one real PowerShell process instead of N broken
subprocess spawns. All other fenced blocks go through
`split_shell_block` instead of a raw `.splitlines()`.

Caught during implementation: the script path must **not** be quoted.
`shell_tool.py` resolves this command via `shlex.split(cmd, posix=False)` +
`subprocess.Popen(list, shell=False)`, which passes argv tokens to the
executable literally — there's no shell in this path to strip quote
characters, so a quoted path would have reached `-File` with the quote
marks baked into the argument. The relative path has no spaces (it's
workspace-relative), so it's unambiguous unquoted. A test asserts no `"`
appears in the constructed command.

## Verification

- `pytest tests/unit -q` — 502 passed (494 prior + 6 new in
  `test_output_blocks.py` + 2 new in `test_agents.py`).
- New tests reproduce the exact PowerShell block from the log and confirm
  it's written to a temp file and invoked as one unquoted `-File` command;
  a second test confirms plain command sequences still split per line
  exactly as before.
- `python -c "import yaml; ..."` confirms `config/models.yaml` parses and
  only the two `turboquant` entries changed (65536); all other models'
  `context_window` values are untouched.

## Files

- `config/models.yaml` — `context_window` corrected to 65536 for the two `provider: turboquant` entries
- `agent/agents/output_blocks.py` — `SHELL_BLOCK_RE` captures fence language; new `is_powershell_script`/`split_shell_block`
- `agent/agents/developer_agent.py` — `_run_shell_blocks` routes PowerShell scripts through a temp `.ps1` file
- `tests/unit/test_output_blocks.py` — new regression tests for both helpers
- `tests/unit/test_agents.py` — new `_run_shell_blocks` integration tests (script routing + plain-command regression)
