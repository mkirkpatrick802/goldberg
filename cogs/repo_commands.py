import nextcord
from nextcord.ext import commands

from config import REPO_LINK, REPO_SIGNUP_LINK, SERVER_ID
from lines import get_line
from utils import is_dev


class RepoCommands(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @nextcord.slash_command(name="repo", description="Let's get you in that repo!", guild_ids=[SERVER_ID])
    async def repo(self, interaction: nextcord.Interaction):
        if not is_dev(interaction):
            await interaction.response.send_message(
                get_line("devs_only"),
                ephemeral=True
            )
            return
        
        await interaction.response.send_message(
            f"{get_line('repo.intro')}\n\n"
            f"Step 1: Click this link and make an account. Yes, a real one. Use your `first.lastname` like a functional adult.\n"
            f"```base\n{REPO_SIGNUP_LINK}\n```\n"
            f"{get_line('repo.password')}\n"
            f"Use as the admin passphrase `MapleCanoePunch42!` (because of course we trust you with power).\n\n"
            f"Step 2: Clone the repo. It's one command. Try not to mess it up:\n"
            f"```bash\nsvn checkout {REPO_LINK}\n```\n"
            f"{get_line('repo.outro')}",
            ephemeral=True
        )


def setup(bot):
    bot.add_cog(RepoCommands(bot))