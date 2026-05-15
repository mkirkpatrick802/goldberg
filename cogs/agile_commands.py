import nextcord
from nextcord.ext import commands

class AgileCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

async def setup(bot):
    bot.add_cog(AgileCommands(bot))