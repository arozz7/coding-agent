"""Job inspection commands: !status, !cancel, !result, !files, !tasks."""
from __future__ import annotations

import asyncio
import re
from typing import Optional

from discord.ext import commands

from api.discord.bot_instance import bot
from api.discord.helpers import _chunk, _truncate, strip_code_blocks
from api.discord.poller import _resolve_last_job_id

_STATUS_ICONS = {
    "pending":  "⏳",
    "running":  "▶️",
    "done":     "✅",
    "failed":   "❌",
    "skipped":  "⏭️",
}


@bot.command(name="status")
async def status(ctx: commands.Context, job_id: Optional[str] = None):
    """Show the status of your current background job. Specify a job_id if the bot restarted."""
    user_id = str(ctx.author.id)
    job_id = job_id or bot.user_jobs.get(user_id)
    if not job_id:
        await ctx.send("No active job. Use `!ask <task>` to start one, or pass an explicit job ID: `!status <job_id>`.")
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
async def cancel(ctx: commands.Context, job_id: Optional[str] = None):
    """Cancel your current running job. Specify a job_id if the bot restarted."""
    user_id = str(ctx.author.id)
    job_id = job_id or bot.user_jobs.get(user_id)
    if not job_id:
        await ctx.send("No active job to cancel. Pass an explicit job ID: `!cancel <job_id>`.")
        return
    try:
        await bot.client.cancel_job(job_id)
        await ctx.send(f"Cancellation requested for `{job_id}`.")
    except Exception as exc:
        await ctx.send(f"Could not cancel: {exc}")


@bot.command(name="result")
async def result(ctx: commands.Context, job_id: Optional[str] = None):
    """Show the agent's prose response from the last job (code blocks stripped). Specify a job_id if the bot restarted."""
    job_id = await _resolve_last_job_id(ctx, job_id, require_done=True)
    if not job_id:
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
        await asyncio.sleep(0.3)


@bot.command(name="files")
async def files(ctx: commands.Context, job_id: Optional[str] = None):
    """List files created or modified by the last task. Specify a job_id if the bot restarted."""
    job_id = await _resolve_last_job_id(ctx, job_id, require_done=False)
    if not job_id:
        return
    try:
        job = await bot.client.get_job(job_id)
    except Exception as exc:
        await ctx.send(f"Could not fetch job: {exc}")
        return

    created = job.get("files_created", [])
    if not created:
        await ctx.send(
            f"No files were recorded for job `{job_id}` "
            f"(status: {job.get('status', '?')}).\n"
            "If files were created via the agent's response text (not FILE: blocks), "
            "use `!result` to read the full output."
        )
        return

    lines = [f"**Files from job `{job_id}`:**"] + [f"  `{f}`" for f in created]
    lines.append("\nUse `!show <path>` to view any of these.")
    await ctx.send("\n".join(lines))


@bot.command(name="tasks")
async def tasks_cmd(ctx: commands.Context, job_id: Optional[str] = None):
    """Show the task list for the current job. Specify a job_id if the bot restarted."""
    user_id = str(ctx.author.id)
    job_id = job_id or bot.user_jobs.get(user_id)
    if not job_id:
        await ctx.send("No recent job. Use `!ask <task>` first, or pass an explicit job ID.")
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

        raw_desc = t["description"]
        if len(raw_desc) > 72:
            cut = raw_desc[:72].rsplit(" ", 1)[0]
            desc = cut + "…"
        else:
            desc = raw_desc

        result_snippet = ""
        if t.get("result") and t["status"] in ("done", "failed"):
            raw = re.sub(r'```[\s\S]*?```', '', t["result"]).strip()
            first_line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
            if first_line:
                snippet = first_line[:60] + ("…" if len(first_line) > 60 else "")
                result_snippet = f"\n    › {snippet}"

        lines.append(f"{icon} **{seq}.** [{agent}] {desc}{result_snippet}")

    await ctx.send(_truncate("\n".join(lines), 1900))
