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

    @setup_group.subcommand(name="jtc_channel",
                            description="Set the Join-to-Create hub voice channel (replaces the channel bot).")
    async def setup_jtc_channel(
        self,
        interaction: nextcord.Interaction,
        channel: nextcord.VoiceChannel = nextcord.SlashOption(
            description="The voice channel members join to spawn their own channel.",
            required=True,
        ),
    ):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return

        self.config["jtc_hub_channel_id"] = channel.id
        # Created channels land in the hub's own category by default; that's
        # almost always where you want them, and it keeps setup to one command.
        self.config["jtc_category_id"] = channel.category_id
        save_config(self.config)

        await interaction.response.send_message(
            f"✅ Join-to-Create is on. Anyone who joins {channel.mention} gets their own "
            f"temporary voice channel, deleted when the last person leaves.",
            ephemeral=True
        )

    @setup_group.subcommand(name="jtc_disable", description="Turn off Join-to-Create voice channels.")
    async def setup_jtc_disable(self, interaction: nextcord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return

        if not self.config.get("jtc_hub_channel_id"):
            await interaction.response.send_message("Join-to-Create wasn't enabled.", ephemeral=True)
            return

        self.config.pop("jtc_hub_channel_id", None)
        self.config.pop("jtc_category_id", None)
        save_config(self.config)

        await interaction.response.send_message(
            "✅ Join-to-Create disabled. Existing temporary channels will still be cleaned up when they empty.",
            ephemeral=True
        )

    @setup_group.subcommand(name="dev_zone", description="Set the Dev Zone category for voice tracking.")
    async def setup_dev_zone(self, interaction: nextcord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return

        if not isinstance(interaction.channel, nextcord.TextChannel):
            await interaction.response.send_message("Run this from a text channel inside the Dev Zone category.",
                                                    ephemeral=True)
            return

        category = interaction.channel.category
        if not category:
            await interaction.response.send_message("This channel has no category.", ephemeral=True)
            return

        self.config["dev_zone_category_id"] = category.id
        save_config(self.config)

        await interaction.response.send_message(
            f"✅ Dev Zone set to category **{category.name}** (`{category.id}`).",
            ephemeral=True
        )

def setup(bot):
    bot.add_cog(Setup(bot))