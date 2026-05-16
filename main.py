"""
Entry point for running the Discord bot on any host (Render, Railway, Heroku, etc.)
Usage: python main.py
Required env vars: DISCORD_BOT_TOKEN, OPENROUTER_API_KEY, GENIUS_ACCESS_TOKEN
"""
import os
import sys

# Ensure the bot code is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "discord-bot"))

# Run the bot directly
from bot import bot, DISCORD_BOT_TOKEN

if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN, log_handler=None)
