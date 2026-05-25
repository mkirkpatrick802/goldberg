import nextcord
from nextcord.ext import commands
import json
import os
import random

DATA_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "bully_data.json")
SETUP_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "setup_data.json")

def load_data():
    if not os.path.exists(DATA_FILE):
        return {}
    with open(DATA_FILE, "r") as f:
        return json.load(f)

def save_data(data):
    os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=4)

def load_setup():
    if not os.path.exists(SETUP_FILE):
        return {}
    with open(SETUP_FILE, "r") as f:
        return json.load(f)

INSULTS = [
    "Oh, it's you again, {mention}. Still too lazy to think for yourself?",
    "Back so soon, {mention}? I guess even basic tasks are too much for you.",
    "Wow, you again? Do you even try to work without my help?",
    "At this point, {mention}, I'm doing more work than you.",
    "You again? Is your entire workflow just asking me for help?",
    "{mention}, your dependency on me is concerning. Maybe try using your brain?",
    "Not this again... {mention}, you know Google exists, right?",
    "I'm starting to think you're just a glorified keyboard smasher, {mention}.",
    "Imagine if you put half as much effort into coding as you do asking me for help, {mention}.",
    "{mention}, do you even open your IDE without calling me first?",
    "You should put 'Discord Bot Dependent' on your resume, {mention}.",
    "At this rate, I'm going to start charging you for my services, {mention}.",
    "You're back? I thought you rage quit game dev already, {mention}.",
    "{mention}, I swear, if you ask me one more thing, I'm reporting you to your manager (oh wait, you're indie).",
    "Ever heard of Stack Overflow, {mention}? Or are you too scared to get downvoted?",
    "You'd think after all this time, you'd have learned something, {mention}… but here we are.",
    "If you keep this up, I'm going to start ignoring you, {mention}.",
    "Your game better be a masterpiece after all this help, {mention}.",
    "{mention}, if I had a nickel for every time you asked me for help, I'd have enough to fund your entire project.",
]

RANDOM_REPLIES = [
    "Ah yes, more words from {mention}. Groundbreaking.",
    "Did anyone ask? I'm asking for a friend.",
    "Fascinating. Truly. I'm so glad you shared that.",
    "Cool story. Needs more dragons.",
    "I've seen better takes in a fortune cookie.",
    "Hey! This is a game dev server.",
    "Okay but have you tried turning your brain off and on again?",
    "I'm not saying you're wrong, but I'm not saying you're right either.",
    "This message has been noted and will be ignored.",
    "Bold words from someone who ships on deadline.",
]

RANDOM_EMOJIS = ["💀", "🤡", "😐", "🫠", "💅", "🙃", "😬", "🧐", "🫡", "👀", "🤌", "😶"]

class Bully(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.data = load_data()

    def get_user(self, user_id: str):
        if user_id not in self.data:
            self.data[user_id] = {
                "interaction_count": 0,
                "message_count": 0,
            }
        return self.data[user_id]

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return

        user_id = str(message.author.id)
        user = self.get_user(user_id)
        setup = load_setup()
        bully_channels = setup.get("bully_channels", [])
        in_bully_channel = message.channel.id in bully_channels

        # Track message count
        user["message_count"] += 1
        save_data(self.data)

        # Handle mentions
        if self.bot.user.mentioned_in(message) and not message.mention_everyone:
            user["interaction_count"] += 1
            save_data(self.data)

            count = user["interaction_count"]
            if count == 1:
                response = (
                    f"Hey {message.author.mention}, I'm your personal game dev assistant! "
                    "I'm here to help because I know you're too lazy to do things yourself. "
                    "What do you need?"
                )
            else:
                insult = INSULTS[(count - 2) % len(INSULTS)]
                response = insult.format(mention=message.author.mention)

            await message.channel.send(response)
            return

        # Random behavior in bully channels only
        if in_bully_channel:
            roll = random.random()
            if roll < 0.01:
                reply = random.choice(RANDOM_REPLIES)
                await message.channel.send(reply.format(mention=message.author.mention))
            elif roll < 0.10:
                emoji = random.choice(RANDOM_EMOJIS)
                await message.add_reaction(emoji)

async def setup(bot):
    bot.add_cog(Bully(bot))