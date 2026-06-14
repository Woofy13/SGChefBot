#!/usr/bin/env python3
import sys
import os
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
sys.path.insert(0, os.path.dirname(__file__))

# Fix for Windows event loop with httpx
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from config import TELEGRAM_BOT_TOKEN, GROQ_API_KEY
from database import init_db
from bot import create_app


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def log_message(self, format, *args):
        pass


def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set. Create a .env file or set the env variable.")
        sys.exit(1)
    if not GROQ_API_KEY or GROQ_API_KEY == "your_groq_api_key_here":
        print("ERROR: GROQ_API_KEY not set. Update .env with your Groq API key.")
        print("Get one free at: https://console.groq.com")
        sys.exit(1)

    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()

    init_db()
    print("Database initialized")

    app = create_app(TELEGRAM_BOT_TOKEN)
    print("SG Chef Bot is running... (Ctrl+C to stop)")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
