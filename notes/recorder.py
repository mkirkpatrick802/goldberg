"""
Live voice capture via nextcord-ext-listening.

Unlike the rest of the notes package this module *does* need the voice stack
(PyNaCl, opus, nextcord). It's imported only by the cog, so the offline pipeline
and its CLI harness stay runnable on a machine with none of that installed.

The library writes one raw PCM file per SSRC and resolves each to a Member, so
"one audio stream per speaker" falls out naturally — which is what lets the
pipeline label the transcript and interleave everyone onto one timeline.
"""

import asyncio
import shutil
import wave
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from nextcord.ext.listening import AudioFile, AudioFileSink, AudioProcessPool, VoiceClient

try:
    import audioop  # stdlib < 3.13; the audioop-lts wheel provides it on 3.13+
except ImportError:  # pragma: no cover - only if audioop-lts is missing
    audioop = None

# What Discord's opus decoder hands us: 48kHz, stereo, 16-bit signed.
SOURCE_RATE = 48000
SOURCE_CHANNELS = 2
SAMPLE_WIDTH = 2

# What we write. Whisper resamples to 16kHz mono internally anyway, so doing it
# here costs nothing in accuracy and saves a lot of disk: an hour of 48kHz
# stereo is ~690MB per speaker, versus ~115MB at 16kHz mono.
TARGET_RATE = 16000

_READ_CHUNK = SOURCE_RATE * SOURCE_CHANNELS * SAMPLE_WIDTH  # one second

_process_pool: AudioProcessPool | None = None


@dataclass
class RecordingSession:
    """One in-progress recording. Owned by the cog, one per guild."""
    voice_client: VoiceClient
    sink: AudioFileSink
    out_dir: Path
    channel_id: int
    title: str
    started_at: datetime
    # Everyone seen in the channel while recording. Snapshotted at start and
    # topped up at stop, so someone who joined late still counts as an attendee.
    attendee_ids: set[int] = field(default_factory=set)


def _get_process_pool() -> AudioProcessPool:
    """
    One decode pool for the lifetime of the bot.

    The pool spawns worker processes, so building a fresh one per meeting would
    leak them. Note this is why bot.py's `if __name__ == "__main__"` guard
    matters — without it, multiprocessing re-imports the module and the event
    loop dies.
    """
    global _process_pool
    if _process_pool is None:
        _process_pool = AudioProcessPool(max_processes=2)
    return _process_pool


def _pcm_to_wav(pcm_path: Path, wav_path: Path) -> None:
    """
    Convert raw PCM to a WAV whisper can read, downmixed to 16kHz mono.

    Deliberately not using the library's WaveAudioFile.convert(), which shells
    out to ffmpeg — this keeps the whole feature free of external binaries.
    Streamed in chunks because a long meeting's PCM runs to gigabytes.
    """
    if audioop is None:
        # No resampler available: write the source format through unchanged.
        # Bigger files, identical transcription results.
        with open(pcm_path, "rb") as src, wave.open(str(wav_path), "wb") as dst:
            dst.setnchannels(SOURCE_CHANNELS)
            dst.setsampwidth(SAMPLE_WIDTH)
            dst.setframerate(SOURCE_RATE)
            shutil.copyfileobj(src, dst._file, _READ_CHUNK)  # type: ignore[attr-defined]
        return

    frame_size = SOURCE_CHANNELS * SAMPLE_WIDTH
    state = None

    with open(pcm_path, "rb") as src, wave.open(str(wav_path), "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(SAMPLE_WIDTH)
        dst.setframerate(TARGET_RATE)

        while chunk := src.read(_READ_CHUNK):
            # A truncated final frame would corrupt the conversion.
            usable = len(chunk) - (len(chunk) % frame_size)
            if usable <= 0:
                break
            mono = audioop.tomono(chunk[:usable], SAMPLE_WIDTH, 0.5, 0.5)
            resampled, state = audioop.ratecv(
                mono, SAMPLE_WIDTH, 1, SOURCE_RATE, TARGET_RATE, state
            )
            dst.writeframes(resampled)


def _label_for(audio_file: AudioFile, taken: set[str]) -> str:
    """
    A human name for a speaker's stream, unique within this meeting.

    The library resolves most SSRCs to a Member, but not always — an unresolved
    stream still holds real speech, so it gets a placeholder rather than being
    dropped.
    """
    user = audio_file.user
    name = getattr(user, "display_name", None) or getattr(user, "name", None)
    if not name:
        name = f"Unknown speaker ({audio_file.ssrc})"

    # Two people can share a display name; the transcript needs them separate.
    label = name
    suffix = 2
    while label in taken:
        label = f"{name} ({suffix})"
        suffix += 1
    taken.add(label)
    return label


async def start(voice_channel, out_dir: Path, title: str) -> RecordingSession:
    """Join the given voice channel and begin recording one stream per speaker."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    guild = voice_channel.guild

    # A previous attempt that half-connected can leave a dangling voice client on
    # the guild. connect(cls=...) then reuses or collides with that stale, often
    # already-disconnected object, and listen() reports "not connected". Clear it
    # first so every /takenotes starts from a clean slate.
    existing = guild.voice_client
    if existing is not None:
        print(
            f"[Notes] Guild {guild.id} already had a voice client "
            f"(connected={existing.is_connected()}); disconnecting it first."
        )
        try:
            await existing.disconnect(force=True)
        except Exception as e:
            print(f"[Notes] Couldn't clear the stale voice client: {e}")

    print(f"[Notes] Connecting to voice channel {voice_channel.id}...")
    voice_client: VoiceClient = await voice_channel.connect(cls=VoiceClient)
    print(
        f"[Notes] connect() returned {type(voice_client).__name__}; "
        f"is_connected={voice_client.is_connected()}"
    )

    # connect() should return already-connected, but if the handshake is still
    # settling, poll briefly rather than charging into a listen() that throws.
    for _ in range(50):  # up to ~5s
        if voice_client.is_connected():
            break
        await asyncio.sleep(0.1)

    if not voice_client.is_connected():
        # Surface the real state instead of the opaque "Not connected to voice".
        print(
            f"[Notes] Still not connected after waiting. "
            f"ws={getattr(voice_client, 'ws', '?')!r} "
            f"channel={getattr(voice_client, 'channel', '?')!r}"
        )
        try:
            await voice_client.disconnect(force=True)
        except Exception:
            pass
        raise RuntimeError(
            "Joined the channel but the voice connection never became ready — "
            "the handshake didn't complete. This is a voice-stack problem, not a "
            "permissions or command problem."
        )

    sink = AudioFileSink(AudioFile, output_dir=str(out_dir))

    print("[Notes] Connected. Starting listener...")
    voice_client.listen(sink, _get_process_pool())
    print("[Notes] Listening.")

    return RecordingSession(
        voice_client=voice_client,
        sink=sink,
        out_dir=out_dir,
        channel_id=voice_channel.id,
        title=title,
        started_at=datetime.now(),
        attendee_ids={m.id for m in voice_channel.members if not m.bot},
    )


async def stop(session: RecordingSession) -> dict[str, Path]:
    """
    Stop recording, leave the channel, and return {speaker label: wav path}.

    Safe to call even if parts of the session already fell over — the cog calls
    this from a finally block, so it must not raise on a half-dead session.
    """
    try:
        session.voice_client.stop_listening()
    except Exception as e:
        print(f"[Notes] stop_listening failed: {e}")

    # Flushes buffered frames and closes every PCM file. Must happen before we
    # read them, or the tail of the meeting is missing.
    try:
        session.sink.cleanup()
    except Exception as e:
        print(f"[Notes] sink cleanup failed: {e}")

    try:
        await session.voice_client.disconnect()
    except Exception as e:
        print(f"[Notes] disconnect failed: {e}")

    sources: dict[str, Path] = {}
    taken: set[str] = set()

    for audio_file in session.sink.output_files.values():
        pcm_path = Path(audio_file.path)
        if not pcm_path.is_file() or pcm_path.stat().st_size == 0:
            continue

        user = audio_file.user
        if user is not None and getattr(user, "id", None):
            session.attendee_ids.add(user.id)

        label = _label_for(audio_file, taken)
        wav_path = pcm_path.with_suffix(".wav")
        try:
            _pcm_to_wav(pcm_path, wav_path)
        except Exception as e:
            print(f"[Notes] Failed converting {pcm_path.name}: {e}")
            continue

        sources[label] = wav_path

    return sources
