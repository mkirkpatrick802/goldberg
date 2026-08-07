import nextcord
from nextcord.ext import commands
import json
import logging
import os

log = logging.getLogger("jointocreate")

SETUP_FILE = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", "setup_data.json"))
# Created-channel ids are written on every join/leave, far more often than the
# setup config. Keeping them in their own file avoids two cogs racing to
# rewrite setup_data.json and clobbering each other.
STATE_FILE = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "data", "jtc_state.json"))


def load_setup_config():
    if not os.path.exists(SETUP_FILE):
        return {}
    with open(SETUP_FILE, "r") as f:
        return json.load(f)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"created_channels": []}
    with open(STATE_FILE, "r") as f:
        return json.load(f)


def save_state(data):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(data, f, indent=4)


class JoinToCreate(commands.Cog):
    """'Join to Create' voice channels.

    When a member joins a configured hub channel, the bot spawns a personal
    voice channel, moves them into it, and deletes it once the last person
    leaves.
    """

    def __init__(self, bot):
        self.bot = bot
        state = load_state()
        # Channels this cog created and is responsible for cleaning up.
        self._created = set(state.get("created_channels", []))

    def _persist(self):
        save_state({"created_channels": sorted(self._created)})

    @commands.Cog.listener()
    async def on_ready(self):
        """Reconcile after a restart.

        If the bot went down while temporary channels were live, some may now be
        empty (or already gone). Sweep them so a restart doesn't leave orphans.
        """
        for channel_id in list(self._created):
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                self._created.discard(channel_id)
                continue
            if not [m for m in channel.members if not m.bot]:
                await self._delete_channel(channel, reason="Join-to-Create: empty on startup")
        self._persist()

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            return

        # Mute/deafen/stream toggles fire this event without changing channel.
        if before.channel == after.channel:
            return

        # ── Someone left a temp channel: delete it if it's now empty ────────────
        if before.channel is not None and before.channel.id in self._created:
            if not [m for m in before.channel.members if not m.bot]:
                await self._delete_channel(before.channel, reason="Join-to-Create: last member left")
                self._persist()

        # ── Someone joined a hub: give them their own channel ────────────────────
        if after.channel is not None:
            setup = load_setup_config()
            hub = next(
                (h for h in setup.get("jtc_hubs", []) if h.get("hub_channel_id") == after.channel.id),
                None,
            )
            if hub:
                await self._create_for(member, after.channel, hub)

    async def _create_for(self, member, hub_channel, hub):
        guild = hub_channel.guild
        category = guild.get_channel(hub.get("category_id")) or hub_channel.category

        # Passing `overwrites` to create_voice_channel replaces the normal
        # category-sync behavior entirely, so without this the temp channel
        # would fall back to bare @everyone permissions and ignore whatever
        # role restrictions the category/hub actually have.
        overwrites = {}
        if category is not None:
            overwrites.update(category.overwrites)
        overwrites.update(hub_channel.overwrites)

        # Let the owner manage their own channel: rename it, set a user limit,
        # drag people in.
        owner_overwrite = overwrites.get(member, nextcord.PermissionOverwrite())
        owner_overwrite.update(manage_channels=True, move_members=True, connect=True)
        overwrites[member] = owner_overwrite
        try:
            new_channel = await guild.create_voice_channel(
                name=f"{member.display_name}'s Channel",
                category=category,
                overwrites=overwrites,
                reason=f"Join-to-Create for {member} ({member.id})",
            )
        except nextcord.Forbidden:
            log.error("Missing Manage Channels permission — can't create Join-to-Create channel for %s", member)
            return
        except nextcord.HTTPException as e:
            log.error("Failed to create Join-to-Create channel for %s: %s", member, e)
            return

        self._created.add(new_channel.id)
        self._persist()

        try:
            await member.move_to(new_channel, reason="Join-to-Create")
        except (nextcord.Forbidden, nextcord.HTTPException) as e:
            # The member disconnected before we could move them, or the bot lacks
            # Move Members. Either way the fresh channel is empty and useless.
            log.warning("Couldn't move %s into their channel (%s); cleaning it up", member, e)
            await self._delete_channel(new_channel, reason="Join-to-Create: move failed")
            self._persist()

    async def _delete_channel(self, channel, reason):
        self._created.discard(channel.id)
        try:
            await channel.delete(reason=reason)
        except nextcord.NotFound:
            pass
        except (nextcord.Forbidden, nextcord.HTTPException) as e:
            log.error("Failed to delete Join-to-Create channel %s: %s", channel.id, e)


def setup(bot):
    bot.add_cog(JoinToCreate(bot))
