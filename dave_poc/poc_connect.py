"""
DAVE proof-of-concept — Stage 1: connect + MLS group + ratchet availability.

THROWAWAY, not part of the bot. It answers the one question the source code
can't: when a nextcord 3.2 + dave-py bot joins a DAVE-required voice channel as
a passive listener, does the MLS group actually establish, and can we get a
decryption key ratchet for a speaker?

  - If yes  -> in-house voice-receive is real engineering (dave.Decryptor exists),
              and Stage 2 (actually decrypting audio to a WAV) is worth building.
  - If no   -> passive receive on DAVE channels is a dead end; use an external
              recorder instead.

This does NOT decrypt any audio. That's deliberate — there's no point building
the receive pipeline until we know the group forms and the keys are reachable.

────────────────────────────────────────────────────────────────────────────
Run notes (see README.md):
  * OWN venv — never the production bot's .venv (nextcord 3.2 breaks the cogs).
  * One gateway session per token: STOP the production bot first, or use a
    separate test-bot token.
  * A human must be sitting in the target voice channel (ideally talking) so
    there's a group to join and a speaker to key.

  python poc_connect.py <voice_channel_id>
    token: read from BOT_TOKEN env var, else ../.env
"""

import asyncio
import logging
import os
import sys
from pathlib import Path

import nextcord

# Surface nextcord's own voice logs — this is how a 4017 (or any close code)
# becomes visible instead of a silent failure.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logging.getLogger("nextcord.voice_client").setLevel(logging.DEBUG)


def log(msg: str) -> None:
    print(f"[DAVE-PoC] {msg}", flush=True)


def get_token() -> str:
    tok = os.getenv("BOT_TOKEN")
    if tok:
        return tok
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.is_file():
        # utf-8-sig: the team's editor saves .env with a BOM.
        for line in env.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("BOT_TOKEN") and "=" in line:
                return line.split("=", 1)[1].strip()
    raise SystemExit("No BOT_TOKEN found in the environment or ../.env")


CHANNEL_ID = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.getenv("POC_CHANNEL_ID", "0"))

intents = nextcord.Intents.all()
client = nextcord.Client(intents=intents)


def _report_ratchets(session, channel) -> None:
    members = [m for m in getattr(channel, "members", []) if not m.bot]
    if not members:
        log("No non-bot members in the channel to key. Get someone talking in it.")
        return
    any_key = False
    for m in members:
        try:
            ratchet = session.get_key_ratchet(str(m.id))
        except Exception as e:
            log(f"  get_key_ratchet({m.display_name} / {m.id}) raised: {type(e).__name__}: {e}")
            continue
        got = ratchet is not None
        any_key = any_key or got
        log(f"  get_key_ratchet({m.display_name} / {m.id}) -> {ratchet!r}"
            f"  {'<< KEY AVAILABLE' if got else '(no key yet — are they speaking?)'}")
    if any_key:
        log(">>> GO: decryption keys are reachable. Stage 2 (actual decrypt) is worth building.")
    else:
        log(">>> INCONCLUSIVE: group formed but no ratchets yet. Retry while someone is actively "
            "talking; if still none, that's a strong NO-GO signal.")


@client.event
async def on_ready():
    try:
        import nextcord.voice_client as vc_mod
        log(f"Logged in as {client.user} | nextcord {nextcord.__version__} | "
            f"has_dave={getattr(vc_mod, 'has_dave', '<missing>')}")

        if not getattr(vc_mod, "has_dave", False):
            log("has_dave is False -> dave-py isn't active in this venv. "
                "Install nextcord[voice]. NO-GO until fixed.")
            return

        channel = client.get_channel(CHANNEL_ID)
        if channel is None:
            log(f"Channel {CHANNEL_ID} not found. Is the bot in that guild? Is the ID a "
                f"VOICE channel? Developer Mode -> right-click the channel -> Copy Channel ID.")
            return
        if not isinstance(channel, nextcord.VoiceChannel):
            log(f"Channel {CHANNEL_ID} is a {type(channel).__name__}, not a voice channel.")
            return
        log(f"Target: #{channel.name} ({channel.id}) in {channel.guild.name}")

        try:
            log("Connecting with the native DAVE-aware VoiceClient (reconnect off, "
                "so a failure surfaces immediately)...")
            voice = await channel.connect(timeout=30, reconnect=False)
        except Exception as e:
            log(f"connect() FAILED: {type(e).__name__}: {e}")
            log(">>> RESULT: could not connect. If the log above shows 'closed with 4017', "
                "even nextcord 3.2 + dave-py isn't negotiating DAVE here -> NO-GO.")
            return

        log(f"CONNECTED. is_connected={voice.is_connected()} | "
            f"max_dave_protocol_version={voice.get_max_dave_protocol_version()}")

        e2ee = getattr(voice, "e2ee_state", None)
        log(f"e2ee_state present: {e2ee is not None}")
        session = getattr(e2ee, "_session", None) if e2ee else None
        if session is None:
            log(">>> RESULT: no MLS session on the connection -> DAVE isn't active on this "
                "channel/connection. Nothing to decrypt.")
            await voice.disconnect(force=True)
            return

        log("Waiting up to 30s for the MLS group to establish...")
        established = False
        for i in range(60):
            try:
                established = session.has_established_group()
            except Exception as e:
                log(f"has_established_group() raised: {type(e).__name__}: {e}")
                break
            if established:
                log(f"MLS GROUP ESTABLISHED after ~{i * 0.5:.1f}s. "
                    f"The bot is a real group member as a passive listener.")
                break
            await asyncio.sleep(0.5)

        if not established:
            log(">>> RESULT: the MLS group did NOT establish within 30s for a passive bot. "
                "That's the likely NO-GO — a listener can't get into the group.")
        else:
            log("Checking for decryption ratchets per speaker:")
            _report_ratchets(session, channel)

        log("Disconnecting.")
        try:
            await voice.disconnect(force=True)
        except Exception:
            pass
    finally:
        await client.close()


if __name__ == "__main__":
    if not CHANNEL_ID:
        raise SystemExit("Usage: python poc_connect.py <voice_channel_id>")
    client.run(get_token())
