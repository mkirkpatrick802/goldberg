import nextcord
from nextcord.ext import commands, tasks
import datetime
import json
import os
import pytz

eastern = pytz.timezone("US/Eastern")
DATA_FILE = "goldberg/data/clock_data.json"

def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    try:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=4, default=str)
    except Exception as e:
        print(f"Error saving data to {DATA_FILE}: {e}")

class Clock(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data = load_data()
        self.message = None
        self.eastern = eastern

    @commands.Cog.listener()
    async def on_ready(self):
        if not self.status_updater.is_running():
            self.status_updater.start()

    def cog_unload(self):
        self.status_updater.cancel()

    def get_dt(self, key):
        val = self.data.get(key)
        if val:
            return datetime.datetime.fromisoformat(val).astimezone(self.eastern)
        return None

    def format_delta(self, td):
        total_seconds = int(td.total_seconds())
        if total_seconds < 0:
            return "Time's up!"
        days, remainder = divmod(total_seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        return f"{days}d {hours}h {minutes}m"

    @tasks.loop(seconds=60, reconnect=True)
    async def status_updater(self):
        print("[Status Updater] Running status update...")

        # Ensure we have a channel
        channel_id = self.data.get("channel_id")
        if not channel_id:
            print("[Status Updater] No channel_id set. Exiting.")
            return

        channel = self.bot.get_channel(channel_id)
        if not channel:
            print(f"[Status Updater] Channel ID {channel_id} not found. Exiting.")
            return

        message = None
        message_id = self.data.get("message_id")

        if message_id:
            try:
                message = await channel.fetch_message(message_id)
                print(f"[Status Updater] Fetched message with ID {message_id}.")
            except nextcord.NotFound:
                print(f"[Status Updater] Message ID {message_id} not found. Creating new one.")
                message = None

        # === Time Calculations ===
        now = datetime.datetime.now(self.eastern)
        sprint_end = self.get_dt("sprint_end")
        project_end = self.get_dt("project_end")
        last_accident = self.get_dt("last_accident")
        accident_reason = self.data.get("accident_reason", "No reason given.")
        last_commit_time = self.get_dt("last_commit_time")

        print(f"[Status Updater] Now: {now}")
        print(f"[Status Updater] Sprint End: {sprint_end}")
        print(f"[Status Updater] Project End: {project_end}")
        print(f"[Status Updater] Last Commit Time: {last_commit_time}")
        print(f"[Status Updater] Last Accident: {last_accident}")
        print(f"[Status Updater] Accident Reason: {accident_reason}")

        # === Message Building ===
        msg = "**Sprint ends in:** " + (self.format_delta(sprint_end - now) if sprint_end else "Not set.") + "\n"
        msg += "**Project ends in:** " + (self.format_delta(project_end - now) if project_end else "Not set.") + "\n"
        msg += "**Time since last commit:** " + (self.format_delta(now - last_commit_time) if last_commit_time else "Unknown") + "\n"
        msg += "**Time since last accident:** " + (self.format_delta(now - last_accident) if last_accident else "Unknown") + "\n"
        msg += f"> {accident_reason}\n"

        if message:
            await message.edit(content=msg)
            print("[Status Updater] Edited existing message.")
        else:
            new_msg = await channel.send(msg)
            self.data["message_id"] = new_msg.id
            save_data(self.data)
            print(f"[Status Updater] Sent new message and updated message_id to {new_msg.id}.")

    def set_last_commit_time(self, commit_time):
        """Set and save the last commit time as an ISO 8601 UTC string."""
        if isinstance(commit_time, datetime.datetime):
            # Always store as UTC in ISO format
            commit_time_utc = commit_time.astimezone(datetime.timezone.utc)
            self.data["last_commit_time"] = commit_time_utc.isoformat()
        else:
            raise ValueError("commit_time must be a datetime object")
        save_data(self.data)

    @commands.command()
    async def set_sprint_end(self, ctx, *, time: str):
        """Set sprint end time in EST (format: YYYY-MM-DD HH:MM)"""
        dt_local = datetime.datetime.strptime(time, "%Y-%m-%d %H:%M")
        dt_eastern = eastern.localize(dt_local)
        dt_utc = dt_eastern.astimezone(datetime.timezone.utc)
        self.data["sprint_end"] = dt_utc.isoformat()
        save_data(self.data)
        await ctx.send(f"✅ Sprint end time set to {dt_eastern.strftime('%Y-%m-%d %H:%M %Z')}.")

    @commands.command()
    async def set_project_end(self, ctx, *, time: str):
        """Set project end time in EST (format: YYYY-MM-DD HH:MM)"""
        dt_local = datetime.datetime.strptime(time, "%Y-%m-%d %H:%M")
        dt_eastern = eastern.localize(dt_local)
        dt_utc = dt_eastern.astimezone(datetime.timezone.utc)
        self.data["project_end"] = dt_utc.isoformat()
        save_data(self.data)
        await ctx.send(f"✅ Project end time set to {dt_eastern.strftime('%Y-%m-%d %H:%M %Z')}.")

    @nextcord.slash_command(
        name="accident",
        description="Report an accident.",
        guild_ids=[1353924127303532648]
    )
    async def accident(self, ctx, *, reason: str):
        """Log an accident and reset the timer."""
        now = datetime.datetime.now(datetime.timezone.utc)
        self.data["last_accident"] = now.isoformat()
        self.data["accident_reason"] = reason
        save_data(self.data)
        await ctx.send("⚠️ Accident recorded.")

    @commands.command()
    async def set_status_channel(self, interaction: nextcord.Interaction):

        # Check if the user is authorized
        authorized_users = [334703021873430528, 198553109012807680]
        if interaction.author.id not in authorized_users:
            await interaction.response.send_message(
                "❌ You don't have permission to use this command.",
                ephemeral=True
            )
            return

        # Defer the response so we can follow up later
        await interaction.response.defer(ephemeral=True)

        # Send the actual status message in the channel
        msg = await interaction.channel.send("🔄 Status will appear here.")

        # Save channel and message ID
        self.data["channel_id"] = interaction.channel.id
        self.data["message_id"] = msg.id
        save_data(self.data)

        # Follow up with confirmation
        await interaction.followup.send("✅ Status message location saved.", ephemeral=True)

async def setup(bot):
    bot.add_cog(Clock(bot))