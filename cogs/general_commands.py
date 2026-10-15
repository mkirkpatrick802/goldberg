import nextcord
from nextcord.ext import commands

from config import SERVER_ID
from lines import get_line
from utils import is_dev, get_company_links

# Fallback for a fresh migration where nobody's populated Company Info yet —
# same links this command always had, kept only so /socials doesn't go blank.
# Safe to delete once the "Socials" group on the Wiki page has real rows.
# Each link's tagline comes from the "socials.<key>" lines category.
_SOCIALS_FALLBACK = [
    {"key": "website",   "label": "🌐 Website",   "url": "https://www.themaplebarrel.com/"},
    {"key": "instagram", "label": "📸 Instagram", "url": "https://www.instagram.com/themaplebarrel/"},
    {"key": "youtube",   "label": "▶️ YouTube",   "url": "https://www.youtube.com/@TheMapleBarrel"},
    {"key": "patreon",   "label": "🎗️ Patreon",  "url": "https://www.patreon.com/TheMapleBarrel"},
    {"key": "linkedin",  "label": "💼 LinkedIn",  "url": "https://www.linkedin.com/company/the-maple-barrel"},
]


class GeneralCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @nextcord.slash_command(name="joke", description="Yes I can joke as well", guild_ids=[SERVER_ID])
    async def joke(self, ctx):
        await ctx.send(get_line("joke"))

    @nextcord.slash_command(name="hello", description="Say hello if you must", guild_ids=[SERVER_ID])
    async def hello(self, interaction: nextcord.Interaction):
        await interaction.response.send_message(get_line("hello", mention=interaction.user.mention))

    @nextcord.slash_command(name="socials", description="Check out The Maple Barrel's socials.", guild_ids=[SERVER_ID])
    async def socials(self, interaction: nextcord.Interaction):
        embed = nextcord.Embed(
            title="🍁 The Maple Barrel",
            description=get_line("socials.description"),
            color=0xc8521a
        )

        # Reads the "Socials" group of Company Info links off the webapp's wiki
        # page, so updating a link is an owner edit there instead of a bot
        # redeploy. Falls back to the original hardcoded list if that group is
        # empty (fresh migration, or the shared database is unreachable).
        links = get_company_links(group_name="Socials")
        if links:
            for l in links:
                embed.add_field(name=l["label"], value=f"[Open]({l['url']})", inline=False)
        else:
            for l in _SOCIALS_FALLBACK:
                embed.add_field(name=l["label"], value=f"[Open]({l['url']}) — {get_line('socials.' + l['key'])}", inline=False)

        embed.set_footer(text=get_line("socials.footer"))
        await interaction.response.send_message(embed=embed)

    @nextcord.slash_command(name="documentation", description="Access team documentation.", guild_ids=[SERVER_ID])
    async def documentation(self, interaction: nextcord.Interaction):
        if not is_dev(interaction):
            await interaction.response.send_message(
                get_line("devs_only"),
                ephemeral=True
            )
            return

        embed = nextcord.Embed(
            title="📚 Documentation",
            description=get_line("documentation.description"),
            color=0x5865f2
        )
        embed.add_field(name="🎨 Figma",
                        value=f"[Design File](https://www.figma.com/design/qri4WHzrleIpXUwH2PudMy/The-Maple-Barrel?node-id=0-1&t=00wUItIiNs4dOEhA-1) — {get_line('documentation.figma')}",
                        inline=False)
        embed.add_field(name="🎮 The Maple Games",
                        value=f"[Google Drive](https://drive.google.com/drive/folders/1Yj__wkWW5ztBsbtQvGtQVI8k5Pbncehx?usp=sharing) — {get_line('documentation.drive')}",
                        inline=False)
        embed.set_footer(text=get_line("documentation.footer"))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @nextcord.slash_command(name="help", description="Learn what Goldberg can do.", guild_ids=[SERVER_ID])
    async def help(self, interaction: nextcord.Interaction):
        if not is_dev(interaction):
            await interaction.response.send_message(
                get_line("devs_only"),
                ephemeral=True
            )
            return

        embed = nextcord.Embed(
            title="🤖 Goldberg — What I Do",
            description=get_line("help.description"),
            color=0xc8521a
        )
        embed.add_field(
            name="📚 /documentation",
            value="Internal team docs. Figma, Drive, all of it.",
            inline=False
        )
        embed.add_field(
            name="📋 /repo",
            value="Walks you through getting SVN access. Yes, it's that simple. No, you don't need to ping someone.",
            inline=False
        )
        embed.add_field(
            name="📦 /builds",
            value="Gets you the download link for the latest game builds. Dev and tester role only.",
            inline=False
        )
        embed.add_field(
            name="🗂️ /sprint_board",
            value="Shows the current sprint tasks. Grouped. Organized. Unlike your commit history.",
            inline=False
        )
        embed.add_field(
            name="✅ /my_tasks",
            value="Shows YOUR tasks for the current sprint, plus where we are in it. The ones you agreed to do. Remember those?",
            inline=False
        )
        embed.add_field(
            name="🕐 /officehours",
            value="Shows who's hosting office hours today. Go ask them your questions instead of pinging me.",
            inline=False
        )
        embed.add_field(
            name="📅 /schedule",
            value="The full weekly office hours schedule. Bookmark it. Tattoo it. Whatever works for you.",
            inline=False
        )
        embed.add_field(
            name="🍁 /socials",
            value="Links to everything The Maple Barrel. Follow us. We worked hard on those.",
            inline=False
        )
        embed.add_field(
            name="🤐 /shutup",
            value="Puts my quips on hold when you're having a *serious* conversation. Takes optional minutes (default 60). I won't enjoy it.",
            inline=False
        )
        embed.add_field(
            name="⏰ /wakeup",
            value="Ends the silent treatment early. You knew you'd miss me.",
            inline=False
        )
        embed.set_footer(text=get_line("help.footer"))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @nextcord.slash_command(name="builds", description="Get the link to download the latest game builds.", guild_ids=[SERVER_ID])
    async def builds(self, interaction: nextcord.Interaction):
        dev_role_id = 1374926068364083280
        tester_role_id = 1394048323698036786
        user_roles = {role.id for role in interaction.user.roles}
        if dev_role_id not in user_roles and tester_role_id not in user_roles:
            await interaction.response.send_message(get_line("builds.no_role"),
                                                    ephemeral=True)
            return
        await interaction.response.send_message(
            "📦 **Game Builds**\nLatest playable builds, every project:\nhttps://themapleserver.themaplebarrel.com/builds",
            ephemeral=True
        )

def setup(bot):
    bot.add_cog(GeneralCommands(bot))