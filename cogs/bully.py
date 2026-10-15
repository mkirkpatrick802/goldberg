import nextcord
from nextcord.ext import commands
import json
import os
import random
import time

import silence
from config import SERVER_ID
from lines import get_line

DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "bully_data.json")
SETUP_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "setup_data.json")

# How long a bare `/shutup` keeps him quiet, and the ceiling on a custom value.
DEFAULT_SHUTUP_MINUTES = 60
MAX_SHUTUP_MINUTES = 24 * 60

def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

def load_setup():
    if not os.path.exists(SETUP_FILE):
        return {}
    with open(SETUP_FILE, "r") as f:
        return json.load(f)

# Name-drop replies: fire when someone types "Goldberg" (no @ required).
NAME_COOLDOWN_SECONDS = 30
NAME_REPLY_CHANCE = 0.15  # otherwise he stays quiet — no more reaction spam


def _fmt_duration(minutes: float) -> str:
    """'90' -> 'an hour and a half'-ish, but plain: '1h 30m' / '45m'."""
    minutes = int(round(minutes))
    hours, mins = divmod(minutes, 60)
    if hours and mins:
        return f"{hours}h {mins}m"
    if hours:
        return f"{hours}h"
    return f"{mins}m"


class Bully(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data = load_data()
        # Per-channel timestamp of his last name-drop reaction, so he doesn't spam threads.
        self.name_cooldowns = {}

    def get_user(self, user_id: str):
        if user_id not in self.data:
            self.data[user_id] = {
                "interaction_count": 0,
                "message_count": 0,
            }
        return self.data[user_id]

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return

        user_id = str(message.author.id)
        user = self.get_user(user_id)
        setup = load_setup()
        bully_channels = setup.get("bully_channels", [])
        in_bully_channel = message.channel.id in bully_channels

        # Track message count
        user["message_count"] += 1
        save_data(self.data)

        # /shutup in effect: keep counting stats, but no unprompted chatter,
        # name-drops, or mention barks until the timer runs out. Slash commands
        # still work — this only silences the spontaneous personality.
        if silence.is_muted():
            return

        # Handle mentions
        if self.bot.user.mentioned_in(message) and not message.mention_everyone:
            user["interaction_count"] += 1
            save_data(self.data)

            count = user["interaction_count"]
            category = "bully.first_mention" if count == 1 else "bully.mention_insult"
            response = get_line(category, mention=message.author.mention)

            await message.channel.send(response)
            return

        # Name-drop: someone typed "Goldberg" as plain text (no @). Works anywhere.
        if "goldberg" in message.content.lower():
            await self.handle_name_drop(message)
            return

        # Random text barks in bully channels only. The emoji reactions that
        # used to fire here were removed — they created false "someone replied
        # to me" notifications and chimed into serious conversations.
        if in_bully_channel:
            if random.random() < 0.005:
                await message.channel.send(get_line("bully.random_reply", mention=message.author.mention))

    async def handle_name_drop(self, message):
        # Rate-limit per channel so a chat full of "Goldberg" doesn't turn into spam.
        now = time.monotonic()
        last = self.name_cooldowns.get(message.channel.id, 0.0)
        if now - last < NAME_COOLDOWN_SECONDS:
            return
        self.name_cooldowns[message.channel.id] = now

        # A sassy text reply now and then; otherwise he stays quiet. (He used to
        # slap on an emoji reaction on the miss — that's gone on purpose.)
        if random.random() < NAME_REPLY_CHANCE:
            try:
                reply = get_line("bully.name_drop", mention=message.author.mention)
                await message.channel.send(reply)
            except nextcord.HTTPException:
                pass

    # ── /shutup & /wakeup ───────────────────────────────────────────────────────
    @nextcord.slash_command(
        name="shutup",
        description="Put Goldberg's quips on hold for a bit. He won't like it.",
        guild_ids=[SERVER_ID],
    )
    async def shutup(
        self,
        interaction: nextcord.Interaction,
        minutes: int = nextcord.SlashOption(
            name="minutes",
            description=f"How long to keep him quiet (default {DEFAULT_SHUTUP_MINUTES}, max {MAX_SHUTUP_MINUTES}).",
            required=False,
            default=DEFAULT_SHUTUP_MINUTES,
            min_value=1,
            max_value=MAX_SHUTUP_MINUTES,
        ),
    ):
        # Anyone can hit the brakes — handy mid-serious-conversation.
        silence.mute_for(minutes)
        line = get_line(
            "bully.shutup",
            duration=_fmt_duration(minutes),
            mention=interaction.user.mention,
        )
        await interaction.response.send_message(line)

    @nextcord.slash_command(
        name="wakeup",
        description="Bring Goldberg's quips back early.",
        guild_ids=[SERVER_ID],
    )
    async def wakeup(self, interaction: nextcord.Interaction):
        if not silence.is_muted():
            await interaction.response.send_message(
                get_line("bully.already_awake", mention=interaction.user.mention), ephemeral=True
            )
            return
        silence.wake()
        await interaction.response.send_message(
            get_line("bully.wakeup", mention=interaction.user.mention)
        )


def setup(bot):
    bot.add_cog(Bully(bot))