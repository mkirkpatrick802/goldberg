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
REPO_LINK               = _require("REPO_LINK")
REPO_SIGNUP_LINK        = _require("REPO_SIGNUP_LINK")
REPO_ADMIN_PASSPHRASE   = _require("REPO_ADMIN_PASSPHRASE")
# Only enable if the SVN server uses a self-signed cert; it disables TLS verification.
SVN_TRUST_SERVER_CERT   = os.getenv("SVN_TRUST_SERVER_CERT", "").lower() == "true"

#Team Links
FIGMA_LINK              = _require("FIGMA_LINK")
DRIVE_LINK              = _require("DRIVE_LINK")
BUILDS_LINK             = _require("BUILDS_LINK")

#Office Hour Info
OFFICE_HOUR_CHANNEL     = int(_require("OFFICE_HOUR_CHANNEL"))

#Taiga Info
TAIGA_URL      = _require("TAIGA_URL")
TAIGA_USERNAME = _require("TAIGA_USERNAME")
TAIGA_PASSWORD = _require("TAIGA_PASSWORD")
TAIGA_PROJECT_SLUG = _require("TAIGA_PROJECT_SLUG")

# Data Info
DATA_SHEET_KEY          = _require("DATA_SHEET_KEY")
SERVICE_ACCOUNT_FILE    = _require("SERVICE_ACCOUNT_FILE")
WORKSHEET_NAME          = "Sheet1"
TIMEZONE                = ZoneInfo("America/New_York")

