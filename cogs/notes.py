import asyncio
import random
import shutil
import time
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

ABANDONED = [
    "Everyone left and nobody stopped me. I'm not going to sit here narrating an empty room.",
    "You all wandered off. I'll take the hint — and the notes.",
    "Last one out didn't hit `/stopnotes`. Shocking. Wrapping up myself.",
    "The meeting appears to be over, judging by the complete absence of humans.",
]

# How long the channel must stay empty before we call it. Long enough to survive
# a reconnect or someone hopping channels, short enough not to record an empty
# room for an hour.
ALONE_GRACE_SECONDS = 60

# How long after recording starts to confirm audio is actually decrypting. Long
# enough for a real meeting's first words and any late DAVE-group formation,
# short enough that a stuck stage is caught in the first minute, not after hours.
LIVENESS_CHECK_SECONDS = 45

# Discord caps a message at 2000 characters; leave headroom.
MESSAGE_LIMIT = 1900


def split_notes(markdown: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """
    Split notes into messages on '## ' section boundaries.

    Splitting purely on length cuts through the middle of a section — half an
    action-item list stranded in the next message — which is what makes long
    notes read badly. Here whole sections travel together, several to a message
    when they fit, and only a single oversized section ever gets hard-split.
    """
    sections: list[str] = []
    current: list[str] = []
    for line in markdown.splitlines():
        # A new '## ' heading starts a new section (the '# Title' stays with the
        # first one).
        if line.startswith("## ") and current:
            sections.append("\n".join(current).strip())
            current = [line]
        else:
            current.append(line)
    if current:
        sections.append("\n".join(current).strip())
    sections = [s for s in sections if s]

    messages: list[str] = []
    buffer = ""
    for section in sections:
        if len(section) > limit:
            # Genuinely too big on its own — flush, then fall back to line splits.
            if buffer:
                messages.append(buffer)
                buffer = ""
            messages.extend(chunk_message(section, limit))
            continue
        if not buffer:
            buffer = section
        elif len(buffer) + 2 + len(section) <= limit:
            buffer += "\n\n" + section
        else:
            messages.append(buffer)
            buffer = section
    if buffer:
        messages.append(buffer)
    return messages or [markdown[:limit]]

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
        # Pending "everyone left" countdowns, one per guild.
        self.alone_timers: dict[int, asyncio.Task] = {}
        # Early "is any audio actually decrypting?" checks, one per guild.
        self.liveness_timers: dict[int, asyncio.Task] = {}
        # Forum tag chosen at /takenotes, applied to the post at /stopnotes.
        self.session_tags: dict[int, str] = {}
        # Messages typed in the channel during a meeting: guild -> [(offset, author, text)].
        self.session_chat: dict[int, list] = {}

    def _forum(self):
        """The configured notes forum channel, or None."""
        ch = self.bot.get_channel(NOTES_FORUM_CHANNEL_ID) if NOTES_FORUM_CHANNEL_ID else None
        return ch if isinstance(ch, nextcord.ForumChannel) else None

    @staticmethod
    def _attendees_in(channel) -> set[int]:
        """
        Who counts as an attendee right now.

        In a stage channel that's the people *on stage* (`.speakers`) — not the
        whole audience, which for an all-hands could be dozens of silent
        listeners. Someone brought up onto the stage becomes a speaker, so they
        get picked up here and by the live tracking in on_voice_state_update. In
        a normal voice channel everyone present counts.
        """
        if channel is None:
            return set()
        if isinstance(channel, nextcord.StageChannel):
            people = channel.speakers
        else:
            people = getattr(channel, "members", [])
        return {m.id for m in people if not m.bot}

    def cog_unload(self):
        for task in (
            *self.timers.values(),
            *self.alone_timers.values(),
            *self.liveness_timers.values(),
        ):
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
        team: Optional[str] = nextcord.SlashOption(
            description="Which group is this? Tags the forum post so it stays organized.",
            required=False,
        ),
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

        # Validate the chosen tag against the forum's real tags. Someone can type
        # free text past the autocomplete, and a bad tag shouldn't lose a meeting.
        if team:
            forum = self._forum()
            names = {t.name.lower() for t in forum.available_tags} if forum else set()
            if team.lower() not in names:
                available = ", ".join(sorted(t.name for t in forum.available_tags)) if forum else "none"
                await interaction.response.send_message(
                    f"**{team}** isn't a tag on the notes forum. Pick one of: "
                    f"{available or 'none set up yet'}.",
                    ephemeral=True,
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
        existing = self.sessions.get(guild_id)
        if existing is not None:
            # Not an arbitrary limit: Discord only gives a bot ONE voice
            # connection per server, so a second meeting genuinely can't be
            # recorded. Say where the first one is so they know why.
            where = self.bot.get_channel(existing.channel_id)
            where_txt = where.mention if where else "another voice channel"
            elapsed = int((datetime.now() - existing.started_at).total_seconds() // 60)
            await interaction.response.send_message(
                f"I'm already recording **{existing.title}** in {where_txt} "
                f"({elapsed} min so far). Discord only lets me sit in one voice "
                f"channel per server, so this meeting will have to wait for "
                f"`/stopnotes` — or take notes the old-fashioned way. Tragic.",
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

        # recorder.start seeds attendees from raw members; on a stage that's the
        # whole audience, so re-seed with just the on-stage crowd.
        session.attendee_ids = self._attendees_in(voice_state.channel)

        self.sessions[guild_id] = session
        self.notify_channels[guild_id] = interaction.channel.id
        self.timers[guild_id] = asyncio.create_task(self._auto_stop(guild_id))
        self.liveness_timers[guild_id] = asyncio.create_task(self._liveness_check(guild_id))
        if team:
            self.session_tags[guild_id] = team

        # Deliberately NOT ephemeral — everyone in the call is being recorded and
        # is entitled to see that plainly.
        tag_line = f"\n🏷️ Tagged **{team}**." if team else ""
        await interaction.followup.send(
            f"🔴 **Recording — {title}**{tag_line}\n"
            f"{random.choice(START_LINES)}\n\n"
            f"*Everyone in {voice_state.channel.mention} is being recorded. "
            f"Notes get posted publicly when someone runs `/stopnotes`. "
            f"I'll stop on my own after {NOTES_MAX_MINUTES} minutes.*"
        )

    @take_notes.on_autocomplete("team")
    async def _team_autocomplete(self, interaction: nextcord.Interaction, value: str):
        """Offer the forum's real tags, filtered by what's been typed so far."""
        forum = self._forum()
        names = sorted(t.name for t in forum.available_tags) if forum else []
        if value:
            names = [n for n in names if value.lower() in n.lower()]
        await interaction.response.send_autocomplete(names[:25])

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

    # ── Capture the channel's text chat during a meeting ─────────────────────────
    @commands.Cog.listener()
    async def on_message(self, message):
        """
        Buffer messages typed in the voice channel while it's being recorded.

        Discord voice/stage channels have their own text chat, and a link or
        decision that only gets typed should still land in the notes. Stamped
        with the same monotonic clock the audio uses, so it interleaves cleanly.
        """
        if message.author.bot or message.guild is None:
            return
        session = self.sessions.get(message.guild.id)
        if session is None or message.channel.id != session.channel_id:
            return

        text = message.clean_content.strip()
        if message.attachments:
            text += " " + " ".join(f"[shared: {a.filename}]" for a in message.attachments)
        if not text.strip():
            return

        offset = max(0.0, time.monotonic() - session.started_monotonic)
        self.session_chat.setdefault(message.guild.id, []).append(
            (offset, message.author.display_name, text.strip())
        )

    # ── Failsafes: nobody left to record ────────────────────────────────────────
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        """
        Stop recording when there's no one left to record.

        Two cases: everyone wandered off and left the bot talking to itself, or
        the bot got disconnected/dragged out of the channel. Either way there's
        no point burning the full NOTES_MAX_MINUTES on an empty room.
        """
        session = self.sessions.get(member.guild.id)
        if session is None:
            return
        guild_id = member.guild.id

        # The bot itself was disconnected, kicked, or moved elsewhere.
        if self.bot.user is not None and member.id == self.bot.user.id:
            still_here = after.channel is not None and after.channel.id == session.channel_id
            if not still_here:
                channel = self._notify_channel_for(guild_id)
                if channel is not None:
                    await channel.send(
                        "I got yanked out of the voice channel mid-meeting. "
                        "Salvaging what I recorded."
                    )
                    await self._finish(guild_id, channel)
            return

        # Someone was brought up onto the stage (or is otherwise now a speaker in
        # the channel we're recording) — count them as an attendee, even if they
        # never say anything.
        if (
            not member.bot
            and after.channel is not None
            and after.channel.id == session.channel_id
            and not after.suppress
        ):
            session.attendee_ids.add(member.id)

        # Are any humans left in the channel we're recording? On a stage the
        # audience keeps it alive too — leaving an empty stage is still empty.
        voice_channel = self.bot.get_channel(session.channel_id)
        humans = [
            m for m in getattr(voice_channel, "members", []) if not m.bot
        ] if voice_channel is not None else []

        pending = self.alone_timers.get(guild_id)
        if humans:
            # Someone's still there (or came back) — call off the countdown.
            if pending is not None and not pending.done():
                pending.cancel()
            self.alone_timers.pop(guild_id, None)
        elif pending is None or pending.done():
            self.alone_timers[guild_id] = asyncio.create_task(self._alone_check(guild_id))

    async def _alone_check(self, guild_id: int) -> None:
        """After the grace period, if still alone, wrap the meeting up."""
        try:
            await asyncio.sleep(ALONE_GRACE_SECONDS)
        except asyncio.CancelledError:
            return

        session = self.sessions.get(guild_id)
        if session is None:
            return

        voice_channel = self.bot.get_channel(session.channel_id)
        if voice_channel is not None and any(not m.bot for m in voice_channel.members):
            return  # somebody came back during the grace period

        channel = self._notify_channel_for(guild_id)
        if channel is None:
            return
        await channel.send(random.choice(ABANDONED))
        await self._finish(guild_id, channel)

    async def _liveness_check(self, guild_id: int) -> None:
        """
        Shortly after recording starts, confirm audio is actually decrypting.

        The recorder can be connected and receiving voice packets yet decrypt
        none of them — most often on a stage, where the bot never got added to
        the DAVE encryption group. Left alone that records a full meeting of
        nothing, discovered only when the empty notes come out. Catch it in the
        first minute so someone can fix it (re-add the bot, or move to a normal
        voice channel) and restart.
        """
        try:
            await asyncio.sleep(LIVENESS_CHECK_SECONDS)
        except asyncio.CancelledError:
            return

        session = self.sessions.get(guild_id)
        if session is None:
            return
        # Packets arriving but no track written = receiving audio and failing to
        # decrypt all of it. No packets at all is just silence so far, not a
        # fault — leave that for the end-of-meeting guard.
        if session.received_packets == 0 or session.tracks:
            return

        channel = self._notify_channel_for(guild_id)
        if channel is None:
            return
        await channel.send(
            "⚠️ I'm in the call and receiving audio, but I can't decrypt any of "
            "it — so right now I'm recording **nothing**. On a stage this means I "
            "was never added to the encryption group. Re-adding me as a speaker, "
            "or moving to a normal voice channel, and restarting should fix it. "
            "I'll keep trying in case it recovers on its own."
        )

    def _notify_channel_for(self, guild_id: int):
        channel_id = self.notify_channels.get(guild_id)
        return self.bot.get_channel(channel_id) if channel_id else None

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

        # Cancel the pending timers — but never the task we're running inside.
        # _auto_stop and _alone_check both call _finish, and cancelling the
        # current task would raise CancelledError partway through the wrap-up,
        # losing the meeting we just recorded.
        current = asyncio.current_task()
        for registry in (self.timers, self.alone_timers, self.liveness_timers):
            task = registry.pop(guild_id, None)
            if task is not None and task is not current and not task.done():
                task.cancel()
        self.notify_channels.pop(guild_id, None)
        team_tag = self.session_tags.pop(guild_id, None)
        chat = self.session_chat.pop(guild_id, [])

        try:
            # Anyone still present at the end counts too, including people who
            # never spoke. On a stage this is the on-stage crowd, not the
            # audience.
            voice_channel = self.bot.get_channel(session.channel_id)
            session.attendee_ids.update(self._attendees_in(voice_channel))

            sources = await recorder.stop(session)
            if not sources:
                # No audio at all. Do NOT summarize chat-only content and pass it
                # off as notes — that is exactly how a total recording failure got
                # dressed up as a real meeting write-up. Say plainly what happened,
                # and separate "heard nothing" (silence) from "heard plenty but
                # couldn't decrypt a single packet" (the stage / DAVE-group
                # failure), since the two need different fixes.
                if session.received_packets == 0:
                    await notify_channel.send(
                        "Recording stopped, but not one voice packet reached me the "
                        "whole time. Nobody was audible, or the voice connection "
                        "wasn't delivering audio. I didn't write anything up — there "
                        "was no meeting to write up."
                    )
                else:
                    await notify_channel.send(
                        "Recording stopped, but I couldn't decrypt **any** of the "
                        "audio I received — every voice packet was unreadable, so I "
                        "captured nothing. This is the known end-to-end-encryption "
                        "failure on **stage channels**: I was never added to the "
                        "encryption group. I did **not** write up notes, because "
                        "there'd be no meeting behind them. Use a normal voice "
                        "channel, or re-add me as a speaker, and start again."
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
                pipeline.process, sources, session.title, attendees, chat
            )

            await self._post_notes(notes, session, notify_channel, team_tag)

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

    async def _post_notes(self, notes, session, notify_channel, team_tag=None) -> None:
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

        # Resolve the chosen team to a real ForumTag. Tags can be renamed or
        # deleted between /takenotes and now, so a miss just means no tag rather
        # than a failed post.
        applied_tags = []
        if team_tag:
            match = next(
                (t for t in forum.available_tags if t.name.lower() == team_tag.lower()),
                None,
            )
            if match is not None:
                applied_tags = [match]

        # Forum post names cap at 100 characters; message bodies at 2000. Split
        # on section boundaries so each message is a coherent set of sections
        # rather than an arbitrary slice.
        chunks = split_notes(notes.summary)

        thread = await forum.create_thread(
            name=notes.title[:100],
            content=chunks[0],
            applied_tags=applied_tags or None,
            allowed_mentions=nextcord.AllowedMentions.none(),
        )

        for chunk in chunks[1:]:
            await thread.send(chunk, allowed_mentions=nextcord.AllowedMentions.none())

        # Transcript last, as its own message, so it sits at the very end of the
        # notes rather than buried under the first section.
        await thread.send(
            "📄 **Full transcript** attached.",
            file=nextcord.File(str(transcript_path), filename="transcript.txt"),
            allowed_mentions=nextcord.AllowedMentions.none(),
        )

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
