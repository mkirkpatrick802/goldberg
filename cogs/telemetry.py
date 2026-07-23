import nextcord
from nextcord.ext import commands
from nextcord.ext import tasks as ext_tasks
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import asyncio
import json
import os
import subprocess
import xml.etree.ElementTree as ET

from config import SERVER_ID, TAIGA_URL, TAIGA_PROJECT_SLUG, REPO_LINK
from utils import get_sheet_members

TELEMETRY_FILE = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", "telemetry.json"))
SETUP_FILE     = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", "setup_data.json"))
EASTERN = ZoneInfo("America/New_York")

OFFICE_HOUR_BUFFER_MINUTES = 30
TAIGA_CHECK_HOUR   = 19
TAIGA_CHECK_MINUTE = 0
SVN_CHECK_MINUTES  = 5

def load_telemetry():
    if not os.path.exists(TELEMETRY_FILE):
        return {"current_sprint": None, "sprints": {}}
    with open(TELEMETRY_FILE, "r") as f:
        return json.load(f)

def save_telemetry(data):
    os.makedirs(os.path.dirname(TELEMETRY_FILE), exist_ok=True)
    with open(TELEMETRY_FILE, "w") as f:
        json.dump(data, f, indent=4)

def load_setup_config():
    if not os.path.exists(SETUP_FILE):
        return {}
    with open(SETUP_FILE, "r") as f:
        return json.load(f)

def blank_user():
    return {
        "commits":                0,
        "office_hours_attended":  0,
        "office_hours_attendees": 0,
        "standup_days":           [],
        "taiga_complete":         None,
        "voice_minutes":          0,
    }

def ensure_user(data, sprint, user_id):
    if user_id not in data["sprints"][sprint]:
        data["sprints"][sprint][user_id] = blank_user()
    # Back-fill any missing keys for existing entries
    for k, v in blank_user().items():
        data["sprints"][sprint][user_id].setdefault(k, v)


# ── SVN helpers (blocking; call via asyncio.to_thread) ────────────────────────
# These deliberately don't reuse commit_notifier's fetch: that one asks for only
# the single newest commit, which is fine for announcing but would undercount
# here whenever two commits land inside one poll window.

_SVN_BASE_FLAGS = [
    "--non-interactive",
    "--config-option", "servers:global:http-timeout=8",
    "--trust-server-cert",
]


def _run_svn(args, timeout):
    result = subprocess.run(
        ["svn", *args, *_SVN_BASE_FLAGS, REPO_LINK],
        text=True, capture_output=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "").strip() or f"svn exited {result.returncode}")
    return result.stdout


def svn_head_revision():
    """The repo's current revision number."""
    return int(_run_svn(["info", "--show-item", "revision"], timeout=15).strip())


def svn_log_from(revision):
    """
    Every commit from `revision` through HEAD as (revision, author) pairs.

    The range starts at a revision we know exists rather than revision + 1,
    which SVN rejects once we've caught up to HEAD. The caller drops the
    first entry, having already counted it.
    """
    out = _run_svn(["log", "-r", f"{revision}:HEAD", "--xml"], timeout=30)
    entries = []
    for entry in ET.fromstring(out).findall("logentry"):
        author = entry.find("author")
        entries.append((
            int(entry.get("revision")),
            author.text.strip() if author is not None and author.text else "",
        ))
    return entries


class Telemetry(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.taiga_token = None
        # Track when each user joined a voice channel: {member_id: datetime}
        self._voice_join_times = {}

    @commands.Cog.listener()
    async def on_ready(self):
        await self.authenticate_taiga()
        if not self.check_sprint.is_running():
            self.check_sprint.start()
        if not self.taiga_completion_check.is_running():
            self.taiga_completion_check.start()
        if not self.check_svn_commits.is_running():
            self.check_svn_commits.start()

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

            resp = await session.get(
                f"{TAIGA_URL}/api/v1/milestones?project={project_id}&closed=false",
                headers={"Authorization": f"Bearer {self.taiga_token}"}
            )
            milestones = await resp.json()
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
                sprint_tasks = await resp.json()
                if not sprint_tasks:
                    break
                all_tasks.extend(sprint_tasks)
                if not resp.headers.get("x-pagination-next"):
                    break
                page += 1
        return all_tasks

    # ── Sprint change detection ─────────────────────────────────────────────────

    @ext_tasks.loop(hours=1)
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

        setup = load_setup_config()
        standup_channel_id = setup.get("standup_channel_id")
        if not standup_channel_id or message.channel.id != standup_channel_id:
            return

        data = load_telemetry()
        sprint = data.get("current_sprint")
        if not sprint:
            return

        user_id = str(message.author.id)
        today = datetime.now(EASTERN).strftime("%Y-%m-%d")

        ensure_user(data, sprint, user_id)

        if today not in data["sprints"][sprint][user_id]["standup_days"]:
            data["sprints"][sprint][user_id]["standup_days"].append(today)
            save_telemetry(data)
            print(f"[Telemetry] Stand-up logged for {message.author.name} on {today}")

    # ── Voice & office hours tracking ───────────────────────────────────────────

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            return

        setup        = load_setup_config()
        category_id  = setup.get("dev_zone_category_id")
        now          = datetime.now(EASTERN)
        user_id      = str(member.id)

        # ── Voice minutes: user LEFT a channel ──────────────────────────────────
        if before.channel is not None:
            in_category = (before.channel.category_id == category_id) if category_id else True
            if in_category and user_id in self._voice_join_times:
                joined_at = self._voice_join_times.pop(user_id)
                minutes = int((now - joined_at).total_seconds() / 60)
                if minutes > 0:
                    data   = load_telemetry()
                    sprint = data.get("current_sprint")
                    if sprint:
                        ensure_user(data, sprint, user_id)
                        data["sprints"][sprint][user_id]["voice_minutes"] += minutes
                        save_telemetry(data)
                        print(f"[Telemetry] {member.name} logged {minutes} voice minutes")

        # ── User JOINED a channel ───────────────────────────────────────────────
        if after.channel is not None:
            in_category = (after.channel.category_id == category_id) if category_id else True
            if in_category:
                self._voice_join_times[user_id] = now

                # ── Office hours attendance (host) ──────────────────────────────
                try:
                    sheet_data = get_sheet_members()
                except Exception as e:
                    print(f"[Telemetry] Failed to load sheet: {e}")
                    return

                member_data = next((m for m in sheet_data if m.get("discord_id") == user_id), None)
                if member_data:
                    scheduled_day  = member_data.get("day")
                    scheduled_time = member_data.get("start_time")

                    if scheduled_day and scheduled_time and now.strftime("%A") == scheduled_day:
                        try:
                            hour, minute = map(int, scheduled_time.split(":"))
                        except ValueError:
                            hour, minute = None, None

                        if hour is not None:
                            scheduled_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                            window_start = scheduled_dt - timedelta(minutes=OFFICE_HOUR_BUFFER_MINUTES)
                            window_end   = scheduled_dt + timedelta(hours=1, minutes=OFFICE_HOUR_BUFFER_MINUTES)

                            if window_start <= now <= window_end:
                                data   = load_telemetry()
                                sprint = data.get("current_sprint")
                                if sprint:
                                    ensure_user(data, sprint, user_id)
                                    current = data["sprints"][sprint][user_id]["office_hours_attended"]
                                    if current < 2:
                                        data["sprints"][sprint][user_id]["office_hours_attended"] = current + 1
                                        save_telemetry(data)
                                        print(f"[Telemetry] Office hours attendance logged for {member.name} ({current + 1}/2)")

                # ── Office hours popularity (count attendees joining host's VC) ──
                # Check if this channel currently has a host running office hours
                for host in sheet_data:
                    host_id            = host.get("discord_id")
                    host_scheduled_day = host.get("day")
                    host_scheduled_time = host.get("start_time")

                    if not host_id or host_id == user_id:
                        continue
                    if not host_scheduled_day or not host_scheduled_time:
                        continue
                    if now.strftime("%A") != host_scheduled_day:
                        continue

                    try:
                        h_hour, h_min = map(int, host_scheduled_time.split(":"))
                    except ValueError:
                        continue

                    h_dt         = now.replace(hour=h_hour, minute=h_min, second=0, microsecond=0)
                    h_window_start = h_dt
                    h_window_end   = h_dt + timedelta(hours=1)

                    if not (h_window_start <= now <= h_window_end):
                        continue

                    # Check if the host is in this channel
                    guild = self.bot.get_guild(SERVER_ID)
                    if not guild:
                        continue
                    host_member = guild.get_member(int(host_id))
                    if not host_member:
                        continue
                    if not host_member.voice or host_member.voice.channel != after.channel:
                        continue

                    # Host is in this channel during their office hours — count this join
                    data   = load_telemetry()
                    sprint = data.get("current_sprint")
                    if sprint:
                        ensure_user(data, sprint, host_id)
                        data["sprints"][sprint][host_id]["office_hours_attendees"] += 1
                        save_telemetry(data)
                        print(f"[Telemetry] {member.name} joined {host['name']}'s office hours")
                    break

    # ── SVN commit tracking ─────────────────────────────────────────────────────

    @ext_tasks.loop(minutes=SVN_CHECK_MINUTES)
    async def check_svn_commits(self):
        """
        Count commits per member for the current sprint.

        Progress is a revision high-water mark in telemetry.json, so restarts
        and failed polls never double-count or skip: a failure leaves the mark
        untouched and the next tick re-reads the same range.
        """
        data = load_telemetry()
        if not data.get("current_sprint"):
            return

        last_rev = data.get("last_commit_revision")

        # First run: start counting at HEAD rather than replaying the repo's
        # entire history into whichever sprint happens to be open.
        if last_rev is None:
            try:
                head = await asyncio.to_thread(svn_head_revision)
            except Exception as e:
                print(f"[Telemetry] Could not read SVN head revision: {e}")
                return
            data["last_commit_revision"] = head
            save_telemetry(data)
            print(f"[Telemetry] Commit tracking baseline set to r{head}")
            return

        try:
            entries = await asyncio.to_thread(svn_log_from, last_rev)
        except Exception as e:
            print(f"[Telemetry] SVN log failed (will retry from r{last_rev}): {e}")
            return

        new = [(rev, author) for rev, author in entries if rev > last_rev]
        if not new:
            return

        # SVN usernames are account usernames — the same credential Apache
        # authenticates against — so an author maps straight to a member.
        by_username = {
            m["username"].lower(): m["discord_id"]
            for m in get_sheet_members()
            if m.get("username") and m.get("discord_id")
        }

        # Re-read: the SVN call above yielded, and the voice/stand-up listeners
        # write this same file.
        data   = load_telemetry()
        sprint = data.get("current_sprint")
        if not sprint:
            return

        for rev, author in new:
            discord_id = by_username.get(author.lower())
            if not discord_id:
                print(f"[Telemetry] r{rev}: no active account for SVN user '{author}'")
                continue
            ensure_user(data, sprint, discord_id)
            data["sprints"][sprint][discord_id]["commits"] += 1
            print(f"[Telemetry] r{rev} counted for {author}")

        data["last_commit_revision"] = max(rev for rev, _ in new)
        save_telemetry(data)

    @check_svn_commits.before_loop
    async def before_check_svn_commits(self):
        await self.bot.wait_until_ready()

    @check_svn_commits.error
    async def check_svn_commits_error(self, error):
        print(f"[Telemetry] check_svn_commits error: {error}")

    # ── Taiga completion check ──────────────────────────────────────────────────

    @ext_tasks.loop(minutes=1)
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

        sprint_tasks = await self.get_sprint_tasks(project_id, sprint_id)

        try:
            sheet_data = get_sheet_members()
        except Exception as e:
            print(f"[Telemetry] Failed to load sheet: {e}")
            return

        name_to_id = {m["taiga_name"].lower(): m["discord_id"] for m in sheet_data if m.get("taiga_name")}

        incomplete_by_user = {}
        for task in sprint_tasks:
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

        data   = load_telemetry()
        sprint = data.get("current_sprint")
        if not sprint:
            return

        for m in sheet_data:
            discord_id = m.get("discord_id")
            if not discord_id:
                continue
            ensure_user(data, sprint, discord_id)
            incomplete = incomplete_by_user.get(discord_id, 0)
            had_tasks = discord_id in incomplete_by_user or any(
                task.get("assigned_to_extra_info", {}) and
                name_to_id.get(
                    task.get("assigned_to_extra_info", {}).get("full_name_display", "").lower()) == discord_id
                for task in sprint_tasks
            )
            if not had_tasks:
                data["sprints"][sprint][discord_id]["taiga_complete"] = None
            else:
                data["sprints"][sprint][discord_id]["taiga_complete"] = incomplete == 0
            print(f"[Telemetry] {m['name']} — taiga_complete: {incomplete == 0} ({incomplete} incomplete tasks)")

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