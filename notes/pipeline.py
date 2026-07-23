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


def build_transcript(sources: dict[str, Path]) -> str:
    """
    Transcribe every speaker's audio and interleave it into one timeline.

    Each file is transcribed independently, so timestamps are only comparable
    because every stream starts at the same moment — which is how the recorder
    writes them. Sorting by start time is what turns N monologues back into a
    conversation.
    """
    tagged: list[tuple[float, str, str]] = []

    for speaker, path in sources.items():
        for segment in transcribe_file(Path(path)):
            tagged.append((segment.start, speaker, segment.text))

    tagged.sort(key=lambda row: row[0])

    return "\n".join(
        f"[{_fmt_timestamp(start)}] {speaker}: {text}" for start, speaker, text in tagged
    )


def process(
    sources: dict[str, Path],
    title: str,
    attendees: list[str] | None = None,
) -> Notes:
    """
    Transcribe the given speaker -> audio file mapping and summarize it.

    `attendees` should be the display names of everyone in the voice channel,
    resolved from their Discord IDs by the caller. It's passed in rather than
    derived from `sources` because the two genuinely differ: someone who sat in
    the call without speaking produces no audio stream but did attend.
    """
    if not sources:
        raise ValueError("No audio sources to process.")

    transcript = build_transcript(sources)

    if not transcript.strip():
        raise ValueError(
            "Nothing was transcribed — the recording appears to be silent. "
            "Check that audio was actually captured before blaming the model."
        )

    summary = summarizer.summarize(title, transcript, attendees=attendees)
    return Notes(
        title=title,
        transcript=transcript,
        summary=summary,
        attendees=list(attendees or []),
    )
