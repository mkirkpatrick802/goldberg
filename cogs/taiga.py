import nextcord
from nextcord.ext import commands, tasks
import aiohttp
import json
import os
import random
from datetime import datetime
from zoneinfo import ZoneInfo

from config import TAIGA_URL, TAIGA_USERNAME, TAIGA_PASSWORD, TAIGA_PROJECT_SLUG, SERVER_ID
from utils import get_sheet_members, chunk_message, is_dev, pick_current_milestone

REMINDER_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "reminder_data.json")
EASTERN = ZoneInfo("America/New_York")

# Personal task-reminder DMs go out every Tuesday, Friday, and Sunday at 10:00
# ET — the cadence the old public sprint board used, now delivered privately.
REMINDER_DAYS = {"Tuesday", "Friday", "Sunday"}
REMINDER_HOUR = 10
REMINDER_MINUTE = 0

# Goldberg's DM voice — sarcastic, game-dev-flavored, allergic to sounding like a
# bot. Two tones: a gentler "nudge" for the first half of the sprint and an
# urgent "final" once we're past the midpoint. The exact day/deadline lives in
# the progress banner, so the intros stay timing-agnostic.
HALFWAY_INTROS = [
    "Reminder: these are *still* sitting on your plate for **{sprint}**. I'm not mad. I'm just documenting it for retro.",
    "Checking in on **{sprint}**. Here's what you still owe me:",
    "Perfect time to pretend you were *just about* to start these **{sprint}** tasks:",
    "The board says you've got unfinished business in **{sprint}**. The board doesn't lie. I do. But not about this:",
    "These **{sprint}** tasks haven't moved. Curious. Let's fix that:",
]

FINAL_INTROS = [
    "Last call. **{sprint}** ends this week and these are still open. No pressure. (Immense pressure.)",
    "**{sprint}** wraps up in days, not decades. These haven't moved. Neither have you, apparently:",
    "The **{sprint}** deadline is breathing down your neck and your tasks are staging a sit-in:",
    "Final warning before **{sprint}** closes: ship these or explain yourself at retro. Your call:",
    "It's crunch o'clock for **{sprint}**. These are the tasks standing between you and a clean burndown chart:",
]

REMINDER_SIGNOFFS = [
    "Don't make me bring this up in the standup channel. 💀",
    "Get to it before I start taking it personally. 🫡",
    "I believe in you. Barely. 🤌",
    "Move it - your future self is already disappointed. 😬",
    "- Goldberg, doing more of your project management than you are. 💅",
    "Close these or I'm telling everyone you rage quit game dev. 👀",
]

def load_reminders():
    if not os.path.exists(REMINDER_FILE):
        return {}
    with open(REMINDER_FILE, "r") as f:
        return json.load(f)

def save_reminders(data):
    with open(REMINDER_FILE, "w") as f:
        json.dump(data, f, indent=2)

def phase_for_sprint(sprint: dict) -> str:
    """Pick the DM tone from where we are in the sprint: 'final' once we're past
    the midpoint (deadline pressure), otherwise 'halfway' (a gentler nudge).
    Defaults to 'halfway' when the sprint has no start/finish dates."""
    start = sprint.get("estimated_start")
    finish = sprint.get("estimated_finish")
    if not start or not finish:
        return "halfway"

    start_d = datetime.strptime(start, "%Y-%m-%d").date()
    finish_d = datetime.strptime(finish, "%Y-%m-%d").date()
    today = datetime.now(EASTERN).date()

    total_days = (finish_d - start_d).days + 1
    day_number = (today - start_d).days + 1
    return "final" if day_number > total_days / 2 else "halfway"


def sprint_progress_line(sprint: dict) -> str | None:
    """A one-line 'where are we in the sprint' banner: which day of how many,
    the deadline date, and how much runway is left. Returns None when the
    sprint is missing its start/finish dates (nothing useful to show)."""
    start = sprint.get("estimated_start")
    finish = sprint.get("estimated_finish")
    if not start or not finish:
        return None

    start_d = datetime.strptime(start, "%Y-%m-%d").date()
    finish_d = datetime.strptime(finish, "%Y-%m-%d").date()
    today = datetime.now(EASTERN).date()

    total_days = (finish_d - start_d).days + 1  # inclusive of both endpoints
    day_number = (today - start_d).days + 1
    day_number = max(1, min(day_number, total_days))  # clamp for display
    days_left = (finish_d - today).days

    if days_left > 1:
        runway = f"{days_left} days left"
    elif days_left == 1:
        runway = "1 day left"
    elif days_left == 0:
        runway = "due today"
    else:
        runway = f"{abs(days_left)} day{'s' if abs(days_left) != 1 else ''} overdue"

    # Avoid %-d / %#d — not portable across platforms. Build the date by hand.
    deadline = f"{finish_d.strftime('%A, %B')} {finish_d.day}"
    return f"🗓️ **Day {day_number} of {total_days}** · deadline **{deadline}** ({runway})"

class Taiga(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.token = None

    @commands.Cog.listener()
    async def on_ready(self):
        await self.authenticate()
        if not self.refresh_token.is_running():
            self.refresh_token.start()
        if not self.task_reminder.is_running():
            self.task_reminder.start()

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
        if resp.status == 401:
            print("[Taiga] Token expired, re-authenticating...")
            await self.authenticate()
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
        # Sprint whose dates contain today (old sprints are left open in Taiga,
        # so the newest-listed milestone isn't reliably the active one).
        return pick_current_milestone(milestones)

    async def get_sprint_tasks(self, session, project_id, sprint_id):
        all_tasks = []
        page = 1

        while True:
            resp = await session.get(
                f"{TAIGA_URL}/api/v1/tasks?project={project_id}&milestone={sprint_id}&page={page}",
                headers={"Authorization": f"Bearer {self.token}"}
            )
            sprint_tasks = await resp.json()
            if not sprint_tasks:
                break
            all_tasks.extend(sprint_tasks)
            next_page = resp.headers.get("x-pagination-next")
            if not next_page:
                break
            page += 1

        return all_tasks

    async def get_user_stories(self, session, project_id, sprint_id):
        all_stories = []
        page = 1

        while True:
            resp = await session.get(
                f"{TAIGA_URL}/api/v1/userstories?project={project_id}&milestone={sprint_id}&page={page}",
                headers={"Authorization": f"Bearer {self.token}"}
            )
            stories = await resp.json()
            if not stories:
                break
            all_stories.extend(stories)
            next_page = resp.headers.get("x-pagination-next")
            if not next_page:
                break
            page += 1

        return all_stories

    def resolve_discord_mention(self, taiga_full_name: str, sheet_data: list) -> str:
        for member in sheet_data:
            if member.get("taiga_name", "").strip().lower() == taiga_full_name.strip().lower():
                discord_id = member.get("discord_id")
                if discord_id:
                    return f"<@{discord_id}>"
        return taiga_full_name

    def group_unfinished_by_member(self, tasks: list, sheet_data: list) -> dict:
        """Group unfinished (New / In Progress) tasks by the assignee's Discord id.

        Returns {discord_id: {"new": [line, ...], "in_progress": [line, ...]}}.
        Only tasks assigned to a known member (matched by Taiga display name) are
        included; unassigned tasks and members without a Discord id are skipped.
        """
        name_to_id = {}
        for member in sheet_data:
            taiga_name = (member.get("taiga_name") or "").strip().lower()
            discord_id = member.get("discord_id")
            if taiga_name and discord_id:
                name_to_id[taiga_name] = str(discord_id)

        grouped: dict = {}
        for task in tasks:
            status = (task.get("status_extra_info") or {}).get("name", "").lower()
            if status not in ("new", "in progress"):
                continue

            assigned = task.get("assigned_to_extra_info")
            if not assigned:
                continue
            name = assigned.get("full_name_display", "").strip().lower()
            discord_id = name_to_id.get(name)
            if not discord_id:
                continue

            title = task.get("subject", "Untitled")
            story_title = (task.get("user_story_extra_info") or {}).get("subject", "No Story")
            entry = f"• {title} *({story_title})*"

            bucket = grouped.setdefault(discord_id, {"new": [], "in_progress": []})
            if status == "new":
                bucket["new"].append(entry)
            else:
                bucket["in_progress"].append(entry)

        return grouped

    def build_reminder_dm(self, sprint_name: str, phase: str, bucket: dict,
                          progress: str | None = None) -> str:
        pool = HALFWAY_INTROS if phase == "halfway" else FINAL_INTROS
        intro = random.choice(pool).format(sprint=sprint_name)

        lines = [intro, ""]
        if progress:
            lines.append(progress)
            lines.append("")
        if bucket["new"]:
            lines.append("🆕 **New** (haven't even pretended to start these)")
            lines.extend(bucket["new"])
            lines.append("")
        if bucket["in_progress"]:
            lines.append("🔄 **In Progress** (allegedly)")
            lines.extend(bucket["in_progress"])
        lines.append("")
        lines.append(random.choice(REMINDER_SIGNOFFS))
        return "\n".join(lines)

    async def send_task_reminders(self, phase: str) -> int:
        """Fetch the current sprint, group unfinished tasks by member, and DM each
        member who has any. Returns the number of members successfully DMed.
        `phase` is 'halfway' or 'final' and only controls the message copy."""
        try:
            sheet_data = get_sheet_members()
        except Exception as e:
            print(f"[Taiga] Reminder: failed to load member data: {e}")
            return 0

        async with aiohttp.ClientSession() as session:
            project_id = await self.get_project_id(session)
            if not project_id:
                print("[Taiga] Reminder: could not find project.")
                return 0
            sprint = await self.get_current_sprint(session, project_id)
            if not sprint:
                print("[Taiga] Reminder: no active sprint.")
                return 0
            sprint_tasks = await self.get_sprint_tasks(session, project_id, sprint.get("id"))

        sprint_name = sprint.get("name", "Current Sprint")
        progress = sprint_progress_line(sprint)
        grouped = self.group_unfinished_by_member(sprint_tasks, sheet_data)

        guild = self.bot.get_guild(SERVER_ID)
        if not guild:
            print("[Taiga] Reminder: guild not found.")
            return 0

        sent = 0
        for discord_id, bucket in grouped.items():
            if not bucket["new"] and not bucket["in_progress"]:
                continue
            member = guild.get_member(int(discord_id))
            if not member:
                continue
            message = self.build_reminder_dm(sprint_name, phase, bucket, progress)
            try:
                for chunk in chunk_message(message):
                    await member.send(chunk)
                sent += 1
            except (nextcord.Forbidden, nextcord.HTTPException) as e:
                print(f"[Taiga] Reminder: couldn't DM {discord_id}: {e}")
        return sent

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
            status = (task.get("status_extra_info") or {}).get("name", "").lower()
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

    @tasks.loop(hours=24)
    async def refresh_token(self):
        print("[Taiga] Refreshing auth token...")
        await self.authenticate()

    @refresh_token.before_loop
    async def before_refresh_token(self):
        await self.bot.wait_until_ready()

    @tasks.loop(minutes=1, reconnect=True)
    async def task_reminder(self):
        now = datetime.now(EASTERN)
        if now.strftime("%A") not in REMINDER_DAYS:
            return
        if now.hour != REMINDER_HOUR or now.minute != REMINDER_MINUTE:
            return

        # Fire at most once per calendar day, so a restart during the 10:00
        # minute can't double-DM everyone.
        today_str = now.date().isoformat()
        reminders = load_reminders()
        if reminders.get("last_sent_date") == today_str:
            return

        async with aiohttp.ClientSession() as session:
            project_id = await self.get_project_id(session)
            if not project_id:
                return
            sprint = await self.get_current_sprint(session, project_id)
            if not sprint:
                print("[Taiga] Reminder: no active sprint; skipping today.")
                return

        phase = phase_for_sprint(sprint)
        count = await self.send_task_reminders(phase)

        reminders["last_sent_date"] = today_str
        save_reminders(reminders)
        print(f"[Taiga] Sent {count} '{phase}' task-reminder DM(s) ({today_str}).")

    @task_reminder.before_loop
    async def before_task_reminder(self):
        await self.bot.wait_until_ready()

    @nextcord.slash_command(name="sprint_board", description="See the current sprint board.", guild_ids=[SERVER_ID])
    async def sprint_board(self, interaction: nextcord.Interaction):
        if not is_dev(interaction):
            await interaction.response.send_message(
                "Devs only. If you have to ask why, you're not one. 🕶️",
                ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            sheet_data = get_sheet_members()
        except Exception as e:
            await interaction.followup.send(f"⚠️ Failed to load sheet data: {e}")
            return

        message = await self.build_sprint_message(sheet_data)
        for chunk in chunk_message(message):
            await interaction.followup.send(chunk, allowed_mentions=nextcord.AllowedMentions.none(), ephemeral=True)

    @nextcord.slash_command(name="test_task_reminders", description="Manually send sprint task-reminder DMs now.", guild_ids=[SERVER_ID])
    async def test_task_reminders(
        self,
        interaction: nextcord.Interaction,
        phase: str = nextcord.SlashOption(
            name="phase",
            description="Which reminder message to send.",
            choices={"Halfway": "halfway", "Final": "final"},
            required=False,
            default="final",
        ),
    ):
        if not interaction.user.guild_permissions.administrator:
            await interaction.response.send_message("Admins only.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)
        count = await self.send_task_reminders(phase)
        await interaction.followup.send(
            f"✅ Sent {count} '{phase}' reminder DM(s) to members with open tasks.",
            ephemeral=True,
        )

    @nextcord.slash_command(name="my_tasks", description="See your current tasks.", guild_ids=[SERVER_ID])
    async def my_tasks(self, interaction: nextcord.Interaction):
        if not is_dev(interaction):
            await interaction.response.send_message(
                "Devs only. If you have to ask why, you're not one. 🕶️",
                ephemeral=True
            )
            return

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

            sprint_tasks  = await self.get_sprint_tasks(session, project_id, sprint.get("id"))

        grouped = self.group_unfinished_by_member(sprint_tasks, sheet_data)
        bucket = grouped.get(user_id, {"new": [], "in_progress": []})

        lines = [f"📋 **Your Tasks — {sprint.get('name', 'Current Sprint')}**\n"]
        progress = sprint_progress_line(sprint)
        if progress:
            lines.append(progress)
            lines.append("")

        if bucket["new"]:
            lines.append("🆕 **New**")
            lines.extend(bucket["new"])
            lines.append("")

        if bucket["in_progress"]:
            lines.append("🔄 **In Progress**")
            lines.extend(bucket["in_progress"])

        if not bucket["new"] and not bucket["in_progress"]:
            lines.append(
                "✅ No new or in progress tasks. Either you're done or you haven't started. Goldberg isn't judging. (He is.)")

        await interaction.followup.send(
            "\n".join(lines),
            allowed_mentions=nextcord.AllowedMentions.none()
        )

def setup(bot):
    bot.add_cog(Taiga(bot))