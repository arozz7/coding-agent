"""Model management commands: !git, !models, !model."""
from __future__ import annotations

import asyncio
import subprocess

import httpx
from discord.ext import commands

from api.discord.bot_instance import bot


@bot.command(name="git")
async def git_cmd(ctx: commands.Context, *, args: str):
    """Run a safe read-only git command: status, log, diff, branch."""
    allowed = {"status", "log", "diff", "branch"}
    first = args.strip().split()[0].lower()
    if first not in allowed:
        await ctx.send(f"Only allowed: `{', '.join(sorted(allowed))}`")
        return
    try:
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
    """List configured models with LM Studio state, plus all downloaded-but-unconfigured models."""
    try:
        data = await bot.client._get("/models")
    except Exception as exc:
        await ctx.send(f"Error: {exc}")
        return

    active = data.get("active_model") or "(default)"
    lines = [f"**Configured Models** · active: `{active}`\n"]

    for m in data.get("models", []):
        marker = "**[active]**" if m.get("is_active") else "       "
        name = m["name"]
        mtype = m.get("type", "?")
        ctx_k = m.get("context_window", 0) // 1000
        state = m.get("state")
        state_icon = " 🟢" if state == "loaded" else (" ⚪" if state == "not-loaded" else "")
        lines.append(f"{marker} `{name}` — {mtype} · {ctx_k}k ctx{state_icon}")

    lines.append("\nUse `!model <name>` to switch · `!model reset` to restore default")

    lm_available = data.get("lm_studio_available", [])
    if lm_available:
        lines.append("\n**Available in LM Studio (not configured)**")
        for m in lm_available:
            mid = m.get("id", "?")
            state = m.get("state", "")
            state_icon = " 🟢" if state == "loaded" else (" ⚪" if state == "not-loaded" else "")
            lines.append(f"  `{mid}`{state_icon}")

    message = "\n".join(lines)
    if len(message) > 1900:
        message = message[:1900] + "\n…(truncated)"
    await ctx.send(message)


@bot.command(name="model")
async def switch_model(ctx: commands.Context, *, name: str = ""):
    """Switch the active model. `!model` shows current. `!model reset` restores default."""
    name = name.strip()

    if not name:
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

    if name.lower() == "reset":
        try:
            data = await bot.client._post("/models/active", {"model": None})
        except Exception as exc:
            await ctx.send(f"Error: {exc}")
            return
        await ctx.send(f"Model reset to default: `{data.get('active_model', '?')}`")
        return

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
