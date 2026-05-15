import random

import nextcord
from nextcord.ext import commands

class GeneralCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @nextcord.slash_command(name="ping", description="My favorite game!", guild_ids=[1353924127303532648])
    async def ping(self, ctx):
        """Test command to check if bot is alive."""
        await ctx.send("Pong! 🏓")

    @nextcord.slash_command(name="joke", description="Yes I can joke as well", guild_ids=[1353924127303532648])
    async def joke(self, ctx):
        """Send a random joke."""
        jokes = [
            "Why do programmers prefer dark mode? Because light attracts bugs! 🐛",
            "Why did the Git commit cross the road? To get merged into master! 🤖",
            "There are 10 kinds of people in this world: those who understand binary and those who don't. 😆",
        ]
        await ctx.send(random.choice(jokes))

    @nextcord.slash_command(name="hello", description="Say hello if you must", guild_ids=[1353924127303532648])
    async def hello(self, interaction: nextcord.Interaction):
        response = (
            f"Oh, it's you, {interaction.user.mention}. What do you need this time? "
            "I'm guessing you're too lazy to do anything yourself again? 😏"
        )
        await interaction.response.send_message(response)

async def setup(bot):
    bot.add_cog(GeneralCommands(bot))