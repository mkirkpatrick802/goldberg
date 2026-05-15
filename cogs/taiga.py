import nextcord
from nextcord.ext import commands, tasks
import aiohttp
import json
import os
from datetime import datetime
from zoneinfo import ZoneInfo

from config import TAIGA_URL, TAIGA_USERNAME, TAIGA_PASSWORD, TAIGA_PROJECT_SLUG, SERVER_ID
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
            tasks = await self.get_sprint_tasks(session, project_id, sprint_id)

        # Group tasks by parent story
        story_map = {s["id"]: s["subject"] for s in stories}
        grouped = {}

        for task in tasks:
            status = task.get("status_extra_info", {}).get("name", "").lower()
            if status not in ("new", "in progress"):
                continue

            story_id = task.get("user_story")
            story_title = story_map.get(story_id, "No Story")
            if story_id not in grouped:
                grouped[story_id] = {"title": story_title, "new": [], "in_progress": []}

            task_title = task.get("subject", "Untitled")
            assigned = task.get("assigned_to_extra_info")
            assignee = self.resolve_discord_mention(
                assigned.get("full_name_display", "Unassigned") if assigned else "Unassigned",
                sheet_data
            )
            entry = f"  • {task_title} — {assignee}"

            if status == "new":
                grouped[story_id]["new"].append(entry)
            else:
                grouped[story_id]["in_progress"].append(entry)

        if not grouped:
            return f"📋 **Sprint Update — {sprint_name}**\n\n✅ No new or in progress tasks. Either you're crushing it or nobody's working."

        lines = [f"📋 **Sprint Update — {sprint_name}**\n"]
        for story_id, data in grouped.items():
            lines.append(f"📖 **{data['title']}**")
            if data["new"]:
                lines.append("🆕 New")
                lines.extend(data["new"])
            if data["in_progress"]:
                lines.append("🔄 In Progress")
                lines.extend(data["in_progress"])
            lines.append("")

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

    @nextcord.slash_command(name="sprint_board", description="See the current sprint board.", guild_ids=[SERVER_ID])
    async def sprint_board(self, interaction: nextcord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            sheet_data = get_sheet_members()
        except Exception as e:
            await interaction.followup.send(f"⚠️ Failed to load sheet data: {e}")
            return

        message = await self.build_sprint_message(sheet_data)
        await interaction.followup.send(message, allowed_mentions=nextcord.AllowedMentions.none())

    @nextcord.slash_command(name="my_tasks", description="See your current tasks.", guild_ids=[SERVER_ID])
    async def my_tasks(self, interaction: nextcord.Interaction):
        await interaction.response.defer(ephemeral=True)

        try:
            sheet_data = get_sheet_members()
        except Exception as e:
            await interaction.followup.send(f"⚠️ Failed to load sheet data: {e}")
            return

        # Find the user's Taiga name from the sheet
        user_id = str(interaction.user.id)
        taiga_name = None
        for member in sheet_data:
            if member.get("discord_id") == user_id:
                taiga_name = member.get("taiga_name")
                break

        if not taiga_name:
            await interaction.followup.send("⚠️ I don't know who you are in Taiga. Bug your admin.", ephemeral=True)
            return

        async with aiohttp.ClientSession() as session:
            project_id = await self.get_project_id(session)
            sprint = await self.get_current_sprint(session, project_id)
            if not sprint:
                await interaction.followup.send("⚠️ No active sprint found.")
                return

            stories = await self.get_user_stories(session, project_id, sprint.get("id"))

        new_items = []
        in_progress_items = []

        for task in tasks:
            assigned = task.get("assigned_to_extra_info")
            if not assigned:
                continue
            if assigned.get("full_name_display", "").strip().lower() != taiga_name.strip().lower():
                continue

            status = task.get("status_extra_info", {}).get("name", "").lower()
            title = task.get("subject", "Untitled")
            story_title = task.get("user_story_extra_info", {}).get("subject", "No Story")

            if status == "new":
                new_items.append(f"• {title} *({story_title})*")
            elif status == "in progress":
                in_progress_items.append(f"• {title} *({story_title})*")

        lines = [f"📋 **Your Tasks — {sprint.get('name', 'Current Sprint')}**\n"]

        if new_items:
            lines.append("🆕 **New**")
            lines.extend(new_items)
            lines.append("")

        if in_progress_items:
            lines.append("🔄 **In Progress**")
            lines.extend(in_progress_items)

        if not new_items and not in_progress_items:
            lines.append(
                "✅ No new or in progress tasks. Either you're done or you haven't started. Goldberg isn't judging. (He is.)")

        await interaction.followup.send(
            "\n".join(lines),
            allowed_mentions=nextcord.AllowedMentions.none()
        )

async def setup(bot):
    bot.add_cog(Taiga(bot))