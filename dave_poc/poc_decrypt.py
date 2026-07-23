"""
DAVE PoC — multi-speaker capture to time-aligned WAVs.

The single-speaker run proved the crypto pipeline end to end (948 packets,
100% at every stage, clean audio). This adds the two things a real meeting
needs:

  * MULTIPLE SPEAKERS — each SSRC routed to its own user's key ratchet and its
    own output file.
  * A SHARED TIMELINE — each frame is placed at its real arrival time, gaps
    padded with silence, and every file padded to the same length. Without
    this, a speaker's audio is silently compressed (their quiet stretches
    vanish), the files drift out of sync, and notes/pipeline.py — which
    interleaves segments by timestamp to rebuild the conversation — would order
    everyone's words wrongly.

The proven recipe, each step confirmed by a probe rather than guessed:

  1. Transport AEAD unwrap (aead_xchacha20_poly1305_rtpsize):
       AAD   = 12-byte RTP header + CSRCs + the 4-byte extension PREAMBLE only
       ct    = the rest, minus a trailing 4-byte nonce
       nonce = those 4 bytes at the FRONT of a 24-byte zero-padded nonce
  2. Strip RTP padding when the padding bit is set.
  3. Skip the RTP extension body to reach the DAVE frame; real DAVE frames end
     with the magic 0xFAFA (Discord's ~11-byte Opus silence frames do not).
  4. DAVE unwrap with that speaker's ratchet, routed by the SPEAKING SSRC map.

Hard-won rules:
  * A key ratchet is MOVED into a Decryptor (nanobind relinquishes it) — it is
    single-use, so fetch a fresh one per Decryptor.
  * libdave/mlspp is NOT thread-safe: the capture thread only does recv(); all
    crypto runs single-threaded afterwards.
  * nextcord's Opus decoder overflows the heap on mono packets — see
    SafeOpusDecoder.

    python poc_decrypt.py <voice_channel_id> [seconds]
"""

import array
import asyncio
import ctypes
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
logging.getLogger("dave").setLevel(logging.CRITICAL)

OUT_DIR = Path(__file__).resolve().parent / "out"
AUDIO_PT = 0x78
MAX_PACKETS = 20000
MAX_DECRYPTS = 4000
DAVE_MAGIC = bytes([0xFA, 0xFA])
DECRYPTOR_BATCH = 10
BYTES_PER_SECOND = 48000 * 2 * 2   # rate * channels * int16
FRAME_ALIGN = 4                     # one stereo sample
SILENCE = b"\x00"

CHANNEL_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 0
SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 30


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
    raise SystemExit("No BOT_TOKEN found")


class ProbeVoiceClient(nextcord.VoiceClient):
    """Records the SSRC -> user mapping from SPEAKING events."""

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
        self.ws = ws
        self._connected.clear()
        while ws.secret_key is None:
            await ws.poll_event()
        self._connected.set()
        return ws


class SafeOpusDecoder(opus.Decoder):
    """
    Works around a heap-overflow bug in nextcord's Decoder.decode.

    It sizes the PCM buffer by the *packet's* channel count, but the decoder is
    created STEREO and libopus always writes interleaved stereo. A MONO packet
    therefore gets half the space it needs -> `malloc(): invalid size`. Sizing
    by the decoder's own channel count is correct, and is what nextcord already
    does in its packet-loss branch. Unnoticed upstream because nextcord only
    ever SENDS audio; decode() is dead code there.
    """

    def decode(self, data, *, fec: bool = False) -> bytes:
        if data is None:
            frame_size = self._get_last_packet_duration() or self.SAMPLES_PER_FRAME
        else:
            frame_size = self.packet_get_nb_frames(data) * self.packet_get_samples_per_frame(data)

        channel_count = self.CHANNELS  # <- the fix
        pcm = (ctypes.c_int16 * (frame_size * channel_count))()
        pcm_ptr = ctypes.cast(pcm, opus.c_int16_ptr)
        ret = opus._lib.opus_decode(
            self._state, data, len(data) if data else 0, pcm_ptr, frame_size, fec
        )
        return array.array("h", pcm[: ret * channel_count]).tobytes()


def parse_rtp(data: bytes) -> tuple[int, int, int, bool]:
    """(ssrc, aad_len, ext_body_len, has_padding)"""
    b0 = data[0]
    base = 12 + (b0 & 0x0F) * 4
    aad_len, ext_body = base, 0
    if (b0 & 0x10) and len(data) >= base + 4:
        ext_words = struct.unpack_from(">H", data, base + 2)[0]
        aad_len = base + 4
        ext_body = ext_words * 4
    ssrc = struct.unpack_from(">I", data, 8)[0]
    return ssrc, aad_len, ext_body, bool(b0 & 0x20)


class Stats:
    def __init__(self):
        self.raw = self.aead_ok = self.aead_fail = 0
        self.padded = self.not_dave = self.unmapped = self.no_ratchet = 0
        self.dave_ok = self.dave_none = 0
        self.opus_ok = self.opus_fail = 0

    def report(self):
        log("-- per-stage counters ------------------------")
        log(f"  raw audio packets     : {self.raw}")
        log(f"  transport decrypt OK  : {self.aead_ok}")
        log(f"  transport decrypt FAIL: {self.aead_fail}")
        log(f"  RTP padding stripped  : {self.padded}")
        log(f"  not DAVE-framed (skip): {self.not_dave}")
        log(f"  SSRC unmapped         : {self.unmapped}")
        log(f"  no ratchet for user   : {self.no_ratchet}")
        log(f"  DAVE decrypt OK       : {self.dave_ok}")
        log(f"  DAVE decrypt None     : {self.dave_none}")
        log(f"  Opus decode OK        : {self.opus_ok}")
        log(f"  Opus decode FAIL      : {self.opus_fail}")


def capture_raw(sock, seconds):
    """Thread body: collect (arrival_seconds, bytes) only. No crypto here."""
    out = []
    start = time.monotonic()
    deadline = start + seconds
    while time.monotonic() < deadline and len(out) < MAX_PACKETS:
        ready, _, _ = select.select([sock], [], [], 0.5)
        if not ready:
            continue
        try:
            data = sock.recv(4096)
        except OSError:
            continue
        if len(data) >= 16 and (data[1] & 0x7F) == AUDIO_PT:
            out.append((time.monotonic() - start, data))
    return out


def process(packets, voice, session, stats):
    """All crypto, single-threaded. Returns ({user: pcm}, {user: frame_count})."""
    box = nacl.secret.Aead(bytes(voice.secret_key))
    decryptors: dict[int, dave.Decryptor] = {}
    seen: dict[int, int] = {}
    decoders: dict[int, SafeOpusDecoder] = {}
    pcm: dict[int, bytearray] = {}
    frames: dict[int, int] = {}
    total = 0

    for arrival, data in packets:
        if total >= MAX_DECRYPTS:
            break
        stats.raw += 1
        ssrc, aad_len, ext_body, has_padding = parse_rtp(data)

        try:
            plain = box.decrypt(
                bytes(data[aad_len:-4]),
                bytes(data[:aad_len]),
                bytes(data[-4:]) + SILENCE * 20,
            )
            stats.aead_ok += 1
        except Exception:
            stats.aead_fail += 1
            continue

        if has_padding and plain:
            pad = plain[-1]
            if 0 < pad <= len(plain):
                plain = plain[:-pad]
                stats.padded += 1

        frame = plain[ext_body:] if ext_body else plain
        if len(frame) < 8:
            continue
        if not frame.endswith(DAVE_MAGIC):
            stats.not_dave += 1
            continue

        uid = voice.ssrc_to_user.get(ssrc)
        if uid is None:
            stats.unmapped += 1
            continue

        n = seen.get(uid, 0)
        if uid not in decryptors or n % DECRYPTOR_BATCH == 0:
            ratchet = session.get_key_ratchet(str(uid))
            if ratchet is None:
                stats.no_ratchet += 1
                continue
            dec = dave.Decryptor()
            dec.transition_to_key_ratchet(ratchet)
            decryptors[uid] = dec
        seen[uid] = n + 1
        total += 1

        out = decryptors[uid].decrypt(dave.MediaType.audio, frame)
        if not out:
            stats.dave_none += 1
            continue
        stats.dave_ok += 1

        if uid not in decoders:
            decoders[uid] = SafeOpusDecoder()
            pcm[uid] = bytearray()
            frames[uid] = 0
        try:
            chunk = decoders[uid].decode(out)
        except Exception:
            stats.opus_fail += 1
            continue

        # Place this frame at its true arrival time, padding the gap with
        # silence so every speaker shares one clock.
        offset = int(arrival * BYTES_PER_SECOND)
        offset -= offset % FRAME_ALIGN
        buf = pcm[uid]
        if len(buf) < offset:
            buf += SILENCE * (offset - len(buf))
        buf += chunk
        stats.opus_ok += 1
        frames[uid] += 1

    return {uid: bytes(b) for uid, b in pcm.items()}, frames


def write_wav(path: Path, data: bytes) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(opus.Decoder.CHANNELS)
        w.setsampwidth(2)
        w.setframerate(opus.Decoder.SAMPLING_RATE)
        w.writeframes(data)


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
        log(f"Logged in as {client.user} | opus_loaded={opus.is_loaded()}")
        if not opus.is_loaded():
            log("libopus missing - apt install libopus0")
            return

        channel = client.get_channel(CHANNEL_ID)
        if not isinstance(channel, nextcord.VoiceChannel):
            log(f"Channel {CHANNEL_ID} is not a voice channel I can see.")
            log("Auto-created ('join to create') channels are deleted when empty,")
            log("so an ID from an earlier session may no longer exist. Voice")
            log("channels I can currently see, newest last:")
            found = False
            for guild in client.guilds:
                for vc in guild.voice_channels:
                    found = True
                    occupants = [m.display_name for m in vc.members if not m.bot]
                    who = f"  <- {occupants}" if occupants else ""
                    log(f"    {vc.id}  #{vc.name}  ({guild.name}){who}")
            if not found:
                log("    (none — is the bot in the server?)")
            log("Pick the one you're sitting in and pass that ID.")
            return

        voice = await channel.connect(cls=ProbeVoiceClient, timeout=30, reconnect=False)
        e2ee = getattr(voice, "e2ee_state", None)
        session = getattr(e2ee, "_session", None) if e2ee else None
        if session is None:
            log("No MLS session - DAVE inactive.")
            await voice.disconnect(force=True)
            return
        for _ in range(60):
            if session.has_established_group():
                break
            await asyncio.sleep(0.5)

        people = [m.display_name for m in channel.members if not m.bot]
        log(f"MLS group established. In channel: {people}")
        log(f"Capturing {SECONDS}s - EVERYONE TALK, take turns and overlap a bit.")

        loop = asyncio.get_running_loop()
        packets = await loop.run_in_executor(None, capture_raw, voice.socket, SECONDS)
        log(f"Captured {len(packets)} audio packets. Decrypting...")
        log(f"SSRC->user map: {voice.ssrc_to_user}")

        stats = Stats()
        pcm_by_user, frames = process(packets, voice, session, stats)
        stats.report()

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        if not pcm_by_user:
            log(">>> No audio decoded - see counters above.")
        else:
            # Pad every file to the same length so they line up sample-for-sample.
            longest = max(len(b) for b in pcm_by_user.values())
            log(f"-- per-speaker output ({len(pcm_by_user)} speaker(s)) --------")
            for uid, data in pcm_by_user.items():
                member = channel.guild.get_member(uid)
                name = member.display_name if member else str(uid)
                safe = "".join(c for c in name if c.isalnum() or c in " _-").strip() or str(uid)
                padded = data + SILENCE * (longest - len(data))
                write_wav(OUT_DIR / f"{safe}.wav", padded)
                speech = len(data) / BYTES_PER_SECOND
                log(f"  {safe}.wav  frames={frames.get(uid, 0)}  "
                    f"speech={speech:.1f}s  total={longest / BYTES_PER_SECOND:.1f}s")
            if len(pcm_by_user) > 1:
                log(">>> All files are the same length and share one clock.")
                log(">>> Play two together - speakers should NOT talk over each other")
                log("    unless they actually did.")
            else:
                log(">>> Only ONE speaker was captured; get others talking to test routing.")

        await voice.disconnect(force=True)
    finally:
        await client.close()


if __name__ == "__main__":
    if not CHANNEL_ID:
        raise SystemExit("Usage: python poc_decrypt.py <voice_channel_id> [seconds]")
    client.run(get_token())
