"""AppProbe — launches a web app, waits for readiness, and takes a screenshot.

Single launch per acceptance round to avoid port-rebinding issues.

AppHandle:   lightweight dataclass holding process + port + start command
detect_start_command(ws): inspects workspace for known entry points
detect_port(ws, start_cmd): infers the HTTP port from start command / source
AppProbe.launch()         -> Optional[AppHandle]
AppProbe.screenshot(h)    -> Optional[str]  (path to saved PNG)
AppProbe.teardown(h)      kills the process tree
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Coroutine, Optional

import structlog

logger = structlog.get_logger()

# Maximum seconds to wait for the server to accept HTTP connections
_READINESS_TIMEOUT = 30
_READINESS_POLL = 1.0

# Known entry-point priority (first match wins)
_ENTRY_CANDIDATES = [
    ("package.json",  "npm start"),
    ("app.py",        "python app.py"),
    ("main.py",       "python main.py"),
    ("server.py",     "python server.py"),
    ("index.js",      "node index.js"),
    ("server.js",     "node server.js"),
]

# Port hints: patterns in start_cmd or source → default port
_PORT_HINTS: list[tuple[str, int]] = [
    ("flask", 5000),
    ("uvicorn", 8000),
    ("gunicorn", 8000),
    ("django", 8000),
    ("fastapi", 8000),
    ("express", 3000),
    ("react-scripts", 3000),
    ("next", 3000),
    ("vite", 5173),
    ("parcel", 1234),
]

_PORT_RE = re.compile(r"port[=\s:]+(\d{2,5})", re.IGNORECASE)


@dataclass
class AppHandle:
    """Holds a reference to a running app process."""

    process: Optional[subprocess.Popen]
    port: Optional[int]
    start_command: str
    screenshot_dir: Path = field(default_factory=lambda: Path(".screenshots"))


def detect_start_command(workspace: Path) -> Optional[str]:
    """Return the best start command for the project, or None."""
    for filename, cmd in _ENTRY_CANDIDATES:
        if (workspace / filename).exists():
            return cmd
    return None


def detect_port(workspace: Path, start_cmd: str) -> Optional[int]:
    """Infer the HTTP port from the start command and source files."""
    combined = start_cmd.lower()

    # Check source files for explicit port= declarations
    for candidate in ("app.py", "main.py", "server.py", "index.js", "server.js"):
        fp = workspace / candidate
        if fp.exists():
            try:
                combined += " " + fp.read_text(encoding="utf-8", errors="ignore").lower()
            except Exception:
                pass

    m = _PORT_RE.search(combined)
    if m:
        return int(m.group(1))

    for keyword, port in _PORT_HINTS:
        if keyword in combined:
            return port

    return None


class AppProbe:
    """Launches an app, screenshots it, and tears it down."""

    def __init__(self, workspace: Path, shell_fn: Callable[[str], Coroutine]) -> None:
        self.workspace = workspace
        self.shell_fn = shell_fn
        self.logger = logger.bind(component="app_probe")

    async def launch(self) -> Optional[AppHandle]:
        """Detect and start the app. Returns None if no entry point found."""
        start_cmd = detect_start_command(self.workspace)
        if not start_cmd:
            self.logger.info("app_probe_no_entry_point")
            return None

        port = detect_port(self.workspace, start_cmd)
        screenshot_dir = self.workspace / ".screenshots"
        screenshot_dir.mkdir(parents=True, exist_ok=True)

        try:
            proc = subprocess.Popen(
                start_cmd,
                shell=True,
                cwd=str(self.workspace),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as exc:
            self.logger.warning("app_probe_launch_failed", error=str(exc))
            return None

        handle = AppHandle(process=proc, port=port, start_command=start_cmd, screenshot_dir=screenshot_dir)

        if port:
            ready = await self._wait_ready(port)
            if not ready:
                self.logger.warning("app_probe_readiness_timeout", port=port, cmd=start_cmd)
                await self.teardown(handle)
                return None

        self.logger.info("app_probe_launched", cmd=start_cmd, port=port)
        return handle

    async def screenshot(self, handle: AppHandle) -> Optional[str]:
        """Take a screenshot of the running app. Returns path or None."""
        if handle is None or handle.port is None:
            return None

        url = f"http://localhost:{handle.port}"
        shot_path = handle.screenshot_dir / f"acceptance_{int(time.time())}.png"

        try:
            # Use playwright CLI (bowser pattern used elsewhere in the project)
            cmd = f'npx playwright screenshot --browser chromium "{url}" "{shot_path}"'
            await self.shell_fn(cmd)
            if shot_path.exists():
                self.logger.info("app_probe_screenshot", path=str(shot_path))
                return str(shot_path)
        except Exception as exc:
            self.logger.warning("app_probe_screenshot_failed", error=str(exc))

        return None

    async def screenshot_file(self, html_path: Path) -> Optional[str]:
        """Take a Playwright screenshot of a static HTML file via file:// URL.

        Waits 2 s after load so JS animations have time to render a first frame.
        """
        screenshot_dir = self.workspace / ".screenshots"
        screenshot_dir.mkdir(parents=True, exist_ok=True)
        shot_path = screenshot_dir / f"static_{int(time.time())}.png"
        url = html_path.as_uri()
        try:
            cmd = (
                f'npx playwright screenshot --browser chromium '
                f'--wait-for-timeout 2000 "{url}" "{shot_path}"'
            )
            await self.shell_fn(cmd)
            if shot_path.exists():
                self.logger.info("app_probe_static_screenshot", path=str(shot_path), html=html_path.name)
                return str(shot_path)
        except Exception as exc:
            self.logger.warning("app_probe_static_screenshot_failed", error=str(exc))
        return None

    async def teardown(self, handle: Optional[AppHandle]) -> None:
        """Kill the app process tree."""
        if handle is None or handle.process is None:
            return
        try:
            proc = handle.process
            if proc.poll() is None:
                # Kill entire process tree (important on Windows)
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                        capture_output=True,
                    )
                else:
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=5)
        except Exception as exc:
            self.logger.warning("app_probe_teardown_error", error=str(exc))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _wait_ready(self, port: int) -> bool:
        """Poll localhost:port until it accepts a connection or timeout."""
        deadline = time.monotonic() + _READINESS_TIMEOUT
        while time.monotonic() < deadline:
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection("127.0.0.1", port), timeout=2.0
                )
                writer.close()
                await writer.wait_closed()
                return True
            except Exception:
                await asyncio.sleep(_READINESS_POLL)
        return False
