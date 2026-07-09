"""Bot singleton, DiscordAgentBot class, and entry-point helpers."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
from discord import Intents, Message
from discord.ext import commands

from api.discord.client import AgentClient, API_URL

# State directory shared with supervisor.py — three parents up from api/discord/
_STATE_DIR = Path(__file__).parent.parent.parent / ".state"
_LAST_CHANNEL_FILE = _STATE_DIR / "last_channel"


class DiscordAgentBot(commands.Bot):
    def __init__(self):
        intents = Intents.default()
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.client = AgentClient()
        self.user_sessions: dict[str, str] = {}   # user_id → session_id
        self.user_jobs: dict[str, str] = {}        # user_id → current job_id

    async def on_ready(self):
        print(f"[bot] Logged in as {self.user}  |  API: {API_URL}")
        if _LAST_CHANNEL_FILE.exists():
            try:
                channel_id = int(_LAST_CHANNEL_FILE.read_text().strip())
                _LAST_CHANNEL_FILE.unlink(missing_ok=True)
                channel = self.get_channel(channel_id)
                if channel:
                    await channel.send("Services restarted and back online.")
            except Exception:
                _LAST_CHANNEL_FILE.unlink(missing_ok=True)

        asyncio.create_task(self._poll_model_switch_events())

    async def _poll_model_switch_events(self) -> None:
        """Poll /events/model-switches every 30 s and post to the status channel.

        Set BOT_STATUS_CHANNEL_ID in .env to enable; silently skips if unset.
        """
        status_channel_id = os.getenv("BOT_STATUS_CHANNEL_ID", "").strip()
        if not status_channel_id:
            return

        try:
            channel_id = int(status_channel_id)
        except ValueError:
            print(f"[bot] BOT_STATUS_CHANNEL_ID is not a valid integer: {status_channel_id!r}")
            return

        while True:
            await asyncio.sleep(30)
            channel = self.get_channel(channel_id)
            if not channel:
                continue
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    r = await client.get(f"{API_URL}/events/model-switches")
                    if r.status_code != 200:
                        continue
                    events = r.json().get("events", [])
                for ev in events:
                    await channel.send(
                        f"⚠️ **Model switch:** `{ev['from_model']}` → `{ev['to_model']}` "
                        f"(reason: `{ev['reason']}`)\n"
                        f"Local model could not be loaded. Use `!model <name>` to override."
                    )
            except Exception as e:
                print(f"[bot] model-switch poll error: {e}")

    async def on_message(self, message: Message):
        if message.author == self.user:
            return
        if not message.content.startswith("!"):
            return
        await self.process_commands(message)


bot = DiscordAgentBot()


async def _start_bot(token: str) -> None:
    """Wait for the API to be reachable, then connect to Discord."""
    await bot.client.wait_until_reachable()
    try:
        await bot.start(token)
    except asyncio.CancelledError:
        pass
    finally:
        if not bot.is_closed():
            await bot.close()
