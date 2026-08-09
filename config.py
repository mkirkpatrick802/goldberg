import os
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing required environment variable: {key}")
    return val

# Discord Info
BOT_TOKEN               = _require("BOT_TOKEN")
SERVER_ID               = int(_require("SERVER_ID"))

#Repo Info
# REPO_LINK is no longer the single repo the bot watches — commit_notifier and
# telemetry now poll every project in the wiki registry that has a repo link
# set (see utils.get_watched_projects). Kept as an optional fallback so an
# existing single-repo deployment doesn't go quiet the moment this rolls out,
# until someone creates the first project on the webapp's Wiki page.
REPO_LINK               = os.getenv("REPO_LINK", "")
REPO_SIGNUP_LINK        = _require("REPO_SIGNUP_LINK")

#Office Hour Info
OFFICE_HOUR_CHANNEL     = int(_require("OFFICE_HOUR_CHANNEL"))

#Voice Notes Info
# The forum channel that gets one post per meeting. 0 disables /takenotes.
# Not _require'd so an existing deployment without these keys still boots.
NOTES_FORUM_CHANNEL_ID  = int(os.getenv("NOTES_FORUM_CHANNEL_ID", "0"))
# Safety net: a forgotten /stopnotes shouldn't record for eight hours.
NOTES_MAX_MINUTES       = int(os.getenv("NOTES_MAX_MINUTES", "120"))

#Taiga Info
TAIGA_URL      = _require("TAIGA_URL")
TAIGA_USERNAME = _require("TAIGA_USERNAME")
TAIGA_PASSWORD = _require("TAIGA_PASSWORD")
TAIGA_PROJECT_SLUG = _require("TAIGA_PROJECT_SLUG")

# Data Info
# Member data can come from the shared SQLite database or the legacy Google
# Sheet. "auto" prefers the database and falls back to the sheet when the
# database is unavailable or empty; force one with "database" or "sheet".
MEMBER_SOURCE           = os.getenv("MEMBER_SOURCE", "auto").strip().lower()

# Sheet credentials are optional now — a deployment running purely on the
# database has no reason to carry Google credentials, and _require would have
# stopped the bot from booting without them.
DATA_SHEET_KEY          = os.getenv("DATA_SHEET_KEY", "")
SERVICE_ACCOUNT_FILE    = os.getenv("SERVICE_ACCOUNT_FILE", "")
WORKSHEET_NAME          = os.getenv("WORKSHEET_NAME", "Sheet1")
TIMEZONE                = ZoneInfo("America/New_York")

