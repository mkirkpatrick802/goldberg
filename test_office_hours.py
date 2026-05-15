"""
test_office_hours.py
────────────────────
Backend test script for the office hours cog.
Runs entirely from the terminal — no Discord interaction needed.

Usage:
    # Test announcement message for a specific dev
    python test_office_hours.py --dev Alice

    # Test announcement message for all devs
    python test_office_hours.py --all

    # Print the full parsed schedule from the sheet
    python test_office_hours.py --schedule

    # Simulate the check loop for a specific day/time
    python test_office_hours.py --simulate "Monday 14:00"
"""

import argparse
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# Import shared helpers from the cog
from cogs.office_hours import get_schedule, build_announcement, is_office_hour_starting, fmt_time

TIMEZONE = ZoneInfo("America/New_York")

# ─── Helpers ───────────────────────────────────────────────────────────────────

def find_dev(name: str, schedule: list[dict]) -> list[dict]:
    return [e for e in schedule if name.lower() in e["name"].lower()]


def print_separator():
    print("─" * 50)


# ─── Test Modes ────────────────────────────────────────────────────────────────

def test_single(name: str):
    """Fire a test announcement for a single dev by name."""
    print(f"\n🔍 Looking up '{name}' in the schedule...")
    try:
        schedule = get_schedule()
    except Exception as e:
        print(f"❌ Failed to fetch schedule: {e}")
        sys.exit(1)

    matches = find_dev(name, schedule)

    if not matches:
        print(f"❌ No dev found matching '{name}'.")
        print(f"   Available: {', '.join(e['name'] for e in schedule)}")
        sys.exit(1)

    if len(matches) > 1:
        print(f"⚠️  Multiple matches: {', '.join(e['name'] for e in matches)}")
        print("   Be more specific.")
        sys.exit(1)

    entry = matches[0]
    print_separator()
    print(f"✅ Match found: {entry['name']} — {entry['day']} @ {fmt_time(entry['start_time'])} EDT")
    print_separator()
    print("\n📣 Announcement preview:\n")
    print(build_announcement(entry))
    print_separator()


def test_all():
    """Print announcement previews for every active dev."""
    print("\n📋 Generating announcements for all active devs...\n")
    try:
        schedule = get_schedule()
    except Exception as e:
        print(f"❌ Failed to fetch schedule: {e}")
        sys.exit(1)

    for entry in schedule:
        print_separator()
        print(f"👤 {entry['name']} — {entry['day']} @ {fmt_time(entry['start_time'])} EDT")
        print_separator()
        print(build_announcement(entry))
        print()


def test_schedule():
    """Print the full parsed schedule as Goldberg sees it."""
    print("\n📊 Fetching schedule from sheet...\n")
    try:
        schedule = get_schedule()
    except Exception as e:
        print(f"❌ Failed to fetch schedule: {e}")
        sys.exit(1)

    day_order = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    by_day = {d: [] for d in day_order}
    for e in schedule:
        if e["day"] in by_day:
            by_day[e["day"]].append(e)

    print_separator()
    for day in day_order:
        entries = sorted(by_day[day], key=lambda x: x["start_time"])
        if not entries:
            continue
        print(f"  {day}")
        for e in entries:
            print(f"    • {fmt_time(e['start_time'])} EDT — {e['name']} (ID: {e['discord_id']})")
    print_separator()
    print(f"\n✅ {len(schedule)} active devs loaded.\n")


def test_simulate(when: str):
    """
    Simulate the background loop check at a given day/time.
    Format: "Monday 14:00"
    """
    print(f"\n⏱  Simulating loop check for: {when}\n")
    try:
        day, time = when.strip().split(" ")
        hour, minute = map(int, time.split(":"))
    except ValueError:
        print("❌ Invalid format. Use: \"Monday 14:00\"")
        sys.exit(1)

    # Build a fake datetime matching the requested day/time in EDT
    now = datetime.now(TIMEZONE)
    # Find the next occurrence of the requested weekday
    days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    if day not in days:
        print(f"❌ Invalid day '{day}'. Use a full day name e.g. Monday.")
        sys.exit(1)

    fake_now = now.replace(hour=hour, minute=minute, second=0, microsecond=0)

    try:
        schedule = get_schedule()
    except Exception as e:
        print(f"❌ Failed to fetch schedule: {e}")
        sys.exit(1)

    triggered = [e for e in schedule if is_office_hour_starting(e, fake_now) and e["day"] == day]

    print_separator()
    if not triggered:
        print(f"  No office hours scheduled for {day} at {fmt_time(time)}.")
    else:
        for entry in triggered:
            print(f"  🔔 Would announce: {entry['name']}")
            print()
            print(build_announcement(entry))
    print_separator()


# ─── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Backend test tool for Goldberg's office hours cog."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dev",      metavar="NAME",     help="Test announcement for a specific dev (partial name ok)")
    group.add_argument("--all",      action="store_true", help="Preview announcements for every active dev")
    group.add_argument("--schedule", action="store_true", help="Print the full parsed schedule from the sheet")
    group.add_argument("--simulate", metavar="\"Day HH:MM\"", help="Simulate the loop check at a given day/time")

    args = parser.parse_args()

    if args.dev:
        test_single(args.dev)
    elif args.all:
        test_all()
    elif args.schedule:
        test_schedule()
    elif args.simulate:
        test_simulate(args.simulate)


if __name__ == "__main__":
    main()
