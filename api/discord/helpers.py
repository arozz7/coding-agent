"""Text formatting helpers and Discord attachment utilities."""
from __future__ import annotations

import io
import pathlib
import re

import discord
from discord import File
from discord.ext import commands

_MAX_ATTACHMENT_BYTES = 7 * 1024 * 1024  # 7 MB (Discord cap is 8 MB)

_BINARY_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "bmp", "ico", "svg", "webp",
    "pdf", "zip", "tar", "gz", "bz2", "7z", "rar",
    "exe", "dll", "so", "dylib", "bin", "whl",
    "mp3", "mp4", "wav", "ogg", "avi", "mov",
    "db", "sqlite", "sqlite3",
}


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
