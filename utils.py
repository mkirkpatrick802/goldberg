import os
import sys
from datetime import date


def pick_current_milestone(milestones: list) -> dict | None:
    """Choose the 'current' sprint from Taiga's open (closed=false) milestones.

    Prefer the milestone whose date range contains today
    (estimated_start <= today <= estimated_finish). Falls back to the first
    milestone when none match — e.g. a gap between sprints, or a milestone
    missing its dates. Returns None only for an empty list.

    This exists because old sprints are often left open in Taiga, so
    `milestones[0]` is not reliably the active one: a pre-created future sprint
    would sort ahead of the real one and empty everyone's task lists.
    """
    if not milestones:
        return None
    today = date.today().isoformat()  # 'YYYY-MM-DD' sorts chronologically
    for m in milestones:
        start = m.get("estimated_start")
        finish = m.get("estimated_finish")
        if start and finish and start <= today <= finish:
            return m
    return milestones[0]


def is_dev(interaction) -> bool:
    dev_role_id = 1374926068364083280
    if not interaction.guild:
        return False
    return any(role.id == dev_role_id for role in interaction.user.roles)


def user_has_any_role(interaction, role_names: list[str]) -> bool:
    if not interaction.guild:
        return False

    user_roles = {role.name for role in interaction.user.roles}
    return bool(user_roles & set(role_names))


# ── Member data access ────────────────────────────────────────────────────────
# Member data has two possible sources: the shared SQLite database (the
# direction we're heading) and the legacy Google Sheet. Both are supported
# because they aren't deployed in lockstep — the database lives in the parent
# repo at maple-server/shared/, so a Goldberg checkout can easily be running
# somewhere that doesn't have it yet.
#
# Every consumer still calls get_sheet_members() and gets the same dict shape
# regardless of source, so office hours, Taiga matching and telemetry are
# unaffected by which one answered.
_SHARED_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared")
)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]


def _members_from_database() -> list[dict]:
    """Active members from maple-server/shared/database.py."""
    if _SHARED_DIR not in sys.path:
        sys.path.insert(0, _SHARED_DIR)

    import database  # resolved from the shared folder
    return database.get_sheet_members()


def _members_from_sheet() -> list[dict]:
    """
    Active members from the legacy Google Sheet.

    Note `username` and `taiga_name` both come from the sheet's "Username"
    column. The database keeps them as separate fields, and telemetry reads
    `username` directly — so omitting it here (as the original sheet code did)
    raises a KeyError in the commit-attribution path.
    """
    import gspread
    from google.oauth2.service_account import Credentials

    from config import DATA_SHEET_KEY, SERVICE_ACCOUNT_FILE, WORKSHEET_NAME

    if not DATA_SHEET_KEY or not SERVICE_ACCOUNT_FILE:
        raise RuntimeError(
            "Sheet source requested but DATA_SHEET_KEY / SERVICE_ACCOUNT_FILE "
            "are not set."
        )

    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(DATA_SHEET_KEY).worksheet(WORKSHEET_NAME)

    all_rows = sheet.get_all_values()
    headers = [h.strip() for h in all_rows[1]]
    data_rows = all_rows[2:]

    members = []
    for row in data_rows:
        entry = dict(zip(headers, row))
        if not entry.get("Name"):
            continue
        username = entry.get("Username", "").strip()
        members.append({
            "name":       entry.get("Name", "").strip(),
            "username":   username,
            "discord_id": entry.get("Discord ID", "").strip(),
            "taiga_name": username,
            "day":        entry.get("Day of the Week", "").strip(),
            "start_time": entry.get("Start Time", "").strip(),
            "active":     entry.get("Active", "").strip(),
        })
    return members


def get_sheet_members() -> list[dict]:
    """
    Return all active members from whichever source is configured.

    Controlled by MEMBER_SOURCE: "database", "sheet", or "auto" (the default),
    which prefers the database and falls back to the sheet if the database is
    missing, broken, or returns nothing. An empty result counts as a failure —
    a database that exists but hasn't been populated on this host would
    otherwise silently empty the office hours schedule.

    Returned dicts: name, username, discord_id, taiga_name, day, start_time,
    active.
    """
    from config import MEMBER_SOURCE

    if MEMBER_SOURCE == "sheet":
        return _members_from_sheet()

    if MEMBER_SOURCE == "database":
        return _members_from_database()

    # auto
    try:
        members = _members_from_database()
        if members:
            return members
        reason = "it returned no active members"
    except Exception as e:
        reason = f"{type(e).__name__}: {e}"

    print(f"[Members] Database unavailable ({reason}) - falling back to the Google Sheet.")
    members = _members_from_sheet()
    print(f"[Members] Loaded {len(members)} members from the sheet.")
    return members


def chunk_message(message: str, limit: int = 1900) -> list[str]:
    """Split a message into chunks that fit within Discord's character limit."""
    chunks = []
    current = ""
    for line in message.split("\n"):
        if len(current) + len(line) + 1 > limit:
            chunks.append(current)
            current = line + "\n"
        else:
            current += line + "\n"
    if current:
        chunks.append(current)
    return chunks
