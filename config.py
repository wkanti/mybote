import os

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
LOG_CHAT_ID = int(os.getenv("LOG_CHAT_ID")) if os.getenv("LOG_CHAT_ID") else None
DB_PATH = os.getenv("DB_PATH", "battle.db")
