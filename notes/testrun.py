"""
testrun.py
──────────
Run the notes pipeline against local audio files. No Discord, no voice capture —
the same code path /stopnotes will use, minus the recording.

Run from inside the goldberg/ folder:

    # Single speaker
    python -m notes.testrun sample.m4a --title "Sprint planning"

    # Name the speaker
    python -m notes.testrun sample.m4a --speaker Michael

    # Multiple speakers, one file each, in matching order
    python -m notes.testrun michael.wav alice.wav --speaker Michael --speaker Alice

    # Keep the output
    python -m notes.testrun sample.m4a --out notes.md
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from notes import pipeline

# The Windows console defaults to cp1252, which can't encode emoji or most of
# the punctuation Claude writes in the notes. Without this the run dies on a
# UnicodeEncodeError while printing perfectly good output.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def default_title() -> str:
    """
    Same shape the /takenotes default will use.

    Built by hand rather than with strftime because the no-leading-zero hour is
    %-I on glibc and %#I on Windows, and this has to run on both.
    """
    now = datetime.now()
    hour = now.hour % 12 or 12
    return f"Meeting — {now:%a %b %d}, {hour}:{now:%M %p}"


def build_sources(files: list[str], speakers: list[str]) -> dict[str, Path]:
    """Pair audio files with speaker labels, falling back to generic names."""
    if speakers and len(speakers) != len(files):
        print(
            f"❌ Got {len(files)} audio file(s) but {len(speakers)} --speaker "
            f"label(s). Give one label per file, or none at all.",
            file=sys.stderr,
        )
        sys.exit(1)

    if not speakers:
        speakers = (
            ["Speaker"] if len(files) == 1
            else [f"Speaker {i}" for i in range(1, len(files) + 1)]
        )

    sources: dict[str, Path] = {}
    for speaker, filename in zip(speakers, files):
        path = Path(filename)
        if not path.is_file():
            print(f"❌ No such file: {path}", file=sys.stderr)
            sys.exit(1)
        sources[speaker] = path
    return sources


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Goldberg notes pipeline on local audio files."
    )
    parser.add_argument("audio", nargs="+", help="Audio file(s): wav, mp3, m4a, ogg...")
    parser.add_argument("--title", help="Meeting title (defaults to the date/time)")
    parser.add_argument(
        "--speaker",
        action="append",
        default=[],
        metavar="NAME",
        help="Speaker label for the corresponding audio file. Repeatable.",
    )
    parser.add_argument(
        "--attendee",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "Someone who was in the meeting. Repeatable. Stands in for the "
            "voice channel member list the cog will supply."
        ),
    )
    parser.add_argument(
        "--out", metavar="PATH", help="Also write the notes to this file"
    )
    parser.add_argument(
        "--transcript-only",
        action="store_true",
        help="Stop after transcription — skip the Anthropic call",
    )
    args = parser.parse_args()

    title = args.title or default_title()
    sources = build_sources(args.audio, args.speaker)

    print(f"\n🎙  {title}")
    for speaker, path in sources.items():
        print(f"    {speaker}: {path}")
    if args.attendee:
        print(f"    attendees: {', '.join(args.attendee)}")
    print()

    if args.transcript_only:
        transcript = pipeline.build_transcript(sources)
        print("─" * 60)
        print("TRANSCRIPT")
        print("─" * 60)
        print(transcript or "(nothing transcribed)")
        return

    try:
        notes = pipeline.process(sources, title, attendees=args.attendee)
    except Exception as e:
        print(f"\n❌ {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

    print("\n" + "─" * 60)
    print("TRANSCRIPT")
    print("─" * 60)
    print(notes.transcript)

    print("\n" + "─" * 60)
    print("NOTES")
    print("─" * 60)
    print(notes.summary)
    print()

    if args.out:
        out_path = Path(args.out)
        out_path.write_text(notes.summary + "\n", encoding="utf-8")
        print(f"✅ Notes written to {out_path}")


if __name__ == "__main__":
    main()
