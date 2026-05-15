import nextcord
from nextcord.ext import commands

from config import REPO_LINK, REPO_SIGNUP_LINK
from utils import user_has_any_role


class RepoCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @nextcord.slash_command(name="repo", description="Let's get you in that repo!",
                            guild_ids=[1353924127303532648])
    async def repo(self, interaction: nextcord.Interaction):
        if not user_has_any_role(interaction, ["Dev"]):
            await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
            return
        
        await interaction.response.send_message(
            f"Alright, time to spoon-feed you into the repo. Here's what you *should* be able to do without me:\n\n"
            f"Step 1: Click this link and make an account. Yes, a real one. Use your `first.lastname` like a functional adult.\n"
            f"```base\n{REPO_SIGNUP_LINK}\n```\n"
            f"Pick whatever password you like—I'm not your babysitter.\n"
            f"Use as the admin passphrase `MapleCanoePunch42!` (because of course we trust you with power).\n\n"
            f"Step 2: Clone the repo. It's one command. Try not to mess it up:\n"
            f"```bash\nsvn checkout {REPO_LINK}\n```\n"
            f"Congrats, you’re now slightly more useful. Let’s see how long that lasts.",
            ephemeral=True
        )


async def setup(bot):
    bot.add_cog(RepoCommands(bot))