"""Admin/utility commands: !jobs, !skills, !wiki, !restart, !helpme."""
from __future__ import annotations

import asyncio

from discord.ext import commands

from api.discord.bot_instance import bot, _STATE_DIR, _LAST_CHANNEL_FILE
from api.discord.helpers import _chunk


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


@bot.command(name="wiki")
async def wiki_cmd(ctx: commands.Context, action: str = "status", *, args: str = ""):
    """Interact with the agent wiki knowledge base.

    !wiki                     — show wiki status (entry count, project breakdown)
    !wiki status              — same as above
    !wiki query <terms>       — search the wiki for matching entries
    !wiki clean               — remove out-of-scope entries from the current wiki index
    !wiki migrate <project>   — move <project>-tagged entries from root wiki to project wiki
    """
    action = action.lower().strip()

    if action in ("status", ""):
        try:
            data = await bot.client._get("/wiki/status")
        except Exception as exc:
            await ctx.send(f"Error: {exc}")
            return

        total = data.get("total", 0)
        current = data.get("current_project", "<root>")
        by_cat = data.get("by_category", {})
        by_proj = data.get("by_project", {})
        last = data.get("last_entry", "—")
        wiki_root = data.get("wiki_root", "")

        cat_lines = "\n".join(f"  `{k}` — {v}" for k, v in sorted(by_cat.items())) or "  (empty)"
        proj_lines = "\n".join(f"  `{k}` — {v} entries" for k, v in sorted(by_proj.items())) or "  (empty)"

        await ctx.send(
            f"**Wiki Status** — `{wiki_root}`\n"
            f"Active project: **{current}**\n"
            f"Total entries: **{total}**\n\n"
            f"**By category:**\n{cat_lines}\n\n"
            f"**By project:**\n{proj_lines}\n\n"
            f"**Last compiled:** {last[:100]}"
        )
        return

    if action == "query":
        if not args:
            await ctx.send("Usage: `!wiki query <term1> <term2> ...`")
            return
        terms = ",".join(args.split())
        try:
            data = await bot.client._get(f"/wiki/query?terms={terms}")
        except Exception as exc:
            await ctx.send(f"Error: {exc}")
            return
        result = data.get("result", "(no matches)")
        if len(result) > 1800:
            result = result[:1800] + "\n…(truncated)"
        await ctx.send(f"**Wiki Query: `{args}`**\n\n{result}")
        return

    if action == "clean":
        msg = await ctx.send("Cleaning out-of-scope entries from wiki index…")
        try:
            data = await bot.client._post("/wiki/clean", {})
        except Exception as exc:
            await msg.edit(content=f"Error: {exc}")
            return
        removed = data.get("removed", 0)
        kept = data.get("kept", 0)
        await msg.edit(
            content=f"Wiki clean complete — removed **{removed}** out-of-scope entries, kept **{kept}**."
        )
        return

    if action == "migrate":
        project = args.strip()
        if not project:
            await ctx.send("Usage: `!wiki migrate <project-name>`")
            return
        msg = await ctx.send(f"Migrating entries tagged `{project}` to their project wiki…")
        try:
            data = await bot.client._post("/wiki/migrate", {"project": project})
        except Exception as exc:
            await msg.edit(content=f"Error: {exc}")
            return
        moved = data.get("moved", 0)
        await msg.edit(
            content=f"Migration complete — moved **{moved}** entries to `{project}/.agent-wiki`."
        )
        return

    await ctx.send(
        "Unknown wiki action. Available: `status`, `query <terms>`, `clean`, `migrate <project>`"
    )


@bot.command(name="restart", aliases=["reboot"])
async def restart_services(ctx: commands.Context):
    """Restart both the API and bot via the supervisor (!reboot also works)."""
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        _LAST_CHANNEL_FILE.write_text(str(ctx.channel.id))
    except Exception as exc:
        print(f"[bot] Could not write last_channel: {exc}")

    try:
        resp = await bot.client.restart()
    except Exception:
        resp = {}

    supervisor_ok = resp.get("supervisor_running", None)
    if supervisor_ok is False:
        await ctx.send(
            "Could not restart: **supervisor.py is not running**.\n"
            "Start it manually: `python supervisor.py`\n"
            "Then use `!restart` again."
        )
        return

    await ctx.send("Restarting services — back in ~15 seconds...")


@bot.command(name="helpme")
async def helpme(ctx: commands.Context):
    """Show available commands."""
    help_text = (
        "**Agent Commands**\n\n"
        "**Core workflow:**\n"
        "`!ask <task>` — Submit a task. Agent works in background; this message updates live.\n"
        "`!dev <task>` — Same as !ask but forces develop mode (use for debug/build/fix tasks).\n"
        "`!continue [job_id] [note]` — Continue fixing the last job when errors remain.\n"
        "`!status [job_id]` — Check your current job's status and phase\n"
        "`!cancel [job_id]` — Cancel your running job\n\n"
        "**Viewing results:**\n"
        "`!result [job_id]` — Show prose response (code blocks stripped)\n"
        "`!files [job_id]` — List files created/modified in the last task\n"
        "`!show <path>` — View a workspace file (attachment for large files)\n\n"
        "**Session:**\n"
        "`!history` — Last 5 messages in your session\n"
        "`!session` — Your session ID and last job ID\n"
        "`!clear` — Clear conversation history\n"
        "`!sessions` — List all sessions\n\n"
        "**Workspace:**\n"
        "`!workspace` — Show workspace path and top-level contents\n"
        "`!project` — Show active project\n"
        "`!project <name>` — Switch to (or create) a project subdirectory\n"
        "`!project clear` — Return to workspace root to start a new project\n"
        "`!project delete <name>` — Preview agent data that would be removed\n"
        "`!project delete <name> confirm` — Permanently remove all agent data (source files untouched)\n\n"
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
        "`!restart` (or `!reboot`) — Restart both the API and bot via the supervisor\n"
        "`!helpme` — This help text\n"
    )
    await ctx.send(help_text)
