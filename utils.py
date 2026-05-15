import os
import gspread
from google.oauth2.service_account import Credentials


def user_has_any_role(interaction, role_names: list[str]) -> bool:
    if not interaction.guild:
        return False
    
    user_roles = {role.name for role in interaction.user.roles}
    return bool(user_roles & set(role_names))

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets.readonly",
    "https://www.googleapis.com/auth/drive.readonly",
]

def get_sheet_members() -> list[dict]:
    """Fetch and return all active members from the Google Sheet."""
    from config import DATA_SHEET_KEY, SERVICE_ACCOUNT_FILE

    creds = Credentials.from_service_account_file(SERVICE_ACCOUNT_FILE, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(DATA_SHEET_KEY).worksheet("Sheet1")

    all_rows = sheet.get_all_values()
    headers = [h.strip() for h in all_rows[1]]
    data_rows = all_rows[2:]

    members = []
    for row in data_rows:
        entry = dict(zip(headers, row))
        if entry.get("Active", "").upper() != "TRUE":
            continue
        if not entry.get("Name"):
            continue
        members.append({
            "name": entry.get("Name", "").strip(),
            "discord_id": entry.get("Discord ID", "").strip(),
            "taiga_name": entry.get("Username", "").strip(),
            "day": entry.get("Day of the Week", "").strip(),
            "start_time": entry.get("Start Time", "").strip(),
        })
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