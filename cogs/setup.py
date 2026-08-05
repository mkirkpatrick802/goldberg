import nextcord
from nextcord.ext import commands
import json
import os
import random

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

# ─── /setup info_channel content ────────────────────────────────────────────

INFO_COLOR = 0xc8521a

INFO_INTROS = [
    "Fine. You want the full rundown? Sit down — this might take a while. Unlike some of you, I actually do a lot.",
    "Since nobody reads pinned messages, here's the complete list. Try to keep up.",
    "Someone with admin powers decided you needed this spelled out. Here's everything I do, in excessive detail.",
]

INFO_OUTROS = [
    "That's the whole act. Try not to need me for all of it at once.",
    "Now you have no excuse for pinging me with questions this answers.",
    "Save this. I'm not typing it again.",
    "Read it twice. I'll wait. (I won't.)",
]


def build_info_embeds() -> list[nextcord.Embed]:
    """Goldberg's own feature rundown, posted by /setup info_channel."""
    overview = nextcord.Embed(
        title="🤖 Everything I Do (Whether You Asked Or Not)",
        description=(
            f"{random.choice(INFO_INTROS)}\n\n"
            "I'm Goldberg — The Maple Barrel's Discord bot. I run commit "
            "notifications, sprint reminders, office hours, meeting notes, and "
            "a personality nobody requested but everybody tolerates. Here's the "
            "full breakdown."
        ),
        color=INFO_COLOR,
    )

    everyone = nextcord.Embed(title="📋 Commands Anyone Can Run", color=INFO_COLOR)
    everyone.add_field(name="/hello", value="I greet you. Reluctantly.", inline=False)
    everyone.add_field(name="/joke", value="A dev joke, usually at your expense.", inline=False)
    everyone.add_field(name="/socials", value="Links to all of The Maple Barrel's social accounts.", inline=False)
    everyone.add_field(
        name="/shutup [minutes]",
        value="Puts my spontaneous commentary on hold — default 60 minutes, up to 24 hours. "
              "Slash commands and real notifications keep working; I just stop volunteering opinions.",
        inline=False,
    )
    everyone.add_field(name="/wakeup", value="Ends an active /shutup early, in case you missed me. You did.", inline=False)

    devs = nextcord.Embed(
        title="🛠️ Commands For Devs",
        description="Gated behind the **dev** role.",
        color=INFO_COLOR,
    )
    devs.add_field(name="/help", value="This, but shorter and ephemeral.", inline=False)
    devs.add_field(name="/documentation", value="Team docs — Figma, Drive, all of it.", inline=False)
    devs.add_field(name="/repo", value="Walks you through SVN sign-up and checkout.", inline=False)
    devs.add_field(name="/officehours", value="Who's hosting office hours today.", inline=False)
    devs.add_field(name="/schedule", value="The full weekly office hours schedule.", inline=False)
    devs.add_field(name="/sprint_board", value="The whole current sprint board, grouped by story.", inline=False)
    devs.add_field(name="/my_tasks", value="Your open tasks this sprint, plus where we are in it.", inline=False)
    devs.add_field(
        name="/takenotes [team] [title]",
        value="I join your voice channel, record, and transcribe. Everyone in the call is being "
              "recorded — I say so out loud when it starts.",
        inline=False,
    )
    devs.add_field(name="/stopnotes", value="Stops the recording and posts the writeup + transcript to the notes forum.", inline=False)

    testers = nextcord.Embed(title="🎮 Dev / Tester Only", color=INFO_COLOR)
    testers.add_field(name="/builds", value="Download link for the latest playable build.", inline=False)

    automatic = nextcord.Embed(title="⚙️ What I Do Without Being Asked", color=INFO_COLOR)
    automatic.add_field(
        name="SVN commit notifications",
        value="Every new commit gets posted, with revision, author, and message. A blank message earns a roast instead.",
        inline=False,
    )
    automatic.add_field(
        name="Sprint task reminders",
        value="DMs — not a public post — every Tuesday, Friday, and Sunday at 10 AM ET, listing what you "
              "still owe. I get meaner as the deadline gets closer.",
        inline=False,
    )
    automatic.add_field(
        name="Office hours pings",
        value="I announce the moment someone's slot starts and tag them, so you have no excuse.",
        inline=False,
    )
    automatic.add_field(
        name="Join-to-Create voice",
        value="Join a configured hub channel and I spin you up your own temporary voice channel, deleted when it's empty.",
        inline=False,
    )
    automatic.add_field(
        name="Unprompted commentary",
        value="Say my name or @ me and you might get a reply. Rare random barks happen in designated channels too. "
              "`/shutup` is how you make it stop.",
        inline=False,
    )
    automatic.add_field(
        name="Quiet bookkeeping",
        value="I also log commits, office-hours attendance, stand-ups, and voice time per sprint in the "
              "background. No public report exists yet — consider it evidence I'm keeping.",
        inline=False,
    )
    automatic.set_footer(text=random.choice(INFO_OUTROS))

    return [overview, everyone, devs, testers, automatic]

class Setup(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.config = load_config()
        self._migrate_jtc_hubs()

    def _migrate_jtc_hubs(self):
        """Fold the old single-hub keys into the jtc_hubs list, one time.

        Pre-multi-hub configs stored one hub as jtc_hub_channel_id /
        jtc_category_id. Converting on load means jointocreate.py only ever
        has to deal with the list format.
        """
        if "jtc_hubs" in self.config:
            return
        old_hub = self.config.pop("jtc_hub_channel_id", None)
        old_category = self.config.pop("jtc_category_id", None)
        if old_hub:
            self.config["jtc_hubs"] = [{"hub_channel_id": old_hub, "category_id": old_category}]
            save_config(self.config)

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
                            description="Add a Join-to-Create hub voice channel.")
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

        hubs = self.config.setdefault("jtc_hubs", [])
        if any(h["hub_channel_id"] == channel.id for h in hubs):
            await interaction.response.send_message(
                f"{channel.mention} is already a Join-to-Create hub.", ephemeral=True
            )
            return

        # Created channels land in the hub's own category by default; that's
        # almost always where you want them, and it keeps setup to one command.
        hubs.append({"hub_channel_id": channel.id, "category_id": channel.category_id})
        save_config(self.config)

        await interaction.response.send_message(
            f"✅ Join-to-Create is on for {channel.mention}. Anyone who joins it gets their own "
            f"temporary voice channel, deleted when the last person leaves. "
            f"({len(hubs)} hub{'s' if len(hubs) != 1 else ''} configured.)",
            ephemeral=True
        )

    @setup_group.subcommand(name="jtc_disable", description="Turn off a Join-to-Create hub voice channel.")
    async def setup_jtc_disable(
        self,
        interaction: nextcord.Interaction,
        channel: nextcord.VoiceChannel = nextcord.SlashOption(
            description="The hub channel to disable.",
            required=True,
        ),
    ):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return

        hubs = self.config.get("jtc_hubs", [])
        remaining = [h for h in hubs if h["hub_channel_id"] != channel.id]
        if len(remaining) == len(hubs):
            await interaction.response.send_message(
                f"{channel.mention} isn't a Join-to-Create hub.", ephemeral=True
            )
            return

        self.config["jtc_hubs"] = remaining
        save_config(self.config)

        await interaction.response.send_message(
            f"✅ Join-to-Create disabled for {channel.mention}. Existing temporary channels will still "
            f"be cleaned up when they empty.",
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

    @setup_group.subcommand(name="info_channel",
                            description="Post Goldberg's full feature rundown in this channel.")
    async def setup_info_channel(self, interaction: nextcord.Interaction):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return

        self.config["info_channel_id"] = interaction.channel.id
        save_config(self.config)

        await interaction.response.send_message(
            f"✅ Info channel set to {interaction.channel.mention}. Posting the rundown now — try to contain yourself.",
            ephemeral=True
        )

        # Public and unprompted on purpose — the whole point is that people can
        # read it without asking, so it goes to the channel, not the admin only.
        for embed in build_info_embeds():
            await interaction.channel.send(embed=embed)

def setup(bot):
    bot.add_cog(Setup(bot))