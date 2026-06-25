import os
import sys


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


# ── Shared database access ────────────────────────────────────────────────────
# The bot is a submodule under the-maple-server/. The shared SQLite database (the
# single source of truth that replaced the Google Sheet) lives one level up at
# maple-server/shared/database.py. We add that folder to the path lazily so the
# bot doesn't need to know about it anywhere else, and importing the bot in
# isolation doesn't hard-fail.
_SHARED_DIR = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "shared")
)


def get_sheet_members() -> list[dict]:
    """
    Return all active members from the shared database.

    Kept the name 'get_sheet_members' (and the exact return shape) so every cog
    that already calls it — office hours, Taiga matching, telemetry — keeps
    working without any change. The data just comes from SQLite now, not Sheets.

    Returned dicts: name, discord_id, taiga_name, day, start_time, active.
    """
    if _SHARED_DIR not in sys.path:
        sys.path.insert(0, _SHARED_DIR)

    import database  # resolved from the shared folder
    return database.get_sheet_members()


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
