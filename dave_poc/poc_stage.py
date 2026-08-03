"""
DAVE PoC — Stage-channel detective.

The other probes reject stage channels outright (`isinstance(channel,
VoiceChannel)`), so the MLS/DAVE layer has never actually been observed on a
stage. That's the gap this fills, and it answers the ONE question that decides
whether the notes bot can ever record a stage in-house:

    Are a stage's voice packets DAVE-encrypted, or only transport-encrypted?

Because on a stage the MLS group never establishes for the bot even once it's an
un-suppressed speaker (observed in production: `group_formed=False,
still_suppressed=False`). So:

  * If the transport-decrypted frames END IN THE DAVE MAGIC (0xFAFA), the audio
    is DAVE-wrapped and we'd need a per-user key ratchet we cannot get -> stage
    recording is a dead end in-house, same conclusion as poc_connect's NO-GO.

  * If they DON'T end in 0xFAFA and Opus-decode straight from the transport
    plaintext, then DAVE simply isn't applied on stages. The whole DAVE layer is
    a no-op here and recorder.py should, on a stage, skip it and decode the
    transport plaintext directly -> stage recording becomes an easy fix.

This does not write any WAV; it captures a few seconds, dumps frame structure,
and prints the verdict. Same throwaway rules as the others (README.md): its OWN
venv, and STOP the production bot first (one gateway session per token), or use
a separate test-bot token. A human must be speaking ON the stage.

    python poc_stage.py <stage_channel_id> [seconds]
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
from pathlib import Path

import nacl.secret
import nextcord
import nextcord.opus as opus
from nextcord.gateway import DiscordVoiceWebSocket

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("dave").setLevel(logging.CRITICAL)

AUDIO_PT = 0x78
MAX_PACKETS = 60
DUMP_N = 8
DAVE_MAGIC = bytes([0xFA, 0xFA])


def log(msg: str) -> None:
    print(f"[STAGE] {msg}", flush=True)


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
SECONDS = int(sys.argv[2]) if len(sys.argv) > 2 else 15


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
    """Mono-safe decode — nextcord sizes the PCM buffer by the packet's channel
    count, but libopus always writes interleaved stereo, so a mono packet
    corrupts the heap. Size by the decoder's own channel count instead."""

    def decode(self, data, *, fec: bool = False) -> bytes:
        if data is None:
            frame_size = self._get_last_packet_duration() or self.SAMPLES_PER_FRAME
        else:
            frame_size = self.packet_get_nb_frames(data) * self.packet_get_samples_per_frame(data)
        channels = self.CHANNELS
        pcm = (ctypes.c_int16 * (frame_size * channels))()
        ret = opus._lib.opus_decode(
            self._state, data, len(data) if data else 0,
            ctypes.cast(pcm, opus.c_int16_ptr), frame_size, fec,
        )
        return array.array("h", pcm[: ret * channels]).tobytes()


def parse_rtp(data: bytes) -> tuple[int, int, int, bool]:
    b0 = data[0]
    base = 12 + (b0 & 0x0F) * 4
    aad_len, ext_body = base, 0
    if (b0 & 0x10) and len(data) >= base + 4:
        ext_words = struct.unpack_from(">H", data, base + 2)[0]
        aad_len = base + 4
        ext_body = ext_words * 4
    ssrc = struct.unpack_from(">I", data, 8)[0]
    return ssrc, aad_len, ext_body, bool(b0 & 0x20)


def capture_raw(sock, seconds):
    """Thread body: ONLY collect bytes. No crypto here — libdave isn't thread-safe."""
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


intents = nextcord.Intents.all()
client = nextcord.Client(intents=intents)


@client.event
async def on_ready():
    try:
        log(f"Logged in as {client.user} | nextcord {nextcord.__version__}")
        channel = client.get_channel(CHANNEL_ID)
        if not isinstance(channel, (nextcord.StageChannel, nextcord.VoiceChannel)):
            log(f"Channel {CHANNEL_ID} is a {type(channel).__name__}, not a stage/voice channel.")
            return
        kind = "STAGE" if isinstance(channel, nextcord.StageChannel) else "VOICE"
        log(f"Target: #{channel.name} ({kind}) in {channel.guild.name}")

        voice = await channel.connect(cls=ProbeVoiceClient, timeout=30, reconnect=False)

        # On a stage the bot lands as suppressed audience; become a speaker, the
        # same as recorder.py does, so we probe under production conditions.
        if isinstance(channel, nextcord.StageChannel):
            try:
                await channel.guild.me.edit(suppress=False)
                log("Un-suppressed — now a speaker on the stage.")
            except Exception as e:
                log(f"Could not un-suppress (need 'Mute Members'?): {type(e).__name__}: {e}")

        e2ee = getattr(voice, "e2ee_state", None)
        session = getattr(e2ee, "_session", None) if e2ee else None
        log(f"e2ee_state present: {e2ee is not None} | mls_session present: {session is not None}")

        established = False
        if session is not None:
            for _ in range(20):  # 10s
                try:
                    established = session.has_established_group()
                except Exception as e:
                    log(f"has_established_group() raised: {type(e).__name__}: {e}")
                    break
                if established:
                    break
                await asyncio.sleep(0.5)
        log(f"MLS group established: {established}  "
            f"(expected False on a stage — that's the whole point)")

        log("Capturing raw packets — TALK ON THE STAGE NOW.")
        loop = asyncio.get_running_loop()
        packets = await loop.run_in_executor(None, capture_raw, voice.socket, SECONDS)
        log(f"Captured {len(packets)} audio packets.")
        if not packets:
            log("No packets — was anyone speaking on the stage?")
            await voice.disconnect(force=True)
            return

        # ── Transport decrypt + structure dump ──────────────────────────────
        box = nacl.secret.Aead(bytes(voice.secret_key))
        decoded = []
        for data in packets:
            ssrc, aad_len, ext_body, _pad = parse_rtp(data)
            try:
                pt = box.decrypt(bytes(data[aad_len:-4]), bytes(data[:aad_len]),
                                 bytes(data[-4:]) + b"\x00" * 20)
            except Exception:
                continue
            decoded.append((ssrc, ext_body, pt))

        log(f"Transport-decrypted {len(decoded)}/{len(packets)} packets.")
        if not decoded:
            log(">>> Transport unwrap FAILED on every packet — framing is wrong for "
                "this channel; fix that before reading anything below.")
            await voice.disconnect(force=True)
            return

        log("── decrypted plaintext structure ──────────────────────")
        for ssrc, ext_body, pt in decoded[:DUMP_N]:
            log(f"  ssrc={ssrc} ext_body={ext_body} len={len(pt)} "
                f"endswith_FAFA={pt.endswith(DAVE_MAGIC)}")
            log(f"      head24={pt[:24].hex()}")
            log(f"      tail16={pt[-16:].hex()}")

        magic = sum(1 for _, _, pt in decoded if pt.endswith(DAVE_MAGIC))
        log(f"frames ending in the DAVE magic marker (0xFAFA): {magic}/{len(decoded)}")

        # ── Verdict, and a direct-decode sanity check when it looks un-DAVE'd ──
        if magic == 0:
            log(">>> LOOKS UN-DAVE'D: no frame ends in 0xFAFA. Stage audio is "
                "probably transport-only. Trying a direct Opus decode to confirm...")
            dec = SafeOpusDecoder()
            ok = 0
            for _ssrc, ext_body, pt in decoded[:20]:
                frame = pt[ext_body:] if ext_body else pt
                if len(frame) < 3:
                    continue
                try:
                    pcm = dec.decode(frame)
                    if pcm:
                        ok += 1
                except Exception:
                    pass
            log(f"    direct Opus decode succeeded on {ok} sample frames.")
            if ok:
                log(">>> GO (stage): DAVE is NOT applied on stages. recorder.py should, "
                    "on a StageChannel, skip the DAVE unwrap and Opus-decode the "
                    "transport plaintext (minus the ext body) directly. Easy fix.")
            else:
                log(">>> INCONCLUSIVE: not FAFA-framed but didn't Opus-decode either. "
                    "Dump the head bytes above and figure out what this framing is.")
        else:
            log(">>> NO-GO (stage): frames ARE DAVE-wrapped, and the group never forms "
                "for the bot, so no ratchet is reachable. In-house stage receive is a "
                "dead end — keep the cog's 'use a normal voice channel' guidance.")

        await voice.disconnect(force=True)
    finally:
        await client.close()


if __name__ == "__main__":
    if not CHANNEL_ID:
        raise SystemExit("Usage: python poc_stage.py <stage_channel_id> [seconds]")
    if not opus.is_loaded():
        try:
            opus._load_default()
        except Exception:
            pass
    client.run(get_token())
