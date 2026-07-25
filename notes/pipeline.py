"""
The core: per-speaker audio files in, meeting notes out.

Shared verbatim by the CLI harness and (in phase 2) by /stopnotes. Nothing in
here knows about Discord, which is exactly why it can be tested from a terminal.
"""

from dataclasses import dataclass
from pathlib import Path

from notes import summarizer
from notes.transcriber import transcribe_file


@dataclass
class Notes:
    """Everything a caller might want to post: the notes, and what they came from."""
    title: str
    transcript: str
    summary: str
    attendees: list[str]


def _fmt_timestamp(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours:d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


# A message typed in the channel's text chat: (seconds from meeting start,
# author, text). Same time base as the audio segments.
ChatMessage = tuple[float, str, str]


def build_transcript(
    sources: dict[str, Path],
    chat: list[ChatMessage] | None = None,
) -> str:
    """
    Transcribe every speaker's audio and interleave it — with any text chat —
    into one timeline.

    Each audio file is transcribed independently, so timestamps are only
    comparable because every stream starts at the same moment (the recorder
    writes them on a shared clock). Typed messages carry their own offset from
    the same start, so sorting everything by time turns N monologues plus the
    chat back into one conversation. Spoken lines and typed lines are labelled
    differently so the summariser knows which is verbatim.
    """
    tagged: list[tuple[float, str, str]] = []

    for speaker, path in sources.items():
        for segment in transcribe_file(Path(path)):
            tagged.append((segment.start, speaker, segment.text))

    for offset, author, text in chat or []:
        tagged.append((offset, f"{author} (typed)", text))

    tagged.sort(key=lambda row: row[0])

    return "\n".join(
        f"[{_fmt_timestamp(start)}] {speaker}: {text}" for start, speaker, text in tagged
    )


def process(
    sources: dict[str, Path],
    title: str,
    attendees: list[str] | None = None,
    chat: list[ChatMessage] | None = None,
) -> Notes:
    """
    Transcribe the given speaker -> audio file mapping, fold in any text chat,
    and summarize.

    `attendees` should be the display names of everyone in the voice channel,
    resolved from their Discord IDs by the caller. It's passed in rather than
    derived from `sources` because the two genuinely differ: someone who sat in
    the call without speaking produces no audio stream but did attend.

    `chat` is messages typed in the channel during the meeting, so a decision or
    link that was only typed still makes it into the notes.
    """
    if not sources and not chat:
        raise ValueError("Nothing to process — no audio and no chat.")

    transcript = build_transcript(sources, chat)

    if not transcript.strip():
        raise ValueError(
            "Nothing was transcribed and no chat was captured — the meeting "
            "appears to be silent. Check that audio was actually recorded."
        )

    summary = summarizer.summarize(title, transcript, attendees=attendees)
    return Notes(
        title=title,
        transcript=transcript,
        summary=summary,
        attendees=list(attendees or []),
    )
