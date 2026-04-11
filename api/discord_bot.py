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
        return f"[{lang} — {n} lines · use !show to view]"

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


# ---------------------------------------------------------------------------
# Async API client
# ---------------------------------------------------------------------------

class AgentClient:
    """Thin async wrapper around the agent REST API."""

    def __init__(self, api_url: str = API_URL):
        self.api_url = api_url

    async def _get(self, path: str, **params) -> dict:
        async with httpx.AsyncClient(timeout=20.0) as c:
            r = await c.get(f"{self.api_url}{path}", params=params or None)
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
    "queued":      "Queued",
    "pending":     "Queued",
    "developing":  "Writing code",
    "reviewing":   "Reviewing",
    "testing":     "Running tests",
    "designing":   "Designing architecture",
    "researching": "Researching codebase",
    "thinking":    "Thinking",
    "working":     "Working",
    "complete":    "Finishing up",
}


async def _poll_job(ctx: commands.Context, status_msg: discord.Message, job_id: str):
    """Edit *status_msg* until the job finishes, then post the result.

    Chat and research jobs stream the full response inline (chunked).
    All other job types (develop, review, test, architect) show a short
    summary and point the user to ``!result`` / ``!files``.
    """
    start = time.monotonic()
    # Task types whose full response should be shown inline in the channel.
    _INLINE_TYPES = {"chat", "research"}

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        elapsed = int(time.monotonic() - start)

        try:
            job = await bot.client.get_job(job_id)
        except Exception as exc:
            await status_msg.edit(content=f"Lost contact with agent: {exc}")
            return

        job_status = job.get("status", "unknown")
        phase = job.get("phase", "")
        label = _PHASE_LABELS.get(phase, phase or "Working")

        if job_status == "done":
            task_type = job.get("task_type", "")
            files = job.get("files_created", [])

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


@bot.command(name="show")
async def show(ctx: commands.Context, *, path: str):
    """View a workspace file. Small files inline, large files as attachment."""
    try:
        data = await bot.client.get_file(path.strip())
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

    lines_count = data.get("lines", len(content.splitlines()))
    ext = path.rsplit(".", 1)[-1] if "." in path else ""
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
