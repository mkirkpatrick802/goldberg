"""
Turns a speaker-tagged transcript into structured meeting notes via Claude.
"""

import anthropic

from notes.settings import ANTHROPIC_MODEL, require_api_key

# A two hour meeting lands somewhere around 150k characters of transcript, well
# inside the context window. Anything past this is a sign something went wrong
# upstream (a stuck recorder, a duplicated stream), and we'd rather say so than
# quietly spend a lot of money on it.
MAX_TRANSCRIPT_CHARS = 500_000

SYSTEM_PROMPT = """You write meeting notes for a game development team.

You are given a transcript produced by automatic speech recognition. It is not \
clean: names, jargon, tool names and acronyms are frequently mis-heard, speakers \
talk over each other, and sentences trail off. Read through the noise and infer \
what was actually meant. If a name or term is clearly garbled but recoverable \
from context, use the corrected form. If something is genuinely unclear, say so \
rather than inventing detail.

The transcript is labelled by speaker so you can follow who is responding to \
whom. Do NOT carry those labels into the notes. Everyone in these meetings \
participates, so it does not matter who said a given thing or who made a call — \
record what was said and decided, not who voiced it. The one exception is work \
assignment: when a task is given to someone, name the owner.

Choose the sections that fit this particular meeting rather than forcing a fixed \
template. Always open with a `# <title>` heading, an `## Attendees` list, and a \
`## Summary`. After that, use whichever of these earn their place — and add \
others if the meeting calls for it:

- **Decisions** — what the group actually settled on.
- **Playtest notes** — this team playtests during most meetings and nobody is \
free to take notes while playing, so this section matters. Capture the state of \
the game as observed: bugs, feel, balance, pacing, what worked, what didn't, and \
anything someone reacted to in the moment. Be concrete and thorough; this is \
often the most valuable part of the notes.
- **Discussion** — substantive topics that led somewhere but weren't decisions.
- **Action items** — `- **Owner** — the task.` Write "Unassigned" when no one \
took it. The team tracks tasks on a Taiga board, but not everyone reads it, so \
the assignments belong here too.
- **Open questions** — raised but unresolved.

Omit any section with nothing real in it; never emit a heading followed by \
"None" or "N/A".

Write notes, not a re-transcription. Be specific about the substance — a reader \
who missed the meeting should learn what happened, not just what topics came up. \
Skip small talk and scheduling chatter unless something real came of it."""


def _attendee_block(attendees: list[str] | None) -> str:
    """Render the attendee list for the prompt, or explain its absence."""
    if attendees:
        names = "\n".join(f"- {name}" for name in attendees)
        return (
            "Attendees (from the voice channel — this list is authoritative, use "
            "it verbatim for the Attendees section and treat these as the correct "
            "spellings of everyone's name):\n"
            f"{names}\n\n"
        )
    return (
        "Attendees: not supplied. Infer them from the transcript if you can, and "
        "say so if you cannot.\n\n"
    )


def summarize(title: str, transcript: str, attendees: list[str] | None = None) -> str:
    """
    Return markdown meeting notes for the given transcript.

    `attendees` is the authoritative list of who was in the call, resolved from
    the voice channel's members rather than guessed from the audio — someone who
    never unmutes still attended. Passing it also gives the model the correct
    spelling of everyone's name, which measurably cuts down on ASR name garbling
    in the notes.
    """
    if not transcript.strip():
        raise ValueError("Refusing to summarize an empty transcript.")
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        raise ValueError(
            f"Transcript is {len(transcript):,} characters, over the "
            f"{MAX_TRANSCRIPT_CHARS:,} limit. Something is probably wrong with "
            f"the recording rather than the meeting."
        )

    client = anthropic.Anthropic(api_key=require_api_key())

    print(f"[Notes] Summarizing {len(transcript):,} chars with {ANTHROPIC_MODEL}...")

    # Streamed so a long transcript plus a large max_tokens can't trip the
    # SDK's HTTP timeout. We don't need the individual events, just the result.
    with client.messages.stream(
        model=ANTHROPIC_MODEL,
        max_tokens=8000,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Meeting title: {title}\n\n"
                    f"{_attendee_block(attendees)}"
                    f"Transcript:\n\n{transcript}"
                ),
            }
        ],
    ) as stream:
        message = stream.get_final_message()

    if message.stop_reason == "refusal":
        raise RuntimeError("Claude declined to summarize this transcript.")

    notes = "\n".join(block.text for block in message.content if block.type == "text")
    if not notes.strip():
        raise RuntimeError(
            f"Claude returned no text (stop_reason={message.stop_reason})."
        )

    usage = message.usage
    print(
        f"[Notes] Done — {usage.input_tokens} in / {usage.output_tokens} out tokens."
    )
    return notes.strip()
