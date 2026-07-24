"""
test_telemetry_office_hours.py
──────────────────────────────
Regression tests for office-hours telemetry counting.

These lock down the edge cases that used to double-count:
  • a host who leaves and rejoins during the same office hour
  • a host who mutes/unmutes (a voice-state event that isn't a real join)
  • an attendee who comes, goes, and comes back
  • distinct attendees each counting once

Runs with no Discord connection — the sheet, guild, and telemetry file are
all faked in-memory.

Usage:
    python test_telemetry_office_hours.py
"""

import asyncio
import sys
import types
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# Windows consoles default to cp1252, which can't render the status glyphs.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ─── Stub bot deps when they aren't installed ────────────────────────────────
# In the real bot environment nextcord/config/utils import normally and these
# blocks are skipped. This lets the tests run anywhere (CI, a bare checkout).

def _stub(name):
    m = types.ModuleType(name)
    sys.modules[name] = m
    return m


try:  # nextcord + its ext.commands / ext.tasks
    import nextcord  # noqa: F401
except ModuleNotFoundError:
    nextcord = _stub("nextcord")
    ext      = _stub("nextcord.ext")
    commands = _stub("nextcord.ext.commands")
    tasks    = _stub("nextcord.ext.tasks")
    nextcord.ext = ext
    ext.commands = commands
    ext.tasks    = tasks

    class _Cog:
        @staticmethod
        def listener(*a, **k):
            return lambda fn: fn
    commands.Cog = _Cog

    class _Loop:
        def __init__(self, fn):      self.fn = fn
        def before_loop(self, fn):   return fn
        def error(self, fn):         return fn
        def start(self, *a, **k):    pass
        def cancel(self, *a, **k):   pass
        def is_running(self):        return False
    tasks.loop = lambda *a, **k: (lambda fn: _Loop(fn))

try:  # config constants used at import time
    import config  # noqa: F401
except Exception:
    config = _stub("config")
    config.SERVER_ID = 1
    config.TAIGA_URL = config.TAIGA_PROJECT_SLUG = config.REPO_LINK = ""

try:  # utils.get_sheet_members (patched per-test anyway)
    import utils  # noqa: F401
except Exception:
    utils = _stub("utils")
    utils.get_sheet_members = lambda: []


import cogs.telemetry as tel

EASTERN = ZoneInfo("America/New_York")

# ─── Fakes ───────────────────────────────────────────────────────────────────

CATEGORY_ID = 999


class FakeChannel:
    def __init__(self, cid, category_id=CATEGORY_ID):
        self.id = cid
        self.category_id = category_id


class FakeVoice:
    def __init__(self, channel):
        self.channel = channel


class FakeMember:
    def __init__(self, mid, name, voice_channel=None, bot=False):
        self.id = mid
        self.name = name
        self.bot = bot
        self.voice = FakeVoice(voice_channel) if voice_channel else None


class FakeGuild:
    def __init__(self, members):
        self._members = {int(m.id): m for m in members}

    def get_member(self, mid):
        return self._members.get(int(mid))


class FakeBot:
    def __init__(self, guild):
        self._guild = guild

    def get_guild(self, _gid):
        return self._guild


# ─── In-memory harness ───────────────────────────────────────────────────────

class Harness:
    """Patches telemetry's I/O to in-memory state for the duration of a test."""

    def __init__(self, roster, guild):
        self.store = {"current_sprint": "Sprint-1", "sprints": {"Sprint-1": {}}}
        self.roster = roster
        self._saved = {}
        self._patch(guild)

    def _patch(self, guild):
        self._saved = {
            "load_telemetry":   tel.load_telemetry,
            "save_telemetry":   tel.save_telemetry,
            "get_sheet_members": tel.get_sheet_members,
            "load_setup_config": tel.load_setup_config,
        }
        tel.load_telemetry    = lambda: self.store
        tel.save_telemetry    = lambda data: self.store.update(data)
        tel.get_sheet_members = lambda: self.roster
        tel.load_setup_config = lambda: {"dev_zone_category_id": CATEGORY_ID}

    def restore(self):
        for name, fn in self._saved.items():
            setattr(tel, name, fn)

    def attended(self, user_id):
        return self.store["sprints"]["Sprint-1"].get(str(user_id), {}).get("office_hours_attended", 0)

    def attendees(self, user_id):
        return self.store["sprints"]["Sprint-1"].get(str(user_id), {}).get("office_hours_attendees", 0)


# ─── Assertions ──────────────────────────────────────────────────────────────

_failures = []


def check(label, actual, expected):
    ok = actual == expected
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}: got {actual}, expected {expected}")
    if not ok:
        _failures.append(label)


# ─── Tests ───────────────────────────────────────────────────────────────────

def make_roster(now):
    """Alice is a host whose slot is happening right now; Bob & Carol are not hosts."""
    hhmm = now.strftime("%H:%M")
    today = now.strftime("%A")
    return [
        {"name": "Alice", "discord_id": "100", "day": today, "start_time": hhmm},
        {"name": "Bob",   "discord_id": "200", "day": today, "start_time": ""},
        {"name": "Carol", "discord_id": "300", "day": today, "start_time": ""},
    ]


def test_host_rejoin():
    print("\n▶ Host leaving and rejoining counts once")
    now = datetime.now(EASTERN)
    roster = make_roster(now)
    alice = FakeMember("100", "Alice")
    guild = FakeGuild([alice])
    h = Harness(roster, guild)
    try:
        cog = tel.Telemetry(FakeBot(guild))
        cog._track_office_hours(alice, FakeChannel(1), now)
        check("after first join", h.attended("100"), 1)
        cog._track_office_hours(alice, FakeChannel(1), now)  # rejoin
        cog._track_office_hours(alice, FakeChannel(1), now)  # rejoin again
        check("after two rejoins", h.attended("100"), 1)
    finally:
        h.restore()


def test_host_next_day_new_session():
    print("\n▶ Host's next office hour (different date) counts again")
    now = datetime.now(EASTERN)
    roster = make_roster(now)
    alice = FakeMember("100", "Alice")
    guild = FakeGuild([alice])
    h = Harness(roster, guild)
    try:
        cog = tel.Telemetry(FakeBot(guild))
        cog._track_office_hours(alice, FakeChannel(1), now)
        check("first session", h.attended("100"), 1)
        # Same weekday/time a week later — a distinct occurrence.
        next_week = now + timedelta(days=7)
        roster2 = make_roster(next_week)
        h.roster = roster2
        tel.get_sheet_members = lambda: roster2
        cog._track_office_hours(alice, FakeChannel(1), next_week)
        check("second session", h.attended("100"), 2)
    finally:
        h.restore()


def test_attendee_rejoin_and_distinct():
    print("\n▶ Attendee rejoin counts once; distinct attendees each count")
    now = datetime.now(EASTERN)
    roster = make_roster(now)
    channel = FakeChannel(1)
    alice = FakeMember("100", "Alice", voice_channel=channel)  # host sitting in VC
    bob   = FakeMember("200", "Bob")
    carol = FakeMember("300", "Carol")
    guild = FakeGuild([alice, bob, carol])
    h = Harness(roster, guild)
    try:
        cog = tel.Telemetry(FakeBot(guild))
        cog._track_office_hours(bob, channel, now)
        check("bob joins", h.attendees("100"), 1)
        cog._track_office_hours(bob, channel, now)  # bob rejoins
        check("bob rejoins", h.attendees("100"), 1)
        cog._track_office_hours(carol, channel, now)
        check("carol joins", h.attendees("100"), 2)
    finally:
        h.restore()


def test_attendee_host_absent():
    print("\n▶ No credit when the host isn't in the channel")
    now = datetime.now(EASTERN)
    roster = make_roster(now)
    channel = FakeChannel(1)
    alice = FakeMember("100", "Alice", voice_channel=None)  # host not in VC
    bob   = FakeMember("200", "Bob")
    guild = FakeGuild([alice, bob])
    h = Harness(roster, guild)
    try:
        cog = tel.Telemetry(FakeBot(guild))
        cog._track_office_hours(bob, channel, now)
        check("host absent", h.attendees("100"), 0)
    finally:
        h.restore()


def test_outside_window():
    print("\n▶ Joining outside the office-hour window counts nothing")
    now = datetime.now(EASTERN)
    # Alice's slot is 3 hours ago — well outside the ±30 min window.
    past = now - timedelta(hours=3)
    roster = [{"name": "Alice", "discord_id": "100",
               "day": now.strftime("%A"), "start_time": past.strftime("%H:%M")}]
    alice = FakeMember("100", "Alice")
    guild = FakeGuild([alice])
    h = Harness(roster, guild)
    try:
        cog = tel.Telemetry(FakeBot(guild))
        cog._track_office_hours(alice, FakeChannel(1), now)
        check("outside window", h.attended("100"), 0)
    finally:
        h.restore()


def test_mute_event_ignored():
    print("\n▶ A mute (same-channel voice event) is not treated as a join")
    now = datetime.now(EASTERN)
    roster = make_roster(now)
    channel = FakeChannel(1)
    alice = FakeMember("100", "Alice", voice_channel=channel)
    guild = FakeGuild([alice])
    h = Harness(roster, guild)
    try:
        cog = tel.Telemetry(FakeBot(guild))
        # Real join first.
        before_join = FakeVoice(None)
        after_join  = FakeVoice(channel)
        asyncio.run(cog.on_voice_state_update(alice, before_join, after_join))
        check("after real join", h.attended("100"), 1)
        # Mute: before.channel == after.channel — must be ignored.
        muted_before = FakeVoice(channel)
        muted_after  = FakeVoice(channel)
        asyncio.run(cog.on_voice_state_update(alice, muted_before, muted_after))
        check("after mute toggle", h.attended("100"), 1)
    finally:
        h.restore()


def test_sprint_change_prunes_sessions():
    print("\n▶ A new sprint wipes the office-hours dedup scratchpad")
    now = datetime.now(EASTERN)
    roster = make_roster(now)
    guild = FakeGuild([])
    h = Harness(roster, guild)
    try:
        # Leftover dedup records from the sprint that's ending.
        h.store["office_hours_sessions"] = {"100-2026-07-24": {"host_counted": True, "attendees": ["200"]}}
        cog = tel.Telemetry(FakeBot(guild))

        async def fake_current_sprint():
            return "Sprint-2", None
        cog.get_current_sprint = fake_current_sprint

        # The @loop decorator wraps the coroutine; grab the raw function to run
        # it once. Stub stores it on .fn, real nextcord on .coro.
        raw = getattr(cog.check_sprint, "fn", None) or getattr(cog.check_sprint, "coro")
        asyncio.run(raw(cog))
        check("sprint advanced", h.store["current_sprint"], "Sprint-2")
        check("scratchpad cleared", h.store["office_hours_sessions"], {})
    finally:
        h.restore()


# ─── Runner ──────────────────────────────────────────────────────────────────

def main():
    print("Office-hours telemetry regression tests")
    print("=" * 50)
    test_host_rejoin()
    test_host_next_day_new_session()
    test_attendee_rejoin_and_distinct()
    test_attendee_host_absent()
    test_outside_window()
    test_mute_event_ignored()
    test_sprint_change_prunes_sessions()
    print("\n" + "=" * 50)
    if _failures:
        print(f"❌ {len(_failures)} check(s) failed: {', '.join(_failures)}")
        raise SystemExit(1)
    print("✅ All checks passed.")


if __name__ == "__main__":
    main()
