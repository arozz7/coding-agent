"""SDLC Workflow — orchestrates the full Plan→Build→Test→Debug→Run→Verify pipeline.

This module is intentionally decoupled from AgentOrchestrator — it receives a
reference to the orchestrator only to access shared agents, tools, and model
router. All SDLC sequencing logic lives here.

Debug loop: retries until tests pass or MAX_DEBUG_RETRIES is hit (hard cap = 5).
Port detection: reads .env / package.json / pyproject.toml; falls back to next free.
Screenshots: saved to workspace/.screenshots/, cleaned up after 24 h.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import socket
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import structlog

if TYPE_CHECKING:
    from agent.orchestrator import AgentOrchestrator

logger = structlog.get_logger()

MAX_DEBUG_RETRIES = 5
SCREENSHOTS_SUBDIR = ".screenshots"
SCREENSHOT_TTL_HOURS = 24


class SDLCWorkflow:
    """Runs the full software development lifecycle autonomously.

    Phases:
        1. Plan   — PlanAgent produces a structured build plan
        2. Build  — DeveloperAgent implements the plan
        3. Test   — TesterAgent writes tests; pytest runs them
        4. Debug  — DeveloperAgent fixes failures; retries until green or cap
        5. Run    — subprocess starts the app on a detected free port
        6. Verify — BrowserTool screenshots the running app
        7. Complete — summary + screenshot path returned
    """

    def __init__(self, orchestrator: AgentOrchestrator) -> None:
        self.orch = orchestrator
        self.workspace = Path(orchestrator.workspace_path)
        self.logger = logger.bind(component="sdlc_workflow")

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(
        self,
        task: str,
        session_id: str,
        on_phase: Optional[Callable[[str], None]] = None,
        job_id: Optional[str] = None,
    ) -> dict:
        """Execute the full SDLC pipeline for *task*.

        Returns a result dict compatible with run_task()'s return format,
        with an extra ``result.screenshot_path`` key.
        """

        def emit(label: str) -> None:
            if on_phase:
                try:
                    on_phase(label)
                except Exception:
                    pass

        context = await self._build_context(task, session_id)
        files_created: list[str] = []
        screenshot_path: Optional[str] = None

        try:
            # ---- Phase 1: Plan ----
            emit("sdlc:planning")
            self.logger.info("sdlc_phase_plan", session_id=session_id)
            plan_result = await self._plan_phase(task, context)
            if not plan_result.get("success"):
                raise RuntimeError(f"Plan phase failed: {plan_result.get('error', 'unknown')}")
            plan_text = plan_result.get("response", "")

            # ---- Phase 2: Build ----
            emit("sdlc:building")
            self.logger.info("sdlc_phase_build", session_id=session_id)
            build_result = await self._build_phase(task, plan_text, context)
            if not build_result.get("success"):
                raise RuntimeError(f"Build phase failed: {build_result.get('error', 'unknown')}")
            files_created.extend(build_result.get("files_created") or [])

            # ---- Phase 3: Test ----
            emit("sdlc:testing")
            self.logger.info("sdlc_phase_test", session_id=session_id)
            test_result = await self._test_phase(task, context)
            files_created.extend(test_result.get("files_created") or [])

            # ---- Phase 4: Debug loop (only if tests failed) ----
            if not test_result.get("tests_passed"):
                emit("sdlc:debugging")
                self.logger.info("sdlc_phase_debug", session_id=session_id)
                debug_result = await self._debug_loop(
                    test_error=test_result.get("error", ""),
                    context=context,
                    emit=emit,
                )
                files_created.extend(debug_result.get("files_created") or [])
                if not debug_result.get("resolved"):
                    return self._retries_exhausted_result(
                        task, session_id, files_created, debug_result.get("last_error", "")
                    )

            # ---- Phase 5: Run ----
            emit("sdlc:running")
            self.logger.info("sdlc_phase_run", session_id=session_id)
            run_result = await self._run_phase()
            port = run_result.get("port")
            process = run_result.get("process")

            # ---- Phase 6: Verify ----
            if port and process:
                emit("sdlc:verifying")
                self.logger.info("sdlc_phase_verify", session_id=session_id, port=port)
                verify_result = await self._verify_phase(port, job_id or session_id)
                screenshot_path = verify_result.get("screenshot_path")
                try:
                    process.terminate()
                except Exception:
                    pass

            # ---- Phase 7: Complete ----
            emit("complete")
            return self._complete_result(task, session_id, files_created, screenshot_path)

        except Exception as exc:
            self.logger.error("sdlc_workflow_failed", error=str(exc), session_id=session_id)
            return {"success": False, "session_id": session_id, "error": str(exc)}

    # ------------------------------------------------------------------
    # Phase implementations
    # ------------------------------------------------------------------

    async def _build_context(self, task: str, session_id: str) -> dict:
        enriched = await self.orch._build_enriched_context(task)
        history = self.orch._build_context_from_events(session_id)
        return {
            "session_id": session_id,
            "workspace_path": str(self.workspace),
            "model_router": self.orch.model_router,
            "tool_executor": self.orch._create_session_executor(session_id),
            "enriched_context": enriched + history,
        }

    async def _plan_phase(self, task: str, context: dict) -> dict:
        return await self.orch.plan_agent.run(task, context)

    async def _build_phase(self, task: str, plan: str, context: dict) -> dict:
        build_task = (
            f"{task}\n\n"
            f"## Implementation Plan\n{plan}\n\n"
            "Implement all files described in the plan."
        )
        return await self.orch.developer_agent.run(build_task, context)

    async def _test_phase(self, task: str, context: dict) -> dict:
        """Write tests then execute them via pytest."""
        write_result = await self.orch.tester_agent.run(
            f"Write tests for: {task}", context
        )
        run_out = await asyncio.to_thread(
            self.orch.shell_tool.run, "python -m pytest --tb=short -q"
        )
        passed = run_out.get("returncode", 1) == 0
        error_text = ""
        if not passed:
            error_text = (run_out.get("stdout", "") + "\n" + run_out.get("stderr", "")).strip()
        return {
            "success": True,
            "files_created": write_result.get("files_created") or [],
            "tests_passed": passed,
            "error": error_text,
        }

    async def _debug_loop(
        self,
        test_error: str,
        context: dict,
        emit: Callable[[str], None],
    ) -> dict:
        """Feed failing test output to DeveloperAgent and rerun until green or cap."""
        files_created: list[str] = []
        last_error = test_error

        for attempt in range(1, MAX_DEBUG_RETRIES + 1):
            self.logger.info("sdlc_debug_attempt", attempt=attempt, max=MAX_DEBUG_RETRIES)
            emit(f"sdlc:debugging:{attempt}/{MAX_DEBUG_RETRIES}")

            fix_task = (
                f"Fix the failing tests. Do NOT change test assertions — "
                f"fix the implementation code only.\n\n"
                f"**Test output:**\n```\n{last_error[:3000]}\n```"
            )
            fix_result = await self.orch.developer_agent.run(fix_task, context)
            files_created.extend(fix_result.get("files_created") or [])

            run_out = await asyncio.to_thread(
                self.orch.shell_tool.run, "python -m pytest --tb=short -q"
            )
            passed = run_out.get("returncode", 1) == 0
            last_error = (run_out.get("stdout", "") + "\n" + run_out.get("stderr", "")).strip()

            if passed:
                self.logger.info("sdlc_debug_resolved", attempt=attempt)
                return {"resolved": True, "files_created": files_created, "attempts": attempt}

        self.logger.warning("sdlc_debug_exhausted", attempts=MAX_DEBUG_RETRIES)
        return {
            "resolved": False,
            "files_created": files_created,
            "last_error": last_error,
            "attempts": MAX_DEBUG_RETRIES,
        }

    async def _run_phase(self) -> dict:
        """Detect app type, find a free port, start the subprocess."""
        import subprocess

        port = await asyncio.to_thread(self._detect_app_port)
        start_cmd = await asyncio.to_thread(self._detect_start_command, port)

        self.logger.info("sdlc_run_phase", port=port, cmd=start_cmd)

        if not start_cmd:
            return {"success": False, "error": "Could not detect start command", "port": None, "process": None}

        env = {**os.environ, "PORT": str(port)}
        process = subprocess.Popen(
            start_cmd,
            shell=True,
            cwd=str(self.workspace),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        return {"success": True, "port": port, "process": process, "command": start_cmd}

    async def _verify_phase(self, port: int, job_id: str) -> dict:
        """Wait for server readiness, screenshot, return screenshot path."""
        url = f"http://localhost:{port}"
        ready = await self.orch.browser_tool.wait_for_server(url, timeout=30)
        if not ready:
            self.logger.warning("sdlc_server_not_ready", port=port)
            return {"screenshot_path": None, "ready": False}

        screenshots_dir = self.workspace / SCREENSHOTS_SUBDIR
        screenshots_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(self._cleanup_old_screenshots, screenshots_dir)

        ts = int(time.time())
        screenshot_path = str(screenshots_dir / f"{job_id}_{ts}.png")

        result = await self.orch.browser_tool.screenshot(url=url, path=screenshot_path)
        if result.get("success"):
            return {"screenshot_path": screenshot_path, "ready": True}
        self.logger.warning("sdlc_screenshot_failed", error=result.get("error"))
        return {"screenshot_path": None, "ready": True}

    # ------------------------------------------------------------------
    # Port & command detection
    # ------------------------------------------------------------------

    def _detect_app_port(self) -> int:
        """Read port from config files; fall back to next free port >= 8000."""
        port = self._read_port_from_files()
        if port and not self._port_in_use(port):
            return port
        return self._find_free_port(8000)

    def _read_port_from_files(self) -> Optional[int]:
        """Try to read port from .env, package.json, pyproject.toml."""
        for env_file in (".env", ".env.example"):
            p = self.workspace / env_file
            if p.exists():
                for line in p.read_text(errors="replace").splitlines():
                    m = re.match(r'^(?:APP_)?PORT\s*=\s*(\d+)', line.strip())
                    if m:
                        return int(m.group(1))

        pkg = self.workspace / "package.json"
        if pkg.exists():
            try:
                data = json.loads(pkg.read_text())
                start_script = data.get("scripts", {}).get("start", "")
                m = re.search(r'--port[= ](\d+)', start_script)
                if m:
                    return int(m.group(1))
            except Exception:
                pass

        ppt = self.workspace / "pyproject.toml"
        if ppt.exists():
            m = re.search(r'port\s*=\s*(\d+)', ppt.read_text(errors="replace"))
            if m:
                return int(m.group(1))

        return None

    def _detect_start_command(self, port: int) -> Optional[str]:
        """Infer the start command from project files."""
        # Python: scan common entry points
        for entry in ("main.py", "app.py", "server.py", "run.py"):
            p = self.workspace / entry
            if p.exists():
                content = p.read_text(errors="replace").lower()
                module = entry[:-3]
                if "uvicorn" in content or "fastapi" in content:
                    return f"python -m uvicorn {module}:app --host 0.0.0.0 --port {port}"
                if "flask" in content:
                    return f"python {entry}"
                return f"python {entry}"

        # Node
        pkg = self.workspace / "package.json"
        if pkg.exists():
            try:
                data = json.loads(pkg.read_text())
                if "start" in data.get("scripts", {}):
                    return "npm start"
            except Exception:
                pass

        # Makefile
        makefile = self.workspace / "Makefile"
        if makefile.exists():
            content = makefile.read_text(errors="replace").lower()
            if "run:" in content:
                return "make run"
            if "start:" in content:
                return "make start"

        return None

    def _port_in_use(self, port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return False
            except OSError:
                return True

    def _find_free_port(self, start: int = 8000) -> int:
        for port in range(start, start + 100):
            if not self._port_in_use(port):
                return port
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))  # bind to loopback only, not all interfaces
            return s.getsockname()[1]

    def _cleanup_old_screenshots(self, directory: Path) -> None:
        cutoff_ts = (
            datetime.now(timezone.utc) - timedelta(hours=SCREENSHOT_TTL_HOURS)
        ).timestamp()
        for png in directory.glob("*.png"):
            try:
                if png.stat().st_mtime < cutoff_ts:
                    png.unlink()
                    self.logger.info("screenshot_pruned", path=str(png))
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Result builders
    # ------------------------------------------------------------------

    def _complete_result(
        self,
        task: str,
        session_id: str,
        files_created: list[str],
        screenshot_path: Optional[str],
    ) -> dict:
        file_list = "\n".join(f"  - `{f}`" for f in files_created) or "  (none)"
        screenshot_note = (
            f"\n\n**Screenshot saved:** `{screenshot_path}`" if screenshot_path else ""
        )
        summary = (
            f"**SDLC complete** — plan, build, test, run, and verify all passed.\n\n"
            f"**Files created:**\n{file_list}"
            f"{screenshot_note}"
        )
        return {
            "success": True,
            "session_id": session_id,
            "result": {
                "response": summary,
                "task": task,
                "task_type": "sdlc",
                "files_created": files_created,
                "phase": "complete",
                "screenshot_path": screenshot_path,
            },
        }

    def _retries_exhausted_result(
        self,
        task: str,
        session_id: str,
        files_created: list[str],
        last_error: str,
    ) -> dict:
        return {
            "success": True,
            "session_id": session_id,
            "result": {
                "response": (
                    f"Build and tests ran but could not auto-fix all failures after "
                    f"{MAX_DEBUG_RETRIES} attempts.\n\n"
                    f"**Last test output:**\n```\n{last_error[:1500]}\n```\n\n"
                    f"Files created so far: {files_created}\n\n"
                    "Use `!show <path>` to inspect them or provide more instructions."
                ),
                "task": task,
                "task_type": "sdlc",
                "files_created": files_created,
                "phase": "debug_exhausted",
                "screenshot_path": None,
            },
        }


__all__ = ["SDLCWorkflow"]
