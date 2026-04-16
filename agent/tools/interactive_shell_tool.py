"""
Interactive Shell Tool — spawn a process and exchange input/output via pipes.

Backed by asyncio subprocess (stdin/stdout pipes).  Works on Windows, macOS, and
Linux for any app that reads stdin line-by-line: readline-based CLIs, REPL
interpreters, text-adventure games, simple servers, etc.

Limitation: apps that require a real TTY (ncurses, raw-terminal mode) won't
behave correctly via pipes because they can't detect terminal capabilities.
For those, consider wrapping them in a web interface and using BrowserTool.
"""
import asyncio
import os
import platform
import re
import shlex
from pathlib import Path
from typing import Any

import structlog

from agent.tools.shell_tool import _TOOL_ENV

logger = structlog.get_logger()

IS_WINDOWS = platform.system() == "Windows"


class InteractiveShellTool:
    """Run a process interactively via stdin/stdout pipes.

    Typical usage from ToolExecutor::

        result = await executor.execute("interactive_shell", {
            "command": "node src/index.js",
            "script": [
                {"expect": "name",   "send": "Alice"},
                {"expect": "option", "send": "2"},
                {"expect": "option", "send": "4"},
            ],
            "timeout": 30,
        })

    Script step fields (all optional, combine freely in one dict):

    ``expect`` (str)
        Regex pattern to wait for in stdout before proceeding.
        The step blocks until the pattern matches or the per-step timeout
        elapses.  Matching is case-insensitive.

    ``send`` (str)
        Text to write to stdin.  A newline is appended automatically if the
        string does not already end with one.

    ``wait`` (float)
        Sleep N seconds without any I/O — useful after a ``send`` that
        triggers async processing.

    Return value::

        {
          "success":    bool,
          "transcript": str,   # interleaved stdout + [sent] markers
          "returncode": int,
        }

    On error ``success`` is False and an ``"error"`` key is present.
    """

    def __init__(self, workspace_path: str):
        # Resolve both paths before comparison to prevent traversal/symlink tricks.
        candidate = Path(workspace_path).resolve(strict=False)
        configured_root = os.getenv("AGENT_EFFECTIVE_WORKSPACE") or os.getenv("WORKSPACE_PATH", "./workspace")
        allowed_root = Path(configured_root).resolve(strict=False)
        try:
            candidate.relative_to(allowed_root)
        except ValueError:
            raise ValueError("workspace_path must be within the configured workspace root")
        self.workspace = candidate
        self.logger = logger.bind(component="interactive_shell_tool")

    async def run(
        self,
        command: str,
        script: list[dict],
        timeout: int = 30,
    ) -> dict[str, Any]:
        """Execute *command* and follow the interaction *script*."""
        transcript: list[str] = []
        proc = None

        try:
            proc = await self._spawn(command)
            self.logger.info("interactive_shell_started", command=command, steps=len(script))
        except Exception as exc:
            return {"success": False, "error": f"Failed to spawn '{command}': {exc}", "transcript": ""}

        try:
            for i, step in enumerate(script):
                # Pure sleep — no I/O.
                if "wait" in step:
                    await asyncio.sleep(float(step["wait"]))
                    continue

                # Wait for expected output first.
                if "expect" in step:
                    output, matched = await self._read_until(
                        proc.stdout,
                        pattern=step["expect"],
                        step_timeout=float(timeout),
                    )
                    if output:
                        transcript.append(output)
                    if not matched:
                        self.logger.warning(
                            "expect_timeout",
                            step=i,
                            pattern=step["expect"],
                            buf_tail=output[-300:] if output else "",
                        )

                # Send input after (or without) an expect.
                if "send" in step:
                    text = step["send"]
                    if not text.endswith("\n"):
                        text += "\n"
                    transcript.append(f"[sent] {step['send']!r}\n")
                    try:
                        proc.stdin.write(text.encode("utf-8"))
                        await proc.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        # Process exited before we finished the script.
                        self.logger.warning("stdin_broken_pipe", step=i)
                        break

            # Signal EOF to the process so it can exit normally.
            try:
                proc.stdin.close()
            except Exception:
                pass

            # Drain any remaining output.
            tail, _ = await self._read_until(
                proc.stdout, pattern=None, step_timeout=min(timeout, 10)
            )
            if tail:
                transcript.append(tail)

            # Wait for the process to exit; kill if it overstays its welcome.
            try:
                await asyncio.wait_for(proc.wait(), timeout=float(timeout))
            except asyncio.TimeoutError:
                self.logger.warning("interactive_shell_kill", command=command)
                proc.kill()
                await proc.wait()

            rc = proc.returncode
            self.logger.info("interactive_shell_done", returncode=rc)
            return {
                "success": True,
                "transcript": "".join(transcript),
                "returncode": rc,
            }

        except Exception as exc:
            self.logger.error("interactive_shell_error", error=str(exc))
            if proc and proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass
            return {
                "success": False,
                "error": str(exc),
                "transcript": "".join(transcript),
            }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _spawn(self, command: str):
        """Spawn *command* with piped stdin/stdout/stderr."""
        kwargs: dict[str, Any] = dict(
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,   # merge stderr into stdout
            cwd=str(self.workspace),
            env=_TOOL_ENV,
        )
        if IS_WINDOWS:
            # shell=True is required on Windows for .cmd/.bat shims (npm, node,
            # npx) and for compound commands containing &&.
            return await asyncio.create_subprocess_shell(command, **kwargs)
        else:
            try:
                args = shlex.split(command)
                return await asyncio.create_subprocess_exec(*args, **kwargs)
            except ValueError:
                # Unusual quoting — fall back to shell.
                return await asyncio.create_subprocess_shell(command, **kwargs)

    async def _read_until(
        self,
        stream: asyncio.StreamReader,
        pattern: str | None,
        step_timeout: float,
    ) -> tuple[str, bool]:
        """Read from *stream* until *pattern* matches or *step_timeout* elapses.

        When *pattern* is ``None`` the method simply drains whatever is
        available before the timeout (used for the final output flush).

        Returns ``(accumulated_text, matched)``.
        """
        compiled = re.compile(pattern, re.IGNORECASE | re.DOTALL) if pattern else None
        buf = ""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + step_timeout

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                break
            try:
                chunk = await asyncio.wait_for(
                    stream.read(512),
                    timeout=min(remaining, 0.5),
                )
            except asyncio.TimeoutError:
                if compiled is None:
                    break   # Draining — no pattern to wait for, stop here.
                continue    # Still waiting for the expected pattern.

            if not chunk:   # EOF from the process
                break

            buf += chunk.decode("utf-8", errors="replace")
            if compiled and compiled.search(buf):
                return buf, True

        # pattern=None means "drain until quiet" — always reports success.
        return buf, compiled is None


__all__ = ["InteractiveShellTool"]
