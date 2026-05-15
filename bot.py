import asyncio

import nextcord
from nextcord.ext import commands
import os

from config import BOT_TOKEN, SERVER_ID

intents = nextcord.Intents.all()
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Load cogs
async def load_cogs():
    cogs_dir = os.path.join(os.path.dirname(__file__), "cogs")
    for filename in os.listdir(cogs_dir):
        if filename.endswith('.py'):
            try:
                bot.load_extension(f'cogs.{filename[:-3]}')
                print(f"Successfully loaded {filename}")
            except Exception as e:
                print(f"Failed to load {filename}: {e}")

@bot.event
async def on_application_command_error(interaction, error):
    if isinstance(error, commands.CheckFailure):
        await interaction.response.send_message("You don't have permission to use this command.", ephemeral=True)
    else:
        raise error
    
# Dictionary to track users who interacted with the bot
user_interactions = {}

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    guild = bot.get_guild(SERVER_ID)
    if message.channel == guild.text_channels[5]:
        return

    if bot.user.mentioned_in(message) and not message.mention_everyone:
        user_id = message.author.id

        if user_id not in user_interactions:
            # First time mentioning the bot
            user_interactions[user_id] = 1
            response = (
                f"Hey {message.author.mention}, I'm your personal game dev assistant! "
                "I'm here to help because I know you're too lazy to do things yourself. "
                "What do you need?"
            )
        else:
            # Already interacted before, start bullying
            user_interactions[user_id] += 1
            insults = [
                f"Oh, it's you again, {message.author.mention}. Still too lazy to think for yourself?",
                f"Back so soon, {message.author.mention}? I guess even basic tasks are too much for you.",
                f"Wow, you again? Do you even try to work without my help?",
                f"At this point, {message.author.mention}, I'm doing more work than you.",
                f"You again? Is your entire workflow just asking me for help?",
                f"{message.author.mention}, your dependency on me is concerning. Maybe try using your brain?",
                f"Not this again... {message.author.mention}, you know Google exists, right?",
                f"I'm starting to think you're just a glorified keyboard smasher, {message.author.mention}.",
                f"Imagine if you put half as much effort into coding as you do asking me for help, {message.author.mention}.",
                f"{message.author.mention}, do you even open your IDE without calling me first?",
                f"You should put 'Discord Bot Dependent' on your resume, {message.author.mention}.",
                f"At this rate, I'm going to start charging you for my services, {message.author.mention}.",
                f"You're back? I thought you rage quit game dev already, {message.author.mention}.",
                f"{message.author.mention}, I swear, if you ask me one more thing, I'm reporting you to your manager (oh wait, you're indie).",
                f"Ever heard of Stack Overflow, {message.author.mention}? Or are you too scared to get downvoted?",
                f"You’d think after all this time, you’d have learned something, {message.author.mention}… but here we are.",
                f"If you keep this up, I’m going to start ignoring you, {message.author.mention}.",
                f"Your game better be a masterpiece after all this help, {message.author.mention}.",
                f"{message.author.mention}, if I had a nickel for every time you asked me for help, I'd have enough to fund your entire project.",
            ]
            response = insults[user_interactions[user_id] % len(insults)]  # Rotate insults

        await message.channel.send(response)

    await bot.process_commands(message)  # Ensure commands still work

@bot.event
async def on_ready():
    print(f"{bot.user.name} has connected to Discord!")

# Run the bot
if __name__ == "__main__":
    asyncio.run(load_cogs())
    bot.run(BOT_TOKEN)
