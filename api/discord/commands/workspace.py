"""Workspace and session commands: !show, !history, !sessions, !clear, !session, !workspace, !project."""
from __future__ import annotations

import io

import httpx
from discord import File
from discord.ext import commands

from api.discord.bot_instance import bot
from api.discord.helpers import (
    _BINARY_EXTENSIONS,
    _MAX_ATTACHMENT_BYTES,
    strip_code_blocks,
)
from api.discord.poller import _safe_edit


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
    session_id = bot.user_sessions.get(user_id)
    if session_id:
        try:
            await bot.client.delete_session(session_id)
        except Exception as exc:
            await ctx.send(f"Could not clear session: {exc}")
            return
    bot.user_sessions.pop(user_id, None)
    bot.user_jobs.pop(user_id, None)
    await ctx.send("Conversation cleared.")


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


@bot.command(name="project")
async def project_cmd(ctx: commands.Context, *, name: str = ""):
    """Show, switch, or delete a project.

    !project                        — show current project and workspace root
    !project <name>                 — switch to WORKSPACE_PATH/<name>
    !project clear                  — return to workspace root
    !project delete <name>          — preview what would be removed
    !project delete <name> confirm  — permanently remove all agent data for project
    """
    name = name.strip()

    if not name:
        try:
            data = await bot.client._get("/workspace")
            ws = data.get("workspace", "unknown")
            proj_data = await bot.client._get("/workspace/project")
            project = proj_data.get("project") or "(none — at workspace root)"
            root = proj_data.get("workspace_root", ws)
            await ctx.send(
                f"**Active project:** `{project}`\n"
                f"**Workspace root:** `{root}`\n"
                f"**Effective path:** `{ws}`\n\n"
                f"Use `!project <name>` to switch, `!project clear` to return to root.\n"
                f"Use `!project delete <name>` to preview project cleanup."
            )
        except Exception as exc:
            await ctx.send(f"Error fetching project info: {exc}")
        return

    if name.lower() == "clear":
        user_id = str(ctx.author.id)
        old_session = bot.user_sessions.get(user_id)
        try:
            data = await bot.client.set_project("")
            if old_session:
                try:
                    await bot.client.delete_session(old_session)
                except Exception:
                    pass
            bot.user_sessions.pop(user_id, None)
            bot.user_jobs.pop(user_id, None)
            await ctx.send(
                f"Cleared to workspace root: `{data.get('workspace')}`\n"
                f"Session cleared — ready for a new project."
            )
        except Exception as exc:
            await ctx.send(f"Could not clear project: {exc}")
        return

    if name.lower().startswith("delete "):
        rest = name[7:].strip()
        confirm = rest.endswith(" confirm")
        project_name = rest[: -len(" confirm")].strip() if confirm else rest.strip()

        if not project_name:
            await ctx.send("Usage: `!project delete <name>` or `!project delete <name> confirm`")
            return

        if not confirm:
            try:
                msg = await ctx.send(f"Checking data for project **{project_name}**…")
                data = await bot.client.preview_delete_project(project_name)
                await _safe_edit(
                    msg,
                    f"**Delete preview — `{project_name}`**\n"
                    f"```\n"
                    f"Sessions   : {data.get('sessions', 0)}\n"
                    f"Jobs       : {data.get('jobs', 0)}\n"
                    f"RAG chunks : {data.get('chroma_chunks', 0)}\n"
                    f"Wiki entries: {data.get('wiki_entries', 0)}\n"
                    f"```\n"
                    f"Source files are **never** deleted.\n"
                    f"To proceed: `!project delete {project_name} confirm`"
                )
            except Exception as exc:
                await ctx.send(f"Preview failed: {exc}")
            return

        try:
            msg = await ctx.send(f"Deleting agent data for **{project_name}**…")
            data = await bot.client.delete_project(project_name)
            await _safe_edit(
                msg,
                f"**Deleted — `{project_name}`**\n"
                f"```\n"
                f"Sessions removed : {data.get('deleted_sessions', 0)}\n"
                f"Jobs removed     : {data.get('jobs', 0)}\n"
                f"RAG chunks cleared: {data.get('chroma_chunks', 0)}\n"
                f"Wiki entries     : {data.get('wiki_entries', 0)}\n"
                f"```\n"
                f"Source files untouched. Use `!project {project_name}` to reinitialise."
            )
        except Exception as exc:
            await ctx.send(f"Delete failed: {exc}")
        return

    user_id = str(ctx.author.id)
    old_session = bot.user_sessions.get(user_id)
    try:
        data = await bot.client.set_project(name)
        if old_session:
            try:
                await bot.client.delete_session(old_session)
            except Exception:
                pass
        bot.user_sessions.pop(user_id, None)
        bot.user_jobs.pop(user_id, None)
        await ctx.send(
            f"Switched to project **{name}**\n"
            f"Workspace: `{data.get('workspace')}`\n"
            f"Session cleared — fresh context for this project. Ready for `!ask`."
        )
    except Exception as exc:
        await ctx.send(f"Could not switch project: {exc}")
