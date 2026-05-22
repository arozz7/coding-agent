"""Task submission commands: !ask, !dev, !research, !chains, !chain, !continue."""
from __future__ import annotations

import asyncio
from typing import Optional

import structlog
from discord.ext import commands

from api.discord.bot_instance import bot
from api.discord.poller import _poll_job, _on_poll_done

logger = structlog.get_logger()


async def _submit_task(
    ctx: commands.Context,
    task: str,
    force_task_type: Optional[str] = None,
) -> None:
    """Shared submit logic for !ask, !dev, !research, and !chain."""
    user_id = str(ctx.author.id)
    session_id = bot.user_sessions.get(user_id, user_id)

    if ctx.message.attachments:
        for att in ctx.message.attachments:
            if att.size < 200_000 and att.filename.rsplit(".", 1)[-1].lower() in (
                "txt", "md", "json", "yaml", "yml", "toml", "py", "js", "ts", "html", "css", "rs",
            ):
                try:
                    raw = await att.read()
                    text = raw.decode("utf-8", errors="replace")
                    task = f"{task}\n\n--- Attachment: {att.filename} ---\n{text}"
                except Exception as _att_err:
                    logger.warning("discord_attachment_read_failed", file=att.filename, error=str(_att_err))

    status_msg = await ctx.send("Submitting…")

    try:
        resp = await bot.client.start_task(task, session_id, force_task_type=force_task_type)
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
    asyncio.create_task(_poll_job(ctx, status_msg, job_id)).add_done_callback(_on_poll_done)


@bot.command(name="ask")
async def ask(ctx: commands.Context, *, task: str):
    """Submit a task. The agent works in the background — this message updates live."""
    await _submit_task(ctx, task)


@bot.command(name="dev")
async def dev(ctx: commands.Context, *, task: str):
    """Submit a task and force it to be treated as a develop task (bypass classifier).

    Use this when the classifier keeps picking chat/research for a debugging or
    build task — e.g. `!dev fix the TypeScript errors and get the game running`.
    """
    await _submit_task(ctx, task, force_task_type="develop")


@bot.command(name="research")
async def research(ctx: commands.Context, *, task: str):
    """Submit a task and force it to be treated as a research task (bypass classifier).

    Use this for web searches, codebase investigations, and analysis tasks —
    e.g. `!research how does the context bridge work in orchestrator.py`.
    The full report is available via `!result`.
    """
    await _submit_task(ctx, task, force_task_type="research")


@bot.command(name="chains")
async def list_chains(ctx: commands.Context):
    """List all available agent chains defined in agent-chain.yaml."""
    try:
        resp = await bot.client._get("/chains")
        chains = resp.get("chains", [])
        if not chains:
            await ctx.send("No chains available.")
            return
        lines = ["**Available chains** (`!chain <name> <task>`):"]
        for c in chains:
            lines.append(f"  `{c['name']}` — {c.get('description', '')}")
        await ctx.send("\n".join(lines))
    except Exception as exc:
        await ctx.send(f"Could not fetch chains: {exc}")


@bot.command(name="chain")
async def run_chain(ctx: commands.Context, chain_name: str, *, task: str):
    """Run a named agent chain.  Example: `!chain plan-build-review add auth to the API`

    Use `!chains` to list available chains.
    """
    await _submit_task(ctx, f"__chain__:{chain_name}:{task}", force_task_type="chain")


@bot.command(name="continue")
async def continue_task(ctx: commands.Context, *, note: str = ""):
    """Continue fixing the last task.  Use when the build still has errors after !ask/!dev.

    Optionally pass extra guidance: `!continue focus on the webpack config errors`.
    Or pass a specific job ID if the bot restarted: `!continue job_123 xyz`
    The session context carries forward so the agent knows what was already tried.
    """
    user_id = str(ctx.author.id)

    job_id_override = None
    if note.startswith("job_"):
        parts = note.split(" ", 1)
        job_id_override = parts[0].strip()
        note = parts[1].strip() if len(parts) > 1 else ""

    last_job_id = job_id_override or bot.user_jobs.get(user_id)

    if not last_job_id:
        await ctx.send("No previous job found in memory. Use `!jobs` to find your job ID, then run `!continue <job_id>`.")
        return

    try:
        last_job = await bot.client.get_job(last_job_id)
        bot.user_jobs[user_id] = last_job_id
        if last_job.get("session_id"):
            bot.user_sessions[user_id] = last_job["session_id"]
    except Exception as exc:
        await ctx.send(f"Could not fetch job `{last_job_id}`: {exc}")
        return

    original_task = last_job.get("task", "the previous task")
    continuation = (
        f"Continue debugging from where we left off. "
        f"The original task was: {original_task}. "
        f"The build still has errors. Keep fixing until it compiles and runs cleanly."
    )
    if note:
        continuation += f" Additional guidance: {note}"

    await _submit_task(ctx, continuation, force_task_type="develop")
