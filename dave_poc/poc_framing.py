"""
DAVE PoC — framing detective.

Stage 2 proved everything works except the transport-layer AEAD unwrap: 1453
audio packets arrived, 0 decrypted. That means the packet byte-layout I assumed
for aead_xchacha20_poly1305_rtpsize is wrong somewhere.

Rather than guess again, this script looks at the real bytes: it dumps the
structure of the first few audio packets, then brute-forces a matrix of
plausible framings (where the AAD ends, where the ciphertext ends, how the nonce
is built) and reports which combination actually authenticates.

Output tells us the exact layout, and then poc_decrypt.py gets a one-line fix.

    python poc_framing.py <voice_channel_id> [seconds]
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

AUDIO_PT = 0x78
SAMPLE_DUMP = 5      # how many packets to show raw
MAX_PACKETS = 60     # how many to run the matrix over


def log(msg: str) -> None:
    print(f"[FRAMING] {msg}", flush=True)


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
    async def connect_websocket(self) -> DiscordVoiceWebSocket:
        ws = await DiscordVoiceWebSocket.from_client(self)
        self.ws = ws
        self._connected.clear()
        while ws.secret_key is None:
            await ws.poll_event()
        self._connected.set()
        return ws


def header_lens(data: bytes) -> dict:
    """The three plausible 'end of unencrypted header' offsets."""
    b0 = data[0]
    csrc = b0 & 0x0F
    has_ext = bool(b0 & 0x10)
    base = 12 + csrc * 4
    out = {"fixed12": base}
    if has_ext and len(data) >= base + 4:
        ext_words = struct.unpack_from(">H", data, base + 2)[0]
        out["preamble"] = base + 4
        out["full_ext"] = base + 4 + ext_words * 4
    else:
        out["preamble"] = base
        out["full_ext"] = base
    return out


def nonce_variants(data: bytes) -> dict:
    """Plausible 24-byte nonce constructions."""
    suffix = bytes(data[-4:])
    return {
        "suffix_first": suffix + b"\x00" * 20,   # matches nextcord's send path
        "suffix_last": b"\x00" * 20 + suffix,
        "header_first12": bytes(data[:12]) + b"\x00" * 12,  # old xsalsa style
    }


def describe(data: bytes) -> str:
    b0 = data[0]
    parts = [
        f"len={len(data)}",
        f"b0=0x{b0:02x}(v={b0 >> 6} pad={bool(b0 & 0x20)} ext={bool(b0 & 0x10)} csrc={b0 & 0x0F})",
        f"pt=0x{data[1] & 0x7F:02x}",
        f"ssrc={struct.unpack_from('>I', data, 8)[0]}",
    ]
    hl = header_lens(data)
    parts.append(f"hlens={hl}")
    parts.append(f"head16={data[:16].hex()}")
    parts.append(f"tail8={data[-8:].hex()}")
    return "  ".join(parts)


def capture(voice, seconds):
    """Collect raw audio packets off the voice socket."""
    sock = voice.socket
    packets = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline and len(packets) < MAX_PACKETS:
        ready, _, _ = select.select([sock], [], [], 0.5)
        if not ready:
            continue
        try:
            data = sock.recv(4096)
        except OSError:
            continue
        if len(data) < 16 or (data[1] & 0x7F) != AUDIO_PT:
            continue
        packets.append(data)
    return packets


def run_matrix(packets, secret_key):
    box = nacl.secret.Aead(bytes(secret_key))
    results = {}
    # ct_end: where the ciphertext stops (before the 4-byte nonce suffix, or the very end)
    for split_name in ("fixed12", "preamble", "full_ext"):
        for nonce_name in ("suffix_first", "suffix_last", "header_first12"):
            for ct_end_name, ct_end in (("minus4", -4), ("end", None)):
                key = f"{split_name} | {nonce_name} | ct_end={ct_end_name}"
                ok = 0
                for data in packets:
                    hl = header_lens(data)[split_name]
                    nonce = nonce_variants(data)[nonce_name]
                    aad = bytes(data[:hl])
                    ct = bytes(data[hl:ct_end]) if ct_end else bytes(data[hl:])
                    if len(ct) <= 16:
                        continue
                    try:
                        box.decrypt(ct, aad, nonce)
                        ok += 1
                    except Exception:
                        pass
                if ok:
                    results[key] = ok
    return results


intents = nextcord.Intents.all()
client = nextcord.Client(intents=intents)


@client.event
async def on_ready():
    try:
        log(f"Logged in as {client.user} | nextcord {nextcord.__version__}")
        channel = client.get_channel(CHANNEL_ID)
        if not isinstance(channel, nextcord.VoiceChannel):
            log(f"Channel {CHANNEL_ID} is not a voice channel I can see.")
            return

        voice = await channel.connect(cls=ProbeVoiceClient, timeout=30, reconnect=False)
        log(f"Connected. mode={voice.mode}")

        sk = voice.secret_key
        log(f"secret_key: type={type(sk).__name__} len={len(sk) if sk is not None else 'None'} "
            f"(expect 32)")
        if not sk or len(sk) != 32:
            log(">>> secret_key is not 32 bytes — that alone would break every decrypt.")
            await voice.disconnect(force=True)
            return

        log(f"Capturing up to {MAX_PACKETS} audio packets over {SECONDS}s — TALK NOW.")
        loop = asyncio.get_running_loop()
        packets = await loop.run_in_executor(None, capture, voice, SECONDS)
        log(f"Captured {len(packets)} audio packets.")

        if not packets:
            log(">>> No audio packets captured. Was anyone talking?")
            await voice.disconnect(force=True)
            return

        log("── raw structure of the first few packets ──────────────")
        for data in packets[:SAMPLE_DUMP]:
            log("  " + describe(data))

        log("── framing matrix (only combinations that authenticated) ──")
        results = run_matrix(packets, sk)
        if not results:
            log(">>> NOTHING authenticated. None of the candidate framings is right;")
            log("    the raw dumps above are what I need to work out the real layout.")
        else:
            for key, ok in sorted(results.items(), key=lambda kv: -kv[1]):
                log(f"  {ok}/{len(packets)} OK   <-  {key}")
            best = max(results.items(), key=lambda kv: kv[1])
            log(f">>> WINNER: {best[0]}  ({best[1]}/{len(packets)} packets)")

        await voice.disconnect(force=True)
    finally:
        await client.close()


if __name__ == "__main__":
    if not CHANNEL_ID:
        raise SystemExit("Usage: python poc_framing.py <voice_channel_id> [seconds]")
    client.run(get_token())
