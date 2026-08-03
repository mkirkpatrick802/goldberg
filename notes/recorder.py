"""
Live voice capture over Discord's DAVE end-to-end encryption.

Discord now requires DAVE (MLS-based E2EE) on voice channels — a client without
it is rejected with voice close code 4017 — and no Python Discord library
implements the *receive* half. This module does, using Discord's own libdave via
dave-py. The recipe below was established empirically in dave_poc/ (see
`docs/DAVE.md`); every step is load-bearing, so change it only with evidence.

Pipeline per packet:
    RTP  ->  transport AEAD unwrap  ->  DAVE unwrap  ->  Opus decode  ->  WAV

Design constraints, each learned the hard way:

  * libdave/mlspp is NOT thread-safe. The capture thread only does recv() and
    hands bytes to a queue; every crypto call happens on the event loop.
  * A dave key ratchet is MOVED into a Decryptor (nanobind relinquishes the
    Python object), so it is single-use. We fetch a fresh one periodically,
    which also keeps up with MLS re-keying when people join or leave.
  * nextcord's Opus decoder overflows the heap on mono packets — see
    SafeOpusDecoder.
  * Audio is written to disk continuously and downsampled on the way in. A
    two-hour meeting at 48kHz stereo would be ~690MB *per speaker*; at 16kHz
    mono (what Whisper wants anyway) it's ~115MB.
  * Every speaker's file shares one clock, with gaps padded by silence, so
    notes/pipeline.py can interleave segments by timestamp and rebuild the
    conversation in the right order.
"""

import array
import asyncio
import ctypes
import queue
import select
import struct
import threading
import time
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import nacl.secret
import nextcord
import nextcord.opus as opus
from nextcord.gateway import DiscordVoiceWebSocket

import dave

try:
    import audioop  # stdlib <3.13; the audioop-lts wheel provides it on 3.13+
except ImportError:  # pragma: no cover
    audioop = None

# ─── Wire format ──────────────────────────────────────────────────────────────
AUDIO_PT = 0x78                    # Discord voice RTP payload type
DAVE_MAGIC = bytes([0xFA, 0xFA])   # a real DAVE frame ends with this

# ─── Audio format ─────────────────────────────────────────────────────────────
SOURCE_RATE, SOURCE_CHANNELS = 48000, 2   # what Opus decodes to
TARGET_RATE, TARGET_CHANNELS = 16000, 1   # what we store (Whisper resamples anyway)
SAMPLE_WIDTH = 2
TARGET_BYTES_PER_SECOND = TARGET_RATE * TARGET_CHANNELS * SAMPLE_WIDTH
SILENCE = b"\x00"

# Re-create each Decryptor after this many frames, which re-fetches the ratchet
# and so follows MLS re-keys.
DECRYPTOR_BATCH = 50
# How often the event loop drains the capture queue.
DRAIN_INTERVAL = 0.25


def _log(msg: str) -> None:
    print(f"[Notes] {msg}", flush=True)


class DaveVoiceClient(nextcord.VoiceClient):
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
        # Assign explicitly: whether the base connect() assigns the return value
        # differs across nextcord 3.x point releases.
        self.ws = ws
        self._connected.clear()
        while ws.secret_key is None:
            await ws.poll_event()
        self._connected.set()
        return ws


class SafeOpusDecoder(opus.Decoder):
    """
    Fixes a heap overflow in nextcord's Decoder.decode.

    It sizes the PCM buffer by the *packet's* channel count, but the decoder is
    created STEREO and libopus always writes interleaved stereo. A mono packet
    therefore gets half the space libopus then writes, corrupting the heap
    (`malloc(): invalid size`). Sizing by the decoder's own channel count is
    correct — nextcord already does exactly that in its packet-loss branch.
    Unnoticed upstream because nextcord only ever sends audio; decode() is dead
    code there.
    """

    def decode(self, data, *, fec: bool = False) -> bytes:
        if data is None:
            frame_size = self._get_last_packet_duration() or self.SAMPLES_PER_FRAME
        else:
            frame_size = self.packet_get_nb_frames(data) * self.packet_get_samples_per_frame(data)

        channels = self.CHANNELS  # the fix
        pcm = (ctypes.c_int16 * (frame_size * channels))()
        ret = opus._lib.opus_decode(
            self._state, data, len(data) if data else 0,
            ctypes.cast(pcm, opus.c_int16_ptr), frame_size, fec,
        )
        return array.array("h", pcm[: ret * channels]).tobytes()


def _parse_rtp(data: bytes) -> tuple[int, int, int, bool]:
    """
    (ssrc, aad_len, ext_body_len, has_padding) for an rtpsize packet.

    The unencrypted header — the AEAD's additional authenticated data — is the
    fixed 12 bytes + CSRCs + the 4-byte extension *preamble* only. The extension
    body lives inside the ciphertext, so it is skipped after decryption.
    """
    b0 = data[0]
    base = 12 + (b0 & 0x0F) * 4
    aad_len, ext_body = base, 0
    if (b0 & 0x10) and len(data) >= base + 4:
        ext_words = struct.unpack_from(">H", data, base + 2)[0]
        aad_len = base + 4
        ext_body = ext_words * 4
    ssrc = struct.unpack_from(">I", data, 8)[0]
    return ssrc, aad_len, ext_body, bool(b0 & 0x20)


class _SpeakerTrack:
    """One speaker's output: decoder, resampler state, and an open WAV file."""

    def __init__(self, user_id: int, out_dir: Path):
        self.user_id = user_id
        self.path = out_dir / f"user-{user_id}.wav"
        self.decoder = SafeOpusDecoder()
        self._resample_state = None
        self._written = 0  # bytes of 16kHz mono audio on disk
        self._wav = wave.open(str(self.path), "wb")
        self._wav.setnchannels(TARGET_CHANNELS)
        self._wav.setsampwidth(SAMPLE_WIDTH)
        self._wav.setframerate(TARGET_RATE)

    def write(self, opus_frame: bytes, arrival: float) -> None:
        """Decode, downmix to 16kHz mono, and place it at its arrival time."""
        pcm = self.decoder.decode(opus_frame)  # 48kHz stereo
        if audioop is not None:
            mono = audioop.tomono(pcm, SAMPLE_WIDTH, 0.5, 0.5)
            pcm, self._resample_state = audioop.ratecv(
                mono, SAMPLE_WIDTH, 1, SOURCE_RATE, TARGET_RATE, self._resample_state
            )

        # Pad the gap since this speaker last spoke, so every track shares one
        # clock and the transcript can be interleaved correctly.
        offset = int(arrival * TARGET_BYTES_PER_SECOND)
        offset -= offset % SAMPLE_WIDTH
        if offset > self._written:
            self._wav.writeframes(SILENCE * (offset - self._written))
            self._written = offset

        self._wav.writeframes(pcm)
        self._written += len(pcm)

    def close(self, pad_to: int = 0) -> None:
        try:
            if pad_to > self._written:
                self._wav.writeframes(SILENCE * (pad_to - self._written))
                self._written = pad_to
            self._wav.close()
        except Exception as e:
            _log(f"Failed closing track for {self.user_id}: {e}")

    @property
    def written(self) -> int:
        return self._written


@dataclass
class RecordingSession:
    """One in-progress recording. Owned by the cog, one per guild."""
    voice_client: DaveVoiceClient
    out_dir: Path
    channel_id: int
    title: str
    started_at: datetime
    attendee_ids: set[int] = field(default_factory=set)
    # Stage channels don't apply DAVE, so their audio is decoded straight from
    # the transport layer with no ratchet (see _decrypt_packet). Set at start().
    is_stage: bool = False

    # internals
    packets: "queue.Queue" = field(default_factory=queue.Queue)
    tracks: dict[int, _SpeakerTrack] = field(default_factory=dict)
    stop_flag: threading.Event = field(default_factory=threading.Event)
    capture_thread: threading.Thread | None = None
    drain_task: asyncio.Task | None = None
    decryptors: dict[int, dave.Decryptor] = field(default_factory=dict)
    frames_seen: dict[int, int] = field(default_factory=dict)
    started_monotonic: float = 0.0
    dropped: int = 0
    # Audio packets pulled off the capture queue, decryptable or not. Lets the
    # cog tell "heard nothing" (silence) apart from "heard plenty but couldn't
    # decrypt a single one" (the stage / DAVE-group failure).
    received_packets: int = 0


def _capture_loop(session: RecordingSession) -> None:
    """
    Thread body: recv audio packets and queue them. Nothing else.

    No crypto here — libdave is not thread-safe, so decryption happens on the
    event loop.
    """
    sock = session.voice_client.socket
    while not session.stop_flag.is_set():
        try:
            ready, _, _ = select.select([sock], [], [], 0.5)
        except Exception:
            break
        if not ready:
            continue
        try:
            data = sock.recv(4096)
        except OSError:
            continue
        if len(data) >= 16 and (data[1] & 0x7F) == AUDIO_PT:
            session.packets.put((time.monotonic() - session.started_monotonic, data))


def _decrypt_packet(session: RecordingSession, arrival: float, data: bytes):
    """Transport + DAVE unwrap. Returns (user_id, opus_frame) or None."""
    voice = session.voice_client
    if not voice.secret_key:
        return None

    ssrc, aad_len, ext_body, has_padding = _parse_rtp(data)
    try:
        plain = nacl.secret.Aead(bytes(voice.secret_key)).decrypt(
            bytes(data[aad_len:-4]), bytes(data[:aad_len]),
            bytes(data[-4:]) + SILENCE * 20,
        )
    except Exception:
        return None

    if has_padding and plain:
        pad = plain[-1]
        if 0 < pad <= len(plain):
            plain = plain[:-pad]

    frame = plain[ext_body:] if ext_body else plain

    user_id = voice.ssrc_to_user.get(ssrc)
    if user_id is None:
        return None

    # Stage channels don't wrap audio in DAVE (see start() and
    # dave_poc/poc_stage.py — 0/60 frames carried the 0xFAFA marker and the
    # transport plaintext Opus-decoded directly). So the frame past the extension
    # body already IS the Opus payload: decode it straight, no ratchet needed.
    # Discord's ~3-byte comfort-noise frames aren't worth decoding.
    if session.is_stage:
        return (user_id, frame) if len(frame) >= 8 else None

    # Normal voice channel: the payload is DAVE-wrapped. Silence/comfort-noise
    # frames aren't DAVE frames; handing one to libdave makes it read past its
    # buffer.
    if len(frame) < 8 or not frame.endswith(DAVE_MAGIC):
        return None

    seen = session.frames_seen.get(user_id, 0)
    if user_id not in session.decryptors or seen % DECRYPTOR_BATCH == 0:
        mls = _mls_session(voice)
        if mls is None:
            return None
        ratchet = mls.get_key_ratchet(str(user_id))
        if ratchet is None:
            return None
        decryptor = dave.Decryptor()
        decryptor.transition_to_key_ratchet(ratchet)
        session.decryptors[user_id] = decryptor
    session.frames_seen[user_id] = seen + 1

    opus_frame = session.decryptors[user_id].decrypt(dave.MediaType.audio, frame)
    return (user_id, opus_frame) if opus_frame else None


def _drain(session: RecordingSession, budget: int = 400) -> None:
    """Process queued packets on the event loop, bounded so we never hog it."""
    for _ in range(budget):
        try:
            arrival, data = session.packets.get_nowait()
        except queue.Empty:
            return
        session.received_packets += 1
        try:
            result = _decrypt_packet(session, arrival, data)
            if result is None:
                continue
            user_id, opus_frame = result
            track = session.tracks.get(user_id)
            if track is None:
                track = _SpeakerTrack(user_id, session.out_dir)
                session.tracks[user_id] = track
            track.write(opus_frame, arrival)
        except Exception as e:
            session.dropped += 1
            if session.dropped in (1, 100, 1000):
                _log(f"Dropped a packet ({session.dropped} so far): {type(e).__name__}: {e}")


async def _drain_loop(session: RecordingSession) -> None:
    try:
        while not session.stop_flag.is_set():
            _drain(session)
            await asyncio.sleep(DRAIN_INTERVAL)
    except asyncio.CancelledError:
        pass


def _mls_session(voice):
    """The libdave MLS session behind a voice client, or None if there isn't one."""
    e2ee = getattr(voice, "e2ee_state", None)
    return getattr(e2ee, "_session", None) if e2ee else None


async def _take_stage(voice_channel, guild) -> None:
    """
    On a stage channel, bring the bot up from audience to speaker.

    A suppressed audience member isn't part of the speakers' DAVE encryption
    group, so it can't decrypt anything. Needs the "Mute Members" permission; if
    that's missing we say so plainly and carry on — a human can bring the bot up
    manually. A no-op on a normal voice channel.
    """
    if not isinstance(voice_channel, nextcord.StageChannel):
        return
    try:
        await guild.me.edit(suppress=False)
        _log("Requested speaker status on the stage.")
    except nextcord.Forbidden:
        _log("WARNING: can't bring myself onto the stage (missing 'Mute Members'). "
             "Someone needs to make me a speaker, or the group won't form.")
    except Exception as e:
        _log(f"WARNING: couldn't take the stage: {e}")


async def _wait_for_group(mls, seconds: float) -> bool:
    """Poll up to `seconds` for the DAVE/MLS group to establish. Returns whether it did."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if mls.has_established_group():
            return True
        await asyncio.sleep(0.5)
    return mls.has_established_group()


async def start(voice_channel, out_dir: Path, title: str) -> RecordingSession:
    """Join the channel and begin recording one track per speaker."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    if not opus.is_loaded():
        try:
            opus._load_default()
        except Exception:
            pass
    if not opus.is_loaded():
        raise RuntimeError("libopus is not available — install libopus0.")

    guild = voice_channel.guild
    existing = guild.voice_client
    if existing is not None:
        # A previous half-connected attempt leaves a dangling client that
        # collides with the new one.
        try:
            await existing.disconnect(force=True)
        except Exception as e:
            _log(f"Couldn't clear a stale voice client: {e}")

    voice: DaveVoiceClient = await voice_channel.connect(cls=DaveVoiceClient, timeout=60)
    if not voice.is_connected():
        raise RuntimeError("Joined the channel but the voice connection never became ready.")

    # On a stage the bot joins as audience (suppressed); bring it up as a speaker.
    await _take_stage(voice_channel, guild)

    # Stage channels don't apply DAVE at all — verified with
    # dave_poc/poc_stage.py: 0/60 frames carried the 0xFAFA marker and the
    # transport plaintext Opus-decoded directly (nextcord even logs "Failed to
    # set up ratchet, encryptor is not initialised"). So on a stage there's no
    # MLS group to wait for and no per-user ratchet to fetch; _decrypt_packet
    # decodes the transport audio straight. Normal voice channels DO use DAVE.
    is_stage = isinstance(voice_channel, nextcord.StageChannel)
    if is_stage:
        _log("Stage channel — DAVE not in use here; decoding transport audio directly.")
    else:
        mls = _mls_session(voice)
        if mls is None:
            await voice.disconnect(force=True)
            raise RuntimeError(
                "This connection has no DAVE session. The bot needs nextcord 3.2+ "
                "with dave-py installed."
            )
        # Don't hard-fail if the group is slow — it can form once audio flows, and
        # the per-packet decrypt picks up the ratchets as soon as it does. Total
        # failure is caught by the cog (a liveness warning within the minute, and a
        # plain error at the end instead of chat dressed up as notes).
        if not await _wait_for_group(mls, 10.0):
            _log("DAVE group not formed in 10s — recording anyway; it may form "
                 "once audio starts flowing.")

    session = RecordingSession(
        voice_client=voice,
        out_dir=out_dir,
        channel_id=voice_channel.id,
        title=title,
        started_at=datetime.now(),
        attendee_ids={m.id for m in voice_channel.members if not m.bot},
        started_monotonic=time.monotonic(),
        is_stage=is_stage,
    )
    session.capture_thread = threading.Thread(
        target=_capture_loop, args=(session,), daemon=True
    )
    session.capture_thread.start()
    session.drain_task = asyncio.create_task(_drain_loop(session))

    _log(f"Recording '{title}' in #{voice_channel.name}")
    return session


async def stop(session: RecordingSession) -> dict[str, Path]:
    """
    Stop recording, leave the channel, and return {speaker label: wav path}.

    Safe on a half-dead session — the cog calls this from a finally block.
    """
    session.stop_flag.set()

    if session.capture_thread is not None:
        session.capture_thread.join(timeout=5)
    if session.drain_task is not None:
        session.drain_task.cancel()
        try:
            await session.drain_task
        except (asyncio.CancelledError, Exception):
            pass

    # Anything still queued.
    _drain(session, budget=100000)

    try:
        await session.voice_client.disconnect(force=True)
    except Exception as e:
        _log(f"Disconnect failed: {e}")

    # Pad every track to the same length so they stay aligned.
    longest = max((t.written for t in session.tracks.values()), default=0)
    guild = getattr(session.voice_client, "guild", None)

    sources: dict[str, Path] = {}
    taken: set[str] = set()
    for user_id, track in session.tracks.items():
        track.close(pad_to=longest)
        if track.written <= 0 or not track.path.is_file():
            continue

        member = guild.get_member(user_id) if guild else None
        name = getattr(member, "display_name", None) or f"User {user_id}"
        label, suffix = name, 2
        while label in taken:  # two people can share a display name
            label = f"{name} ({suffix})"
            suffix += 1
        taken.add(label)

        session.attendee_ids.add(user_id)
        sources[label] = track.path

    secs = longest / TARGET_BYTES_PER_SECOND if longest else 0
    _log(f"Recorded {len(sources)} speaker(s), {secs:.0f}s"
         + (f", dropped {session.dropped} packets" if session.dropped else ""))
    return sources
