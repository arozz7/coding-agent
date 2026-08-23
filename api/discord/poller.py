"""Background job poller and related Discord message helpers."""
from __future__ import annotations

import asyncio
import time
from typing import Optional

import discord
import structlog
from discord.ext import commands

from api.discord.bot_instance import bot
from api.discord.client import POLL_INTERVAL, _backoff
from api.discord.helpers import _chunk, _truncate, _send_screenshot

logger = structlog.get_logger()

_PHASE_LABELS: dict[str, str] = {
    "queued":          "Queued",
    "pending":         "Queued",
    "planning":        "Building plan",
    "planning:review": "Reviewing plan…",
    "developing":      "Writing code",
    "reviewing":       "Reviewing",
    "testing":         "Running tests",
    "designing":       "Designing architecture",
    "researching":     "Researching codebase",
    "mapping":         "Mapping project structure",
    "securing":        "Security audit",
    "documenting":     "Writing docs",
    "thinking":        "Thinking",
    "working":         "Working",
    "complete":        "Finishing up",
    "sdlc:planning":   "SDLC — Planning",
    "sdlc:building":   "SDLC — Building",
    "sdlc:testing":    "SDLC — Running tests",
    "sdlc:debugging":  "SDLC — Debugging",
    "sdlc:running":    "SDLC — Starting app",
    "sdlc:verifying":  "SDLC — Verifying (screenshot)",
}

_HEARTBEAT_INTERVAL = 60  # seconds between "still working…" edits when server is silent


async def _resolve_last_job_id(
    ctx: commands.Context,
    explicit_job_id: Optional[str],
    *,
    require_done: bool = False,
) -> Optional[str]:
    """Return the best job_id to use for !files / !result / !status.

    Priority: explicit arg → in-memory bot.user_jobs → API fallback (most recent done job).
    Returns None and posts an error to Discord if nothing can be resolved.
    """
    user_id = str(ctx.author.id)

    job_id = explicit_job_id or bot.user_jobs.get(user_id)
    if job_id:
        return job_id

    try:
        data = await bot.client.get_recent_jobs(limit=5)
        jobs = data.get("jobs", [])
        target_statuses = ("done",) if require_done else ("done", "failed", "running")
        for job in jobs:
            if job.get("status") in target_statuses:
                recovered_id = job["job_id"]
                bot.user_jobs[user_id] = recovered_id
                await ctx.send(
                    f"ℹ️ Bot was restarted — auto-recovered last job `{recovered_id}`. "
                    f"Use `!jobs` to see all recent jobs."
                )
                return recovered_id
    except Exception:
        pass

    await ctx.send(
        "No recent job found. Use `!ask <task>` to start one, "
        "or pass an explicit job ID: `!files <job_id>`."
    )
    return None


async def _safe_edit(msg: discord.Message, content: str) -> None:
    """Edit a Discord message, swallowing any Discord API failure.

    A status edit failing (message deleted, missing perms, rate limit,
    server error, ...) must never kill the _poll_job loop — the job itself
    keeps running regardless, so losing the ability to *report* progress is
    not a reason to also stop *tracking* it.
    """
    try:
        await msg.edit(content=content)
    except discord.errors.HTTPException as exc:
        logger.warning("discord_status_edit_failed", error=str(exc), status=getattr(exc, "status", None))
    except Exception as exc:
        logger.warning("discord_status_edit_failed", error=str(exc), error_type=type(exc).__name__)


def _on_poll_done(fut: asyncio.Future) -> None:
    """Log any exception that escaped _poll_job so it isn't silently dropped."""
    if not fut.cancelled() and (exc := fut.exception()):
        logger.error("poll_job_unhandled_exception", error_type=type(exc).__name__, error=str(exc))


async def _poll_job(ctx: commands.Context, status_msg: discord.Message, job_id: str):
    """Edit *status_msg* until the job finishes, then post the result.

    Chat and plan jobs stream the full response inline (chunked).
    All other job types show a short summary and point to ``!result`` / ``!files``.
    Transient HTTP failures are retried indefinitely with the backoff curve.
    """
    start = time.monotonic()
    consecutive_failures = 0
    last_label = "Working"

    _STALE_PHASE_WARN_SECS = 45 * 60
    _stale_warned = False
    _last_phase_change = time.monotonic()
    _last_phase = ""

    _INLINE_TYPES = {"chat", "plan"}

    while True:
        await asyncio.sleep(POLL_INTERVAL)
        elapsed = int(time.monotonic() - start)

        try:
            job = await bot.client.get_job(job_id)
            if consecutive_failures > 0:
                consecutive_failures = 0
                await _safe_edit(status_msg, f"{last_label}… ({elapsed}s) — reconnected")
        except Exception:
            consecutive_failures += 1
            delay = _backoff(consecutive_failures - 1)
            await _safe_edit(
                status_msg,
                f"{last_label}… ({elapsed}s) — "
                f"connection lost, next retry in {delay:.0f}s",
            )
            await asyncio.sleep(delay)
            continue

        job_status = job.get("status", "unknown")
        phase = job.get("phase", "")

        if phase.startswith("chain:"):
            parts = phase.split(":", 4)
            chain_name = parts[1] if len(parts) > 1 else "?"
            rest = parts[2] if len(parts) > 2 else ""
            if rest == "complete":
                label = f"Chain `{chain_name}` — complete"
            elif rest == "starting":
                label = f"Chain `{chain_name}` — starting"
            elif rest == "step" and len(parts) >= 5:
                step_progress = parts[3]
                agent = parts[4]
                label = f"Chain `{chain_name}` step {step_progress} [{agent}]"
            else:
                label = f"Chain `{chain_name}` — {rest}"

        elif phase.startswith("task:"):
            parts = phase.split(":", 3)
            progress = parts[1] if len(parts) > 1 else "?"
            if len(parts) >= 4:
                agent = parts[2]
                desc = parts[3][:40]
                label = f"Task {progress} [{agent}] — {desc}" if desc else f"Task {progress} [{agent}]"
            elif len(parts) == 3:
                desc = parts[2][:40]
                label = f"Task {progress} — {desc}" if desc else f"Task {progress}"
            else:
                label = f"Task {progress}"
        elif phase == "planning:tasks":
            label = "Planning tasks…"
        elif phase.startswith("sdlc:debugging:"):
            label = f"SDLC — Debugging ({phase.rsplit(':', 1)[-1]})"
        elif phase.startswith("model_switch:"):
            summary = phase[len("model_switch:"):]
            label = f"⚠️ {summary}"
            if phase != _last_phase:
                await ctx.send(
                    f"⚠️ **Model switch during your task:** {summary}\n"
                    f"Use `!model <name>` to manually override if needed."
                )
        else:
            label = _PHASE_LABELS.get(phase, phase or "Working")

        last_label = label

        if phase != _last_phase:
            _last_phase = phase
            _last_phase_change = time.monotonic()
            _stale_warned = False
        elif (
            not _stale_warned
            and job_status == "running"
            and (time.monotonic() - _last_phase_change) > _STALE_PHASE_WARN_SECS
        ):
            _stale_warned = True
            warn_mins = int(_STALE_PHASE_WARN_SECS // 60)
            await ctx.send(
                f"**Warning:** job `{job_id}` has been stuck on **{label}** "
                f"for over {warn_mins} minutes.\n"
                f"The model may have been unloaded — the supervisor will attempt "
                f"an automatic restart. You can also use `!restart` manually."
            )

        if job_status == "done":
            task_type = job.get("task_type", "")
            files = job.get("files_created", [])
            screenshot_path = job.get("screenshot_path")

            if job.get("handover_triggered") and job.get("new_session_id"):
                new_sid = job["new_session_id"]
                user_id = str(ctx.author.id)
                bot.user_sessions[user_id] = new_sid
                await ctx.send(
                    f"**Context bridged** — session was near capacity so a fresh "
                    f"session was started and pre-loaded with a summary of our work. "
                    f"New session: `{new_sid}`. Everything continues seamlessly."
                )
            elif job.get("context_budget") == "warn":
                await ctx.send(
                    "**Heads-up:** context window is 75 %+ full. "
                    "The next task may trigger an automatic context bridge."
                )

            if task_type in _INLINE_TYPES:
                try:
                    result_data = await bot.client.get_job_result(job_id)
                    full = (result_data.get("result") or "").strip()
                except Exception as exc:
                    full = ""
                    await _safe_edit(status_msg, f"Done [{task_type}] · {elapsed}s (could not fetch result: {exc})")
                    return

                if not full:
                    await _safe_edit(status_msg, f"Done [{task_type}] · {elapsed}s — (empty response)")
                    return

                await _safe_edit(status_msg, f"**Done** [{task_type}] · {elapsed}s")
                chunks = _chunk(full)
                for chunk in chunks:
                    await ctx.send(chunk)
                    await asyncio.sleep(0.3)

            else:
                summary = job.get("summary") or "(task complete)"
                lines = [f"**Done** [{task_type}] · {elapsed}s\n", summary]
                if files:
                    file_lines = "\n".join(f"  `{f}`" for f in files[:10])
                    lines.append(f"\n**Files created/modified:**\n{file_lines}")
                lines.append(
                    "\n`!result` — full response  ·  `!files` — file list  ·  `!show <path>` — view a file"
                )
                await _safe_edit(status_msg, _truncate("\n".join(lines)))

            if screenshot_path:
                await _send_screenshot(ctx, screenshot_path, task_type, elapsed)
            return

        elif job_status == "failed":
            error = (job.get("error") or "unknown error")[:400]
            await _safe_edit(status_msg, f"**Task failed** after {elapsed}s:\n```\n{error}\n```")
            return

        elif job_status == "cancelled":
            await _safe_edit(status_msg, f"Task cancelled after {elapsed}s.")
            return

        else:
            await _safe_edit(status_msg, f"{label}… ({elapsed}s elapsed)")
