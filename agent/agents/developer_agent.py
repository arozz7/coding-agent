from typing import Dict, Any
import os
import re
import uuid
from agent.agents.base_agent import AgentRole
from agent.agents.fix_loop import is_readonly_probe as _is_readonly_probe, run_fix_loop
from agent.agents.output_blocks import (
    SHELL_BLOCK_RE as _SHELL_BLOCK_RE,
    INLINE_CMD_RE as _INLINE_CMD_RE,
    SKILL_BLOCK_RE as _SKILL_BLOCK_RE,
    SCRIPT_BLOCK_RE as _SCRIPT_BLOCK_RE,
    extract_file_writes as _extract_file_writes,
    extract_file_appends as _extract_file_appends,
    execute_append as _execute_append,
    extract_file_edits as _extract_file_edits,
    is_powershell_script as _is_powershell_script,
    split_shell_block as _split_shell_block,
)

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

# Developer generations write full files/diffs on top of a long thinking
# trace (see config/models.yaml max_tokens for Qwen3.8-27B-Q4_K_S) and can
# legitimately run past the model_router default of 600s. Give the developer
# role more wall-clock room than lighter-weight roles (classification,
# planning) so a slow-but-progressing generation doesn't get killed by
# ollama_hard_timeout and forced into a full task retry.
_DEVELOPER_TIMEOUT_SECS = 1500.0

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
- append: Add content to the END of an existing file without touching earlier content (use APPEND: format)
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

IMPORTANT: FILE: and APPEND: paths must ALWAYS be relative to the workspace root (e.g. `src/App.tsx`, `package.json`). Never use absolute paths like `C:\\Users\\...` or `J:\\Projects\\...` — the system resolves them from the workspace root automatically.

Format for appending to the END of an existing file (preserves all earlier content):
APPEND: path/to/file.ext
```language
new content to add at the very end
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
- ⚠️ If your task says "append" or "APPEND ONLY": use APPEND: blocks EXCLUSIVELY. Using FILE: on an append task will destroy all previously written content.
- Use find_files and grep_code instead of shell find/grep — they work cross-platform.
- Canvas parallax direction: background objects scroll LEFT (x decreases each frame) when the car drives rightward. Never reverse this.
- Be concise in your responses.

Code quality rules (apply to all code you write):
- Function names: verb-noun pattern (fetch_user, calculate_total, is_valid_email)
- Early returns: guard at the top, never nest more than 2 levels deep
- Named constants: UPPER_CASE for magic numbers/strings (MAX_RETRIES = 3, not if count > 3)
- One responsibility per function; keep functions under 50 lines
- No comments that restate the code — only comment the WHY when non-obvious"""

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
            language = block.group("lang") or ""
            block_content = block.group("content")
            if _is_powershell_script(language, block_content):
                # Multi-statement script (variable assignments read by later
                # lines, loop/conditional bodies) — must run as one unit via
                # a real PowerShell process, not be split into per-line
                # "commands" that each fail as an unrecognized executable.
                script_path = f".agent-tmp/ps-script-{uuid.uuid4().hex[:8]}.ps1"
                await tool_executor.execute(
                    "file_write", {"path": script_path, "content": block_content.strip()}
                )
                # No quotes around script_path: shell_tool.py resolves this
                # via shlex.split(cmd, posix=False) + subprocess.Popen(list,
                # shell=False), which passes argv tokens to the executable
                # literally with no shell to strip quote characters — a
                # quoted path here would reach `-File` with the quote marks
                # baked into the argument. The relative path has no spaces
                # (workspace-relative), so it's unambiguous unquoted.
                commands.append(f"powershell -NoProfile -ExecutionPolicy Bypass -File {script_path}")
            else:
                commands.extend(_split_shell_block(block_content))

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

        response = await model_router.generate(
            prompt, model, system_prompt=self.get_system_prompt(), timeout=_DEVELOPER_TIMEOUT_SECS
        )

        if tool_executor:
            file_writes = _extract_file_writes(response)
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

            for file_path, content in _extract_file_appends(response):
                await _execute_append(file_path, content, files_created, tool_executor, self.logger)

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
            and not _extract_file_edits(response)
            and not _extract_file_writes(response)
        ):
            read_context = "\n\n".join(shell_outputs[:5])
            _append_hint = (
                " If the task says 'append', use APPEND: blocks — do NOT use FILE: as that overwrites."
                if re.search(r'\bappend\b', task, re.IGNORECASE) else ""
            )
            write_prompt = (
                f"Task: {task}\n\n"
                f"You have read these files:\n\n{read_context[:8000]}\n\n"
                f"Now write the actual code changes using EDIT: blocks (preferred), APPEND: blocks "
                f"(to add to end of a file), or FILE: blocks (new files only).{_append_hint}\n"
                f"Do NOT run any commands — only output code fixes."
            )
            write_response = await model_router.generate(
                write_prompt, model, system_prompt=self.get_system_prompt(), timeout=_DEVELOPER_TIMEOUT_SECS
            )
            response += "\n\n**Write phase:**\n" + write_response

            # Apply FILE: full writes from write phase.
            for file_path, content in _extract_file_writes(write_response):
                try:
                    await tool_executor.execute("file_write", {"path": file_path, "content": content})
                    verify = await tool_executor.execute("file_read", {"path": file_path})
                    if not verify.startswith("Error") and file_path not in files_created:
                        files_created.append(file_path)
                except Exception as e:
                    self.logger.error("write_phase_file_write_failed", path=file_path, error=str(e))

            # Apply APPEND: blocks from write phase.
            for file_path, content in _extract_file_appends(write_response):
                await _execute_append(file_path, content, files_created, tool_executor, self.logger)

            # Apply EDIT: hunks from write phase.
            wp_edits_by_path: dict[str, list[dict]] = {}
            for file_path, old_text, new_text in _extract_file_edits(write_response):
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
            force_run_response = await model_router.generate(
                force_run_prompt, model, system_prompt=self.get_system_prompt(), timeout=_DEVELOPER_TIMEOUT_SECS
            )

            # Write any files the LLM generated before running
            for file_path, content in _extract_file_writes(force_run_response):
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
        # code and re-run — see agent/agents/fix_loop.py::run_fix_loop for
        # the full loop (iteration budget, cycling detection, file-context
        # gathering, REPLACE:/EDIT:/FILE: application, npm-install triggers).
        # Recompute real_failures after the force-run block may have updated failed_outputs.
        real_failures = [e for e in failed_outputs if not _is_readonly_probe(e)]
        if real_failures and tool_executor:
            response = await run_fix_loop(
                task=task,
                model_router=model_router,
                tool_executor=tool_executor,
                on_phase=on_phase,
                system_prompt=self.get_system_prompt(),
                logger=self.logger,
                run_shell_blocks_fn=self._run_shell_blocks,
                response=response,
                files_created=files_created,
                shell_outputs=shell_outputs,
                failed_outputs=failed_outputs,
                real_failures=real_failures,
            )

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

        # Persist any SKILL: blocks the LLM emitted as reusable fix recipes
        if _SKILL_BLOCK_RE.search(response):
            try:
                from agent.skills.skill_writer import SkillWriter
                _ws = os.getenv("WORKSPACE_PATH", ".")
                _sw = SkillWriter(_ws)
                saved = _sw.parse_and_write_skill_blocks(response, agent_type="developer")
                if saved:
                    self.logger.info("skill_blocks_saved", count=len(saved))
            except Exception:
                pass

        # Persist any SCRIPT: blocks as reusable helper scripts in scripts/
        for m in _SCRIPT_BLOCK_RE.finditer(response):
            script_name = m.group("name").strip()
            script_content = m.group("content").strip()
            if script_name and script_content:
                try:
                    from agent.tools.project_scripts import ProjectScriptsTool
                    _ws = os.getenv("WORKSPACE_PATH", ".")
                    ProjectScriptsTool(_ws).save_script(script_name, script_content)
                except Exception:
                    pass

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