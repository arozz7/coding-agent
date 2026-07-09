# Phase 48 — Fix CI: Windows-runner-specific failures + missing deps

Phase 47 fixed the lint gate and got CodeQL green, but CI still failed on
GitHub's `windows-latest` runner with two failures that never reproduced
locally — both because the local dev machine's environment happened to mask
them.

## Missing dependencies (`ModuleNotFoundError: No module named 'pdfplumber'`)

`agent/tools/document_tool.py` uses `pdfplumber` (PDF text extraction),
`python-docx` (`.docx` reading), and `openpyxl` (`.xlsx` reading) — none were
declared in `pyproject.toml`'s dependencies. They were never caught locally
because these packages happened to already be installed in the ambient dev
environment (conda base env with lots of prior installs) — a fresh CI
install has none of them. `pdfplumber` broke tests outright (`test_pdf_fetch.py`
patches `pdfplumber.open`, which requires the module to exist even to be
patched); `python-docx`/`openpyxl` degrade gracefully at runtime (return an
error dict) and have no test coverage, so they didn't fail CI, but the
`.docx`/`.xlsx` reading features were silently non-functional on any clean
install. Ran a full source-tree import audit (ast-based, not grep) to catch
all three at once rather than fixing one and missing the others. Added all
three to `[project.dependencies]`.

## Real bug: `ShellExecutor._detect_shell()` returns a path, not a name

```python
def _detect_shell(self) -> str:
    if system == "Windows":
        return shutil.which("pwsh") or "powershell"   # returns a PATH, not "pwsh"
```

`shutil.which("pwsh")` returns the resolved executable path (e.g.
`C:\Program Files\PowerShell\7\pwsh.EXE`), not the bare string `"pwsh"`. But
every caller compares `self._shell` against the literal `"pwsh"`:

```python
if self.is_windows() and shell in ("powershell", "pwsh"):
    command = command.replace("&&", ";")
```

On any machine where `pwsh` actually resolves via `shutil.which` (the CI
runner has PowerShell 7 installed; most of the fleet likely does too), this
comparison is silently always `False` — the `&&` → `;` command-chaining
translation needed for PowerShell never fires, so a compound command that
works in `cmd.exe` syntax (`a && b`) would run as a literal `&&` in
PowerShell 7, which doesn't understand it as a chain operator the same way.
This never reproduced on the local dev machine only because `pwsh` isn't on
that machine's PATH, so `_detect_shell()` happened to fall back to the
literal string `"powershell"` there. Fixed: `_detect_shell()` now always
returns the bare command name.

## Verification

- `ruff check agent api llm mcp observability` → 0 errors.
- `python -m pytest tests -q` → 567 passed, run locally with `pdfplumber`,
  `python-docx`, and `openpyxl` installed to actually exercise the
  previously-broken `test_pdf_fetch.py` path rather than trust CI blindly.
