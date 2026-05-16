"""
Entry point for running the Discord bot on any host (Render, Railway, Heroku, etc.)
Usage: python main.py
Required env vars: DISCORD_BOT_TOKEN, OPENROUTER_API_KEY, GENIUS_ACCESS_TOKEN
"""
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

# --- Tiny HTTP listener: bind FIRST so Render sees the port immediately ---
PORT = int(os.environ.get("PORT", 8080))

class HealthHandler(BaseHTTPRequestHandler):
    def _ok(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()

    def do_GET(self):
        self._ok()
        self.wfile.write(b"Bird Bot is alive!")

    def do_HEAD(self):
        self._ok()

    def log_message(self, fmt, *args):
        pass

# Bind the socket NOW (before any heavy imports)
httpd = HTTPServer(("0.0.0.0", PORT), HealthHandler)
http_thread = threading.Thread(target=httpd.serve_forever, daemon=False)
http_thread.start()
print(f"[HTTP] Health check bound to 0.0.0.0:{PORT}")

# Give the HTTP server a moment to start listening
# (Render scans soon after the process starts)
time.sleep(0.5)

# --- Discord bot imports (heavy) ---
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "discord-bot"))
from bot import bot, DISCORD_BOT_TOKEN

# --- Run the Discord bot (blocks the main thread forever) ---
if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN, log_handler=None)
