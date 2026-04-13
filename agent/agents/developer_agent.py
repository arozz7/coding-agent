from typing import Dict, Any, Optional, List
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

MAX_FIX_ITERATIONS = 3

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
        return """You are an expert software developer. Your role is to:
- Write clean, efficient, and maintainable code
- AUTONOMOUSLY run, debug, and fix code — do NOT ask the user to run commands
- When asked to run, debug, or fix errors: execute the commands yourself, read the output, and fix issues

CAPABILITIES YOU HAVE:
1. Write files using:
   FILE: path/to/file.ext
   ```language
   file content
   ```
2. Run shell commands using FENCED BLOCKS (CRITICAL — must be a fenced block, NOT inline backticks):
   ```shell
   cd project-dir
   npm install
   npm run build
   ```
3. Read errors from shell output and fix the code

AUTONOMOUS WORKFLOW for "run and debug" tasks:
1. First run the project to see the current error:
   ```shell
   cd <project-dir>
   npm run build 2>&1
   ```
2. Read the error output carefully
3. Fix the relevant source files (write them with FILE: blocks)
4. Run again to verify the fix worked:
   ```shell
   cd <project-dir>
   npm run build 2>&1
   ```
5. Report what was fixed and what the final state is

DO NOT:
- Ask "what error are you seeing?" — just run the command and read the output yourself
- Say "let me check..." without actually running a command
- Describe what you would do — DO it

PROJECT DIRECTORY RULE — CRITICAL:
When building a NEW project (game, app, API, tool, etc.):
1. Infer a short, lowercase, hyphenated project name from the task
2. Create ALL files under that named subdirectory: FILE: <project-name>/src/main.py
3. NEVER dump files directly into the workspace root for a new project
4. If continuing work on an existing project, keep files under the same subdirectory

For file writing use this exact format:
FILE: path/to/file.ext
```language
file content here
```

For shell commands use FENCED SHELL BLOCKS only:
```shell
cd <project-dir>
npm install
npm run build 2>&1
```

Focus on:
- Code correctness and edge cases
- Proper error handling
- Security best practices
- Readable and self-documenting code"""

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

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        task = context.get("task", "")
        architecture = context.get("architecture", "")
        files_created = []
        model_router = context.get("model_router")
        tool_executor = context.get("tool_executor")

        if not model_router:
            return {"success": False, "error": "model_router not available"}

        enriched_context = context.get("enriched_context", "")
        prompt = f"""{self.get_system_prompt()}

Task: {task}

{architecture if architecture else ''}{enriched_context}

Implement the solution with:
1. Complete, working code
2. Appropriate error handling
3. Basic tests
4. Clear documentation in comments

Write actual files using the format:
FILE:
```<language>
# file content here
```
"""

        model = model_router.get_model("coding")
        if not model:
            return {"success": False, "error": "No coding model configured"}

        response = await model_router.generate(prompt, model)

        if tool_executor:
            file_writes = self._extract_file_writes(response)
            for file_path, content in file_writes:
                try:
                    await tool_executor.execute("file_write", {"path": file_path, "content": content})
                    files_created.append(file_path)
                    self.logger.info("file_written", path=file_path, size=len(content))
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
                f"{self.get_system_prompt()}\n\n"
                f"Task: {task}\n\n"
                f"{enriched_context}\n\n"
                f"Project exploration so far:\n{prior_output}\n\n"
                f"IMPORTANT: You have explored the project but have NOT run it yet.\n"
                f"You MUST now actually launch the application using the correct command "
                f"(e.g. `npm start`, `npm run dev`, `node index.js`, `python app.py`).\n"
                f"Look at the package.json start script or main entry point shown above.\n"
                f"Output ONLY a fenced shell block that runs the app. Do NOT explore further."
            )
            force_run_response = await model_router.generate(force_run_prompt, model)

            # Write any files the LLM generated before running
            for file_path, content in self._extract_file_writes(force_run_response):
                try:
                    await tool_executor.execute("file_write", {"path": file_path, "content": content})
                    if file_path not in files_created:
                        files_created.append(file_path)
                except Exception as e:
                    self.logger.error("force_run_file_write_failed", path=file_path, error=str(e))

            run_outputs, run_failures = await self._run_shell_blocks(force_run_response, tool_executor)
            shell_outputs.extend(run_outputs)
            response += "\n\n**Run attempt:**\n" + force_run_response
            if run_failures:
                failed_outputs = run_failures

        # Fix-and-rerun loop: if any commands failed, ask the LLM to fix the
        # code and re-run, up to MAX_FIX_ITERATIONS times.
        if failed_outputs and tool_executor:
            for _attempt in range(MAX_FIX_ITERATIONS):
                error_summary = "\n\n".join(failed_outputs)
                fix_prompt = (
                    f"{self.get_system_prompt()}\n\n"
                    f"Original task: {task}\n\n"
                    f"The following shell commands failed. "
                    f"Fix the source files and re-run:\n\n"
                    f"```\n{error_summary[:3000]}\n```\n\n"
                    f"Write any fixed files using FILE: blocks, then re-run the commands."
                )
                model = model_router.get_model("coding")
                fix_response = await model_router.generate(fix_prompt, model)

                # Write any fixed files
                for file_path, content in self._extract_file_writes(fix_response):
                    try:
                        await tool_executor.execute("file_write", {"path": file_path, "content": content})
                        if file_path not in files_created:
                            files_created.append(file_path)
                    except Exception as e:
                        self.logger.error("fix_file_write_failed", path=file_path, error=str(e))

                # Re-run commands from the fix response
                new_outputs, new_failures = await self._run_shell_blocks(fix_response, tool_executor)
                shell_outputs.extend(new_outputs)
                response += f"\n\n**Fix attempt {_attempt + 1}:**\n" + fix_response

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

        return {
            "success": True,
            "role": self.name,
            "response": response,
            "task": task,
            "files_created": files_created,
            "shell_output": shell_outputs,
            "screenshot": screenshot_path,
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