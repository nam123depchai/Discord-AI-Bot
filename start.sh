#!/bin/bash
set -e

# Start Discord bot in the background
python3 discord-bot/bot.py &
BOT_PID=$!
echo "Discord bot started (PID $BOT_PID)"

# Start the API server in the foreground (keeps the container alive)
exec node --enable-source-maps artifacts/api-server/dist/index.mjs
