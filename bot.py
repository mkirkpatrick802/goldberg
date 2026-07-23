import asyncio
import logging

import nextcord
from nextcord.ext import commands
import os

from config import BOT_TOKEN

# nextcord 3.x's bot.run() does NOT configure logging (unlike discord.py), so
# the library's own messages — including the real reason a voice connection
# fails — go nowhere by default. Configure it here so those reach goldberg.log.
#
# INFO keeps the voice handshake visible (nextcord logs each step, and failures
# with the Discord close code at ERROR) without drowning the log. Raise
# nextcord.voice_client to DEBUG if you ever need to debug voice again.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
# libdave logs one line per undecryptable frame; silence/comfort-noise packets
# make that constant noise during a normal call.
logging.getLogger("dave").setLevel(logging.CRITICAL)

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
