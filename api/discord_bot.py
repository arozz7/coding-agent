"""Discord bot for the local coding agent.

UX model
--------
- `!ask <task>` submits a task to the background job API and immediately
  returns. A background coroutine edits the status message every few seconds
  with phase updates. When the job finishes the message is replaced with a
  short summary — no code is ever dumped into the channel.
- Code output lives server-side. Use `!show <path>` to upload a file,
  `!result` to view the prose response, `!files` to list created files.
"""

import asyncio
import io
import os
import re
import subprocess
import time
from typing import Optional

import discord
import httpx
from discord import Intents, Message, File
from discord.ext import commands

API_URL = os.getenv("AGENT_API_URL", "http://localhost:5005")
POLL_INTERVAL = int(os.getenv("BOT_POLL_INTERVAL", "5"))  # seconds


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def strip_code_blocks(text: str) -> str:
    """Replace fenced code blocks with a one-liner so prose stays readable."""
    def _replace(m: re.Match) -> str:
        lang = m.group(1).strip() or "code"
        n = len(m.group(2).strip().splitlines())
        return f"[{lang} — {n} lines · use `!files` then `!show <path>` to view]"

    return re.sub(r'```(\w*)\n([\s\S]*?)```', _replace, text)


def _chunk(text: str, limit: int = 1900) -> list[str]:
    """Split text into Discord-safe chunks, breaking on newlines where possible."""
    chunks: list[str] = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Try to break at a newline
        split = text.rfind("\n", 0, limit)
        if split == -1:
            split = limit
        chunks.append(text[:split])
        text = text[split:].lstrip("\n")
    return chunks


def _truncate(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


async def _send_screenshot(
    ctx: commands.Context,
    screenshot_path: str,
    task_type: str,
    elapsed: int,
) -> None:
    """Send a screenshot file as a Discord attachment with a caption."""
    import pathlib

    p = pathlib.Path(screenshot_path)
    if not p.exists() or not p.is_file():
        await ctx.send(f"Screenshot was captured but the file is no longer available: `{screenshot_path}`")
        return

    size_bytes = p.stat().st_size
    if size_bytes > _MAX_ATTACHMENT_BYTES:
        await ctx.send(
            f"Screenshot too large to attach ({size_bytes / 1024 / 1024:.1f} MB). "
            f"Use `!show {p.name}` or check the workspace directly."
        )
        return

    caption = f"**SDLC verify** [{task_type}] · {elapsed}s — running app screenshot"
    with p.open("rb") as fh:
        await ctx.send(caption, file=File(fh, filename=p.name))


# ---------------------------------------------------------------------------
# Async API client
# ---------------------------------------------------------------------------

class AgentClient:
    """Thin async wrapper around the agent REST API."""

    def __init__(self, api_url: str = API_URL):
        self.api_url = api_url

    async def _get(self, endpoint: str, **params) -> dict:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(f"{self.api_url}{endpoint}", params=params or None)
            r.raise_for_status()
            return r.json()

    async def _post(self, path: str, body: dict, timeout: float = 30.0) -> dict:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post(f"{self.api_url}{path}", json=body)
            r.raise_for_status()
            return r.json()

    async def _delete(self, path: str) -> dict:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.delete(f"{self.api_url}{path}")
            r.raise_for_status()
            return r.json()

    async def start_task(self, task: str, session_id: Optional[str] = None) -> dict:
        payload: dict = {"task": task}
        if session_id:
            payload["session_id"] = session_id
        return await self._post("/task/start", payload)

    async def get_job(self, job_id: str) -> dict:
        return await self._get(f"/task/{job_id}")

    async def get_job_result(self, job_id: str) -> dict:
        return await self._get(f"/task/{job_id}/result")

    async def cancel_job(self, job_id: str) -> dict:
        return await self._delete(f"/task/{job_id}")

    async def get_job_tasks(self, job_id: str) -> dict:
        return await self._get(f"/task/{job_id}/tasks")

    async def get_file(self, path: str) -> dict:
        return await self._get("/workspace/file", path=path)

    async def get_session_history(self, session_id: str) -> dict:
        return await self._get(f"/sessions/{session_id}")

    async def list_sessions(self) -> dict:
        return await self._get("/sessions")

    async def delete_session(self, session_id: str) -> dict:
        return await self._delete(f"/sessions/{session_id}")

    # Sync health probe — only called before the event loop starts
    def is_reachable(self) -> bool:
        import requests as _req
        try:
            _req.get(f"{self.api_url}/health", timeout=5).raise_for_status()
            return True
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------

class DiscordAgentBot(commands.Bot):
    def __init__(self):
        intents = Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.client = AgentClient()
        # Per-user state
        self.user_sessions: dict[str, str] = {}   # user_id → session_id
        self.user_jobs: dict[str, str] = {}        # user_id → current job_id

    async def on_ready(self):
        print(f"[bot] Logged in as {self.user}  |  API: {API_URL}")

    async def on_message(self, message: Message):
        if message.author == self.user:
            return
        if not message.content.startswith("!"):
            return
        await self.process_commands(message)


bot = DiscordAgentBot()


# ---------------------------------------------------------------------------
# Background job poller
# ---------------------------------------------------------------------------

_PHASE_LABELS: dict[str, str] = {
    "queued":        "Queued",
    "pending":       "Queued",
    "planning":      "Building plan",
    "developing":    "Writing code",
    "reviewing":     "Reviewing",
    "testing":       "Running tests",
    "designing":     "Designing architecture",
    "researching":   "Researching codebase",
    "thinking":      "Thinking",
    "working":       "Working",
    "complete":      "Finishing up",
    # SDLC pipeline phases
    "sdlc:planning":  "SDLC — Planning",
    "sdlc:building":  "SDLC — Building",
    "sdlc:testing":   "SDLC — Running tests",
    "sdlc:debugging": "SDLC — Debugging",
    "sdlc:running":   "SDLC — Starting app",
    "sdlc:verifying": "SDLC — Verifying (screenshot)",
}


_MAX_POLL_FAILURES = 6        # give up after this many consecutive errors
_HEARTBEAT_INTERVAL = 60     # seconds between "still working…" edits when server is silent


async def _poll_job(ctx: commands.Context, status_msg: discord.Message, job_id: str):
    """Edit *status_msg* until the job finishes, then post the result.

    Chat and research jobs stream the full response inline (chunked).
    All other job types (develop, review, test, architect) show a short
    summary and point the user to ``!result`` / ``!files``.

    Resilience: transient HTTP failures are retried up to _MAX_POLL_FAILURES
    consecutive times before giving up.  A heartbeat edit is sent every
    _HEARTBEAT_INTERVAL seconds so the status message stays visibly alive
    during long-running tasks.
    """
    start = time.monotonic()
    consecutive_failures = 0
    last_label = "Working"

    # Task types whose full response should be shown inline in the channel.
    _INLINE_TYPES = {"chat", "research", "plan"}

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        elapsed = int(time.monotonic() - start)

        try:
            job = await bot.client.get_job(job_id)
            consecutive_failures = 0  # reset on success
        except Exception as exc:
            consecutive_failures += 1
            err_str = str(exc) or type(exc).__name__
            if consecutive_failures >= _MAX_POLL_FAILURES:
                await status_msg.edit(
                    content=(
                        f"Lost contact with agent after {consecutive_failures} retries "
                        f"({elapsed}s elapsed). Last status: **{last_label}**\n"
                        f"Error: `{err_str}`\n"
                        f"The job may still be running — use `!status` to check once "
                        f"the server is back."
                    )
                )
                return
            # Transient failure — update message and keep polling
            await status_msg.edit(
                content=(
                    f"{last_label}… ({elapsed}s) — "
                    f"reconnecting [{consecutive_failures}/{_MAX_POLL_FAILURES}]"
                )
            )
            continue

        job_status = job.get("status", "unknown")
        phase = job.get("phase", "")

        # Task-loop phases: "task:N/M:description" or "planning:tasks"
        if phase.startswith("task:"):
            parts = phase.split(":", 2)
            progress = parts[1] if len(parts) > 1 else "?"
            desc = parts[2][:40] if len(parts) > 2 else ""
            label = f"Task {progress} — {desc}" if desc else f"Task {progress}"
        elif phase == "planning:tasks":
            label = "Planning tasks…"
        elif phase.startswith("sdlc:debugging:"):
            label = f"SDLC — Debugging ({phase.rsplit(':', 1)[-1]})"
        else:
            label = _PHASE_LABELS.get(phase, phase or "Working")

        last_label = label  # keep for reconnect messages

        if job_status == "done":
            task_type = job.get("task_type", "")
            files = job.get("files_created", [])
            screenshot_path = job.get("screenshot_path")

            if task_type in _INLINE_TYPES:
                # Fetch and stream the full response for conversational tasks.
                try:
                    result_data = await bot.client.get_job_result(job_id)
                    full = (result_data.get("result") or "").strip()
                except Exception as exc:
                    full = ""
                    await status_msg.edit(content=f"Done [{task_type}] · {elapsed}s (could not fetch result: {exc})")
                    return

                if not full:
                    await status_msg.edit(content=f"Done [{task_type}] · {elapsed}s — (empty response)")
                    return

                # Edit the status message to a short header, then send chunks.
                await status_msg.edit(content=f"**Done** [{task_type}] · {elapsed}s")
                chunks = _chunk(full)
                for chunk in chunks:
                    await ctx.send(chunk)
                    await asyncio.sleep(0.3)  # avoid Discord rate-limit dropping messages

            else:
                # Summarise-only for file-producing tasks.
                summary = job.get("summary") or "(task complete)"
                lines = [f"**Done** [{task_type}] · {elapsed}s\n", summary]
                if files:
                    file_lines = "\n".join(f"  `{f}`" for f in files[:10])
                    lines.append(f"\n**Files created/modified:**\n{file_lines}")
                lines.append(
                    "\n`!result` — full response  ·  `!files` — file list  ·  `!show <path>` — view a file"
                )
                await status_msg.edit(content=_truncate("\n".join(lines)))

            # Send screenshot as attachment if the SDLC workflow produced one
            if screenshot_path:
                await _send_screenshot(ctx, screenshot_path, task_type, elapsed)
            return

        elif job_status == "failed":
            error = (job.get("error") or "unknown error")[:400]
            await status_msg.edit(content=f"**Task failed** after {elapsed}s:\n```\n{error}\n```")
            return

        elif job_status == "cancelled":
            await status_msg.edit(content=f"Task cancelled after {elapsed}s.")
            return

        else:
            await status_msg.edit(content=f"{label}… ({elapsed}s elapsed)")


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

@bot.command(name="ask")
async def ask(ctx: commands.Context, *, task: str):
    """Submit a task. The agent works in the background — this message updates live."""
    user_id = str(ctx.author.id)
    session_id = bot.user_sessions.get(user_id, user_id)

    status_msg = await ctx.send("Submitting…")

    try:
        resp = await bot.client.start_task(task, session_id)
    except Exception as exc:
        await status_msg.edit(content=f"Could not reach agent: {exc}")
        return

    job_id = resp.get("job_id")
    if not job_id:
        await status_msg.edit(content=f"No job ID returned: {resp}")
        return

    bot.user_sessions[user_id] = resp.get("session_id", session_id)
    bot.user_jobs[user_id] = job_id

    task_type = resp.get("task_type", "")
    await status_msg.edit(content=f"Got it [{task_type}] — working on it…")
    asyncio.create_task(_poll_job(ctx, status_msg, job_id))


@bot.command(name="status")
async def status(ctx: commands.Context):
    """Show the status of your current background job."""
    user_id = str(ctx.author.id)
    job_id = bot.user_jobs.get(user_id)
    if not job_id:
        await ctx.send("No active job. Use `!ask <task>` to start one.")
        return
    try:
        job = await bot.client.get_job(job_id)
    except Exception as exc:
        await ctx.send(f"Could not fetch status: {exc}")
        return

    s = job.get("status", "?")
    phase = job.get("phase", "")
    task_preview = (job.get("task") or "")[:80]
    await ctx.send(
        f"**Job:** `{job_id}`\n**Status:** {s} [{phase}]\n**Task:** {task_preview}…"
    )


@bot.command(name="cancel")
async def cancel(ctx: commands.Context):
    """Cancel your current running job."""
    user_id = str(ctx.author.id)
    job_id = bot.user_jobs.get(user_id)
    if not job_id:
        await ctx.send("No active job to cancel.")
        return
    try:
        await bot.client.cancel_job(job_id)
        await ctx.send(f"Cancellation requested for `{job_id}`.")
    except Exception as exc:
        await ctx.send(f"Could not cancel: {exc}")


@bot.command(name="result")
async def result(ctx: commands.Context):
    """Show the agent's prose response from the last job (code blocks stripped)."""
    user_id = str(ctx.author.id)
    job_id = bot.user_jobs.get(user_id)
    if not job_id:
        await ctx.send("No recent job. Use `!ask <task>` first.")
        return
    try:
        data = await bot.client.get_job_result(job_id)
    except Exception as exc:
        await ctx.send(f"Could not fetch result: {exc}")
        return

    if data.get("status") != "done":
        await ctx.send(f"Job not done yet (status: {data.get('status')}). Try again shortly.")
        return

    full = data.get("result") or "(empty response)"
    clean = strip_code_blocks(full).strip()

    if not clean:
        await ctx.send(
            "The response was all code. Use `!files` to see what was created, "
            "then `!show <path>` to view a file."
        )
        return

    for chunk in _chunk(clean):
        await ctx.send(chunk)
        await asyncio.sleep(0.3)  # avoid Discord rate-limit dropping messages


@bot.command(name="files")
async def files(ctx: commands.Context):
    """List files created or modified by the last task."""
    user_id = str(ctx.author.id)
    job_id = bot.user_jobs.get(user_id)
    if not job_id:
        await ctx.send("No recent job. Use `!ask <task>` first.")
        return
    try:
        job = await bot.client.get_job(job_id)
    except Exception as exc:
        await ctx.send(f"Could not fetch job: {exc}")
        return

    created = job.get("files_created", [])
    if not created:
        await ctx.send("No files were created or modified in the last task.")
        return

    lines = ["**Files from last task:**"] + [f"  `{f}`" for f in created]
    lines.append("\nUse `!show <path>` to view any of these.")
    await ctx.send("\n".join(lines))


_STATUS_ICONS = {
    "pending":  "⏳",
    "running":  "▶️",
    "done":     "✅",
    "failed":   "❌",
    "skipped":  "⏭️",
}


@bot.command(name="tasks")
async def tasks_cmd(ctx: commands.Context):
    """Show the task list for the current job."""
    user_id = str(ctx.author.id)
    job_id = bot.user_jobs.get(user_id)
    if not job_id:
        await ctx.send("No recent job. Use `!ask <task>` first.")
        return

    try:
        data = await bot.client.get_job_tasks(job_id)
    except Exception as exc:
        await ctx.send(f"Could not fetch task list: {exc}")
        return

    task_list = data.get("tasks", [])
    if not task_list:
        await ctx.send(
            "No task plan yet — the agent may still be planning, or this job "
            "type doesn't use the task manager (chat/plan/review)."
        )
        return

    counts = data.get("counts", {})
    total = data.get("total", len(task_list))
    done_count = counts.get("done", 0) + counts.get("skipped", 0)

    lines = [f"**Task plan** ({done_count}/{total} done)\n"]
    for t in task_list:
        icon = _STATUS_ICONS.get(t["status"], "•")
        agent = t["agent_type"]
        seq = t["sequence"]

        # Truncate description at a word boundary
        raw_desc = t["description"]
        if len(raw_desc) > 72:
            cut = raw_desc[:72].rsplit(" ", 1)[0]
            desc = cut + "…"
        else:
            desc = raw_desc

        # Strip code blocks and leading whitespace from result snippet
        result_snippet = ""
        if t.get("result") and t["status"] in ("done", "failed"):
            raw = re.sub(r'```[\s\S]*?```', '', t["result"]).strip()
            # Take first non-empty line as snippet
            first_line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
            if first_line:
                snippet = first_line[:60] + ("…" if len(first_line) > 60 else "")
                result_snippet = f"\n    › {snippet}"

        lines.append(f"{icon} **{seq}.** [{agent}] {desc}{result_snippet}")

    await ctx.send(_truncate("\n".join(lines), 1900))


_BINARY_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "bmp", "ico", "svg", "webp",
    "pdf", "zip", "tar", "gz", "bz2", "7z", "rar",
    "exe", "dll", "so", "dylib", "bin", "whl",
    "mp3", "mp4", "wav", "ogg", "avi", "mov",
    "db", "sqlite", "sqlite3",
}
_MAX_ATTACHMENT_BYTES = 7 * 1024 * 1024  # 7 MB (Discord cap is 8 MB)


@bot.command(name="show")
async def show(ctx: commands.Context, *, path: str = ""):
    """View a workspace file. Small files inline, large files as attachment."""
    path = path.strip()
    if not path:
        await ctx.send(
            "Usage: `!show <file path>`\n"
            "Example: `!show workspace/app.py`\n"
            "Use `!files` to list files created by the last task."
        )
        return

    # Reject known binary extensions before even fetching
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if ext in _BINARY_EXTENSIONS:
        await ctx.send(
            f"**`{path}`** is a binary file (`{ext}`) and cannot be displayed in Discord."
        )
        return

    try:
        data = await bot.client.get_file(path)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            await ctx.send(f"File not found: `{path}`")
        else:
            await ctx.send(f"Error {exc.response.status_code}: {exc.response.text[:200]}")
        return
    except Exception as exc:
        await ctx.send(f"Could not read file: {exc}")
        return

    if "error" in data:
        await ctx.send(f"Error: {data['error']}")
        return

    content = data.get("content", "")
    if not content:
        await ctx.send(f"`{path}` is empty.")
        return

    size_bytes = data.get("size", len(content.encode("utf-8", errors="replace")))
    if size_bytes > _MAX_ATTACHMENT_BYTES:
        mb = size_bytes / 1024 / 1024
        await ctx.send(
            f"**`{path}`** is too large to upload ({mb:.1f} MB — Discord limit is 8 MB). "
            f"Access it directly from the workspace."
        )
        return

    lines_count = data.get("lines", len(content.splitlines()))
    filename = path.replace("\\", "/").split("/")[-1]

    if len(content) <= 1800:
        await ctx.send(f"**`{path}`** ({lines_count} lines)\n```{ext}\n{content}\n```")
    else:
        await ctx.send(
            f"**`{path}`** ({lines_count} lines):",
            file=File(io.BytesIO(content.encode("utf-8")), filename=filename),
        )


@bot.command(name="history")
async def history(ctx: commands.Context):
    """Show the last 5 messages in your session (code stripped)."""
    user_id = str(ctx.author.id)
    session_id = bot.user_sessions.get(user_id, user_id)
    try:
        data = await bot.client.get_session_history(session_id)
    except Exception as exc:
        await ctx.send(f"Error: {exc}")
        return

    messages = data.get("history", [])
    if not messages:
        await ctx.send("No conversation history yet.")
        return

    lines = [f"**{len(messages)} messages in session:**"]
    for msg in messages[-5:]:
        role = msg.get("role", "?")
        preview = strip_code_blocks(msg.get("content", ""))[:120].replace("\n", " ")
        lines.append(f"**{role}:** {preview}…")
    await ctx.send("\n".join(lines))


@bot.command(name="sessions")
async def list_sessions(ctx: commands.Context):
    """List all sessions."""
    try:
        data = await bot.client.list_sessions()
    except Exception as exc:
        await ctx.send(f"Error: {exc}")
        return

    sessions = data.get("sessions", [])
    if not sessions:
        await ctx.send("No sessions found.")
        return

    lines = [f"**{len(sessions)} sessions:**"]
    for s in sessions[:8]:
        sid = s.get("session_id", "?")
        count = s.get("message_count", 0)
        lines.append(f"  `{sid}` — {count} messages")
    await ctx.send("\n".join(lines))


@bot.command(name="clear")
async def clear(ctx: commands.Context):
    """Clear your conversation history."""
    user_id = str(ctx.author.id)
    session_id = bot.user_sessions.get(user_id, user_id)
    try:
        await bot.client.delete_session(session_id)
        bot.user_jobs.pop(user_id, None)
        await ctx.send("Conversation cleared.")
    except Exception as exc:
        await ctx.send(f"Could not clear session: {exc}")


@bot.command(name="session")
async def session_info(ctx: commands.Context):
    """Show your current session ID and last job ID."""
    user_id = str(ctx.author.id)
    session_id = bot.user_sessions.get(user_id, user_id)
    job_id = bot.user_jobs.get(user_id, "none")
    await ctx.send(f"**Session:** `{session_id}`\n**Last job:** `{job_id}`")


@bot.command(name="workspace")
async def workspace(ctx: commands.Context):
    """Show the current workspace path and its top-level contents."""
    try:
        data = await bot.client._get("/workspace")
        await ctx.send(f"**Workspace:** `{data.get('workspace', 'unknown')}`")

        dirs = await bot.client._get("/workspace/directories")
        items = dirs.get("items", [])
        if items:
            lines = ["**Contents:**"]
            for item in items[:12]:
                icon = "📁" if item["type"] == "directory" else "📄"
                lines.append(f"{icon} `{item['name']}`")
            await ctx.send("\n".join(lines))
    except Exception as exc:
        await ctx.send(f"Error: {exc}")


@bot.command(name="git")
async def git_cmd(ctx: commands.Context, *, args: str):
    """Run a safe read-only git command: status, log, diff, branch."""
    allowed = {"status", "log", "diff", "branch"}
    first = args.strip().split()[0].lower()
    if first not in allowed:
        await ctx.send(f"Only allowed: `{', '.join(sorted(allowed))}`")
        return
    try:
        # Run in a thread so the event loop (and Discord heartbeat) stay free
        out = await asyncio.to_thread(
            subprocess.run,
            f"git {args}",
            capture_output=True, text=True, shell=True, timeout=15,
        )
        text = (out.stdout or out.stderr or "No output")[:1800]
        await ctx.send(f"```\n{text}\n```")
    except Exception as exc:
        await ctx.send(f"Error: {exc}")


@bot.command(name="models")
async def list_models(ctx: commands.Context):
    """List all configured models and show which one is active."""
    try:
        data = await bot.client._get("/models")
    except Exception as exc:
        await ctx.send(f"Error: {exc}")
        return

    active = data.get("active_model") or "(default)"
    lines = [f"**Models** · active: `{active}`\n"]
    for m in data.get("models", []):
        marker = "**[active]**" if m.get("is_active") else "       "
        name = m["name"]
        mtype = m.get("type", "?")
        ctx_k = m.get("context_window", 0) // 1000
        lines.append(f"{marker} `{name}` — {mtype} · {ctx_k}k ctx")
    lines.append("\nUse `!model <name>` to switch · `!model reset` to restore default")
    await ctx.send("\n".join(lines))


@bot.command(name="model")
async def switch_model(ctx: commands.Context, *, name: str = ""):
    """Switch the active model. `!model` shows current. `!model reset` restores default."""
    name = name.strip()

    if not name:
        # Show current
        try:
            data = await bot.client._get("/models/active")
        except Exception as exc:
            await ctx.send(f"Error: {exc}")
            return
        effective = data.get("effective_model", "?")
        active = data.get("active_model") or "(yaml default)"
        await ctx.send(
            f"**Current model:** `{effective}`\n"
            f"**Active override:** {active}\n"
            f"Use `!models` to list all · `!model <name>` to switch"
        )
        return

    # Reset to default
    if name.lower() == "reset":
        try:
            data = await bot.client._post("/models/active", {"model": None})
        except Exception as exc:
            await ctx.send(f"Error: {exc}")
            return
        await ctx.send(f"Model reset to default: `{data.get('active_model', '?')}`")
        return

    # Switch to named model
    try:
        data = await bot.client._post("/models/active", {"model": name})
    except httpx.HTTPStatusError as exc:
        body = exc.response.json() if exc.response.content else {}
        detail = body.get("detail", exc.response.text[:200])
        await ctx.send(f"Could not switch model: {detail}")
        return
    except Exception as exc:
        await ctx.send(f"Error: {exc}")
        return

    await ctx.send(f"Switched to `{data.get('active_model', name)}` — {data.get('message', '')}")


@bot.command(name="jobs")
async def list_jobs(ctx: commands.Context, limit: int = 10):
    """List your recent jobs (newest first). Optionally pass a number: !jobs 20"""
    from datetime import datetime, timezone
    try:
        data = await bot.client._get("/jobs", limit=min(limit, 50))
    except Exception as exc:
        await ctx.send(f"Error: {exc}")
        return

    jobs = data.get("jobs", [])
    if not jobs:
        await ctx.send("No jobs found yet. Use `!ask <task>` to start one.")
        return

    now = datetime.now(timezone.utc)
    _status_icon = {"done": "✅", "failed": "❌", "running": "⏳",
                    "cancelled": "🚫", "pending": "⏸️"}

    lines = [f"**Recent jobs** ({len(jobs)}):\n"]
    for job in jobs:
        job_id   = job.get("job_id", "?")
        status   = job.get("status", "?")
        ttype    = job.get("task_type", "?")
        preview  = (job.get("task") or "")[:55].replace("\n", " ")
        created  = job.get("created_at", "")
        icon     = _status_icon.get(status, "❔")

        age = ""
        if created:
            try:
                dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                mins = int((now - dt).total_seconds() // 60)
                age = f"{mins}m ago" if mins < 60 else f"{mins // 60}h ago"
            except Exception:
                pass

        lines.append(f"{icon} `{job_id}` [{ttype}] {age}\n   {preview}…")

    for chunk in _chunk("\n".join(lines)):
        await ctx.send(chunk)


@bot.command(name="skills")
async def skills_cmd(ctx: commands.Context, action: str = "list"):
    """Manage agent skills. Usage: !skills  |  !skills fetch"""
    if action == "fetch":
        msg = await ctx.send("Fetching skills from remote registry…")
        try:
            data = await bot.client._post("/skills/fetch", {}, timeout=30.0)
            fetched = data.get("fetched", 0)
            skipped = data.get("skipped", 0)
            await msg.edit(
                content=f"Skills updated — {fetched} fetched, {skipped} already current."
            )
        except Exception as exc:
            await msg.edit(content=f"Fetch failed: {exc}")
        return

    # Default: list loaded skills
    try:
        data = await bot.client._get("/skills")
    except Exception as exc:
        await ctx.send(f"Error: {exc}")
        return

    skills = data.get("skills", [])
    if not skills:
        await ctx.send(
            "No skills loaded. Run `!skills fetch` to download from the remote registry."
        )
        return

    lines = [f"**{len(skills)} skill(s) loaded:**"]
    for s in skills:
        lines.append(f"  `{s['name']}` — {(s.get('description') or '')[:70]}")
    lines.append("\nUse `!skills fetch` to update from remote.")
    await ctx.send("\n".join(lines))


@bot.command(name="helpme")
async def helpme(ctx: commands.Context):
    """Show available commands."""
    help_text = (
        "**Agent Commands**\n\n"
        "**Core workflow:**\n"
        "`!ask <task>` — Submit a task. Agent works in background; this message updates live.\n"
        "`!status` — Check your current job's status and phase\n"
        "`!cancel` — Cancel your running job\n\n"
        "**Viewing results:**\n"
        "`!result` — Show prose response (code blocks stripped)\n"
        "`!files` — List files created/modified in the last task\n"
        "`!show <path>` — View a workspace file (attachment for large files)\n\n"
        "**Session:**\n"
        "`!history` — Last 5 messages in your session\n"
        "`!session` — Your session ID and last job ID\n"
        "`!clear` — Clear conversation history\n"
        "`!sessions` — List all sessions\n\n"
        "**Workspace:**\n"
        "`!workspace` — Show workspace path and top-level contents\n\n"
        "**Models:**\n"
        "`!models` — List all configured models\n"
        "`!model` — Show active model\n"
        "`!model <name>` — Switch to a different model\n"
        "`!model reset` — Revert to the default from models.yaml\n\n"
        "**Jobs:**\n"
        "`!jobs` — List recent jobs (newest first)\n"
        "`!jobs 20` — List up to 20 recent jobs\n\n"
        "**Skills:**\n"
        "`!skills` — List loaded agent skills\n"
        "`!skills fetch` — Download latest skills from remote registry\n\n"
        "**Utilities:**\n"
        "`!git <status|log|diff|branch>` — Safe read-only git commands\n"
        "`!helpme` — This help text\n"
    )
    await ctx.send(help_text)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_bot(token: str):
    if not token:
        print("ERROR: DISCORD_BOT_TOKEN is not set.")
        return
    print(f"[bot] Starting — API: {API_URL}")
    bot.run(token)


if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    run_bot(token)
