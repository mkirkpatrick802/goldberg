import asyncio
import logging

import nextcord
from nextcord.ext import commands
import os

from config import BOT_TOKEN

# nextcord 3.x's bot.run() does NOT configure logging (unlike discord.py), so
# the library's own messages — including the real reason a voice connection
# fails — go nowhere by default. Configure it here so those surface in
# goldberg.log.
#
# The voice handshake detail we're chasing is logged at DEBUG, so the root
# handler runs at DEBUG; the chatty subsystems are then muted back up so the log
# isn't drowned. This is turned up for debugging the voice feature — dial the
# root back to INFO once voice works.
logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
for _noisy in ("nextcord.gateway", "nextcord.client", "nextcord.http", "asyncio", "aiohttp"):
    logging.getLogger(_noisy).setLevel(logging.INFO)
# The one we actually want in full detail — the voice connection state machine.
logging.getLogger("nextcord.voice_client").setLevel(logging.DEBUG)

intents = nextcord.Intents.all()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Load cogs
async def load_cogs():
    cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")
    for filename in os.listdir(cogs_dir):
        if filename.endswith('.py'):
            try:
                bot.load_extension(f'cogs.{filename[:-3]}')
                print(f"Successfully loaded {filename}")
            except Exception as e:
                print(f"Failed to load {filename}: {e}")

@bot.event
async def on_application_command_error(interaction, error):
    if isinstance(error, commands.CheckFailure):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
    else:
        raise error

@bot.event
async def on_ready():
    print(f"{bot.user.name} has connected to Discord!")

# Run the bot
if __name__ == "__main__":
    asyncio.run(load_cogs())
    bot.run(BOT_TOKEN)
