import nextcord
from nextcord.ext import commands, tasks
import aiohttp
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from config import TAIGA_URL, TAIGA_USERNAME, TAIGA_PASSWORD, TAIGA_PROJECT_SLUG
from utils import get_sheet_members

SETUP_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "setup_data.json")
EASTERN = ZoneInfo("America/New_York")

ANNOUNCE_DAYS = {"Tuesday", "Friday", "Sunday"}
ANNOUNCE_HOUR = 10
ANNOUNCE_MINUTE = 0

def load_setup():
    if not os.path.exists(SETUP_FILE):
        return {}
    with open(SETUP_FILE, "r") as f:
        return json.load(f)

class Taiga(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.token = None

    @commands.Cog.listener()
    async def on_ready(self):
        await self.authenticate()
        if not self.sprint_update.is_running():
            self.sprint_update.start()

    async def authenticate(self):
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                f"{TAIGA_URL}/api/v1/auth",
                json={
                    "type": "normal",
                    "username": TAIGA_USERNAME,
                    "password": TAIGA_PASSWORD
                }
            )
            data = await resp.json()
            self.token = data.get("auth_token")
            if self.token:
                print("[Taiga] Authenticated successfully.")
            else:
                print(f"[Taiga] Authentication failed: {data}")

    async def get_project_id(self, session):
        resp = await session.get(
            f"{TAIGA_URL}/api/v1/projects/by_slug?slug={TAIGA_PROJECT_SLUG}",
            headers={"Authorization": f"Bearer {self.token}"}
        )
        data = await resp.json()
        return data.get("id")

    async def get_current_sprint(self, session, project_id):
        resp = await session.get(
            f"{TAIGA_URL}/api/v1/milestones?project={project_id}&closed=false",
            headers={"Authorization": f"Bearer {self.token}"}
        )
        milestones = await resp.json()
        if not milestones:
            return None
        # Return the first open sprint
        return milestones[0]

    async def get_sprint_tasks(self, session, project_id, sprint_id):
        resp = await session.get(
            f"{TAIGA_URL}/api/v1/tasks?project={project_id}&milestone={sprint_id}",
            headers={"Authorization": f"Bearer {self.token}"}
        )
        return await resp.json()

    async def get_user_stories(self, session, project_id, sprint_id):
        resp = await session.get(
            f"{TAIGA_URL}/api/v1/userstories?project={project_id}&milestone={sprint_id}",
            headers={"Authorization": f"Bearer {self.token}"}
        )
        return await resp.json()

    def resolve_discord_mention(self, taiga_full_name: str, sheet_data: list) -> str:
        for member in sheet_data:
            if member.get("taiga_name", "").strip().lower() == taiga_full_name.strip().lower():
                discord_id = member.get("discord_id")
                if discord_id:
                    return f"<@{discord_id}>"
        return taiga_full_name

    async def build_sprint_message(self, sheet_data):
        async with aiohttp.ClientSession() as session:
            project_id = await self.get_project_id(session)
            if not project_id:
                return "⚠️ Could not find Taiga project."

            sprint = await self.get_current_sprint(session, project_id)
            if not sprint:
                return "⚠️ No active sprint found."

            sprint_name = sprint.get("name", "Current Sprint")
            sprint_id = sprint.get("id")

            stories = await self.get_user_stories(session, project_id, sprint_id)

            new_items = []
            in_progress_items = []

            for story in stories:
                status = story.get("status_extra_info", {}).get("name", "").lower()
                title = story.get("subject", "Untitled")
                assigned = story.get("assigned_to_extra_info")
                assignee = self.resolve_discord_mention(
                    assigned.get("full_name_display", "Unassigned") if assigned else "Unassigned",
                    sheet_data
                )

                if "new" in status:
                    new_items.append(f"• {title} — {assignee}")
                elif "in progress" in status or "in-progress" in status:
                    in_progress_items.append(f"• {title} — {assignee}")

            lines = [f"📋 **Sprint Update — {sprint_name}**\n"]

            if new_items:
                lines.append("🆕 **New**")
                lines.extend(new_items)
                lines.append("")

            if in_progress_items:
                lines.append("🔄 **In Progress**")
                lines.extend(in_progress_items)

            if not new_items and not in_progress_items:
                lines.append("✅ No new or in progress tasks. Either you're crushing it or nobody's working.")

            return "\n".join(lines)

    @tasks.loop(minutes=1, reconnect=True)
    async def sprint_update(self):
        now = datetime.now(EASTERN)
        if now.strftime("%A") not in ANNOUNCE_DAYS:
            return
        if now.hour != ANNOUNCE_HOUR or now.minute != ANNOUNCE_MINUTE:
            return

        setup = load_setup()
        channel_id = setup.get("taiga_channel_id")
        if not channel_id:
            print("[Taiga] No taiga channel set. Use /setup taigachannel.")
            return

        channel = self.bot.get_channel(channel_id)
        if not channel:
            return

        try:
            sheet_data = get_sheet_members()
        except Exception as e:
            print(f"[Taiga] Failed to load sheet data: {e}")
            sheet_data = []

        message = await self.build_sprint_message(sheet_data)
        await channel.send(message, allowed_mentions=nextcord.AllowedMentions.none())

    @sprint_update.before_loop
    async def before_sprint_update(self):
        await self.bot.wait_until_ready()

async def setup(bot):
    bot.add_cog(Taiga(bot))