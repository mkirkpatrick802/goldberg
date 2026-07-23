"""
DAVE proof-of-concept — Stage 2: actually decrypt a speaker to a WAV.

THROWAWAY. Stage 1 proved the MLS group forms and decryption ratchets are
reachable. This proves the rest of the pipeline end to end: receive the voice
UDP packets, strip the transport encryption (aead_xchacha20_poly1305_rtpsize),
strip the DAVE end-to-end layer with dave.Decryptor, Opus-decode, and write one
WAV per speaker you can actually listen to.

If a clear recording of the people talking comes out, in-house voice-receive is
proven and we wire this into the real cog. If the audio is garbage or empty, the
per-stage counters this prints say exactly which layer failed.

Runs the same way as poc_connect.py — its OWN venv, production bot stopped, and
this time the server needs libopus (`sudo apt install libopus0`) because we
decode audio here.

    python poc_decrypt.py <voice_channel_id> [seconds]
    # token from BOT_TOKEN env var, else ../.env ; seconds defaults to 20
"""

import asyncio
import logging
import os
import select
import struct
import sys
import time
import wave
from pathlib import Path

import nacl.secret
import nextcord
import nextcord.opus as opus
from nextcord.gateway import DiscordVoiceWebSocket

import dave

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("nextcord.voice_client").setLevel(logging.INFO)

OUT_DIR = Path(__file__).resolve().parent / "out"
AUDIO_PT = 0x78  # Discord voice RTP payload type (120)


def log(msg: str) -> None:
    print(f"[DAVE-PoC] {msg}", flush=True)


def get_token() -> str:
    tok = os.getenv("BOT_TOKEN")
    if tok:
        return tok
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.is_file():
        for line in env.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if line.startswith("BOT_TOKEN") and "=" in line:
                return line.split("=", 1)[1].strip()
    raise SystemExit("No BOT_TOKEN found in the environment or ../.env")


CHANNEL_ID = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.getenv("POC_CHANNEL_ID", "0"))
SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 20


class ProbeVoiceClient(nextcord.VoiceClient):
    """VoiceClient that records the SSRC -> user mapping from SPEAKING events."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ssrc_to_user: dict[int, int] = {}

    async def _ws_hook(self, ws, msg) -> None:
        if msg.get("op") == DiscordVoiceWebSocket.SPEAKING:
            d = msg.get("d", {})
            if d.get("ssrc") is not None and d.get("user_id"):
                self.ssrc_to_user[int(d["ssrc"])] = int(d["user_id"])

    async def connect_websocket(self) -> DiscordVoiceWebSocket:
        ws = await DiscordVoiceWebSocket.from_client(self, hook=self._ws_hook)
        # Assign explicitly so this works regardless of whether the base
        # connect() assigns the return value (it differs across 3.x point releases).
        self.ws = ws
        self._connected.clear()
        while ws.secret_key is None:
            await ws.poll_event()
        self._connected.set()
        return ws


def parse_rtp_header_len(data: bytes) -> int:
    """
    Length of the unencrypted RTP header for an rtpsize packet: the fixed 12
    bytes, plus CSRCs, plus the header extension if present. All of it is the
    AAD; the ciphertext begins after it.
    """
    b0 = data[0]
    csrc_count = b0 & 0x0F
    has_extension = b0 & 0x10
    hlen = 12 + csrc_count * 4
    if has_extension and len(data) >= hlen + 4:
        ext_words = struct.unpack_from(">H", data, hlen + 2)[0]
        hlen += 4 + ext_words * 4
    return hlen


class Stats:
    def __init__(self):
        self.raw = 0
        self.non_audio = 0
        self.too_short = 0
        self.aead_ok = 0
        self.aead_fail = 0
        self.unmapped = 0
        self.dave_ok = 0
        self.dave_none = 0
        self.opus_ok = 0
        self.opus_fail = 0

    def report(self):
        log("── per-stage counters ─────────────────────────")
        log(f"  raw packets received : {self.raw}")
        log(f"  non-audio (skipped)  : {self.non_audio}")
        log(f"  too short (skipped)  : {self.too_short}")
        log(f"  transport decrypt OK : {self.aead_ok}")
        log(f"  transport decrypt FAIL: {self.aead_fail}")
        log(f"  SSRC unmapped        : {self.unmapped}")
        log(f"  DAVE decrypt OK      : {self.dave_ok}")
        log(f"  DAVE decrypt None    : {self.dave_none}")
        log(f"  Opus decode OK       : {self.opus_ok}")
        log(f"  Opus decode FAIL     : {self.opus_fail}")


def receive_loop(voice, session, user_ids, seconds, stats):
    """
    Blocking capture loop (run in an executor). Returns {user_id: pcm bytes}.

    One Decryptor per user (ratchet set once). Transport-decrypt, route by SSRC
    to a user, DAVE-decrypt, Opus-decode, accumulate PCM.
    """
    secret_key = bytes(voice.secret_key)
    box = nacl.secret.Aead(secret_key)

    decryptors: dict[int, dave.Decryptor] = {}
    for uid in user_ids:
        ratchet = session.get_key_ratchet(str(uid))
        if ratchet is None:
            continue
        dec = dave.Decryptor()
        dec.transition_to_key_ratchet(ratchet)
        decryptors[uid] = dec

    decoders: dict[int, opus.Decoder] = {}
    pcm: dict[int, bytearray] = {}
    ssrc_cache: dict[int, int] = {}  # ssrc -> user, once we've confirmed one

    sock = voice.socket
    deadline = time.monotonic() + seconds

    while time.monotonic() < deadline:
        ready, _, _ = select.select([sock], [], [], 0.5)
        if not ready:
            continue
        try:
            data = sock.recv(4096)
        except OSError:
            continue

        stats.raw += 1
        if len(data) < 16:
            stats.too_short += 1
            continue
        if (data[1] & 0x7F) != AUDIO_PT:  # only RTP audio
            stats.non_audio += 1
            continue

        hlen = parse_rtp_header_len(data)
        ssrc = struct.unpack_from(">I", data, 8)[0]
        aad = data[:hlen]
        ciphertext = data[hlen:-4]
        nonce = bytes(data[-4:]) + b"\x00" * 20

        try:
            frame = box.decrypt(bytes(ciphertext), bytes(aad), nonce)
            stats.aead_ok += 1
        except Exception:
            stats.aead_fail += 1
            continue

        # Route SSRC -> user: SPEAKING map first, then confirm-by-decrypt fallback.
        uid = voice.ssrc_to_user.get(ssrc) or ssrc_cache.get(ssrc)
        candidates = [uid] if uid in decryptors else list(decryptors.keys())
        if not any(c in decryptors for c in candidates):
            stats.unmapped += 1
            continue

        opus_frame = None
        chosen = None
        for cand in candidates:
            dec = decryptors.get(cand)
            if dec is None:
                continue
            out = dec.decrypt(dave.MediaType.audio, frame)
            if out:
                opus_frame, chosen = out, cand
                ssrc_cache[ssrc] = cand
                break

        if opus_frame is None:
            stats.dave_none += 1
            continue
        stats.dave_ok += 1

        if chosen not in decoders:
            decoders[chosen] = opus.Decoder()
            pcm[chosen] = bytearray()
        try:
            pcm[chosen] += decoders[chosen].decode(opus_frame)
            stats.opus_ok += 1
        except Exception:
            stats.opus_fail += 1

    return {uid: bytes(buf) for uid, buf in pcm.items()}


def write_wav(path: Path, pcm: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(opus.Decoder.CHANNELS)      # 2
        w.setsampwidth(2)                           # 16-bit
        w.setframerate(opus.Decoder.SAMPLING_RATE)  # 48000
        w.writeframes(pcm)


intents = nextcord.Intents.all()
client = nextcord.Client(intents=intents)


@client.event
async def on_ready():
    try:
        if not opus.is_loaded():
            try:
                opus._load_default()
            except Exception:
                pass
        log(f"Logged in as {client.user} | nextcord {nextcord.__version__} | opus_loaded={opus.is_loaded()}")
        if not opus.is_loaded():
            log("libopus not loaded — install it (apt install libopus0). Can't decode without it.")
            return

        channel = client.get_channel(CHANNEL_ID)
        if not isinstance(channel, nextcord.VoiceChannel):
            log(f"Channel {CHANNEL_ID} is not a voice channel I can see.")
            return
        log(f"Target: #{channel.name} in {channel.guild.name}; capturing {SECONDS}s.")

        voice = await channel.connect(cls=ProbeVoiceClient, timeout=30, reconnect=False)
        log(f"Connected. mode={voice.mode} is_connected={voice.is_connected()}")

        e2ee = getattr(voice, "e2ee_state", None)
        session = getattr(e2ee, "_session", None) if e2ee else None
        if session is None:
            log("No MLS session — DAVE not active. Nothing to decrypt.")
            await voice.disconnect(force=True)
            return

        for _ in range(60):
            if session.has_established_group():
                break
            await asyncio.sleep(0.5)
        if not session.has_established_group():
            log("MLS group never established. Aborting.")
            await voice.disconnect(force=True)
            return
        log("MLS group established. Capturing — TALK NOW.")

        user_ids = [m.id for m in channel.members if not m.bot]
        OUT_DIR.mkdir(parents=True, exist_ok=True)

        stats = Stats()
        loop = asyncio.get_running_loop()
        pcm_by_user = await loop.run_in_executor(
            None, receive_loop, voice, session, user_ids, SECONDS, stats
        )

        stats.report()
        if not pcm_by_user:
            log(">>> No audio decoded. The counters above show which layer stopped it.")
        else:
            log("── WAV files written ──────────────────────────")
            for uid, pcm in pcm_by_user.items():
                member = channel.guild.get_member(uid)
                name = member.display_name if member else str(uid)
                safe = "".join(c for c in name if c.isalnum() or c in " _-").strip() or str(uid)
                path = OUT_DIR / f"{safe}.wav"
                write_wav(path, pcm)
                secs = len(pcm) / (opus.Decoder.SAMPLING_RATE * opus.Decoder.CHANNELS * 2)
                log(f"  {path}  ({secs:.1f}s of audio)")
            log(">>> Download the WAV(s) and listen. Clear speech = GO: build the real feature.")

        await voice.disconnect(force=True)
    finally:
        await client.close()


if __name__ == "__main__":
    if not CHANNEL_ID:
        raise SystemExit("Usage: python poc_decrypt.py <voice_channel_id> [seconds]")
    client.run(get_token())
