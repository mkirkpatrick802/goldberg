import asyncio
import random
import shutil
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional

import nextcord
from nextcord.ext import commands

from config import NOTES_FORUM_CHANNEL_ID, NOTES_MAX_MINUTES, SERVER_ID
from notes import pipeline, recorder
from notes.settings import NOTES_TMP_DIR
from utils import chunk_message, is_dev

# ─── Goldberg's Vocabulary ─────────────────────────────────────────────────────

START_LINES = [
    "Fine. I'm listening. Try to say something worth writing down.",
    "Recording. Everything you say from here on is evidence.",
    "I'm taking notes now, so this is the part where you pretend to be prepared.",
    "Tape's rolling. Make it count, or don't — I'm transcribing either way.",
    "Alright, I'm awake and paying attention. Rare for both of us.",
]

STOP_LINES = [
    "Recording's off. Let me go make sense of whatever that was.",
    "Done listening. Now the hard part: figuring out what you all meant.",
    "Stopped. Give me a minute to turn that into something readable.",
    "That's a wrap. Processing — this is me doing the work you didn't want to.",
]

NO_SESSION = [
    "I'm not recording anything. You have to start before you can stop. Basic stuff.",
    "There's no session running. You can't stop what never began — philosophical, I know.",
    "Nothing to stop. I've been sitting here doing nothing, same as always.",
]

# ─── Cog ───────────────────────────────────────────────────────────────────────


class Notes(commands.Cog):
    """Records a voice call, transcribes it locally, and posts the notes."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # One recording per guild. Discord only lets the bot hold one voice
        # connection per guild anyway, so this mirrors reality.
        self.sessions: dict[int, recorder.RecordingSession] = {}
        self.timers: dict[int, asyncio.Task] = {}
        self.notify_channels: dict[int, int] = {}

    def cog_unload(self):
        for task in self.timers.values():
            task.cancel()

    # ── /takenotes ──────────────────────────────────────────────────────────────
    @nextcord.slash_command(
        name="takenotes",
        description="Start recording this voice call and take notes on it.",
        guild_ids=[SERVER_ID],
    )
    async def take_notes(
        self,
        interaction: nextcord.Interaction,
        title: Optional[str] = nextcord.SlashOption(
            description="What's this meeting about? Defaults to the date and time.",
            required=False,
        ),
    ):
        if not is_dev(interaction):
            await interaction.response.send_message(
                "Devs only. If you have to ask why, you're not one. 🕶️", ephemeral=True
            )
            return

        if not NOTES_FORUM_CHANNEL_ID:
            await interaction.response.send_message(
                "No notes forum is configured, so there's nowhere to put them. "
                "Someone needs to set `NOTES_FORUM_CHANNEL_ID`.",
                ephemeral=True,
            )
            return

        voice_state = interaction.user.voice
        if voice_state is None or voice_state.channel is None:
            await interaction.response.send_message(
                "You're not in a voice channel. I can't record the sound of you "
                "sitting alone in a text channel.",
                ephemeral=True,
            )
            return

        guild_id = interaction.guild.id
        if guild_id in self.sessions:
            await interaction.response.send_message(
                "I'm already recording. One meeting at a time — I'm a bot, not a "
                "court stenographer with a clone army.",
                ephemeral=True,
            )
            return

        if title is None:
            now = datetime.now()
            hour = now.hour % 12 or 12
            title = f"Meeting — {now:%a %b %d}, {hour}:{now:%M %p}"

        await interaction.response.defer()

        session_dir = NOTES_TMP_DIR / f"{guild_id}-{int(datetime.now().timestamp())}"
        try:
            session = await recorder.start(voice_state.channel, session_dir, title)
        except Exception as e:
            shutil.rmtree(session_dir, ignore_errors=True)
            # Log the full traceback to goldberg.log — the Discord message only
            # shows the final line, which for voice failures is uselessly vague.
            print(f"[Notes] /takenotes failed to start recording: {e!r}")
            traceback.print_exc()
            await interaction.followup.send(
                f"Couldn't start recording. Something's broken.\n```{e}```"
            )
            return

        self.sessions[guild_id] = session
        self.notify_channels[guild_id] = interaction.channel.id
        self.timers[guild_id] = asyncio.create_task(self._auto_stop(guild_id))

        # Deliberately NOT ephemeral — everyone in the call is being recorded and
        # is entitled to see that plainly.
        await interaction.followup.send(
            f"🔴 **Recording — {title}**\n"
            f"{random.choice(START_LINES)}\n\n"
            f"*Everyone in {voice_state.channel.mention} is being recorded. "
            f"Notes get posted publicly when someone runs `/stopnotes`. "
            f"I'll stop on my own after {NOTES_MAX_MINUTES} minutes.*"
        )

    # ── /stopnotes ──────────────────────────────────────────────────────────────
    @nextcord.slash_command(
        name="stopnotes",
        description="Stop recording and post the meeting notes.",
        guild_ids=[SERVER_ID],
    )
    async def stop_notes(self, interaction: nextcord.Interaction):
        if not is_dev(interaction):
            await interaction.response.send_message(
                "Devs only. If you have to ask why, you're not one. 🕶️", ephemeral=True
            )
            return

        guild_id = interaction.guild.id
        if guild_id not in self.sessions:
            await interaction.response.send_message(
                random.choice(NO_SESSION), ephemeral=True
            )
            return

        # Transcription and summarization take minutes on a long meeting, so
        # defer immediately — otherwise the interaction times out in 3 seconds.
        await interaction.response.defer()
        await interaction.followup.send(
            f"{random.choice(STOP_LINES)}\n⏳ *Transcribing and writing up. "
            f"This takes a bit — I'll post the notes when they're ready.*"
        )

        await self._finish(guild_id, interaction.channel)

    # ── Shared finish path ──────────────────────────────────────────────────────
    async def _finish(self, guild_id: int, notify_channel) -> None:
        """
        Stop the session, build the notes, post them.

        Shared by /stopnotes and the auto-stop timer so a timed-out meeting
        produces exactly the same output as a manual one.
        """
        session = self.sessions.pop(guild_id, None)
        if session is None:
            return

        timer = self.timers.pop(guild_id, None)
        if timer is not None and not timer.done():
            timer.cancel()
        self.notify_channels.pop(guild_id, None)

        try:
            # Anyone still sitting in the channel counts, including people who
            # never spoke and so produced no audio stream.
            voice_channel = self.bot.get_channel(session.channel_id)
            if voice_channel is not None:
                session.attendee_ids.update(
                    m.id for m in voice_channel.members if not m.bot
                )

            sources = await recorder.stop(session)
            if not sources:
                await notify_channel.send(
                    "Recording stopped, but there's no audio to work with. "
                    "Either nobody said a word or something went wrong on my end."
                )
                return

            guild = self.bot.get_guild(guild_id)
            attendees = []
            for user_id in sorted(session.attendee_ids):
                member = guild.get_member(user_id) if guild else None
                attendees.append(member.display_name if member else f"User {user_id}")

            # Both the whisper pass and the API call are blocking and slow, so
            # they go off the event loop — the bot stays responsive to other
            # commands while a meeting is being written up.
            notes = await asyncio.to_thread(
                pipeline.process, sources, session.title, attendees
            )

            await self._post_notes(notes, session, notify_channel)

        except Exception as e:
            print(f"[Notes] Failed to produce notes for guild {guild_id}: {e}")
            traceback.print_exc()
            try:
                await notify_channel.send(
                    f"I recorded the meeting and then fell over trying to write "
                    f"it up. Sorry.\n```{type(e).__name__}: {e}```"
                )
            except Exception:
                pass
        finally:
            # Meeting audio is large and there's no reason to keep it.
            shutil.rmtree(session.out_dir, ignore_errors=True)

    async def _post_notes(self, notes, session, notify_channel) -> None:
        """Create the forum post: notes as the body, transcript as an attachment."""
        forum = self.bot.get_channel(NOTES_FORUM_CHANNEL_ID)
        if forum is None or not isinstance(forum, nextcord.ForumChannel):
            await notify_channel.send(
                f"I wrote the notes but `NOTES_FORUM_CHANNEL_ID` "
                f"({NOTES_FORUM_CHANNEL_ID}) isn't a forum channel I can see. "
                f"Posting here instead:"
            )
            for chunk in chunk_message(notes.summary):
                await notify_channel.send(chunk)
            return

        transcript_path = session.out_dir / "transcript.txt"
        transcript_path.write_text(notes.transcript, encoding="utf-8")

        # Forum post names cap at 100 characters; message bodies at 2000.
        chunks = chunk_message(notes.summary)

        thread = await forum.create_thread(
            name=notes.title[:100],
            content=chunks[0],
            file=nextcord.File(str(transcript_path), filename="transcript.txt"),
            allowed_mentions=nextcord.AllowedMentions.none(),
        )

        for chunk in chunks[1:]:
            await thread.send(chunk, allowed_mentions=nextcord.AllowedMentions.none())

        if notify_channel.id != thread.id:
            await notify_channel.send(f"📝 Notes are up: {thread.jump_url}")

    # ── Auto-stop ───────────────────────────────────────────────────────────────
    async def _auto_stop(self, guild_id: int) -> None:
        """Stop a session someone forgot about, rather than recording forever."""
        try:
            await asyncio.sleep(NOTES_MAX_MINUTES * 60)
        except asyncio.CancelledError:
            return

        if guild_id not in self.sessions:
            return

        channel_id = self.notify_channels.get(guild_id)
        channel = self.bot.get_channel(channel_id) if channel_id else None
        if channel is None:
            return

        await channel.send(
            f"We've hit the {NOTES_MAX_MINUTES} minute cap and nobody ran "
            f"`/stopnotes`. Wrapping it up myself."
        )
        await self._finish(guild_id, channel)


# ─── Setup ─────────────────────────────────────────────────────────────────────


def setup(bot: commands.Bot):
    # Sync on purpose. The other cogs use `async def setup`, which nextcord 3.x
    # rejects outright ("attempting to load an asynchronous setup function
    # incorrectly"); a plain def loads on every version.
    bot.add_cog(Notes(bot))
