"""
DAVE PoC — DAVE-layer detective.

Where we are: the transport AEAD unwrap is solved (framing confirmed 60/60).
What fails now is the DAVE layer — libdave reports "no valid cryptor found",
and the previous run died with `malloc(): invalid size`, i.e. heap corruption.

Two fixes to how the probe works, not just what it tries:

1. NO CRYPTO ON A WORKER THREAD. libdave/mlspp is not thread-safe, and the last
   version called it from the capture thread while the event loop drove the same
   MLS session. Here the thread only does socket recv into a list of bytes;
   every crypto call happens single-threaded afterwards.

2. DIAGNOSTICS BEFORE RISKY CALLS. The structure dump is printed and flushed
   before any DAVE decrypt is attempted, so a native crash can't take the
   findings with it.

Then it brute-forces (user ratchet x payload start offset) to find what libdave
actually accepts.

    python poc_dave.py <voice_channel_id> [seconds]
"""

import asyncio
import logging
import os
import select
import struct
import sys
import time
from pathlib import Path

import nacl.secret
import nextcord
from nextcord.gateway import DiscordVoiceWebSocket

import dave

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
# libdave logs a wall of errors per failed frame; we count them ourselves.
logging.getLogger("dave").setLevel(logging.CRITICAL)

AUDIO_PT = 0x78
MAX_PACKETS = 40
DUMP_N = 6
# DAVE frames end with a 2-byte magic marker; seeing it proves the payload
# boundary is right before we even try to decrypt.
DAVE_MAGIC = bytes([0xFA, 0xFA])


def log(msg: str) -> None:
    print(f"[DAVE] {msg}", flush=True)


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


def parse_rtp(data: bytes) -> tuple[int, int, int]:
    b0 = data[0]
    csrc = b0 & 0x0F
    has_ext = b0 & 0x10
    base = 12 + csrc * 4
    aad_len, ext_body = base, 0
    if has_ext and len(data) >= base + 4:
        ext_words = struct.unpack_from(">H", data, base + 2)[0]
        aad_len = base + 4
        ext_body = ext_words * 4
    return struct.unpack_from(">I", data, 8)[0], aad_len, ext_body


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
        log(f"Logged in as {client.user}")
        channel = client.get_channel(CHANNEL_ID)
        if not isinstance(channel, nextcord.VoiceChannel):
            log(f"Channel {CHANNEL_ID} not a voice channel I can see.")
            return

        voice = await channel.connect(cls=ProbeVoiceClient, timeout=30, reconnect=False)
        e2ee = getattr(voice, "e2ee_state", None)
        session = getattr(e2ee, "_session", None) if e2ee else None
        if session is None:
            log("No MLS session.")
            await voice.disconnect(force=True)
            return
        for _ in range(60):
            if session.has_established_group():
                break
            await asyncio.sleep(0.5)
        log("MLS group established. Capturing raw packets — TALK NOW.")

        loop = asyncio.get_running_loop()
        packets = await loop.run_in_executor(None, capture_raw, voice.socket, SECONDS)
        log(f"Captured {len(packets)} audio packets (no crypto ran in the thread).")
        if not packets:
            log("No packets — was anyone talking?")
            await voice.disconnect(force=True)
            return

        # ---- Transport decrypt (main thread) + structure dump, printed FIRST ----
        box = nacl.secret.Aead(bytes(voice.secret_key))
        decoded = []
        for data in packets:
            ssrc, aad_len, ext_body = parse_rtp(data)
            try:
                pt = box.decrypt(bytes(data[aad_len:-4]), bytes(data[:aad_len]),
                                 bytes(data[-4:]) + b"\x00" * 20)
            except Exception:
                continue
            decoded.append((ssrc, ext_body, pt))

        log(f"Transport-decrypted {len(decoded)}/{len(packets)} packets.")
        log("── decrypted plaintext structure ──────────────────────")
        for ssrc, ext_body, pt in decoded[:DUMP_N]:
            ends_magic = pt.endswith(DAVE_MAGIC)
            log(f"  ssrc={ssrc} ext_body={ext_body} len={len(pt)} "
                f"endswith_FAFA={ends_magic}")
            log(f"      head24={pt[:24].hex()}")
            log(f"      tail24={pt[-24:].hex()}")

        magic_count = sum(1 for _, _, pt in decoded if pt.endswith(DAVE_MAGIC))
        log(f"frames ending in the DAVE magic marker: {magic_count}/{len(decoded)}")
        log(f"SSRC->user map from SPEAKING: {voice.ssrc_to_user}")

        # ---- Now the risky part: DAVE decrypt matrix ----
        members = [m.id for m in channel.members if not m.bot]
        ratchets = {}
        for uid in members:
            r = session.get_key_ratchet(str(uid))
            if r is not None:
                ratchets[uid] = r
        log(f"ratchets available for {len(ratchets)}/{len(members)} members")

        log("── DAVE decrypt matrix (user x payload start offset) ──")
        offsets = sorted({0, *(eb for _, eb, _ in decoded)})
        log(f"   trying offsets {offsets} against {len(ratchets)} ratchets")

        sample = decoded[:10]  # keep native-crash exposure small
        results = {}
        for uid, ratchet in ratchets.items():
            for off in offsets:
                dec = dave.Decryptor()
                dec.transition_to_key_ratchet(ratchet)
                ok = 0
                for _ssrc, _eb, pt in sample:
                    frame = pt[off:]
                    if len(frame) < 8:
                        continue
                    try:
                        if dec.decrypt(dave.MediaType.audio, frame):
                            ok += 1
                    except Exception:
                        pass
                if ok:
                    results[(uid, off)] = ok

        if results:
            for (uid, off), ok in sorted(results.items(), key=lambda kv: -kv[1]):
                member = channel.guild.get_member(uid)
                nm = member.display_name if member else uid
                log(f"  {ok}/{len(sample)} OK  <-  user={nm} offset={off}")
            log(">>> WINNER above — that's the user/offset the DAVE layer accepts.")
        else:
            log(">>> No combination decrypted. The structure dump above is the")
            log("    evidence I need — especially whether frames end in FAFA.")

        await voice.disconnect(force=True)
    finally:
        await client.close()


if __name__ == "__main__":
    if not CHANNEL_ID:
        raise SystemExit("Usage: python poc_dave.py <voice_channel_id> [seconds]")
    client.run(get_token())
