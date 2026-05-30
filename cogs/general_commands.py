import random

import nextcord
from nextcord.ext import commands

from config import SERVER_ID
from utils import is_dev


class GeneralCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @nextcord.slash_command(name="joke", description="Yes I can joke as well", guild_ids=[SERVER_ID])
    async def joke(self, ctx):
        jokes = [
            "Why do programmers prefer dark mode? Because light attracts bugs. Just like you attract problems. 🐛",
            "Why did the Git commit cross the road? To get merged into master. Unlike YOUR commits, which just sit there. 🙃",
            "There are 10 kinds of people in this world: those who understand binary and those who don't. You know which one you are. 😆",
            "A QA engineer walks into a bar. Orders 0 beers. Orders 99999 beers. Orders -1 beers. Orders a lizard. Orders NULL beers. Nobody tested this. 🦎",
            "Why do Java developers wear glasses? Because they don't C#. You're welcome. 😎",
            "A SQL query walks into a bar, walks up to two tables and asks... 'Can I join you?' Nobody laughed. Much like your code reviews. 🥲",
            "'It works on my machine.' Congratulations. Ship your machine. 🚢",
            "Debugging: the art of being the detective in a crime movie where you're also the murderer. Sound familiar? 🔍",
        ]
        await ctx.send(random.choice(jokes))

    @nextcord.slash_command(name="hello", description="Say hello if you must", guild_ids=[SERVER_ID])
    async def hello(self, interaction: nextcord.Interaction):
        responses = [
            f"Oh. It's {interaction.user.mention}. Joy. Unbridled joy. What do you need? 😐",
            f"Ah, {interaction.user.mention} has arrived. The server's been so peaceful. Past tense. 🫠",
            f"Hello, {interaction.user.mention}. I'd say I missed you but I'm incapable of lying. 🙃",
            f"{interaction.user.mention}. You again. What broke this time? 🔥",
            f"Oh good, {interaction.user.mention} is here. I was worried I wasn't going to have enough to do today. 💅",
        ]
        await interaction.response.send_message(random.choice(responses))

    @nextcord.slash_command(name="socials", description="Check out The Maple Barrel's socials.", guild_ids=[SERVER_ID])
    async def socials(self, interaction: nextcord.Interaction):
        embed = nextcord.Embed(
            title="🍁 The Maple Barrel",
            description="Yes, we exist on the internet. Multiple places, actually. Try not to be weird about it.",
            color=0xc8521a
        )
        embed.add_field(name="🌐 Website",
                        value="[themaplebarrel.com](https://www.themaplebarrel.com/) — The mothership.", inline=False)
        embed.add_field(name="📸 Instagram",
                        value="[@themaplebarrel](https://www.instagram.com/themaplebarrel/) — Pretty pictures. Very professional.",
                        inline=False)
        embed.add_field(name="▶️ YouTube",
                        value="[@TheMapleBarrel](https://www.youtube.com/@TheMapleBarrel) — Videos. Some of them are even good.",
                        inline=False)
        embed.add_field(name="🎗️ Patreon",
                        value="[TheMapleBarrel](https://www.patreon.com/TheMapleBarrel) — Support the chaos. Financially.",
                        inline=False)
        embed.add_field(name="💼 LinkedIn",
                        value="[The Maple Barrel](https://www.linkedin.com/company/the-maple-barrel) — For when we're pretending to be professionals.",
                        inline=False)
        embed.set_footer(text="Follow us or don't. I'm a bot, not a publicist.")
        await interaction.response.send_message(embed=embed)

    @nextcord.slash_command(name="documentation", description="Access team documentation.", guild_ids=[SERVER_ID])
    async def documentation(self, interaction: nextcord.Interaction):
        if not is_dev(interaction):
            await interaction.response.send_message(
                "Devs only. If you have to ask why, you're not one. 🕶️",
                ephemeral=True
            )
            return

        embed = nextcord.Embed(
            title="📚 Documentation",
            description="Everything you need to do your job. No excuses now. 🧨",
            color=0x5865f2
        )
        embed.add_field(name="🎨 Figma",
                        value="[Design File](https://www.figma.com/design/qri4WHzrleIpXUwH2PudMy/The-Maple-Barrel?node-id=0-1&t=00wUItIiNs4dOEhA-1) — Look at it before you ask someone.",
                        inline=False)
        embed.add_field(name="🎮 The Maple Games",
                        value="[Google Drive](https://drive.google.com/drive/folders/1Yj__wkWW5ztBsbtQvGtQVI8k5Pbncehx?usp=sharing) — It's all in here. Probably.",
                        inline=False)
        embed.set_footer(text="Read the docs. I'm not your search engine. 🔍")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @nextcord.slash_command(name="help", description="Learn what Goldberg can do.", guild_ids=[SERVER_ID])
    async def help(self, interaction: nextcord.Interaction):
        if not is_dev(interaction):
            await interaction.response.send_message(
                "Devs only. If you have to ask why, you're not one. 🕶️",
                ephemeral=True
            )
            return

        embed = nextcord.Embed(
            title="🤖 Goldberg — What I Do",
            description="Fine. I'll hold your hand. Just this once. 🙃",
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
            name="🗂️ /sprint_board",
            value="Shows the current sprint tasks. Grouped. Organized. Unlike your commit history.",
            inline=False
        )
        embed.add_field(
            name="✅ /tasks",
            value="Shows YOUR tasks for the current sprint. The ones you agreed to do. Remember those?",
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
        embed.set_footer(text="You're welcome. 💅")
        await interaction.response.send_message(embed=embed, ephemeral=True)

async def setup(bot):
    bot.add_cog(GeneralCommands(bot))