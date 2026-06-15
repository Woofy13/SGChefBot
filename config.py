import os
from dotenv import load_dotenv

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
GROQ_BASE_URL = os.getenv("GROQ_BASE_URL", "https://api.groq.com/openai/v1")
GROQ_MODEL = os.getenv("GROQ_MODEL", "llama-3.1-8b-instant")
GROQ_QUALITY_MODEL = os.getenv("GROQ_QUALITY_MODEL", "qwen/qwen3-32b")
GROQ_VISION_MODEL = os.getenv("GROQ_VISION_MODEL", "llama-3.2-11b-vision-preview")
DB_PATH = os.path.join(os.path.dirname(__file__), "sgchefbot.db")
OWNER_TELEGRAM_ID = int(os.getenv("OWNER_TELEGRAM_ID", "216186895"))
