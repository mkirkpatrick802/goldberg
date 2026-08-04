"""Shared mute state for Goldberg's spontaneous quips.

`/shutup` sets a wake-up time; the Bully cog checks `is_muted()` before
butting into a conversation. State is intentionally in-memory only — a bot
restart clears the mute, which is the behaviour we want (a forgotten `/shutup`
shouldn't outlive the process). Nothing here touches scheduled, functional
messages like office-hours announcements or sprint reminders; it only gates the
unprompted chatter.
"""

from datetime import datetime, timedelta, timezone

# Absolute UTC time Goldberg is allowed to talk again, or None when he's free.
_muted_until: datetime | None = None


def mute_for(minutes: float) -> datetime:
    """Silence spontaneous quips for `minutes`. Returns the wake-up time (UTC)."""
    global _muted_until
    _muted_until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    return _muted_until


def wake() -> None:
    """Clear any active mute so Goldberg can chime in again immediately."""
    global _muted_until
    _muted_until = None


def is_muted() -> bool:
    """True while a `/shutup` window is still active."""
    return _muted_until is not None and datetime.now(timezone.utc) < _muted_until


def muted_until() -> datetime | None:
    """The active wake-up time (UTC), or None if not muted."""
    return _muted_until if is_muted() else None
