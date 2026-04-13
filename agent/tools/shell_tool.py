import subprocess
import platform
import re
import shlex
from pathlib import Path
import structlog

logger = structlog.get_logger()

IS_WINDOWS = platform.system() == "Windows"

# Patterns that are unconditionally blocked regardless of platform.
# These guard against LLM-generated command injection and destructive operations.
_BLOCKED_PATTERNS = [
    re.compile(r"rm\s+-[rf]{1,2}\s+[/~*]", re.IGNORECASE),        # rm -rf / or ~
    re.compile(r"rm\s+-[rf]{1,2}\s+\.\.", re.IGNORECASE),          # rm -rf ..
    re.compile(r"del\s+/[fqs].*\s+/[sq]", re.IGNORECASE),          # del /f /q (mass delete)
    re.compile(r"\bformat\s+[a-z]:", re.IGNORECASE),                # format C:
    re.compile(r"\bmkfs\b", re.IGNORECASE),                         # mkfs
    re.compile(r":\(\)\s*\{.*\}", re.IGNORECASE),                   # fork bomb :(){:|}:&}
    re.compile(r"\|\s*(ba|da|z|c)?sh\b", re.IGNORECASE),           # pipe to shell
    re.compile(r">\s*/dev/sd", re.IGNORECASE),                      # write to raw block device
    re.compile(r">\s*/proc/", re.IGNORECASE),                       # write to /proc
    re.compile(r">\s*C:\\Windows", re.IGNORECASE),                  # overwrite Windows system dir
    re.compile(r"\bshutdown\b", re.IGNORECASE),                     # shutdown / reboot
    re.compile(r"\breboot\b", re.IGNORECASE),
    re.compile(r"(;|&&|\|\|)\s*rm\s", re.IGNORECASE),              # chained rm after another cmd
    re.compile(r"\$\(.*rm\s", re.IGNORECASE),                       # subshell rm
    re.compile(r"`.*rm\s.*`", re.IGNORECASE),                       # backtick rm
]

# Windows shell built-ins that cannot run without shell=True.
_WINDOWS_BUILTINS = frozenset([
    "dir", "type", "del", "copy", "move", "mkdir", "rmdir", "rd",
    "echo", "set", "cd", "cls", "ver", "where", "whoami",
])


def _validate_command(command: str) -> None:
    """Raise ValueError if the command matches a blocked pattern."""
    for pattern in _BLOCKED_PATTERNS:
        if pattern.search(command):
            raise ValueError(f"Command blocked by safety policy: {command[:120]!r}")


def _is_windows_builtin(cmd: str) -> bool:
    """Return True if the first token is a Windows shell built-in."""
    first_token = cmd.strip().split()[0].lower() if cmd.strip() else ""
    return first_token in _WINDOWS_BUILTINS


class ShellTool:
    def __init__(self, workspace_path: str):
        # workspace_path comes from WORKSPACE_PATH env var / server config, not user HTTP input.
        self.workspace = Path(workspace_path).resolve()  # lgtm[py/path-injection]
        self.logger = logger.bind(component="shell_tool")
        self.logger.info("shell_initialized", os=platform.system(), workspace=str(self.workspace))

    def _translate_unix_to_windows(self, cmd: str) -> str:
        """Translate common Unix commands to their Windows equivalents.

        Unix flags that have no direct Windows counterpart are dropped rather
        than passed through unchanged (e.g. `ls -la` → `dir`, not `dir -la`).
        """
        parts = cmd.split()
        verb = parts[0].lower() if parts else ""

        if verb == "ls":
            # Collect non-flag arguments (directory targets)
            targets = [p for p in parts[1:] if not p.startswith("-")]
            return "dir " + " ".join(targets) if targets else "dir"

        if verb == "cat":
            # `cat file` → `type file`; `cat file1 file2` → `type file1 file2`
            targets = [p for p in parts[1:] if not p.startswith("-")]
            return "type " + " ".join(targets) if targets else "type"

        if verb == "rm":
            targets = [p for p in parts[1:] if not p.startswith("-")]
            return "del " + " ".join(targets) if targets else "del"

        if verb == "mkdir":
            targets = [p for p in parts[1:] if not p.startswith("-")]
            return "mkdir " + " ".join(targets) if targets else "mkdir"

        if verb == "rmdir":
            targets = [p for p in parts[1:] if not p.startswith("-")]
            return "rmdir " + " ".join(targets) if targets else "rmdir"

        if verb == "touch":
            filename = " ".join(parts[1:]).strip()
            return f"echo. > {filename}" if filename else "echo."

        if verb == "pwd":
            return "cd"

        if verb == "which":
            targets = " ".join(parts[1:])
            return f"where {targets}" if targets else "where"

        if verb == "grep":
            # Best-effort: `grep pattern file` → `findstr pattern file`
            targets = " ".join(parts[1:])
            return f"findstr {targets}"

        if verb == "cp":
            targets = " ".join(p for p in parts[1:] if not p.startswith("-"))
            return f"copy {targets}"

        if verb == "mv":
            targets = " ".join(p for p in parts[1:] if not p.startswith("-"))
            return f"move {targets}"

        if verb == "clear":
            return "cls"

        if verb == "echo" and ">" not in cmd and ">>" not in cmd:
            return cmd  # echo works on Windows already

        return cmd

    def run(self, command: str, timeout: int = 60) -> dict:
        """Run a shell command in the workspace directory.

        On Unix the command is tokenised with shlex and run with shell=False,
        which prevents shell-metacharacter injection.  On Windows, built-in
        commands (dir, type, del …) still require shell=True; external
        commands (npm, python, git …) are run with shell=False.

        All commands are checked against a blocklist of dangerous patterns
        before execution regardless of platform.
        """
        cmd = command.strip()

        if IS_WINDOWS:
            cmd = self._translate_unix_to_windows(cmd)

        try:
            _validate_command(cmd)
        except ValueError as e:
            self.logger.warning("shell_blocked", command=command, reason=str(e))
            return {"success": False, "error": str(e)}

        self.logger.info("shell_run", original=command, translated=cmd, cwd=str(self.workspace))

        try:
            if IS_WINDOWS and _is_windows_builtin(cmd):
                # Built-ins must use shell=True; already validated above.
                args: str | list = cmd
                use_shell = True
            else:
                # External executables: tokenise and avoid shell=True.
                try:
                    args = shlex.split(cmd, posix=not IS_WINDOWS)
                except ValueError:
                    # shlex failed (e.g. unmatched quotes) — fallback with shell blocked
                    self.logger.warning("shlex_parse_failed", cmd=cmd)
                    return {"success": False, "error": f"Could not parse command: {cmd!r}"}
                use_shell = False

            result = subprocess.run(
                args,
                shell=use_shell,
                cwd=str(self.workspace),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return {
                "success": result.returncode == 0,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        except subprocess.TimeoutExpired:
            self.logger.error("shell_timeout", command=command, timeout=timeout)
            return {"success": False, "error": f"Command timed out after {timeout}s"}
        except Exception as e:
            self.logger.error("shell_error", command=command, error=str(e))
            return {"success": False, "error": str(e)}
    
    def run_npm(self, args: str, timeout: int = 120) -> dict:
        """Run an npm command"""
        return self.run(f"npm {args}", timeout=timeout)
    
    def run_python(self, args: str, timeout: int = 60) -> dict:
        """Run a python command"""
        return self.run(f"python {args}", timeout=timeout)