import asyncio
import re
import subprocess
import random
from nextcord import DiscordServerError, HTTPException
from nextcord.ext import tasks, commands
import dateutil.parser
import xml.etree.ElementTree as ET

from config import SERVER_ID, REPO_LINK

# List of funny messages for missing commit messages
funny_messages = [
    "No commit message provided... 🤦‍♂️ Are you even trying?",
    "Another mysterious commit with no explanation... 🕵️‍♂️",
    "Commit message? Nah, who needs those? 🤡",
    "Silence is golden, except in commit messages. Say something! 🦗",
    "Ah yes, the legendary 'fix stuff' commit. Classic. 😎",
    "Code changed, but the mystery remains... 🔍",
    "When future devs see this, they will be *so* grateful. Not. 🙃",
    "Commit messages are optional, right? RIGHT?? 😬",
    "One day, archaeologists will uncover this and wonder what happened. 🏺",
    "Congratulations! You’ve won the 'Least Informative Commit' award! 🏆",
    "Commit message? We don't do that here. 🛑",
    "Left the message field blank—just like your soul. 🕳️",
    "This commit brought to you by caffeine and poor decisions. ☕🤯",
    "If this breaks everything, we’ll never know why. 🔥🚒",
    "Might work. Might not. Who knows? 🤷‍♂️",
    "Change made. Explanation withheld. 🕶️",
    "This is why we can't have nice things. 🧨",
    "Future me, I’m sorry. Present me didn’t care. 😔",
    "No message, just vibes. ✨",
    "I could explain this commit... but I won't. 🐸☕",
    "A commit a day keeps the tech lead away. Maybe. 🧑‍💻🕊️",
    "This was definitely done on purpose. Totally. 💯",
    "Blame the last person who touched this. 😈",
    "Too lazy to write a message, but not too lazy to commit. 🎯",
    "If this breaks the build, it was already broken. 🧱",
    "Shhh... it’s a stealth commit. 🥷",
    "This commit has no description because it’s a surprise! 🎁",
    "Added magic. Don’t ask how. 🪄",
    "Like a ninja, this change appeared without warning. 🥷✨",
    "Message redacted for national security. 🕵️‍♀️🔒"
]

last_commit = None


async def get_latest_commit():
    """Fetch the latest commit details from SVN."""

    cmd = [
    "svn", "log", "-l", "1",
    "--xml", "--incremental",
    "--non-interactive",
    "--config-option", "servers:global:http-timeout=8",
    "--trust-server-cert",
    REPO_LINK,
    ]


    def run():
        return subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=10,
        )

    try:
        result = await asyncio.to_thread(run)
    except subprocess.TimeoutExpired:
        return {"error": "SVN request timed out (10s). Backing off and will retry."}
    except subprocess.CalledProcessError as e:
        return {"error": f"Error fetching commit: {str(e)}"}
    except Exception as e:
        return {"error": f"Unexepected error invoking SVN: {e}"}
    
    if result.returncode != 0:
        err = (result.stderr or "").strip()
        if "E170013" in err or "timed out" in err.lower():
            return {"error": "SVN connection timed out. Will retry with backoff."}
        return {"error": f"SVN returned {result.returncode}: {err or 'unknown error'}"}
    
    try:
        entry = ET.fromstring(result.stdout)
        if entry.tag is None:
            return {"error": "No log entries found."}

        revision = entry.get("revision").strip()
        author = entry.find("author").text.strip()
        date = entry.find("date").text.strip()
        commit_message = entry.find("msg").text

        if commit_message is None:
            commit_message = random.choice(funny_messages)

        return {
            "revision": revision,
            "author": author,
            "date": date,
            "message": commit_message.strip()
        }
    
    except Exception as e:
        return {"error": f"⚠️ Unexpected error: {str(e)}"}

class SvnCommits(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.last_revision = None
        self.backoff_seconds = 0

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.check_svn_commits.is_running():
            self.check_svn_commits.start()

    @tasks.loop(seconds=30, reconnect=True)
    async def check_svn_commits(self):
        try:
            if self.backoff_seconds > 0:
                print(f"[Check SVN Commits] Backing off for {self.backoff_seconds} seconds...")
                await asyncio.sleep(self.backoff_seconds)

            guild = self.bot.get_guild(SERVER_ID)
            if guild is None:
                return

            commit_channel_id = 1354153095009013821
            channel = guild.get_channel(commit_channel_id)
            if channel is None:
                return
            
            commit_info = await get_latest_commit()
            if not commit_info:
                return

            if "error" in commit_info:
                await channel.send(commit_info["error"])
                return

            async for message in channel.history(limit=20):
                if message.author == self.bot.user:
                    match = re.search(r"\*\*Revision:\*\* (\S+)", message.content)
                    if match:
                        self.last_revision = match.group(1)
                    break

            revision = commit_info["revision"]
            if self.last_revision == revision:
                self.backoff_seconds = 0  # reset backoff if no error
                return

            self.last_revision = revision

            clock_cog = self.bot.get_cog("Clock")
            if clock_cog:
                raw_date = commit_info["date"]
                clean_date = raw_date.split(" (")[0]
                commit_time = dateutil.parser.parse(clean_date)
                clock_cog.set_last_commit_time(commit_time)

            raw_date = commit_info["date"]
            commit_time = dateutil.parser.isoparse(raw_date).astimezone()
            formatted_date = commit_time.strftime("%Y-%m-%d %H:%M:%S %z (%a, %d %b %Y)")
            commit_info["date"] = formatted_date

            message = (
                f"🔹 **New Commit!**\n"
                f"🔸 **Revision:** {revision}\n"
                f"🔸 **Author:** {commit_info['author']}\n"
                f"🔸 **Date:** {commit_info['date']}\n"
                f"🔸 **Message:** {commit_info['message']}"
            )
            await channel.send(message)
            self.backoff_seconds = 0  # success resets backoff

        except (DiscordServerError, HTTPException) as e:
            print(f"[Check SVN Commits] Discord error: {e}")
            self.increase_backoff()
        except Exception as e:
            print(f"[Check SVN Commits] Unexpected error: {e}")
            self.increase_backoff()


    @check_svn_commits.error
    async def check_svn_commits_error(self, exception):
        print(f"[Check SVN Commits] Error occurred: {exception}")

    def increase_backoff(self):
        """Doubles backoff time up to a max of 300 seconds (5 minutes)."""
        if self.backoff_seconds == 0:
            self.backoff_seconds = 10
        else:
            self.backoff_seconds = min(self.backoff_seconds * 2, 300)


async def setup(bot):
    bot.add_cog(SvnCommits(bot))
