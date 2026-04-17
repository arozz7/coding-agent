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

MAX_FIX_ITERATIONS = int(os.getenv("MAX_FIX_ITERATIONS", "50"))


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
    )


def _npm_install_cmd(verify_cmd: str) -> str:
    """Return an npm install command, preserving any leading 'cd X &&' prefix."""
    m = re.match(r'^(cd\s+\S+\s*&&\s*)', verify_cmd, re.IGNORECASE)
    return (m.group(1) + "npm install") if m else "npm install"

# Maximum characters of error output sent to the LLM per iteration.
# TypeScript / webpack errors repeat the same stack endlessly — cap them
# so we don't blow up the context window on iteration 3+.
_MAX_ERROR_CHARS = 4000

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
    r'\b(run|debug|launch|start|fix\s+the\s+error|fix\s+errors?|there\s+are\s+(?:still\s+)?errors?)\b',
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

Format for surgical edits (preferred for fixes — changes only targeted regions):
EDIT: path/to/file.ext
<<<OLD
exact text to replace (must match the file exactly, be as minimal as possible)
===
new replacement text
>>>

You may have multiple EDIT: blocks for the same or different files.
Each EDIT: block is matched against the original file, not after earlier edits,
so do NOT make overlapping edits — merge nearby changes into one block.

Guidelines:
- Prefer EDIT: over FILE: for bug fixes — it is faster and produces a cleaner diff.
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
        """Extract EDIT: blocks from the response.

        Returns a list of (path, old_text, new_text) tuples.
        Multiple hunks for the same path are grouped by the caller.
        """
        results = []
        for m in _EDIT_BLOCK_RE.finditer(response):
            path = m.group("path").strip()
            old_text = m.group("old_text")
            new_text = m.group("new_text")
            results.append((path, old_text, new_text))
        return results

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

        # Forced-run step: if this is a run/debug task and the initial response
        # only explored the project (no actual app-run command was executed),
        # issue a second targeted call that explicitly runs the app.
        is_run_debug = bool(_RUN_DEBUG_INTENT_RE.search(task))
        ran_app = bool(_APP_RUN_CMD_RE.search("\n".join(shell_outputs)))
        if is_run_debug and not ran_app and not failed_outputs and tool_executor:
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
        if failed_outputs and tool_executor:
            # Extract the verify command from the first failed entry ("$ <cmd>\n...")
            verify_cmd: str | None = None
            first_fail = failed_outputs[0]
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
                fix_prompt = (
                    f"Original task: {task}\n\n"
                    f"The following commands are still failing (attempt {_attempt + 1}/{MAX_FIX_ITERATIONS}).\n"
                    f"{history_note}"
                    f"Fix the source files:\n\n"
                    f"```\n{raw_errors}\n```\n\n"
                    f"Use EDIT: blocks for surgical fixes (preferred):\n"
                    f"EDIT: path/to/file.ext\n"
                    f"<<<OLD\n"
                    f"exact text to replace\n"
                    f"===\n"
                    f"replacement text\n"
                    f">>>\n\n"
                    f"Or use FILE: blocks for full file rewrites.\n"
                    f"Do NOT include shell blocks — the system re-runs the build automatically.\n"
                    f"Fix ALL errors shown above, not just the first one."
                )
                model = model_router.get_model("coding")
                fix_response = await model_router.generate(fix_prompt, model, system_prompt=self.get_system_prompt())

                # Apply EDIT: hunks first (surgical patches), then fall back to FILE: full writes.
                iteration_files: list[str] = []

                # Group EDIT: hunks by file path.
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