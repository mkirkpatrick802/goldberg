import asyncio
import json
import os
import re
import subprocess
from nextcord import DiscordServerError, HTTPException
from nextcord.ext import tasks, commands
import dateutil.parser
import xml.etree.ElementTree as ET

from config import SERVER_ID
from lines import get_line
from utils import get_watched_projects

_SETUP_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "setup_data.json")

def _get_commit_channel_id():
    try:
        with open(_SETUP_FILE, "r") as f:
            return json.load(f).get("commit_channel_id")
    except FileNotFoundError:
        return None


async def get_latest_commit(repo_link: str):
    """Fetch the latest commit details from one SVN repo."""

    # `svn log` issues a WebDAV REPORT that this server answers in ~9s (far
    # slower than the lightweight `svn info` request), so an 8s http-timeout
    # aborted every poll. Give it real headroom; keep the subprocess kill a few
    # seconds above the http-timeout so svn reports its own error first.
    cmd = [
    "svn", "log", "-l", "1",
    "--xml", "--incremental",
    "--non-interactive",
    "--config-option", "servers:global:http-timeout=30",
    "--trust-server-cert",
    repo_link,
    ]


    def run():
        return subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=35,
        )

    try:
        result = await asyncio.to_thread(run)
    except subprocess.TimeoutExpired:
        return {"error": "SVN request timed out (35s). Backing off and will retry."}
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
            commit_message = get_line("commit.no_message")

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
        # Keyed by project id (or "legacy" — see _watched_projects) rather than
        # a single scalar, since one channel can now carry commits from
        # several repos.
        self.last_revision = {}
        self.backoff_seconds = 0
        self.svn_down = False

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.check_svn_commits.is_running():
            self.check_svn_commits.start()

    def _recover_last_revisions(self, history):
        """
        Rebuild last_revision from the channel's recent posts, per project —
        recovers state across a bot restart the same way the single-repo
        version always did, just keyed by the **Project:** line each post now
        carries instead of assuming there's only one repo to recover.
        """
        seen = {}
        for message in history:
            if message.author != self.bot.user:
                continue
            proj_match = re.search(r"\*\*Project:\*\* (.+)", message.content)
            rev_match = re.search(r"\*\*Revision:\*\* (\S+)", message.content)
            if proj_match and rev_match:
                seen.setdefault(proj_match.group(1).strip(), rev_match.group(1))
        return seen

    @tasks.loop(seconds=30, reconnect=True)
    async def check_svn_commits(self):
        try:
            if self.backoff_seconds > 0:
                print(f"[Check SVN Commits] Backing off for {self.backoff_seconds} seconds...")
                await asyncio.sleep(self.backoff_seconds)

            guild = self.bot.get_guild(SERVER_ID)
            if guild is None:
                return

            commit_channel_id = _get_commit_channel_id()
            if commit_channel_id is None:
                print("[Check SVN Commits] No commit channel set. Use /setup commits.")
                return
            channel = guild.get_channel(commit_channel_id)
            if channel is None:
                return

            projects = get_watched_projects()
            if not projects:
                print("[Check SVN Commits] No projects have a repo link set yet.")
                return

            if not self.last_revision:
                history = [m async for m in channel.history(limit=50)]
                by_name = self._recover_last_revisions(history)
                for p in projects:
                    if p["name"] in by_name:
                        self.last_revision[p["id"]] = by_name[p["name"]]

            any_error = False
            for proj in projects:
                commit_info = await get_latest_commit(proj["repo_link"])
                if not commit_info:
                    continue

                if "error" in commit_info:
                    any_error = True
                    if not self.svn_down:
                        await channel.send(f"**{proj['name']}:** {commit_info['error']}")
                    continue

                revision = commit_info["revision"]
                if self.last_revision.get(proj["id"]) == revision:
                    continue
                self.last_revision[proj["id"]] = revision

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
                    f"🔸 **Project:** {proj['name']}\n"
                    f"🔸 **Revision:** {revision}\n"
                    f"🔸 **Author:** {commit_info['author']}\n"
                    f"🔸 **Date:** {commit_info['date']}\n"
                    f"🔸 **Message:** {commit_info['message']}"
                )
                await channel.send(message)

            # SVN answered for at least one repo. Only announce recovery /
            # keep backing off if every repo in the list is currently failing.
            if any_error:
                if not self.svn_down:
                    self.svn_down = True
                self.increase_backoff()
            else:
                if self.svn_down:
                    await channel.send("✅ SVN is reachable again.")
                    self.svn_down = False
                self.backoff_seconds = 0

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


def setup(bot):
    bot.add_cog(SvnCommits(bot))
