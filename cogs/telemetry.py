import nextcord
from nextcord.ext import commands, tasks
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import json
import os

from config import SERVER_ID, TAIGA_URL, TAIGA_PROJECT_SLUG
from utils import get_sheet_members

TELEMETRY_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "telemetry.json")
SETUP_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "setup_data.json")
EASTERN = ZoneInfo("America/New_York")

OFFICE_HOUR_BUFFER_MINUTES = 30
TAIGA_CHECK_HOUR = 19
TAIGA_CHECK_MINUTE = 0

def load_telemetry():
    if not os.path.exists(TELEMETRY_FILE):
        return {"current_sprint": None, "sprints": {}}
    with open(TELEMETRY_FILE, "r") as f:
        return json.load(f)

def save_telemetry(data):
    os.makedirs(os.path.dirname(TELEMETRY_FILE), exist_ok=True)
    with open(TELEMETRY_FILE, "w") as f:
        json.dump(data, f, indent=4)

def load_setup():
    if not os.path.exists(SETUP_FILE):
        return {}
    with open(SETUP_FILE, "r") as f:
        return json.load(f)

class Telemetry(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.taiga_token = None

    @commands.Cog.listener()
    async def on_ready(self):
        await self.authenticate_taiga()
        if not self.check_sprint.is_running():
            self.check_sprint.start()
        if not self.taiga_completion_check.is_running():
            self.taiga_completion_check.start()

    # ── Taiga Auth ──────────────────────────────────────────────────────────────

    async def authenticate_taiga(self):
        import aiohttp
        from config import TAIGA_USERNAME, TAIGA_PASSWORD
        async with aiohttp.ClientSession() as session:
            resp = await session.post(
                f"{TAIGA_URL}/api/v1/auth",
                json={"type": "normal", "username": TAIGA_USERNAME, "password": TAIGA_PASSWORD}
            )
            data = await resp.json()
            self.taiga_token = data.get("auth_token")
            if self.taiga_token:
                print("[Telemetry] Taiga authenticated.")
            else:
                print(f"[Telemetry] Taiga auth failed: {data}")

    async def get_current_sprint(self):
        import aiohttp
        async with aiohttp.ClientSession() as session:
            # Get project ID
            resp = await session.get(
                f"{TAIGA_URL}/api/v1/projects/by_slug?slug={TAIGA_PROJECT_SLUG}",
                headers={"Authorization": f"Bearer {self.taiga_token}"}
            )
            if resp.status == 401:
                await self.authenticate_taiga()
                resp = await session.get(
                    f"{TAIGA_URL}/api/v1/projects/by_slug?slug={TAIGA_PROJECT_SLUG}",
                    headers={"Authorization": f"Bearer {self.taiga_token}"}
                )
            project = await resp.json()
            project_id = project.get("id")
            if not project_id:
                return None, None

            # Get active sprint
            resp = await session.get(
                f"{TAIGA_URL}/api/v1/milestones?project={project_id}&closed=false",
                headers={"Authorization": f"Bearer {self.taiga_token}"}
            )
            milestones = await resp.json()
            if not milestones:
                return None, None
            
            if not isinstance(milestones, list) or len(milestones) == 0:
                return None, None
            
            sprint = milestones[0]
            return sprint.get("name"), project_id

    async def get_sprint_tasks(self, project_id, sprint_id):
        import aiohttp
        all_tasks = []
        page = 1
        async with aiohttp.ClientSession() as session:
            while True:
                resp = await session.get(
                    f"{TAIGA_URL}/api/v1/tasks?project={project_id}&milestone={sprint_id}&page={page}",
                    headers={"Authorization": f"Bearer {self.taiga_token}"}
                )
                tasks = await resp.json()
                if not tasks:
                    break
                all_tasks.extend(tasks)
                if not resp.headers.get("x-pagination-next"):
                    break
                page += 1
        return all_tasks

    # ── Sprint change detection ─────────────────────────────────────────────────

    @tasks.loop(hours=1)
    async def check_sprint(self):
        sprint_name, _ = await self.get_current_sprint()
        if not sprint_name:
            return

        data = load_telemetry()
        if data["current_sprint"] == sprint_name:
            return

        print(f"[Telemetry] New sprint detected: {sprint_name}")
        data["current_sprint"] = sprint_name
        if sprint_name not in data["sprints"]:
            data["sprints"][sprint_name] = {}
        save_telemetry(data)

    @check_sprint.before_loop
    async def before_check_sprint(self):
        await self.bot.wait_until_ready()

    @check_sprint.error
    async def check_sprint_error(self, error):
        print(f"[Telemetry] check_sprint error: {error}")

    # ── Stand-up tracking ───────────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return

        setup = load_setup()
        standup_channel_id = setup.get("standup_channel_id")
        if not standup_channel_id or message.channel.id != standup_channel_id:
            return

        data = load_telemetry()
        sprint = data.get("current_sprint")
        if not sprint:
            return

        user_id = str(message.author.id)
        today = datetime.now(EASTERN).strftime("%Y-%m-%d")

        if user_id not in data["sprints"][sprint]:
            data["sprints"][sprint][user_id] = {
                "office_hours_attended": False,
                "standup_days": [],
                "taiga_complete": None
            }

        if today not in data["sprints"][sprint][user_id]["standup_days"]:
            data["sprints"][sprint][user_id]["standup_days"].append(today)
            save_telemetry(data)
            print(f"[Telemetry] Stand-up logged for {message.author.name} on {today}")

    # ── Office hours VC tracking ────────────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            return
        if after.channel is None:
            return

        try:
            sheet_data = get_sheet_members()
        except Exception as e:
            print(f"[Telemetry] Failed to load sheet: {e}")
            return

        user_id = str(member.id)
        member_data = next((m for m in sheet_data if m.get("discord_id") == user_id), None)
        if not member_data:
            return

        scheduled_day = member_data.get("day")
        scheduled_time = member_data.get("start_time")
        if not scheduled_day or not scheduled_time:
            return

        now = datetime.now(EASTERN)
        if now.strftime("%A") != scheduled_day:
            return

        try:
            hour, minute = map(int, scheduled_time.split(":"))
        except ValueError:
            return

        scheduled_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        window_start = scheduled_dt - timedelta(minutes=OFFICE_HOUR_BUFFER_MINUTES)
        window_end = scheduled_dt + timedelta(hours=1, minutes=OFFICE_HOUR_BUFFER_MINUTES)

        if not (window_start <= now <= window_end):
            return

        data = load_telemetry()
        sprint = data.get("current_sprint")
        if not sprint:
            return

        if user_id not in data["sprints"][sprint]:
            data["sprints"][sprint][user_id] = {
                "office_hours_attended": False,
                "standup_days": [],
                "taiga_complete": None
            }

        if not data["sprints"][sprint][user_id]["office_hours_attended"]:
            data["sprints"][sprint][user_id]["office_hours_attended"] = True
            save_telemetry(data)
            print(f"[Telemetry] Office hours attendance logged for {member.name}")

    # ── Taiga completion check ──────────────────────────────────────────────────

    @tasks.loop(minutes=1)
    async def taiga_completion_check(self):
        now = datetime.now(EASTERN)
        if now.strftime("%A") != "Sunday":
            return
        if now.hour != TAIGA_CHECK_HOUR or now.minute != TAIGA_CHECK_MINUTE:
            return

        print("[Telemetry] Running Sunday Taiga completion check...")

        sprint_name, project_id = await self.get_current_sprint()
        if not sprint_name or not project_id:
            print("[Telemetry] Could not get current sprint for completion check.")
            return

        # Get sprint ID
        import aiohttp
        async with aiohttp.ClientSession() as session:
            resp = await session.get(
                f"{TAIGA_URL}/api/v1/milestones?project={project_id}&closed=false",
                headers={"Authorization": f"Bearer {self.taiga_token}"}
            )
            milestones = await resp.json()
            if not milestones:
                return
            sprint_id = milestones[0].get("id")

        tasks = await self.get_sprint_tasks(project_id, sprint_id)

        try:
            sheet_data = get_sheet_members()
        except Exception as e:
            print(f"[Telemetry] Failed to load sheet: {e}")
            return

        # Build a map of taiga_name -> discord_id
        name_to_id = {m["taiga_name"].lower(): m["discord_id"] for m in sheet_data if m.get("taiga_name")}

        # Check each person's tasks
        incomplete_by_user = {}
        for task in tasks:
            status = task.get("status_extra_info", {}).get("name", "").lower()
            if status not in ("new", "in progress"):
                continue
            assigned = task.get("assigned_to_extra_info")
            if not assigned:
                continue
            taiga_name = assigned.get("full_name_display", "").lower()
            discord_id = name_to_id.get(taiga_name)
            if discord_id:
                incomplete_by_user[discord_id] = incomplete_by_user.get(discord_id, 0) + 1

        data = load_telemetry()
        sprint = data.get("current_sprint")
        if not sprint:
            return

        for member in sheet_data:
            discord_id = member.get("discord_id")
            if not discord_id:
                continue
            if discord_id not in data["sprints"].get(sprint, {}):
                data["sprints"][sprint][discord_id] = {
                    "office_hours_attended": False,
                    "standup_days": [],
                    "taiga_complete": None
                }
            incomplete = incomplete_by_user.get(discord_id, 0)
            data["sprints"][sprint][discord_id]["taiga_complete"] = incomplete == 0
            print(f"[Telemetry] {member['name']} — taiga_complete: {incomplete == 0} ({incomplete} incomplete tasks)")

        save_telemetry(data)
        print("[Telemetry] Taiga completion check done.")

    @taiga_completion_check.before_loop
    async def before_taiga_check(self):
        await self.bot.wait_until_ready()

    @taiga_completion_check.error
    async def taiga_completion_check_error(self, error):
        print(f"[Telemetry] taiga_completion_check error: {error}")


async def setup(bot):
    bot.add_cog(Telemetry(bot))