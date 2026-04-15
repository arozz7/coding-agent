"""
supervisor.py — Process manager for the coding agent.

Starts the API and bot as child processes, monitors them, and restarts
both in order when a restart is requested via POST /restart on the API.

Usage:
    python supervisor.py

Environment variables (all optional):
    AGENT_API_URL         — base URL for the agent API (default: http://localhost:5005)
    RESTART_DELAY_SECS    — seconds between service stops/starts (default: 3)
    API_STARTUP_TIMEOUT   — seconds to wait for API health check (default: 60)
    BOT_PYTHON            — Python interpreter for the bot process.
                            Defaults to sys.executable (same as the API).
                            Set this when the bot requires a different venv/install
                            than the API (e.g. BOT_PYTHON=C:/Python313/python.exe).
    DISCORD_BOT_TOKEN     — passed through to the bot subprocess automatically
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

# Load .env before reading any env vars so BOT_PYTHON and other settings are available.
try:
    from dotenv import load_dotenv as _load_dotenv
    _load_dotenv(Path(__file__).parent / ".env", override=False)
except ImportError:
    pass

# ── Paths ─────────────────────────────────────────────────────────────────────

_ROOT = Path(__file__).parent
STATE_DIR = _ROOT / ".state"
RESTART_FLAG = STATE_DIR / "restart.flag"

# ── Configuration ─────────────────────────────────────────────────────────────

API_URL = os.getenv("AGENT_API_URL", "http://127.0.0.1:5005")
_HEALTH_URL = f"{API_URL}/health"
RESTART_DELAY = int(os.getenv("RESTART_DELAY_SECS", "3"))
API_STARTUP_TIMEOUT = int(os.getenv("API_STARTUP_TIMEOUT", "120"))
_POLL_INTERVAL = 2  # seconds between restart-flag / crash checks

# Separate Python interpreter for the bot — useful when the bot's dependencies
# (e.g. discord.py) are installed in a different environment than the API.
_BOT_PYTHON = os.getenv("BOT_PYTHON", sys.executable)

# Crash backoff: how long to wait before restarting a bot that exits immediately.
# Doubles on each consecutive fast failure (< _FAST_FAIL_SECS uptime).
# After _BOT_MAX_FAST_FAILS fast failures in a row the supervisor stops retrying
# until a manual restart is requested.
_FAST_FAIL_SECS = 10
_BOT_MAX_FAST_FAILS = 5
_BOT_BACKOFF_STEPS = [2, 5, 15, 30, 60]

# Heartbeat: supervisor writes a timestamp file every _HEARTBEAT_INTERVAL seconds
# so the API can detect whether the supervisor is alive.
_HEARTBEAT_FILE = None  # set after STATE_DIR is known
_HEARTBEAT_INTERVAL = 5  # seconds

# Stale-job watchdog: if a job stays in the same phase for longer than this,
# force a full restart so a hung LLM call doesn't block overnight.
_STALE_JOB_THRESHOLD = 45 * 60  # 45 minutes in seconds
_JOBS_DB = None  # set after ROOT is known

# API-unreachable watchdog: if GET /jobs has failed this many consecutive
# stale-check cycles (each cycle is 5 min), assume the event loop is blocked
# (e.g. httpx AsyncClient blocking the asyncio loop) and force a restart.
_API_UNREACHABLE_MAX_CYCLES = 3   # 3 × 5 min = 15 min of unreachable API
_api_unreachable_cycles: int = 0


# ── Heartbeat & watchdog helpers ─────────────────────────────────────────────

def _write_heartbeat() -> None:
    """Write the current epoch timestamp to the heartbeat file."""
    try:
        _HEARTBEAT_FILE.write_text(str(time.time()))
    except Exception:
        pass


def _check_stale_job() -> bool:
    """Return True if any running job has been stuck for too long, OR if the
    API has been unreachable for _API_UNREACHABLE_MAX_CYCLES consecutive checks.

    A blocked asyncio event loop (e.g. httpx.AsyncClient stuck mid-read) makes
    the entire FastAPI server unresponsive — GET /jobs will time out.  After
    _API_UNREACHABLE_MAX_CYCLES consecutive failures we assume the server is
    hung and force a restart.

    Uses the GET /jobs API endpoint so we don't need a direct SQLite import.
    """
    global _api_unreachable_cycles
    try:
        with urllib.request.urlopen(
            f"{API_URL}/jobs?limit=5", timeout=3
        ) as resp:
            data = json.loads(resp.read())

        # API is reachable — reset the unreachable counter.
        _api_unreachable_cycles = 0

        jobs = data.get("jobs", [])
        for job in jobs:
            if job.get("status") != "running":
                continue
            # phase_updated_at is not stored yet — approximate from created_at
            # plus a conservative minimum runtime.  This is intentionally
            # simple: a stuck job always has status=running and the same phase
            # for a very long time.  We detect that by looking at created_at.
            created_raw = job.get("created_at", "")
            if not created_raw:
                continue
            from datetime import datetime, timezone
            try:
                created_dt = datetime.fromisoformat(
                    created_raw.replace("Z", "+00:00")
                )
                age = (
                    datetime.now(timezone.utc) - created_dt
                ).total_seconds()
                if age > _STALE_JOB_THRESHOLD:
                    print(
                        f"[supervisor] Stale job detected: {job.get('job_id')} "
                        f"has been running for {age/60:.0f} min — forcing restart"
                    )
                    return True
            except Exception:
                pass

    except Exception:
        # GET /jobs failed — API may be unreachable (hung event loop, crash, etc.)
        _api_unreachable_cycles += 1
        print(
            f"[supervisor] API unreachable during stale-check "
            f"(cycle {_api_unreachable_cycles}/{_API_UNREACHABLE_MAX_CYCLES})"
        )
        if _api_unreachable_cycles >= _API_UNREACHABLE_MAX_CYCLES:
            print(
                "[supervisor] API has been unreachable for "
                f"{_API_UNREACHABLE_MAX_CYCLES} consecutive checks "
                "— event loop may be blocked, forcing restart"
            )
            _api_unreachable_cycles = 0
            return True

    return False


# ── Process helpers ───────────────────────────────────────────────────────────

def _kill(proc: subprocess.Popen | None) -> None:
    """Kill a process and its entire child tree. Windows-safe via taskkill /T.
    No-op when proc is None or has already exited.
    """
    if proc is None or proc.poll() is not None:
        return
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
            capture_output=True,
        )
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _wait_for_health(timeout: int = API_STARTUP_TIMEOUT) -> bool:
    """Poll GET /health until 200 OK or timeout. Returns True on success."""
    import urllib.request

    deadline = time.monotonic() + timeout
    attempt = 0
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(_HEALTH_URL, timeout=3) as resp:
                if resp.status == 200:
                    print(f"[supervisor] API healthy (probes: {attempt + 1})")
                    return True
        except Exception:
            pass
        time.sleep(2)
        attempt += 1

    print(
        f"[supervisor] WARNING: API did not return 200 /health after {timeout}s "
        "— continuing anyway (bot will retry indefinitely)"
    )
    return False


def _start_api() -> subprocess.Popen:
    """Launch the FastAPI server as a child process using sys.executable."""
    cmd = [
        sys.executable, "-m", "uvicorn",
        "api.main:app",
        "--host", "0.0.0.0",
        "--port", "5005",
        "--log-level", "info",
    ]
    print(f"[supervisor] Starting API:  {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        cwd=str(_ROOT),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )


def _start_bot() -> subprocess.Popen:
    """Launch the Discord bot using BOT_PYTHON (may differ from sys.executable)."""
    cmd = [_BOT_PYTHON, "-m", "api.discord_bot"]
    print(f"[supervisor] Starting bot:  {' '.join(cmd)}")
    return subprocess.Popen(
        cmd,
        cwd=str(_ROOT),
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
    )


# ── Lifecycle ─────────────────────────────────────────────────────────────────

def _launch_all() -> tuple[subprocess.Popen, subprocess.Popen]:
    """Cold-start: API first, wait for health, then bot."""
    global _HEARTBEAT_FILE
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    RESTART_FLAG.unlink(missing_ok=True)
    _HEARTBEAT_FILE = STATE_DIR / "supervisor.heartbeat"
    _write_heartbeat()

    api_proc = _start_api()
    _wait_for_health()

    time.sleep(RESTART_DELAY)
    bot_proc = _start_bot()
    return api_proc, bot_proc


def _restart_all(
    api_proc: subprocess.Popen,
    bot_proc: subprocess.Popen,
) -> tuple[subprocess.Popen, subprocess.Popen]:
    """Ordered shutdown → restart of both services."""
    print("[supervisor] Restart: stopping bot...")
    _kill(bot_proc)

    print(f"[supervisor] Restart: waiting {RESTART_DELAY}s before stopping API...")
    time.sleep(RESTART_DELAY)

    print("[supervisor] Restart: stopping API...")
    _kill(api_proc)
    RESTART_FLAG.unlink(missing_ok=True)

    print(f"[supervisor] Restart: waiting {RESTART_DELAY}s before relaunch...")
    time.sleep(RESTART_DELAY)

    print("[supervisor] Relaunching all services...")
    return _launch_all()


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    print(f"[supervisor] Coding agent supervisor starting")
    print(f"[supervisor] API python: {sys.executable}")
    print(f"[supervisor] Bot python: {_BOT_PYTHON}")

    api_proc, bot_proc = _launch_all()

    bot_fast_fails = 0      # consecutive fast failures (bot died < _FAST_FAIL_SECS)
    bot_started_at = time.monotonic()
    last_heartbeat = time.monotonic()
    last_stale_check = time.monotonic()

    try:
        while True:
            time.sleep(_POLL_INTERVAL)
            now = time.monotonic()

            # Write heartbeat so the API knows we're alive
            if now - last_heartbeat >= _HEARTBEAT_INTERVAL:
                _write_heartbeat()
                last_heartbeat = now

            # Stale-job watchdog — check every 5 minutes
            if now - last_stale_check >= 300:
                last_stale_check = now
                if _check_stale_job():
                    print("[supervisor] Stale-job watchdog triggered — restarting")
                    api_proc, bot_proc = _restart_all(api_proc, bot_proc)
                    bot_fast_fails = 0
                    bot_started_at = time.monotonic()
                    continue

            # Restart requested by POST /restart
            if RESTART_FLAG.exists():
                print("[supervisor] Restart flag detected")
                api_proc, bot_proc = _restart_all(api_proc, bot_proc)
                bot_fast_fails = 0
                bot_started_at = time.monotonic()
                continue

            # Auto-recover a crashed API — restart both since bot depends on it
            if api_proc.poll() is not None:
                print(
                    f"[supervisor] API exited (code {api_proc.returncode})"
                    " — restarting all services"
                )
                api_proc, bot_proc = _restart_all(api_proc, bot_proc)
                bot_fast_fails = 0
                bot_started_at = time.monotonic()
                continue

            # Auto-recover a crashed bot — restart bot only
            if bot_proc.poll() is not None:
                uptime = time.monotonic() - bot_started_at
                is_fast_fail = uptime < _FAST_FAIL_SECS

                if is_fast_fail:
                    bot_fast_fails += 1
                    if bot_fast_fails > _BOT_MAX_FAST_FAILS:
                        print(
                            f"[supervisor] Bot crashed {bot_fast_fails} times in under "
                            f"{_FAST_FAIL_SECS}s — giving up. Fix the issue and use "
                            f"!restart or restart the supervisor."
                        )
                        # Keep running the API; don't restart the bot automatically.
                        # Wait for a manual restart flag.
                        while not RESTART_FLAG.exists():
                            time.sleep(_POLL_INTERVAL)
                        print("[supervisor] Restart flag detected — resuming")
                        api_proc, bot_proc = _restart_all(api_proc, bot_proc)
                        bot_fast_fails = 0
                        bot_started_at = time.monotonic()
                        continue

                    delay = _BOT_BACKOFF_STEPS[min(bot_fast_fails - 1, len(_BOT_BACKOFF_STEPS) - 1)]
                    print(
                        f"[supervisor] Bot exited after {uptime:.1f}s "
                        f"(code {bot_proc.returncode}, fast-fail #{bot_fast_fails}) "
                        f"— waiting {delay}s before restart"
                    )
                    time.sleep(delay)
                else:
                    bot_fast_fails = 0
                    print(
                        f"[supervisor] Bot exited (code {bot_proc.returncode},"
                        f" uptime {uptime:.0f}s) — restarting"
                    )

                bot_proc = _start_bot()
                bot_started_at = time.monotonic()

    except KeyboardInterrupt:
        print("\n[supervisor] Shutting down...")
        _kill(bot_proc)
        _kill(api_proc)
        print("[supervisor] Done.")


if __name__ == "__main__":
    main()
