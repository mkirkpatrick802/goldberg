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

    @setup_group.subcommand(name="add_bully_channel", description="Add this channel to Goldberg's bully channels.")
    async def setup_bully_channel(self, interaction: nextcord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return

        bully_channels = self.config.get("bully_channels", [])
        if interaction.channel.id in bully_channels:
            await interaction.response.send_message("This channel is already a bully channel.", ephemeral=True)
            return

        bully_channels.append(interaction.channel.id)
        self.config["bully_channels"] = bully_channels
        save_config(self.config)

        await interaction.response.send_message(
            f"✅ {interaction.channel.mention} added to bully channels.",
            ephemeral=True
        )

    @setup_group.subcommand(name="remove_bully_channel",
                            description="Remove this channel from Goldberg's bully channels.")
    async def remove_bully_channel(self, interaction: nextcord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return

        bully_channels = self.config.get("bully_channels", [])
        if interaction.channel.id not in bully_channels:
            await interaction.response.send_message("This channel isn't a bully channel.", ephemeral=True)
            return

        bully_channels.remove(interaction.channel.id)
        self.config["bully_channels"] = bully_channels
        save_config(self.config)

        await interaction.response.send_message(
            f"✅ {interaction.channel.mention} removed from bully channels.",
            ephemeral=True
        )

    @setup_group.subcommand(name="taiga_channel", description="Set the channel for Taiga sprint updates.")
    async def setup_taiga_channel(self, interaction: nextcord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return

        self.config["taiga_channel_id"] = interaction.channel.id
        save_config(self.config)

        await interaction.response.send_message(
            f"✅ Taiga sprint updates will now be posted in {interaction.channel.mention}.",
            ephemeral=True
        )

    @setup_group.subcommand(name="standup_channel", description="Set the channel for stand-up tracking.")
    async def setup_standup_channel(self, interaction: nextcord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return

        self.config["standup_channel_id"] = interaction.channel.id
        save_config(self.config)

        await interaction.response.send_message(
            f"✅ Stand-up tracking will now monitor {interaction.channel.mention}.",
            ephemeral=True
        )

    @setup_group.subcommand(name="report_channel", description="Set the channel for sprintly reports.")
    async def setup_report_channel(self, interaction: nextcord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return

        self.config["report_channel_id"] = interaction.channel.id
        save_config(self.config)

        await interaction.response.send_message(
            f"✅ Sprintly reports will be posted in {interaction.channel.mention}.",
            ephemeral=True
        )

async def setup(bot):
    bot.add_cog(Setup(bot))