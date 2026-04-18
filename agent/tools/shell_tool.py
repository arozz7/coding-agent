import asyncio
import os
import platform
import re
import shlex
import shutil
import signal
import subprocess
from pathlib import Path
from typing import Callable, Optional

import structlog

logger = structlog.get_logger()

IS_WINDOWS = platform.system() == "Windows"


def _build_tool_env() -> dict:
    """Return a copy of os.environ with PATH augmented to include common tool directories.

    Solves the "npm not found" class of errors that occur when the API server
    is started from an IDE terminal or service that inherits a minimal PATH.

    Discovery order:
      1. Current os.environ (inherits whatever the server was started with)
      2. Well-known Windows install directories for Node, Python, Git, Cargo
      3. User-level package manager directories (nvm, fnm, pyenv, volta)
      4. EXTRA_PATH env var — comma/semicolon-separated dirs the user adds in .env
         for truly non-standard installs
    """
    env = os.environ.copy()
    path_parts = [p for p in env.get("PATH", "").split(os.pathsep) if p]

    if IS_WINDOWS:
        appdata = os.environ.get("APPDATA", "")
        localappdata = os.environ.get("LOCALAPPDATA", "")
        userprofile = os.environ.get("USERPROFILE", "")
        programfiles = os.environ.get("ProgramFiles", "C:\\Program Files")
        programfiles86 = os.environ.get("ProgramFiles(x86)", "C:\\Program Files (x86)")

        _COMMON_WIN_PATHS = [
            # Node / npm
            Path(programfiles) / "nodejs",
            Path(programfiles86) / "nodejs",
            Path(appdata) / "npm",
            # nvm for Windows
            Path(userprofile) / "AppData" / "Roaming" / "nvm",
            Path(localappdata) / "nvm",
            # Volta
            Path(userprofile) / ".volta" / "bin",
            # fnm
            Path(localappdata) / "fnm" / "aliases" / "default" / "bin",
            # Python launchers
            Path(localappdata) / "Programs" / "Python" / "Launcher",
            # Git
            Path(programfiles) / "Git" / "cmd",
            Path(programfiles86) / "Git" / "cmd",
            # Rust / Cargo
            Path(userprofile) / ".cargo" / "bin",
            # Yarn
            Path(localappdata) / "Yarn" / "bin",
            Path(appdata) / "npm",  # yarn also installs here on Windows
        ]
        # Also probe all Python3x dirs under %LOCALAPPDATA%\Programs\Python
        py_root = Path(localappdata) / "Programs" / "Python"
        if py_root.exists():
            for d in py_root.iterdir():
                if d.is_dir() and d.name.startswith("Python"):
                    _COMMON_WIN_PATHS.append(d)
                    _COMMON_WIN_PATHS.append(d / "Scripts")
    else:
        _COMMON_WIN_PATHS = [
            # Homebrew (macOS Intel / Apple Silicon)
            Path("/usr/local/bin"),
            Path("/opt/homebrew/bin"),
            # nvm / nodenv default locations
            Path.home() / ".nvm" / "versions" / "node",
            Path.home() / ".nodenv" / "shims",
            # pyenv
            Path.home() / ".pyenv" / "shims",
            # Cargo
            Path.home() / ".cargo" / "bin",
            # Volta
            Path.home() / ".volta" / "bin",
        ]

    for candidate in _COMMON_WIN_PATHS:
        s = str(candidate)
        if candidate.exists() and s not in path_parts:
            path_parts.append(s)

    # User-supplied extra paths via EXTRA_PATH in .env
    extra = os.environ.get("EXTRA_PATH", "").strip()
    if extra:
        for p in re.split(r"[;,]", extra):
            p = p.strip()
            if p and p not in path_parts:
                path_parts.append(p)

    env["PATH"] = os.pathsep.join(path_parts)
    return env


# Built once at module load; shared by all ShellTool instances.
_TOOL_ENV = _build_tool_env()

# Patterns that are unconditionally blocked regardless of platform.
# These guard against LLM-generated command injection and destructive operations.
# IMPORTANT: _validate_command() is called on the ORIGINAL command (before any
# Unix→Windows translation) AND on the translated form — so both rm and del are caught.
_BLOCKED_PATTERNS = [
    # Unix destructive
    re.compile(r"rm\s+-[rf]{1,2}\s+[/~*]", re.IGNORECASE),        # rm -rf / or ~
    re.compile(r"rm\s+-[rf]{1,2}\s+\.\.", re.IGNORECASE),          # rm -rf ..
    re.compile(r"rm\s+--no-preserve-root", re.IGNORECASE),         # rm --no-preserve-root
    # Windows CMD destructive
    re.compile(r"del\s+/[fqs].*\s+/[sq]", re.IGNORECASE),          # del /f /q (mass delete)
    re.compile(r"del\s+/s\b", re.IGNORECASE),                      # del /s (recursive delete)
    re.compile(r"\brd\s+/s\b", re.IGNORECASE),                     # rd /s (remove dir tree)
    re.compile(r"\brmdir\s+/s\b", re.IGNORECASE),                  # rmdir /s (remove dir tree)
    # PowerShell destructive
    re.compile(r"Remove-Item\s+.*-Recurse", re.IGNORECASE),        # Remove-Item -Recurse
    re.compile(r"\bri\b.*-r\b", re.IGNORECASE),                    # ri -r (alias)
    re.compile(r"Remove-Item\s+[/\\~*]", re.IGNORECASE),           # Remove-Item /
    # Disk / device operations
    re.compile(r"\bformat\s+[a-z]:", re.IGNORECASE),                # format C:
    re.compile(r"\bmkfs\b", re.IGNORECASE),                         # mkfs
    re.compile(r">\s*/dev/sd", re.IGNORECASE),                      # write to raw block device
    re.compile(r">\s*/proc/", re.IGNORECASE),                       # write to /proc
    re.compile(r">\s*C:\\Windows", re.IGNORECASE),                  # overwrite Windows system dir
    re.compile(r">\s*/etc/", re.IGNORECASE),                        # overwrite /etc files
    # Shell injection / execution
    re.compile(r":\(\)\s*\{.*\}", re.IGNORECASE),                   # fork bomb :(){:|}:&}
    re.compile(r"\|\s*(ba|da|z|c)?sh\b", re.IGNORECASE),           # pipe to shell
    re.compile(r"(;|&&|\|\|)\s*rm\s", re.IGNORECASE),              # chained rm
    re.compile(r"\$\(.*rm\s", re.IGNORECASE),                       # subshell rm
    re.compile(r"`.*rm\s.*`", re.IGNORECASE),                       # backtick rm
    # System state
    re.compile(r"\bshutdown\b", re.IGNORECASE),
    re.compile(r"\breboot\b", re.IGNORECASE),
    # Environment poisoning
    re.compile(r"\bsetx\s+PATH\b", re.IGNORECASE),                 # Windows PATH overwrite
    re.compile(r"export\s+PATH\s*=\s*/tmp", re.IGNORECASE),        # PATH hijack to /tmp
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


def _kill_process_tree(pid: int) -> None:
    """Kill a process and all its children.

    On Windows uses ``taskkill /F /T /PID`` to terminate the entire job tree.
    On Unix sends SIGKILL to the process group so daemonised children die too.
    """
    try:
        if IS_WINDOWS:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                capture_output=True,
                timeout=10,
            )
        else:
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGKILL)
            except ProcessLookupError:
                pass  # process already gone
    except Exception:
        # Best-effort; don't propagate kill errors.
        pass


class ShellTool:
    def __init__(self, workspace_path: str):
        # Validate workspace_path against the configured workspace root (trusted env var)
        # using the inline containment pattern CodeQL recognises as safe for py/path-injection.
        configured_root = os.getenv("WORKSPACE_PATH", "./workspace")
        workspace_root = Path(configured_root).resolve()
        candidate = (workspace_root / workspace_path).resolve()
        if not candidate.is_relative_to(workspace_root):
            raise ValueError(
                f"workspace_path '{workspace_path}' resolves outside configured workspace root"
            )
        self.workspace = candidate
        self.logger = logger.bind(component="shell_tool")
        # Log which key tools are resolvable so PATH issues are visible at startup.
        _found = {t: shutil.which(t, path=_TOOL_ENV["PATH"]) for t in ("npm", "node", "python", "git", "cargo")}
        self.logger.info("shell_initialized", os=platform.system(), workspace=str(self.workspace), tools_found=_found)

    def _translate_unix_to_windows(self, cmd: str) -> str:
        """Translate common Unix commands to their Windows equivalents.

        Unix flags that have no direct Windows counterpart are dropped rather
        than passed through unchanged (e.g. `ls -la` → `dir`, not `dir -la`).
        """
        parts = cmd.split()
        verb = parts[0].lower() if parts else ""

        if verb == "ls":
            targets = [p for p in parts[1:] if not p.startswith("-")]
            return "dir " + " ".join(targets) if targets else "dir"

        if verb == "cat":
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

    def _resolve_args(self, cmd: str) -> tuple:
        """Resolve (args, use_shell) for subprocess from the translated command string."""
        if IS_WINDOWS:
            if _is_windows_builtin(cmd):
                return cmd, True
            try:
                parsed = shlex.split(cmd, posix=False)
            except ValueError:
                return None, None  # caller handles parse failure

            first_tok = parsed[0] if parsed else ""
            resolved = shutil.which(first_tok, path=_TOOL_ENV["PATH"])
            if resolved and resolved.lower().endswith((".cmd", ".bat")):
                return cmd, True
            return parsed, False
        else:
            try:
                return shlex.split(cmd), False
            except ValueError:
                return None, None

    def run(self, command: str, timeout: int = 60) -> dict:
        """Run a shell command in the workspace directory.

        Uses ``subprocess.Popen`` so the process PID is available for
        process-tree termination on timeout.  On Windows, ``taskkill /F /T``
        kills the entire child tree; on Unix the process group is killed via
        SIGKILL so daemonised grandchildren don't survive.
        """
        cmd = command.strip()

        # Validate the ORIGINAL command before translation so Unix-form patterns
        # (e.g. `rm -rf /`) are caught even when running on Windows.
        try:
            _validate_command(cmd)
        except ValueError as e:
            self.logger.warning("shell_blocked", command=command, reason=str(e))
            return {"success": False, "error": str(e)}

        if IS_WINDOWS:
            cmd = self._translate_unix_to_windows(cmd)

        # Validate again after translation — catches Windows-form equivalents
        # that the translation may have produced (e.g. `del /s`).
        try:
            _validate_command(cmd)
        except ValueError as e:
            self.logger.warning("shell_blocked", command=command, reason=str(e))
            return {"success": False, "error": str(e)}

        self.logger.info("shell_run", original=command, translated=cmd, cwd=str(self.workspace))

        args, use_shell = self._resolve_args(cmd)
        if args is None:
            self.logger.warning("shlex_parse_failed", cmd=cmd)
            return {"success": False, "error": f"Could not parse command: {cmd!r}"}

        # On Unix, start process in a new session/group so killpg covers children.
        popen_kwargs: dict = dict(
            cwd=str(self.workspace),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_TOOL_ENV,
        )
        if not IS_WINDOWS:
            popen_kwargs["start_new_session"] = True

        try:
            proc = subprocess.Popen(args, shell=use_shell, **popen_kwargs)
        except Exception as e:
            self.logger.error("shell_spawn_failed", command=command, error=str(e))
            return {"success": False, "error": str(e)}

        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.logger.error("shell_timeout", command=command, timeout=timeout, pid=proc.pid)
            _kill_process_tree(proc.pid)
            try:
                stdout, stderr = proc.communicate(timeout=5)
            except Exception:
                stdout, stderr = "", ""
            return {"success": False, "error": f"Command timed out after {timeout}s", "stdout": stdout, "stderr": stderr}
        except Exception as e:
            _kill_process_tree(proc.pid)
            self.logger.error("shell_error", command=command, error=str(e))
            return {"success": False, "error": str(e)}

        return {
            "success": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": stderr,
        }

    async def run_streaming(
        self,
        command: str,
        timeout: int = 60,
        on_data: Optional[Callable[[str], None]] = None,
    ) -> dict:
        """Run a shell command, calling ``on_data(chunk)`` as output arrives.

        Used by the tool executor to emit watchdog heartbeats during long-
        running commands (npm start, pytest, etc.) without blocking the
        supervisor's stale-job timer.

        Returns the same dict shape as :meth:`run`.
        """
        cmd = command.strip()

        # Validate original form first, then the translated form.
        try:
            _validate_command(cmd)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        if IS_WINDOWS:
            cmd = self._translate_unix_to_windows(cmd)

        try:
            _validate_command(cmd)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        args, use_shell = self._resolve_args(cmd)
        if args is None:
            return {"success": False, "error": f"Could not parse command: {cmd!r}"}

        create_flags = 0
        if IS_WINDOWS:
            CREATE_NEW_PROCESS_GROUP = 0x00000200
            create_flags = CREATE_NEW_PROCESS_GROUP

        try:
            proc = await asyncio.create_subprocess_exec(
                *(args if isinstance(args, list) else [args]),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(self.workspace),
                env=_TOOL_ENV,
            )
        except Exception as e:
            return {"success": False, "error": str(e)}

        stdout_chunks: list[str] = []

        async def _read_output() -> None:
            assert proc.stdout is not None
            async for raw_line in proc.stdout:
                chunk = raw_line.decode("utf-8", errors="replace")
                stdout_chunks.append(chunk)
                if on_data:
                    try:
                        on_data(chunk)
                    except Exception:
                        pass

        try:
            await asyncio.wait_for(_read_output(), timeout=timeout)
            await proc.wait()
        except asyncio.TimeoutError:
            self.logger.error("shell_streaming_timeout", command=command, timeout=timeout)
            if proc.pid:
                _kill_process_tree(proc.pid)
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            return {"success": False, "error": f"Command timed out after {timeout}s"}

        stdout = "".join(stdout_chunks)
        return {
            "success": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": stdout,
            "stderr": "",
        }

    def run_npm(self, args: str, timeout: int = 120) -> dict:
        """Run an npm command."""
        return self.run(f"npm {args}", timeout=timeout)

    def run_python(self, args: str, timeout: int = 60) -> dict:
        """Run a python command."""
        return self.run(f"python {args}", timeout=timeout)