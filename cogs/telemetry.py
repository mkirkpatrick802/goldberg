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

from config import SERVER_ID, TAIGA_URL, TAIGA_PROJECT_SLUG
from utils import get_sheet_members, pick_current_milestone, get_watched_projects

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


# ── Office-hours session dedup ────────────────────────────────────────────────
# Both office-hour counters must be idempotent per person, per office-hour
# occurrence: a host who mutes/rejoins, or an attendee who leaves and comes
# back, must not be counted twice. We identify an occurrence by the host's
# discord id plus the calendar date of the scheduled slot, and remember what
# we've already counted for it in telemetry.json so restarts don't reset it.

def _office_hour_session(member_data, now):
    """
    If `now` falls within `member_data`'s office-hour window, return that
    occurrence's session key ('{discord_id}-{YYYY-MM-DD}'). Otherwise None.

    The window is the scheduled 1-hour slot padded by OFFICE_HOUR_BUFFER_MINUTES
    on each side, and is shared by both the host-attendance and attendee counts
    so they always agree on when an office hour is "live".
    """
    discord_id = member_data.get("discord_id")
    day        = member_data.get("day")
    time_str   = member_data.get("start_time")
    if not discord_id or not day or not time_str:
        return None
    if now.strftime("%A") != day:
        return None
    try:
        hour, minute = map(int, time_str.split(":"))
    except ValueError:
        return None

    scheduled_dt = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    window_start = scheduled_dt - timedelta(minutes=OFFICE_HOUR_BUFFER_MINUTES)
    window_end   = scheduled_dt + timedelta(hours=1, minutes=OFFICE_HOUR_BUFFER_MINUTES)
    if not (window_start <= now <= window_end):
        return None
    return f"{discord_id}-{scheduled_dt.strftime('%Y-%m-%d')}"


def _session_record(data, session_key):
    """Get (creating if needed) the dedup record for one office-hour occurrence."""
    sessions = data.setdefault("office_hours_sessions", {})
    return sessions.setdefault(session_key, {"host_counted": False, "attendees": []})


# ── SVN helpers (blocking; call via asyncio.to_thread) ────────────────────────
# These deliberately don't reuse commit_notifier's fetch: that one asks for only
# the single newest commit, which is fine for announcing but would undercount
# here whenever two commits land inside one poll window.

_SVN_BASE_FLAGS = [
    "--non-interactive",
    "--config-option", "servers:global:http-timeout=8",
    "--trust-server-cert",
]


def _run_svn(args, timeout, repo_link):
    result = subprocess.run(
        ["svn", *args, *_SVN_BASE_FLAGS, repo_link],
        text=True, capture_output=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "").strip() or f"svn exited {result.returncode}")
    return result.stdout


def svn_head_revision(repo_link):
    """The repo's current revision number."""
    return int(_run_svn(["info", "--show-item", "revision"], timeout=15, repo_link=repo_link).strip())


def svn_log_from(revision, repo_link):
    """
    Every commit from `revision` through HEAD as (revision, author) pairs.

    The range starts at a revision we know exists rather than revision + 1,
    which SVN rejects once we've caught up to HEAD. The caller drops the
    first entry, having already counted it.
    """
    out = _run_svn(["log", "-r", f"{revision}:HEAD", "--xml"], timeout=30, repo_link=repo_link)
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
        # One-shot latch so check_svn_commits reports having nothing to poll
        # once, rather than every SVN_CHECK_MINUTES.
        self._warned_no_repos = False

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
            sprint = pick_current_milestone(milestones)
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
        # Office-hour dedup records are keyed by date and only matter within the
        # sprint they were counted in, so a new sprint clears the scratchpad.
        data["office_hours_sessions"] = {}
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

        # Ignore in-channel state changes — mute, deafen, start/stop streaming,
        # camera on/off all fire this event with before.channel == after.channel.
        # Treating them as joins re-ran every counter on each toggle, inflating
        # office-hour attendance and popularity. Only real joins/moves/leaves
        # change the channel.
        if before.channel == after.channel:
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
                self._track_office_hours(member, after.channel, now)

    # ── Office-hours attendance & popularity ────────────────────────────────────
    def _track_office_hours(self, member, channel, now):
        """
        Record office-hour attendance for a real channel join. Both counters
        dedup per office-hour occurrence (see _office_hour_session), so a host
        who rejoins or an attendee who comes and goes is only ever counted once.
        """
        user_id = str(member.id)
        try:
            sheet_data = get_sheet_members()
        except Exception as e:
            print(f"[Telemetry] Failed to load sheet: {e}")
            return

        # ── Host attended their own office hours ────────────────────────────────
        member_data = next((m for m in sheet_data if m.get("discord_id") == user_id), None)
        if member_data:
            session_key = _office_hour_session(member_data, now)
            if session_key:
                data   = load_telemetry()
                sprint = data.get("current_sprint")
                if sprint:
                    record = _session_record(data, session_key)
                    if not record["host_counted"]:
                        ensure_user(data, sprint, user_id)
                        data["sprints"][sprint][user_id]["office_hours_attended"] += 1
                        record["host_counted"] = True
                        save_telemetry(data)
                        print(f"[Telemetry] Office hours attendance logged for {member.name}")

        # ── Attendee joined a host's live office hours ──────────────────────────
        guild = self.bot.get_guild(SERVER_ID)
        for host in sheet_data:
            host_id = host.get("discord_id")
            if not host_id or host_id == user_id:
                continue
            session_key = _office_hour_session(host, now)
            if not session_key:
                continue

            # The host must actually be sitting in the channel this person joined.
            if not guild:
                continue
            host_member = guild.get_member(int(host_id))
            if not host_member or not host_member.voice or host_member.voice.channel != channel:
                continue

            data   = load_telemetry()
            sprint = data.get("current_sprint")
            if sprint:
                record = _session_record(data, session_key)
                if user_id not in record["attendees"]:
                    ensure_user(data, sprint, host_id)
                    data["sprints"][sprint][host_id]["office_hours_attendees"] += 1
                    record["attendees"].append(user_id)
                    save_telemetry(data)
                    print(f"[Telemetry] {member.name} joined {host['name']}'s office hours")
            break

    # ── SVN commit tracking ─────────────────────────────────────────────────────

    @ext_tasks.loop(minutes=SVN_CHECK_MINUTES)
    async def check_svn_commits(self):
        """
        Count commits per member for the current sprint, across every project
        that has a repo link (see utils.get_watched_projects) — each member's
        total is summed across every repo they commit to rather than broken
        out per project, so no telemetry.json schema change was needed there.

        Progress is a revision high-water mark PER PROJECT in
        last_commit_revisions, so restarts and failed polls never double-count
        or skip: a failure leaves that project's mark untouched and the next
        tick re-reads the same range, independently of every other project.

        Saved after each project rather than batched at the end, so one
        project's progress is never lost if a later project's SVN call yields
        control and a concurrent listener (voice/stand-up) writes the file.
        """
        data = load_telemetry()
        if not data.get("current_sprint"):
            return

        projects = get_watched_projects()
        if not projects:
            # Nothing to poll: no active wiki project has a repo_link and the
            # legacy REPO_LINK fallback is empty. This used to return in
            # silence, which is how commit tracking sat dormant for whole
            # sprints while the dashboard showed a column of zeroes that looked
            # like real "nobody committed" data. Say it once per process so the
            # log names the cause without repeating every SVN_CHECK_MINUTES.
            if not self._warned_no_repos:
                self._warned_no_repos = True
                print("[Telemetry] Commit tracking is idle: no active project has a "
                      "repo link set (Wiki -> Projects), and REPO_LINK is unset. "
                      "Commits will read as untracked until one is configured.")
            return

        # A repo showed up — let the warning fire again if they all go away.
        self._warned_no_repos = False

        # SVN usernames are account usernames — the same credential Apache
        # authenticates against — so an author maps straight to a member.
        by_username = {
            m["username"].lower(): m["discord_id"]
            for m in get_sheet_members()
            if m.get("username") and m.get("discord_id")
        }

        for proj in projects:
            key = str(proj["id"])
            data = load_telemetry()
            if not data.get("current_sprint"):
                return
            revisions = data.setdefault("last_commit_revisions", {})

            # One-time migration from the single-repo scalar this used to be —
            # only meaningful when there's exactly one watched repo, since
            # with several there's no way to know which one the old baseline
            # belonged to. Anything else starts fresh at its own HEAD below.
            legacy_rev = data.pop("last_commit_revision", None)
            if legacy_rev is not None and len(projects) == 1 and key not in revisions:
                revisions[key] = legacy_rev
                save_telemetry(data)

            last_rev = revisions.get(key)

            # First run for this project: start counting at HEAD rather than
            # replaying its entire history into whichever sprint is open.
            if last_rev is None:
                try:
                    head = await asyncio.to_thread(svn_head_revision, proj["repo_link"])
                except Exception as e:
                    print(f"[Telemetry] {proj['name']}: could not read SVN head revision: {e}")
                    continue
                revisions[key] = head
                save_telemetry(data)
                print(f"[Telemetry] {proj['name']}: commit tracking baseline set to r{head}")
                continue

            try:
                entries = await asyncio.to_thread(svn_log_from, last_rev, proj["repo_link"])
            except Exception as e:
                print(f"[Telemetry] {proj['name']}: SVN log failed (will retry from r{last_rev}): {e}")
                continue

            new = [(rev, author) for rev, author in entries if rev > last_rev]
            if not new:
                continue

            # Re-read: the SVN call above yielded, and the voice/stand-up
            # listeners write this same file.
            data   = load_telemetry()
            sprint = data.get("current_sprint")
            if not sprint:
                return
            revisions = data.setdefault("last_commit_revisions", {})

            for rev, author in new:
                discord_id = by_username.get(author.lower())
                if not discord_id:
                    print(f"[Telemetry] {proj['name']} r{rev}: no active account for SVN user '{author}'")
                    continue
                ensure_user(data, sprint, discord_id)
                data["sprints"][sprint][discord_id]["commits"] += 1
                print(f"[Telemetry] {proj['name']} r{rev} counted for {author}")

            revisions[key] = max(rev for rev, _ in new)
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
            sprint_id = pick_current_milestone(milestones).get("id")

        sprint_tasks = await self.get_sprint_tasks(project_id, sprint_id)

        try:
            sheet_data = get_sheet_members()
        except Exception as e:
            print(f"[Telemetry] Failed to load sheet: {e}")
            return

        name_to_id = {m["taiga_name"].lower(): m["discord_id"] for m in sheet_data if m.get("taiga_name")}

        incomplete_by_user = {}
        for task in sprint_tasks:
            status = (task.get("status_extra_info") or {}).get("name", "").lower()
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


def setup(bot):
    bot.add_cog(Telemetry(bot))