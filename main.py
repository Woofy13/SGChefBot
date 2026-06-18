#!/usr/bin/env python3
import sys
import os
import asyncio
import threading
import time
from datetime import date
from http.server import HTTPServer, BaseHTTPRequestHandler
sys.path.insert(0, os.path.dirname(__file__))

# Fix for Windows event loop with httpx
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from config import TELEGRAM_BOT_TOKEN, DEEPSEEK_API_KEY, OWNER_TELEGRAM_ID
from database import init_db, add_called_ingredient, get_user_preference, set_user_preference, get_monthly_reminders, add_monthly_reminder
from bot import create_app


class HealthHandler(BaseHTTPRequestHandler):
    def _respond(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()

    def do_GET(self):
        self._respond()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self._respond()

    def log_message(self, format, *args):
        pass


def run_health_server():
    port = int(os.environ.get("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthHandler)
    server.serve_forever()


def expiry_reminder_loop(token):
    import asyncio
    from bot import send_monthly_reminders
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    sent = set()
    while True:
        time.sleep(43200)
        today_key = date.today().isoformat()
        if date.today().day == 1 and today_key not in sent:
            try:
                loop.run_until_complete(send_monthly_reminders(token))
                sent.add(today_key)
            except Exception as e:
                print(f"Expiry reminder error: {e}")


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set. Create a .env file or set the env variable.")
        sys.exit(1)
    if not DEEPSEEK_API_KEY or DEEPSEEK_API_KEY == "your_deepseek_api_key_here":
        print("ERROR: DEEPSEEK_API_KEY not set. Update .env with your DeepSeek API key.")
        print("Get one free at: https://platform.deepseek.com")
        sys.exit(1)

    health_thread = threading.Thread(target=run_health_server, daemon=True)
    health_thread.start()

    reminder_thread = threading.Thread(target=expiry_reminder_loop, args=(TELEGRAM_BOT_TOKEN,), daemon=True)
    reminder_thread.start()

    init_db()
    for ing in ["Cincalok", "Belacan", "Bubu"]:
        add_called_ingredient(OWNER_TELEGRAM_ID, ing)
    print("Database initialized")

    rb_path = os.path.join(os.path.dirname(__file__), "money_manager.md")
    if os.path.exists(rb_path):
        existing_rb = get_user_preference(OWNER_TELEGRAM_ID, "rulebook_md")
        if not existing_rb:
            try:
                with open(rb_path, "r", encoding="utf-8") as f:
                    set_user_preference(OWNER_TELEGRAM_ID, "rulebook_md", f.read())
                print("Loaded money_manager.md into user preferences")
            except Exception as e:
                print(f"Could not load money_manager.md: {e}")

    existing_reminders = get_monthly_reminders(OWNER_TELEGRAM_ID)
    if not existing_reminders:
        add_monthly_reminder(OWNER_TELEGRAM_ID, "Bill reminder! Check your monthly fees.", 15)
        print("Seeded monthly bill reminder (15th)")

    app = create_app(TELEGRAM_BOT_TOKEN)
    print("SG Chef Bot is running... (Ctrl+C to stop)")
    app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
