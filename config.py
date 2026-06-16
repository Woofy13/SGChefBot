import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
DB_PATH = os.path.join(os.path.dirname(__file__), "sgchefbot.db")
OWNER_TELEGRAM_ID = int(os.getenv("OWNER_TELEGRAM_ID", "216186895"))
