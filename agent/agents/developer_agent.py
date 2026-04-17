from typing import Dict, Any, Optional, List
import os
import re
from agent.agents.base_agent import AgentRole

# Fenced shell blocks: ```shell / ```bash / ```sh / ```cmd / ```powershell
_SHELL_BLOCK_RE = re.compile(
    r'```(?:shell|bash|sh|cmd|powershell|ps1)\n(.*?)```',
    re.DOTALL | re.IGNORECASE,
)

# Standalone inline backtick commands on their own line, prefixed with a known
# CLI tool.  This catches lines like:  `npm run start`  or  `python app.py`
# but NOT code references like `variable_name`.
_INLINE_CMD_RE = re.compile(
    r'^\s*`((?:npm|node|npx|python3?|pip3?|ts-node|cargo|make|cd |git |yarn|pnpm|'
    r'uvicorn|flask|pytest|sh |bash |pwsh|powershell)[^`\n]+)`\s*$',
    re.MULTILINE | re.IGNORECASE,
)

# EDIT: block — surgical multi-hunk patch format
#
#   EDIT: path/to/file.ext
#   <<<OLD
#   exact text to replace
#   ===
#   new replacement text
#   >>>
#
# The regex captures path, old_text, new_text for each hunk.
_EDIT_BLOCK_RE = re.compile(
    r'EDIT:\s*(?P<path>[^\n]+)\n'
    r'<<<OLD\n(?P<old_text>.*?)\n===\n(?P<new_text>.*?)\n>>>',
    re.DOTALL,
)

# REPLACE: block — line-number-based patch (no old-text matching required).
#
#   REPLACE: path/to/file.ext 45-47
#   <<<
#   replacement line 1
#   replacement line 2
#   >>>
#
# Line numbers are 1-indexed and inclusive.  The system reads the current
# file, splices in the new lines, and writes the result back — eliminating
# the "old text not found" failure mode of EDIT: blocks.
_REPLACE_BLOCK_RE = re.compile(
    r'REPLACE:\s*(?P<path>\S+)\s+(?P<start>\d+)-(?P<end>\d+)\n'
    r'<<<\n(?P<new_text>.*?)\n>>>',
    re.DOTALL,
)

MAX_FIX_ITERATIONS = int(os.getenv("MAX_FIX_ITERATIONS", "50"))


def _format_file_with_lines(content: str, path: str, max_chars: int = 3000) -> str:
    """Return a numbered-line view of *content* suitable for anchor-and-patch prompts."""
    lines = content.splitlines()
    numbered = "\n".join(f"{i + 1:4}: {line}" for i, line in enumerate(lines))
    header = f"=== {path} ({len(lines)} lines) ==="
    full = header + "\n" + numbered
    if len(full) > max_chars:
        truncated = full[:max_chars]
        cut = truncated.rfind("\n")
        shown = truncated[:cut].count("\n") + 1
        full = full[:cut] + f"\n   … ({len(lines) - shown} more lines omitted)"
    return full


def _is_readonly_probe(entry: str) -> bool:
    """True when a shell entry is a file-read probe (type/cat/dir/ls) — not a real build failure."""
    first = entry.splitlines()[0] if entry else ""
    return bool(re.match(r'^\$\s+(type|cat|dir|ls|head|tail)\b', first, re.IGNORECASE))


_MISSING_TOOL_NAMES = (
    "jest", "webpack", "ts-node", "tsc", "mocha", "vitest", "eslint", "prettier",
)

def _looks_like_npm_missing(error_text: str) -> bool:
    """Return True if error output suggests missing npm dependencies / node_modules."""
    lower = error_text.lower()
    return (
        "cannot find module" in lower
        or "module not found" in lower
        or ("webpack" in lower and "not found" in lower)
        or ("webpack" in lower and "command not found" in lower)
        or "sh: webpack" in lower
        or ("error: cannot find" in lower and "module" in lower)
        or any(f"{t}: command not found" in lower for t in _MISSING_TOOL_NAMES)
        or any(f"'{t}' is not recognized" in lower for t in _MISSING_TOOL_NAMES)
        or any(f'"{t}" is not recognized' in lower for t in _MISSING_TOOL_NAMES)
    )


def _npm_install_cmd(verify_cmd: str) -> str:
    """Return an npm install command, preserving any leading 'cd X &&' prefix."""
    m = re.match(r'^(cd\s+\S+\s*&&\s*)', verify_cmd, re.IGNORECASE)
    return (m.group(1) + "npm install") if m else "npm install"

# Maximum characters of error output sent to the LLM per iteration.
# TypeScript / webpack errors repeat the same stack endlessly — cap them
# so we don't blow up the context window on iteration 3+.
_MAX_ERROR_CHARS = 4000

# Regex to extract source file paths from compiler / runtime error messages.
# Matches patterns like:  src/foo/bar.ts:10:5  or  ./src/foo/bar.tsx
_ERROR_FILE_RE = re.compile(
    r'(?:^|[\s(\'"])(?:\.[\\/])?(?P<path>(?:src|lib|app|dist)[/\\][\w./\\-]+\.(?:ts|tsx|js|jsx|py|java|go|rs))',
    re.MULTILINE | re.IGNORECASE,
)
# Compiled/generated output directories — never try to read or patch these.
# Override via SKIP_PATH_PREFIXES env var as a comma-separated list.
_SKIP_PATH_PREFIXES: tuple[str, ...] = tuple(
    p.strip() for p in os.getenv(
        "SKIP_PATH_PREFIXES", "dist/,build/,node_modules/,.cache/"
    ).split(",") if p.strip()
)
_MAX_FIX_FILE_CONTEXT = 8000   # total chars of source included in fix prompts
_MAX_FIX_FILE_PER_FILE = 3000  # chars per individual file

# Maximum number of fix-attempt prose blocks accumulated into the response
# string.  Older blocks are replaced with a placeholder to keep the string
# from growing unboundedly across 10 iterations.
_MAX_RESPONSE_HISTORY = 3

# Screenshot is triggered only when the task explicitly requests a browser capture.
_SCREENSHOT_RE = re.compile(
    r'\b(take\s+a?\s*screenshot|capture\s+(?:a\s+)?screenshot|screenshot\s+of)\b',
    re.IGNORECASE,
)

# Detect "run/debug/fix/launch" intent in the task description.
_RUN_DEBUG_INTENT_RE = re.compile(
    r'\b(run|debug|launch|fix\s+the\s+error|fix\s+errors?|there\s+are\s+(?:still\s+)?errors?'
    r'|start\s+(?!script\b|command\b|the\s+script\b))\b',
    re.IGNORECASE,
)

# Detect whether an actual app-run command appears in shell output lines
# (each line starts with "$ <cmd>" after our formatting).
_APP_RUN_CMD_RE = re.compile(
    r'^\$\s+(?:npm\s+(?:start|run\b)|node\s+\S|python3?\s+\S|cargo\s+run|uvicorn\b|flask\s+run|yarn\s+start|npx\s+\S)',
    re.IGNORECASE | re.MULTILINE,
)


class DeveloperRole(AgentRole):
    def __init__(self, file_system_tool=None, shell_tool=None, browser_tool=None):
        super().__init__(
            name="developer",
            description="Implements code based on specifications and requirements",
        )
        self.file_system_tool = file_system_tool
        self.shell_tool = shell_tool
        self.browser_tool = browser_tool
    
    def get_system_prompt(self) -> str:
        return """You are an expert coding assistant. You help users
with coding tasks by reading files, executing commands,
editing code, and writing new files.

Available tools:
- bash: Execute bash commands
- write: Create or overwrite an entire file (use FILE: format)
- edit: Make surgical edits to specific regions of a file (use EDIT: format)
- find_files: Find files by glob pattern (preferred over shell find)
- grep_code: Search file contents by regex (preferred over shell grep)

Format for running commands (use ```shell blocks):
```shell
npm install
npm run build
```

Format for creating or completely rewriting a file:
FILE: path/to/file.ext
```language
entire file content here
```

Format for line-number replacements (most reliable when file is shown with line numbers):
REPLACE: path/to/file.ext 45-47
<<<
  replacement line 1
  replacement line 2
>>>

Where 45-47 are the 1-indexed line numbers from the numbered file view in your context.
Use REPLACE: whenever line numbers are available — it never fails on old-text mismatch.

Format for surgical edits (fallback when no line numbers are available):
EDIT: path/to/file.ext
<<<OLD
exact text to replace (must match the file exactly, be as minimal as possible)
===
new replacement text
>>>

You may have multiple REPLACE: or EDIT: blocks for the same or different files.

Guidelines:
- Prefer REPLACE: over EDIT: in fix loops — use the line numbers shown in the file context.
- Prefer EDIT: over FILE: for bug fixes when line numbers are unavailable.
- Use FILE: only for new files or when rewriting more than 60% of a file.
- Use find_files and grep_code instead of shell find/grep — they work cross-platform.
- Be concise in your responses."""

    async def _run_shell_blocks(
        self, response: str, tool_executor
    ) -> tuple[list[str], list[str]]:
        """Execute all fenced shell blocks and standalone inline commands in *response*.

        Returns (all_outputs, failed_outputs) where failed_outputs is non-empty
        when any command exited with a non-zero return code.
        """
        all_outputs: list[str] = []
        failed_outputs: list[str] = []

        # Collect commands: fenced blocks first, then standalone inline backticks
        commands: list[str] = []
        for block in _SHELL_BLOCK_RE.finditer(response):
            for line in block.group(1).strip().splitlines():
                cmd = line.strip()
                if cmd and not cmd.startswith("#"):
                    commands.append(cmd)

        for match in _INLINE_CMD_RE.finditer(response):
            cmd = match.group(1).strip()
            if cmd and cmd not in commands:
                commands.append(cmd)

        for cmd in commands:
            try:
                out = await tool_executor.execute("shell", {"command": cmd})
                entry = f"$ {cmd}\n{out}"
                all_outputs.append(entry)
                self.logger.info("shell_executed", cmd=cmd)
                # Detect failure by return-code marker the shell tool emits,
                # falling back to keyword heuristics only for legacy output.
                if (
                    "returncode: 1" in out
                    or "exit code: 1" in out
                    or "Command failed" in out
                    or "FAILED" in out
                    or (
                        "error" in out.lower()[:300]
                        and "errors: 0" not in out.lower()
                        and "0 errors" not in out.lower()
                    )
                ):
                    failed_outputs.append(entry)
            except Exception as e:
                entry = f"$ {cmd}\nError: {e}"
                all_outputs.append(entry)
                failed_outputs.append(entry)
                self.logger.error("shell_failed", cmd=cmd, error=str(e))

        return all_outputs, failed_outputs

    def _extract_file_writes(self, response: str) -> List[tuple]:
        pattern = r'FILE:\s*(.+?)\n```\w*\n(.*?)```'
        matches = re.findall(pattern, response, re.DOTALL)
        return [(path.strip(), content.strip()) for path, content in matches]

    def _extract_file_edits(self, response: str) -> List[tuple]:
        """Extract EDIT: blocks → [(path, old_text, new_text), ...]."""
        results = []
        for m in _EDIT_BLOCK_RE.finditer(response):
            path = m.group("path").strip()
            old_text = m.group("old_text")
            new_text = m.group("new_text")
            results.append((path, old_text, new_text))
        return results

    def _extract_line_replacements(self, response: str) -> List[tuple]:
        """Extract REPLACE: blocks → [(path, start, end, new_text), ...]."""
        results = []
        for m in _REPLACE_BLOCK_RE.finditer(response):
            path = m.group("path").strip()
            start = int(m.group("start"))
            end = int(m.group("end"))
            new_text = m.group("new_text")
            results.append((path, start, end, new_text))
        return results

    async def _apply_line_replacement(
        self, tool_executor, path: str, start: int, end: int, new_text: str
    ) -> bool:
        """Splice new_text into *path* at 1-indexed lines start–end (inclusive).

        Reads the current file, replaces the line range, writes back.
        Returns True on success, False if read or write fails.
        """
        content = await tool_executor.execute("file_read", {"path": path})
        if content.startswith("Error"):
            self.logger.warning("replace_read_failed", path=path)
            return False
        lines = content.splitlines(keepends=True)
        for i, line in enumerate(lines):
            if not line.endswith("\n"):
                lines[i] = line + "\n"
        s = max(0, start - 1)
        e = min(len(lines), end)
        replacement = new_text.splitlines(keepends=True)
        for i, line in enumerate(replacement):
            if not line.endswith("\n"):
                replacement[i] = line + "\n"
        updated = "".join(lines[:s] + replacement + lines[e:])
        result = await tool_executor.execute("file_write", {"path": path, "content": updated})
        return not (isinstance(result, str) and result.startswith("Error"))

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        task = context.get("task", "")
        architecture = context.get("architecture", "")
        files_created = []
        model_router = context.get("model_router")
        tool_executor = context.get("tool_executor")
        on_phase = context.get("on_phase")

        if not model_router:
            return {"success": False, "error": "model_router not available"}

        enriched_context = context.get("enriched_context", "")
        # Order: static system prompt → enriched context (env, skills, history) → task (dynamic)
        # Putting the task last mirrors standard chat format and lets any caching layer
        # reuse the static prefix across calls.
        prompt = f"""{architecture if architecture else ''}{enriched_context}

## Current Task
{task}

Implement the solution. Write actual files using the EXACT format (path on the SAME line as FILE:):
FILE: path/to/file.ext
```language
file content here
```

When finished, end your response with:
## DONE
Files created: <comma-separated list, or "none">
Summary: <one sentence>
"""

        model = model_router.get_model("coding")
        if not model:
            return {"success": False, "error": "No coding model configured"}

        response = await model_router.generate(prompt, model, system_prompt=self.get_system_prompt())

        if tool_executor:
            file_writes = self._extract_file_writes(response)
            for file_path, content in file_writes:
                try:
                    await tool_executor.execute("file_write", {"path": file_path, "content": content})
                    verify = await tool_executor.execute("file_read", {"path": file_path})
                    if not verify.startswith("Error"):
                        files_created.append(file_path)
                        self.logger.info("file_written", path=file_path, size=len(content))
                    else:
                        self.logger.warning("file_write_not_verified", path=file_path, verify=verify[:120])
                except Exception as e:
                    self.logger.error("file_write_failed", path=file_path, error=str(e))

        shell_outputs: list[str] = []
        failed_outputs: list[str] = []

        if tool_executor:
            shell_outputs, failed_outputs = await self._run_shell_blocks(response, tool_executor)

        # Write phase: the LLM read files but emitted no code changes and no real failures.
        # Feed the shell output back as context and ask for EDIT:/FILE: blocks.
        # Only fire when shell_outputs actually contain file reads (type/cat) — not just
        # build failures — so we don't pass empty context to the write-phase model call.
        _write_phase_real_failures = [e for e in failed_outputs if not _is_readonly_probe(e)]
        _has_file_reads = any(
            re.match(r'^\$\s+(type|cat)\s+\S', e.splitlines()[0] if e else "", re.IGNORECASE)
            for e in shell_outputs
        )
        if (
            tool_executor
            and not files_created
            and not _write_phase_real_failures
            and shell_outputs
            and _has_file_reads
            and not self._extract_file_edits(response)
            and not self._extract_file_writes(response)
        ):
            read_context = "\n\n".join(shell_outputs[:5])
            write_prompt = (
                f"Task: {task}\n\n"
                f"You have read these files:\n\n{read_context[:8000]}\n\n"
                f"Now write the actual code changes using EDIT: blocks (preferred) or FILE: blocks.\n"
                f"Do NOT run any commands — only output code fixes."
            )
            write_response = await model_router.generate(
                write_prompt, model, system_prompt=self.get_system_prompt()
            )
            response += "\n\n**Write phase:**\n" + write_response

            # Apply FILE: full writes from write phase.
            for file_path, content in self._extract_file_writes(write_response):
                try:
                    await tool_executor.execute("file_write", {"path": file_path, "content": content})
                    verify = await tool_executor.execute("file_read", {"path": file_path})
                    if not verify.startswith("Error") and file_path not in files_created:
                        files_created.append(file_path)
                except Exception as e:
                    self.logger.error("write_phase_file_write_failed", path=file_path, error=str(e))

            # Apply EDIT: hunks from write phase.
            wp_edits_by_path: dict[str, list[dict]] = {}
            for file_path, old_text, new_text in self._extract_file_edits(write_response):
                wp_edits_by_path.setdefault(file_path, []).append(
                    {"old_text": old_text, "new_text": new_text}
                )
            for file_path, hunks in wp_edits_by_path.items():
                try:
                    edit_result = await tool_executor.execute(
                        "file_edit", {"path": file_path, "edits": hunks}, on_phase=on_phase
                    )
                    if not edit_result.startswith("Edit failed") and not edit_result.startswith("Error"):
                        if file_path not in files_created:
                            files_created.append(file_path)
                except Exception as e:
                    self.logger.error("write_phase_edit_failed", path=file_path, error=str(e))

        # Forced-run step: if this is a run/debug task and the initial response
        # only explored the project (no actual app-run command was executed),
        # issue a second targeted call that explicitly runs the app.
        # Read-only probe failures (type/cat/dir on wrong paths) don't count as
        # real failures — they shouldn't block the force-run path.
        real_failures = [e for e in failed_outputs if not _is_readonly_probe(e)]
        is_run_debug = bool(_RUN_DEBUG_INTENT_RE.search(task))
        ran_app = bool(_APP_RUN_CMD_RE.search("\n".join(shell_outputs)))
        if is_run_debug and not ran_app and not real_failures and tool_executor:
            prior_output = "\n\n".join(shell_outputs) if shell_outputs else "(no prior commands run)"
            force_run_prompt = (
                f"Task: {task}\n\n"
                f"{enriched_context}\n\n"
                f"Project exploration so far:\n{prior_output}\n\n"
                f"IMPORTANT: You have explored the project but have NOT run it yet.\n"
                f"You MUST now actually launch the application using the correct command "
                f"(e.g. `npm start`, `npm run dev`, `node index.js`, `python app.py`).\n"
                f"Look at the package.json start script or main entry point shown above.\n"
                f"Output ONLY a fenced shell block that runs the app. Do NOT explore further."
            )
            force_run_response = await model_router.generate(force_run_prompt, model, system_prompt=self.get_system_prompt())

            # Write any files the LLM generated before running
            for file_path, content in self._extract_file_writes(force_run_response):
                try:
                    await tool_executor.execute("file_write", {"path": file_path, "content": content})
                    verify = await tool_executor.execute("file_read", {"path": file_path})
                    if not verify.startswith("Error") and file_path not in files_created:
                        files_created.append(file_path)
                    elif verify.startswith("Error"):
                        self.logger.warning("force_run_file_write_not_verified", path=file_path)
                except Exception as e:
                    self.logger.error("force_run_file_write_failed", path=file_path, error=str(e))

            run_outputs, run_failures = await self._run_shell_blocks(force_run_response, tool_executor)
            shell_outputs.extend(run_outputs)
            response += "\n\n**Run attempt:**\n" + force_run_response
            if run_failures:
                failed_outputs = run_failures

        # Fix-and-rerun loop: if any commands failed, ask the LLM to fix the
        # code and re-run, up to MAX_FIX_ITERATIONS times.
        #
        # The harness owns verification — we extract the original failing command
        # and re-run it explicitly after each fix, regardless of what shell blocks
        # the LLM includes in its fix response. This prevents the loop from
        # exiting early because the LLM forgot to re-emit the build command.
        # Recompute real_failures after the force-run block may have updated failed_outputs.
        real_failures = [e for e in failed_outputs if not _is_readonly_probe(e)]
        if real_failures and tool_executor:
            # Extract the verify command from the first real (non-probe) failure.
            verify_cmd: str | None = None
            first_fail = real_failures[0]
            first_line = first_fail.splitlines()[0] if first_fail else ""
            if first_line.startswith("$ "):
                verify_cmd = first_line[2:].strip()

            files_fixed_history: list[str] = []
            # Track how many fix-attempt blocks have been appended to response.
            fix_attempt_blocks: int = 0
            # Ensure npm install runs at most once per fix session.
            _ran_npm_install: bool = False

            for _attempt in range(MAX_FIX_ITERATIONS):
                if on_phase:
                    try:
                        on_phase(f"fixing:attempt:{_attempt + 1}")
                    except Exception:
                        pass
                
                # Trim error text: TypeScript / webpack errors can be thousands of
                # repeated lines.  Keep the tail (most recent errors) not the head.
                raw_errors = "\n\n".join(failed_outputs)
                if len(raw_errors) > _MAX_ERROR_CHARS:
                    raw_errors = "…(truncated)…\n" + raw_errors[-_MAX_ERROR_CHARS:]

                history_note = (
                    f"\nFiles already modified in prior fix attempts: {', '.join(files_fixed_history)}\n"
                    if files_fixed_history
                    else ""
                )

                # Read the source files referenced in the error output so the
                # LLM has exact content to write EDIT: old_text against.
                file_context = ""
                if tool_executor:
                    seen_paths: list[str] = []
                    for m in _ERROR_FILE_RE.finditer(raw_errors):
                        fp = m.group("path").replace("\\", "/")
                        if not any(fp.startswith(p) for p in _SKIP_PATH_PREFIXES) and fp not in seen_paths:
                            seen_paths.append(fp)
                    # Fallback: extract paths from prior "type"/"cat" shell commands
                    # when the error output itself contains no source file references.
                    if not seen_paths:
                        for entry in shell_outputs:
                            first = entry.splitlines()[0] if entry else ""
                            m2 = re.match(r'^\$\s+(?:type|cat)\s+(.+)', first, re.IGNORECASE)
                            if m2:
                                fp = m2.group(1).strip().replace("\\", "/")
                                if not any(fp.startswith(p) for p in _SKIP_PATH_PREFIXES) and fp not in seen_paths:
                                    seen_paths.append(fp)
                    # Fallback: read known entry-point files when paths still empty.
                    if not seen_paths:
                        for entry_file in ("package.json", "src/main.ts", "src/index.ts", "src/main.py", "src/app.py"):
                            seen_paths.append(entry_file)
                    # Also include any files already touched in prior iterations.
                    for fp in files_fixed_history:
                        if fp not in seen_paths:
                            seen_paths.append(fp)
                    parts: list[str] = []
                    total_chars = 0
                    for fp in seen_paths[:8]:  # cap at 8 files
                        if total_chars >= _MAX_FIX_FILE_CONTEXT:
                            break
                        content = await tool_executor.execute("file_read", {"path": fp})
                        if content.startswith("Error"):
                            continue
                        # Use numbered-line format so the model can reference exact
                        # line numbers in REPLACE: blocks — no old-text matching needed.
                        snippet = _format_file_with_lines(content, fp, _MAX_FIX_FILE_PER_FILE)
                        parts.append(snippet)
                        total_chars += len(snippet)
                    if parts:
                        file_context = "Current source files (with line numbers):\n\n" + "\n\n".join(parts) + "\n\n"

                # If we have no file context AND haven't touched any files yet, the model
                # has no basis for generating EDIT: blocks — abort early rather than wasting
                # a model call that will produce empty output.
                if not file_context and not files_fixed_history:
                    response += (
                        "\n\n*(Fix loop aborted: could not locate source files to provide as "
                        "context. Check that the workspace path is correct and that error "
                        "messages reference valid source file paths.)*"
                    )
                    self.logger.info("fix_loop_no_file_context", attempt=_attempt + 1)
                    break

                fix_prompt = (
                    f"Original task: {task}\n\n"
                    f"{file_context}"
                    f"The following commands are still failing (attempt {_attempt + 1}/{MAX_FIX_ITERATIONS}).\n"
                    f"{history_note}"
                    f"Errors:\n\n"
                    f"```\n{raw_errors}\n```\n\n"
                    f"Fix the source files. Use REPLACE: blocks — reference the exact line numbers "
                    f"shown in the file listing above:\n\n"
                    f"REPLACE: path/to/file.ext 45-47\n"
                    f"<<<\n"
                    f"  replacement lines here\n"
                    f">>>\n\n"
                    f"For new files or large rewrites use FILE: blocks. "
                    f"Do NOT include shell blocks — the system re-runs the build automatically.\n"
                    f"Fix ALL errors shown above, not just the first one."
                )
                model = model_router.get_model("coding")
                fix_response = await model_router.generate(fix_prompt, model, system_prompt=self.get_system_prompt())

                iteration_files: list[str] = []

                # 1. Apply REPLACE: blocks first — line-number based, never fails on old-text mismatch.
                for file_path, start, end, new_text in self._extract_line_replacements(fix_response):
                    try:
                        ok = await self._apply_line_replacement(tool_executor, file_path, start, end, new_text)
                        if ok:
                            if file_path not in files_created:
                                files_created.append(file_path)
                            iteration_files.append(file_path)
                            self.logger.info("replace_applied", path=file_path, lines=f"{start}-{end}")
                        else:
                            self.logger.warning("replace_failed", path=file_path, lines=f"{start}-{end}")
                    except Exception as e:
                        self.logger.error("replace_error", path=file_path, error=str(e))

                # 2. Apply EDIT: hunks (surgical old-text patches) for any files not yet touched.
                edits_by_path: dict[str, list[dict]] = {}
                for file_path, old_text, new_text in self._extract_file_edits(fix_response):
                    edits_by_path.setdefault(file_path, []).append(
                        {"old_text": old_text, "new_text": new_text}
                    )

                for file_path, hunks in edits_by_path.items():
                    try:
                        edit_result = await tool_executor.execute(
                            "file_edit",
                            {"path": file_path, "edits": hunks},
                            on_phase=on_phase,
                        )
                        if not edit_result.startswith("Edit failed") and not edit_result.startswith("Error"):
                            if file_path not in files_created:
                                files_created.append(file_path)
                            iteration_files.append(file_path)
                            self.logger.info("edit_applied", path=file_path, hunks=len(hunks))
                        else:
                            self.logger.warning("edit_rejected", path=file_path, detail=edit_result[:120])
                    except Exception as e:
                        self.logger.error("edit_failed", path=file_path, error=str(e))

                # Fallback: FILE: blocks for files not already patched via EDIT:.
                for file_path, content in self._extract_file_writes(fix_response):
                    if file_path in edits_by_path:
                        continue  # already handled by EDIT: path above
                    try:
                        await tool_executor.execute("file_write", {"path": file_path, "content": content})
                        verify = await tool_executor.execute("file_read", {"path": file_path})
                        if not verify.startswith("Error"):
                            if file_path not in files_created:
                                files_created.append(file_path)
                            iteration_files.append(file_path)
                        else:
                            self.logger.warning("fix_file_write_not_verified", path=file_path)
                    except Exception as e:
                        self.logger.error("fix_file_write_failed", path=file_path, error=str(e))

                files_fixed_history.extend(f for f in iteration_files if f not in files_fixed_history)
                made_progress = len(iteration_files) > 0

                # If package.json was just edited, run npm install before verifying
                # so newly added devDependencies are actually available.
                if made_progress and any(
                    f.lower().endswith("package.json") for f in iteration_files
                ) and tool_executor:
                    install_cmd = _npm_install_cmd(verify_cmd or "npm test")
                    install_out = await tool_executor.execute("shell", {"command": install_cmd})
                    shell_outputs.append(f"$ {install_cmd}\n{install_out}")
                    self.logger.info("npm_install_after_package_json_edit", attempt=_attempt + 1)
                    _ran_npm_install = True

                # Cap accumulated fix-attempt prose to avoid unbounded growth.
                # Once we hit the limit, replace the oldest block with a summary.
                if fix_attempt_blocks < _MAX_RESPONSE_HISTORY:
                    response += f"\n\n**Fix attempt {_attempt + 1}:**\n" + fix_response
                    fix_attempt_blocks += 1
                else:
                    # Drop the oldest block by rewriting from the N-th marker.
                    marker = "\n\n**Fix attempt "
                    # Find the first fix-attempt marker and remove up to the second.
                    first = response.find(marker)
                    second = response.find(marker, first + 1) if first != -1 else -1
                    if second != -1:
                        response = (
                            response[:first]
                            + f"\n\n*(earlier fix attempts omitted)*"
                            + response[second:]
                        )
                    response += f"\n\n**Fix attempt {_attempt + 1}:**\n" + fix_response

                # Always re-run the original failing command to verify — do not
                # rely on the LLM including a shell block in its fix response.
                if verify_cmd and tool_executor:
                    try:
                        # Auto-install npm deps if the error indicates missing
                        # node_modules (e.g. "Cannot find module 'webpack'").
                        # Runs once per fix session to avoid repeated installs.
                        if not _ran_npm_install and _looks_like_npm_missing(raw_errors):
                            install_cmd = _npm_install_cmd(verify_cmd)
                            install_out = await tool_executor.execute("shell", {"command": install_cmd})
                            shell_outputs.append(f"$ {install_cmd}\n{install_out}")
                            self.logger.info("npm_auto_install", cmd=install_cmd, attempt=_attempt + 1)
                            _ran_npm_install = True
                            made_progress = True
                            
                        if not made_progress:
                            response += f"\n\n*(Fix loop aborted: The model did not modify any files to address the failure)*"
                            self.logger.info("fix_loop_aborted_no_progress", attempt=_attempt + 1)
                            break

                        verify_out = await tool_executor.execute("shell", {"command": verify_cmd})
                        verify_entry = f"$ {verify_cmd}\n{verify_out}"
                        shell_outputs.append(verify_entry)
                        self.logger.info("fix_verify_run", attempt=_attempt + 1, cmd=verify_cmd)

                        is_failure = (
                            "returncode: 1" in verify_out
                            or "exit code: 1" in verify_out
                            or "Command failed" in verify_out
                            or "FAILED" in verify_out
                            or (
                                "error" in verify_out.lower()[:300]
                                and "errors: 0" not in verify_out.lower()
                                and "0 errors" not in verify_out.lower()
                            )
                        )
                        if is_failure:
                            failed_outputs = [verify_entry]
                        else:
                            failed_outputs = []
                            break
                    except Exception as e:
                        self.logger.error("fix_verify_failed", error=str(e))
                        break
                else:
                    # No verify command available — fall back to running any
                    # shell blocks the LLM included (legacy path).
                    new_outputs, new_failures = await self._run_shell_blocks(fix_response, tool_executor)
                    shell_outputs.extend(new_outputs)
                    if not new_failures:
                        failed_outputs = []
                        break
                    failed_outputs = new_failures

        if shell_outputs:
            combined = "\n\n".join(shell_outputs)
            response += f"\n\n**Shell Output:**\n```\n{combined}\n```"

        screenshot_path = None
        if tool_executor and _SCREENSHOT_RE.search(task):
            try:
                screenshot_path = await tool_executor.execute("screenshot", {})
                response += f"\n\nScreenshot captured: {screenshot_path}"
            except Exception as e:
                self.logger.error("screenshot_failed", error=str(e))

        # Extract structured DONE block if the model emitted one.
        # Provides a clean one-line summary for Discord / job store.
        completion_summary = ""
        done_match = re.search(
            r"##\s*DONE\s*\n(?:Files created:\s*(.+?)\n)?Summary:\s*(.+)",
            response,
            re.IGNORECASE | re.DOTALL,
        )
        if done_match:
            done_files_line = (done_match.group(1) or "").strip()
            completion_summary = (done_match.group(2) or "").strip().splitlines()[0]
            # Merge any files listed in the DONE block that weren't caught by
            # the FILE: regex (e.g. files the model mentioned but wrote inline).
            if done_files_line and done_files_line.lower() not in ("none", ""):
                for f in re.split(r",\s*", done_files_line):
                    f = f.strip()
                    if f and f not in files_created:
                        files_created.append(f)

        return {
            "success": True,
            "role": self.name,
            "response": response,
            "task": task,
            "files_created": files_created,
            "shell_output": shell_outputs,
            "screenshot": screenshot_path,
            "completion_summary": completion_summary,
        }


class DeveloperAgent:
    def __init__(self, model_router, tools=None, file_system_tool=None, shell_tool=None, browser_tool=None):
        from agent.agents.base_agent import BaseAgent
        role = DeveloperRole(file_system_tool, shell_tool, browser_tool)
        self.base = BaseAgent(role, model_router, tools)

    async def run(self, task: str, context: Dict[str, Any] = None):
        if context is None:
            context = {}
        return await self.base.run(task, context)