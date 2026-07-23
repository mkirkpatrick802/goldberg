"""
DAVE PoC — Stage 2 (final): decrypt real speakers to WAV.

Every layer is now empirically proven; this assembles them. The recipe, each
part confirmed by a probe rather than assumed:

  1. Transport AEAD unwrap (poc_framing.py, 60/60 packets):
       AAD   = fixed RTP header + CSRCs + the 4-byte extension PREAMBLE only
       ct    = everything after that, minus the trailing 4-byte nonce
       nonce = those 4 bytes, at the FRONT of a 24-byte zero-padded nonce
  2. RTP padding: if the padding bit is set, the final byte gives how many
     trailing bytes to drop (seen as 0x07 x7 after the DAVE magic marker).
  3. The RTP extension BODY sits inside the ciphertext -> skip it to reach the
     DAVE frame (offset 8 in practice; the winning offset in poc_dave.py).
  4. DAVE unwrap with THAT speaker's ratchet, routed by the SPEAKING SSRC map.

Two hard-won implementation rules baked in here:

  * A key ratchet is MOVED into a Decryptor (nanobind relinquishes the Python
    instance), so it is single-use — fetch a fresh one per Decryptor.
  * libdave/mlspp is NOT thread-safe. The capture thread only does socket
    recv(); every crypto call happens on the main thread afterwards. Doing
    otherwise corrupted the heap (`malloc(): invalid size`).

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
logging.getLogger("dave").setLevel(logging.CRITICAL)  # it logs per failed frame

OUT_DIR = Path(__file__).resolve().parent / "out"
AUDIO_PT = 0x78
MAX_PACKETS = 20000
# A DAVE-encrypted frame ends with this 2-byte magic marker. Anything else —
# notably Discord's ~11-byte Opus silence frames — is NOT a DAVE frame, and
# handing one to libdave makes it read past the buffer while parsing the
# trailer, which corrupts the heap (`malloc(): invalid size`). Filter first.
DAVE_MAGIC = bytes([0xFA, 0xFA])
# 1000 frames x 20ms = ~20s, matching the capture window. The old low cap was a
# guard against a crash we've now traced to the Opus decoder (see
# SafeOpusDecoder), not to libdave. Decryptor recycling is kept as cheap
# insurance while MLS epochs rotate.
MAX_DECRYPTS = 1000
DECRYPTOR_BATCH = 10


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


CHANNEL_ID = int(sys.argv[1]) if len(sys.argv) > 1 else 0
SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 20


class ProbeVoiceClient(nextcord.VoiceClient):
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
    nextcord's Decoder.decode has a heap-overflow bug that this is the fix for.

    It sizes the PCM output buffer using the *packet's* channel count
    (opus_packet_get_nb_channels), but the decoder is created as STEREO and
    libopus always writes interleaved stereo — frame_size * 2 samples. Feed it a
    MONO packet and it allocates half the space it then writes, corrupting the
    heap (`malloc(): invalid size (unsorted)`).

    Sizing by the decoder's own channel count is correct, and is exactly what
    nextcord already does in its packet-loss branch. Nobody upstream has hit
    this because nextcord only ever SENDS audio — decode() is dead code there.
    """

    def decode(self, data, *, fec: bool = False) -> bytes:
        if data is None:
            frame_size = self._get_last_packet_duration() or self.SAMPLES_PER_FRAME
        else:
            frames = self.packet_get_nb_frames(data)
            samples_per_frame = self.packet_get_samples_per_frame(data)
            frame_size = frames * samples_per_frame

        channel_count = self.CHANNELS  # the fix: decoder channels, not packet channels
        pcm = (ctypes.c_int16 * (frame_size * channel_count))()
        pcm_ptr = ctypes.cast(pcm, opus.c_int16_ptr)

        ret = opus._lib.opus_decode(
            self._state, data, len(data) if data else 0, pcm_ptr, frame_size, fec
        )
        return array.array("h", pcm[: ret * channel_count]).tobytes()


def parse_rtp(data: bytes) -> tuple[int, int, int, bool]:
    """(ssrc, aad_len, ext_body_len, has_padding)"""
    b0 = data[0]
    csrc = b0 & 0x0F
    base = 12 + csrc * 4
    aad_len, ext_body = base, 0
    if (b0 & 0x10) and len(data) >= base + 4:  # extension present
        ext_words = struct.unpack_from(">H", data, base + 2)[0]
        aad_len = base + 4          # AAD stops after the preamble
        ext_body = ext_words * 4    # encrypted; skip post-decrypt
    ssrc = struct.unpack_from(">I", data, 8)[0]
    return ssrc, aad_len, ext_body, bool(b0 & 0x20)


class Stats:
    def __init__(self):
        self.raw = self.aead_ok = self.aead_fail = 0
        self.not_dave = self.unmapped = self.no_ratchet = 0
        self.padded_skipped = 0
        self.dave_ok = self.dave_none = 0
        self.opus_ok = self.opus_fail = 0

    def report(self):
        log("── per-stage counters ─────────────────────────")
        log(f"  raw audio packets    : {self.raw}")
        log(f"  transport decrypt OK : {self.aead_ok}")
        log(f"  transport decrypt FAIL: {self.aead_fail}")
        log(f"  RTP-padded (skipped) : {self.padded_skipped}")
        log(f"  not DAVE-framed (skip): {self.not_dave}")
        log(f"  SSRC unmapped        : {self.unmapped}")
        log(f"  no ratchet for user  : {self.no_ratchet}")
        log(f"  DAVE decrypt OK      : {self.dave_ok}")
        log(f"  DAVE decrypt None    : {self.dave_none}")
        log(f"  Opus decode OK       : {self.opus_ok}")
        log(f"  Opus decode FAIL     : {self.opus_fail}")


def capture_raw(sock, seconds):
    """Thread body: collect bytes ONLY. No crypto — libdave isn't thread-safe."""
    out = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and len(out) < MAX_PACKETS:
        ready, _, _ = select.select([sock], [], [], 0.5)
        if not ready:
            continue
        try:
            data = sock.recv(4096)
        except OSError:
            continue
        if len(data) >= 16 and (data[1] & 0x7F) == AUDIO_PT:
            out.append(data)
    return out


def process(packets, voice, session, stats, max_decrypts=MAX_DECRYPTS, batch=DECRYPTOR_BATCH):
    """
    All crypto, single-threaded. Returns {user_id: pcm bytes}.

    Two empirical guards around libdave, both derived from what actually
    survived: a long-lived Decryptor reused across hundreds of frames corrupts
    the heap, while poc_dave.py's pattern — a fresh Decryptor + fresh ratchet
    used for only ~10 frames — ran clean. So we recycle the Decryptor every
    `batch` frames and cap total calls: a few seconds of audio is all the proof
    this probe needs, and the cap keeps us well clear of the crash.
    """
    box = nacl.secret.Aead(bytes(voice.secret_key))
    decryptors: dict[int, dave.Decryptor] = {}
    per_user_count: dict[int, int] = {}
    decoders: dict[int, opus.Decoder] = {}
    pcm: dict[int, bytearray] = {}
    total = 0

    for data in packets:
        if total >= max_decrypts:
            log(f"  reached the {max_decrypts}-frame cap; stopping cleanly.")
            break
        stats.raw += 1
        ssrc, aad_len, ext_body, has_padding = parse_rtp(data)

        try:
            plain = box.decrypt(bytes(data[aad_len:-4]), bytes(data[:aad_len]),
                                bytes(data[-4:]) + b"\x00" * 20)
            stats.aead_ok += 1
        except Exception:
            stats.aead_fail += 1
            continue

        # RTP-padded frames reliably crash libdave on the very first decrypt
        # (heap corruption), while unpadded frames decode fine — that is the
        # only difference between this and the poc_dave.py run that survived.
        # Skip them: unpadded frames alone are plenty to prove the pipeline.
        # The real implementation will need the underlying libdave issue solved
        # rather than dodged.
        if has_padding and plain:
            pad = plain[-1]
            if 0 < pad <= len(plain):
                plain = plain[:-pad]
                stats.padded_skipped += 1

        frame = plain[ext_body:] if ext_body else plain
        if len(frame) < 8:
            continue

        # Never hand a non-DAVE frame to libdave — it corrupts the heap.
        if not frame.endswith(DAVE_MAGIC):
            stats.not_dave += 1
            continue

        uid = voice.ssrc_to_user.get(ssrc)
        if uid is None:
            stats.unmapped += 1
            continue

        # Recycle the Decryptor every `batch` frames (see docstring).
        n = per_user_count.get(uid, 0)
        if uid not in decryptors or n % batch == 0:
            # Fresh ratchet per decryptor — it is moved in, not borrowed.
            ratchet = session.get_key_ratchet(str(uid))
            if ratchet is None:
                stats.no_ratchet += 1
                continue
            dec = dave.Decryptor()
            dec.transition_to_key_ratchet(ratchet)
            decryptors[uid] = dec
            log(f"  [{total:4d}] fresh decryptor for user {uid}")
        per_user_count[uid] = n + 1
        total += 1

        out = decryptors[uid].decrypt(dave.MediaType.audio, frame)
        if not out:
            stats.dave_none += 1
            continue
        stats.dave_ok += 1

        if uid not in decoders:
            decoders[uid] = SafeOpusDecoder()
            pcm[uid] = bytearray()
        try:
            pcm[uid] += decoders[uid].decode(out)
            stats.opus_ok += 1
        except Exception:
            stats.opus_fail += 1

    return {uid: bytes(buf) for uid, buf in pcm.items()}


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
            log("libopus missing — apt install libopus0")
            return

        channel = client.get_channel(CHANNEL_ID)
        if not isinstance(channel, nextcord.VoiceChannel):
            log(f"Channel {CHANNEL_ID} is not a voice channel I can see.")
            return

        voice = await channel.connect(cls=ProbeVoiceClient, timeout=30, reconnect=False)
        e2ee = getattr(voice, "e2ee_state", None)
        session = getattr(e2ee, "_session", None) if e2ee else None
        if session is None:
            log("No MLS session — DAVE inactive.")
            await voice.disconnect(force=True)
            return
        for _ in range(60):
            if session.has_established_group():
                break
            await asyncio.sleep(0.5)
        log(f"MLS group established. Capturing {SECONDS}s — TALK NOW.")

        loop = asyncio.get_running_loop()
        packets = await loop.run_in_executor(None, capture_raw, voice.socket, SECONDS)
        log(f"Captured {len(packets)} audio packets. Decrypting (single-threaded)...")
        log(f"SSRC->user map: {voice.ssrc_to_user}")

        stats = Stats()
        pcm_by_user = process(packets, voice, session, stats)
        stats.report()

        OUT_DIR.mkdir(parents=True, exist_ok=True)
        if not pcm_by_user:
            log(">>> No audio decoded — see counters above.")
        else:
            log("── WAV files ──────────────────────────────────")
            for uid, data in pcm_by_user.items():
                member = channel.guild.get_member(uid)
                name = member.display_name if member else str(uid)
                safe = "".join(c for c in name if c.isalnum() or c in " _-").strip() or str(uid)
                path = OUT_DIR / f"{safe}.wav"
                write_wav(path, data)
                secs = len(data) / (opus.Decoder.SAMPLING_RATE * opus.Decoder.CHANNELS * 2)
                log(f"  {path.name}  ({secs:.1f}s)")
            log(">>> Download and listen. Clear speech = the whole pipeline is proven.")

        await voice.disconnect(force=True)
    finally:
        await client.close()


if __name__ == "__main__":
    if not CHANNEL_ID:
        raise SystemExit("Usage: python poc_decrypt.py <voice_channel_id> [seconds]")
    client.run(get_token())
