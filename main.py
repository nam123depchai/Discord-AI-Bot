"""
Entry point for running the Discord bot on any host (Render, Railway, Heroku, etc.)
Usage: python main.py
Required env vars: DISCORD_BOT_TOKEN, OPENROUTER_API_KEY, GENIUS_ACCESS_TOKEN
"""
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

# Ensure the bot code is on the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "discord-bot"))

# --- Tiny HTTP listener so Render (and similar hosts) see an open port ---
PORT = int(os.environ.get("PORT", 8080))

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bird Bot is alive!")
    def log_message(self, fmt, *args):
        pass  # Suppress noisy HTTP logs

def start_http():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    server.serve_forever()

# Start HTTP server in a background thread
threading.Thread(target=start_http, daemon=True).start()

# --- Run the Discord bot (blocks forever) ---
from bot import bot, DISCORD_BOT_TOKEN

if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN, log_handler=None)
