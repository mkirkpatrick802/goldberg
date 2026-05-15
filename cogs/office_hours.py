import nextcord
from nextcord.ext import commands, tasks
from datetime import datetime
import gspread
from google.oauth2.service_account import Credentials
import random
from google.auth.transport.requests import Request

from config import DATA_SHEET_KEY, OFFICE_HOUR_CHANNEL, SCOPES, SERVER_ID, SERVICE_ACCOUNT_FILE, TIMEZONE, WORKSHEET_NAME

# ─── Goldberg's Vocabulary ─────────────────────────────────────────────────────

ANNOUNCEMENT_INTROS = [
    "Alright, listen up.",
    "Oh look, it's that time again.",
    "Everybody stop what you're doing. Or don't. Probably don't.",
    "Your calendar reminder that you absolutely ignored? That was this.",
    "Yes, it's happening. No, you can't reschedule.",
    "Attention, inhabitants of this server.",
    "Put down whatever you're doing that definitely isn't work.",
]

ANNOUNCEMENT_OUTROS = [
    "Try not to ask anything too obvious.",
    "Questions only. Save the small talk for someone who cares.",
    "They showed up. The least you can do is have an actual question ready.",
    "This is your one chance to look like you know what you're doing. Use it wisely.",
    "Don't waste their time. Or do. I'm a bot, not a cop.",
]

NO_HOURS_TODAY = [
    "Nobody's on the clock today. Enjoy your blissful, question-answering-free existence.",
    "No office hours today. You're on your own. Good luck with that.",
    "Nope. Nothing. Not a single person has agreed to deal with you today.",
    "Today is office-hour-free. Figure it out yourself for once.",
]

SCHEDULE_FETCH_ERROR = [
    "I tried to pull the schedule and something broke. Shocking, truly.",
    "The sheet isn't cooperating. Classic.",
    "Couldn't load the schedule. Someone probably broke something. Not naming names.",
]

# ─── Helpers ───────────────────────────────────────────────────────────────────

def get_schedule() -> list[dict]:
    """Fetch and parse the schedule from Google Sheets."""
    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(DATA_SHEET_KEY).worksheet(WORKSHEET_NAME)

    # Row 1 is a merged header, row 2 is column headers, data starts row 3
    all_rows = sheet.get_all_values()
    headers = [h.strip() for h in all_rows[1]]
    data_rows = all_rows[2:]

    schedule = []
    for row in data_rows:
        entry = dict(zip(headers, row))
        if entry.get("Active", "").upper() != "TRUE":
            continue
        if not entry.get("Day of the Week") or not entry.get("Start Time"):
            continue
        schedule.append({
            "name":       entry["Name"],
            "discord_id": str(entry["Discord ID"]).strip(),
            "day":        entry["Day of the Week"].strip(),
            "start_time": entry["Start Time"].strip(),
        })
    return schedule


def is_office_hour_starting(entry: dict, now: datetime) -> bool:
    """Return True if this entry's office hour is starting at `now` (to the minute)."""
    if now.strftime("%A") != entry["day"]:
        return False
    try:
        hour, minute = map(int, entry["start_time"].split(":"))
    except ValueError:
        return False
    return now.hour == hour and now.minute == minute


def fmt_time(time_str: str) -> str:
    """Convert 24h time string to 12h format. e.g. '14:00' -> '2:00 PM'"""
    try:
        t = datetime.strptime(time_str, "%H:%M")
        return t.strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return time_str


def build_announcement(entry: dict) -> str:
    intro = random.choice(ANNOUNCEMENT_INTROS)
    outro = random.choice(ANNOUNCEMENT_OUTROS)
    return (
        f"{intro}\n\n"
        f"🕐 **Office Hours are starting NOW.**\n"
        f"<@{entry['discord_id']}> (**{entry['name']}**) has graciously agreed to field your questions.\n"
        f"📅 Every **{entry['day']}** at **{fmt_time(entry['start_time'])}** EDT\n\n"
        f"*{outro}*"
    )

# ─── Cog ───────────────────────────────────────────────────────────────────────

class OfficeHours(commands.Cog):
    """Announces office hours. Goldberg does it with attitude."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.announced_today: set[str] = set()

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.check_office_hours.is_running():
            self.check_office_hours.start()

    def cog_unload(self):
        self.check_office_hours.cancel()

    # ── Background task: runs every 60 seconds ──────────────────────────────────
    @tasks.loop(seconds=60, reconnect=True)
    async def check_office_hours(self):
        now = datetime.now(TIMEZONE)
        current_minute_key = now.strftime("%A-%H:%M")
        print(f"[OfficeHours] 🔄 Tick — {now.strftime('%A %Y-%m-%d %H:%M')} EDT")

        # Purge stale keys from previous minutes
        self.announced_today = {k for k in self.announced_today if k == current_minute_key}

        channel = self.bot.get_channel(OFFICE_HOUR_CHANNEL)
        if channel is None:
            print(f"[OfficeHours] ⚠️  Channel {OFFICE_HOUR_CHANNEL} not found. Did someone delete it? Fantastic.")
            return
        print(f"[OfficeHours] ✅ Channel found: #{channel.name}")

        try:
            schedule = get_schedule()
            print(f"[OfficeHours] 📋 Loaded {len(schedule)} active devs from sheet")
        except Exception as e:
            print(f"[OfficeHours] ⚠️  Sheet fetch failed: {e}")
            return

        for entry in schedule:
            slot_key = f"{entry['discord_id']}-{current_minute_key}"

            if slot_key in self.announced_today:
                print(f"[OfficeHours] ⏭️  Skipping {entry['name']} — already announced this slot")
                continue

            match = is_office_hour_starting(entry, now)
            print(f"[OfficeHours] 🔍 {entry['name']:<20} scheduled {entry['day']} @ {entry['start_time']} — match: {match}")

            if match:
                try:
                    await channel.send(build_announcement(entry))
                    self.announced_today.add(slot_key)
                    print(f"[OfficeHours] ✅ Announced: {entry['name']} @ {now.strftime('%A %H:%M')} EDT")
                except Exception as e:
                    print(f"[OfficeHours] ⚠️  Failed to send announcement for {entry['name']}: {e}")

    @check_office_hours.before_loop
    async def before_check(self):
        await self.bot.wait_until_ready()
        print("[OfficeHours] ✅ Bot ready — office hours loop is starting")

    @check_office_hours.error
    async def on_loop_error(self, error):
        print(f"[OfficeHours] 💥 Loop crashed with an unhandled exception: {error}")
        import traceback
        traceback.print_exc()

    # ── /officehours — today's schedule ─────────────────────────────────────────
    @nextcord.slash_command(
        name="officehours",
        description="See who's on the hook for office hours today.",
        guild_ids=[SERVER_ID]
    )
    async def office_hours_today(self, interaction: nextcord.Interaction):
        now = datetime.now(TIMEZONE)
        today = now.strftime("%A")

        # ephemeral=True on defer() is what makes the whole interaction ephemeral in nextcord
        await interaction.response.defer(ephemeral=True)

        try:
            schedule = get_schedule()
        except Exception as e:
            await interaction.followup.send(
                f"{random.choice(SCHEDULE_FETCH_ERROR)}\n```{e}```"
            )
            return

        todays = sorted(
            [e for e in schedule if e["day"] == today],
            key=lambda x: x["start_time"]
        )

        if not todays:
            await interaction.followup.send(random.choice(NO_HOURS_TODAY))
            return

        lines = [f"📅 **Office Hours — {today}**\n*Here's who you get to bother today:*\n"]
        for e in todays:
            lines.append(f"• **{fmt_time(e['start_time'])} EDT** — <@{e['discord_id']}> ({e['name']})")

        await interaction.followup.send(
            "\n".join(lines),
            allowed_mentions=nextcord.AllowedMentions.none()
        )

    # ── /schedule — full weekly schedule ────────────────────────────────────────
    @nextcord.slash_command(
        name="schedule",
        description="The full weekly office hours schedule. Try to actually look at it this time.",
        guild_ids=[SERVER_ID]
    )
    async def full_schedule(self, interaction: nextcord.Interaction):

        # ephemeral=True on defer() is what makes the whole interaction ephemeral in nextcord
        await interaction.response.defer(ephemeral=True)

        try:
            schedule = get_schedule()
        except Exception as e:
            await interaction.followup.send(
                f"{random.choice(SCHEDULE_FETCH_ERROR)}\n```{e}```"
            )
            return

        day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        by_day: dict[str, list] = {d: [] for d in day_order}
        for e in schedule:
            if e["day"] in by_day:
                by_day[e["day"]].append(e)

        lines = [
            "📋 **Weekly Office Hours Schedule**",
            "*Yes, this exists. No, that's not an excuse for not knowing about it.*\n"
        ]
        for day in day_order:
            entries = sorted(by_day[day], key=lambda x: x["start_time"])
            if not entries:
                continue
            lines.append(f"**{day}**")
            for e in entries:
                lines.append(f"  • {fmt_time(e['start_time'])} EDT — <@{e['discord_id']}> ({e['name']})")
            lines.append("")

        await interaction.followup.send(
            "\n".join(lines),
            allowed_mentions=nextcord.AllowedMentions.none()
        )


# ─── Setup ─────────────────────────────────────────────────────────────────────

async def setup(bot: commands.Bot):
    bot.add_cog(OfficeHours(bot))