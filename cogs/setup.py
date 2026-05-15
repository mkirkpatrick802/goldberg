import nextcord
from nextcord.ext import commands
import json
import os

from config import SERVER_ID

DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "setup_data.json")

def load_config():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_config(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

class Setup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = load_config()

    @nextcord.slash_command(name="setup", description="Goldberg setup commands.", guild_ids=[SERVER_ID])
    async def setup_group(self, interaction: nextcord.Interaction):
        pass

    @setup_group.subcommand(name="commit_notifier", description="Set the channel for SVN commit notifications.")
    async def setup_commits(self, interaction: nextcord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return

        self.config["commit_channel_id"] = interaction.channel.id
        save_config(self.config)

        await interaction.response.send_message(
            f"✅ Commit notifications will now be posted in {interaction.channel.mention}.",
            ephemeral=True
        )

async def setup(bot):
    bot.add_cog(Setup(bot))