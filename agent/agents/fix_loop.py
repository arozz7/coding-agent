"""Fix-and-rerun loop — extracted from DeveloperRole.execute().

If any commands failed after the initial write, this loop asks the LLM to
fix the code and re-runs the original failing command explicitly after
each fix (regardless of what shell blocks the LLM's fix response includes),
up to MAX_FIX_ITERATIONS times. Detects cycling (identical error text on
consecutive attempts) and aborts early rather than burning the whole
iteration budget.

All dependencies (model_router, tool_executor, logger, system_prompt, the
shell-block runner) are passed in explicitly rather than read off `self`,
so this is a free function rather than a method — DeveloperRole.execute()
calls it once real_failures/tool_executor are confirmed present.
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Awaitable, Callable, List, Optional

from agent.agents.output_blocks import (
    apply_line_replacement,
    extract_file_edits,
    extract_file_writes,
    extract_line_replacements,
    format_file_with_lines,
)

MAX_FIX_ITERATIONS = int(os.getenv("MAX_FIX_ITERATIONS", "50"))

# Same rationale as developer_agent._DEVELOPER_TIMEOUT_SECS — fix-loop
# generations write full replacements on top of a long thinking trace and
# can legitimately run past the model_router default of 600s.
_DEVELOPER_TIMEOUT_SECS = 1500.0

_MISSING_TOOL_NAMES = (
    "jest", "webpack", "ts-node", "tsc", "mocha", "vitest", "eslint", "prettier",
)


def is_readonly_probe(entry: str) -> bool:
    """True when a shell entry is a file-read probe (type/cat/dir/ls) — not a real build failure."""
    first = entry.splitlines()[0] if entry else ""
    return bool(re.match(r'^\$\s+(type|cat|dir|ls|head|tail)\b', first, re.IGNORECASE))


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
_MAX_ERROR_CHARS = 2000

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
_MAX_FIX_FILE_CONTEXT = 4000   # total chars of source included in fix prompts
_MAX_FIX_FILE_PER_FILE = 3000  # chars per individual file

# Maximum number of fix-attempt prose blocks accumulated into the response
# string.  Older blocks are replaced with a placeholder to keep the string
# from growing unboundedly across 10 iterations.
_MAX_RESPONSE_HISTORY = 3


async def run_fix_loop(
    *,
    task: str,
    model_router,
    tool_executor,
    on_phase: Optional[Callable[[str], None]],
    system_prompt: str,
    logger,
    run_shell_blocks_fn: Callable[[str, object], Awaitable[tuple[list[str], list[str]]]],
    response: str,
    files_created: List[str],
    shell_outputs: List[str],
    failed_outputs: List[str],
    real_failures: List[str],
) -> str:
    """Run the fix-and-rerun loop. Mutates files_created/shell_outputs in
    place; returns the updated response string (the caller's `response` is
    not mutated in place since strings are immutable — reassign from the
    return value)."""
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
    # Detect cycling: if the same error hash appears twice in a row the
    # model is stuck — abort rather than burning all iterations.
    _prev_error_hash: str = ""

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

        # Break early if the same error text repeats — the model is cycling.
        _cur_hash = hashlib.md5(raw_errors.encode()).hexdigest()
        if _attempt > 0 and _cur_hash == _prev_error_hash:
            response += "\n\n*(Fix loop aborted: identical error on consecutive attempts — model is cycling)*"
            logger.info("fix_loop_cycling_detected", attempt=_attempt + 1)
            break
        _prev_error_hash = _cur_hash

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
                snippet = format_file_with_lines(content, fp, _MAX_FIX_FILE_PER_FILE)
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
            logger.info("fix_loop_no_file_context", attempt=_attempt + 1)
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
        fix_response = await model_router.generate(
            fix_prompt, model, system_prompt=system_prompt, timeout=_DEVELOPER_TIMEOUT_SECS
        )

        iteration_files: list[str] = []

        # 1. Apply REPLACE: blocks first — line-number based, never fails on old-text mismatch.
        for file_path, start, end, new_text in extract_line_replacements(fix_response):
            try:
                ok = await apply_line_replacement(tool_executor, file_path, start, end, new_text, logger)
                if ok:
                    if file_path not in files_created:
                        files_created.append(file_path)
                    iteration_files.append(file_path)
                    logger.info("replace_applied", path=file_path, lines=f"{start}-{end}")
                else:
                    logger.warning("replace_failed", path=file_path, lines=f"{start}-{end}")
            except Exception as e:
                logger.error("replace_error", path=file_path, error=str(e))

        # 2. Apply EDIT: hunks (surgical old-text patches) for any files not yet touched.
        edits_by_path: dict[str, list[dict]] = {}
        for file_path, old_text, new_text in extract_file_edits(fix_response):
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
                    logger.info("edit_applied", path=file_path, hunks=len(hunks))
                else:
                    logger.warning("edit_rejected", path=file_path, detail=edit_result[:120])
            except Exception as e:
                logger.error("edit_failed", path=file_path, error=str(e))

        # Fallback: FILE: blocks for files not already patched via EDIT:.
        for file_path, content in extract_file_writes(fix_response):
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
                    logger.warning("fix_file_write_not_verified", path=file_path)
            except Exception as e:
                logger.error("fix_file_write_failed", path=file_path, error=str(e))

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
            logger.info("npm_install_after_package_json_edit", attempt=_attempt + 1)
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
                    + "\n\n*(earlier fix attempts omitted)*"
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
                    logger.info("npm_auto_install", cmd=install_cmd, attempt=_attempt + 1)
                    _ran_npm_install = True
                    made_progress = True

                if not made_progress:
                    if _attempt == 0:
                        # First pass produced analysis but no code changes.
                        # Inject a direct instruction so the next attempt writes code.
                        failed_outputs = [
                            raw_errors
                            + "\n\n[Fix loop note: you analyzed the issue but wrote no "
                            "code changes. The next response MUST contain REPLACE: or "
                            "EDIT: blocks with the actual fix. Do NOT describe — emit "
                            "the code directly.]"
                        ]
                        logger.info("fix_loop_no_progress_retry", attempt=1)
                        continue
                    response += "\n\n*(Fix loop aborted: The model did not modify any files to address the failure)*"
                    logger.info("fix_loop_aborted_no_progress", attempt=_attempt + 1)
                    break

                verify_out = await tool_executor.execute("shell", {"command": verify_cmd})
                verify_entry = f"$ {verify_cmd}\n{verify_out}"
                shell_outputs.append(verify_entry)
                logger.info("fix_verify_run", attempt=_attempt + 1, cmd=verify_cmd)

                is_failure = (
                    "returncode: 1" in verify_out
                    or "exit code: 1" in verify_out
                    or "Command failed" in verify_out
                    or "timed out" in verify_out.lower()
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
                logger.error("fix_verify_failed", error=str(e))
                break
        else:
            # No verify command available — fall back to running any
            # shell blocks the LLM included (legacy path).
            new_outputs, new_failures = await run_shell_blocks_fn(fix_response, tool_executor)
            shell_outputs.extend(new_outputs)
            if not new_failures:
                failed_outputs = []
                break
            failed_outputs = new_failures

    return response
