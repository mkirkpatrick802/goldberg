import os
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

def _require(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing required enviroment variable: {key}")
    return val

# Discord Info
BOT_TOKEN = _require("BOT_TOKEN")
SERVER_ID = int(_require("SERVER_ID"))

#Repo Info
REPO_LINK = _require("REPO_LINK")
REPO_SIGNUP_LINK=_require("REPO_SIGNUP_LINK")

#Office Hour Info
OFFICE_HOUR_CHANNEL= int(_require("OFFICE_HOUR_CHANNEL"))

# Data Info
DATA_SHEET_KEY= _require("DATA_SHEET_KEY")
SERVICE_ACCOUNT_FILE= _require("SERVICE_ACCOUNT_FILE")
WORKSHEET_NAME      = "Sheet1"
TIMEZONE            = ZoneInfo("America/New_York")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

