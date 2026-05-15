import asyncio

import nextcord
from nextcord.ext import commands
import os

from config import BOT_TOKEN

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
