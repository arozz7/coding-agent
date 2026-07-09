"""Discord bot entry point — imports command modules to register all @bot.command() decorators."""
from __future__ import annotations

import asyncio
import os

from api.discord.bot_instance import _start_bot
from api.discord.client import API_URL

# Importing these modules triggers @bot.command() registration at import time.
import api.discord.commands.tasks      # noqa: F401
import api.discord.commands.jobs       # noqa: F401
import api.discord.commands.workspace  # noqa: F401
import api.discord.commands.models     # noqa: F401
import api.discord.commands.admin      # noqa: F401


def run_bot(token: str):
    if not token:
        print("ERROR: DISCORD_BOT_TOKEN is not set.")
        return
    print(f"[bot] Starting — API: {API_URL}")
    try:
        asyncio.run(_start_bot(token))
    except KeyboardInterrupt:
        print("[bot] Stopped.")


if __name__ == "__main__":
    token = os.getenv("DISCORD_BOT_TOKEN")
    run_bot(token)
