import os

from dotenv import load_dotenv


load_dotenv()

MANAGER_ID = os.getenv("MANAGER_ID", "").strip()


# =========================
# TELEGRAM
# =========================

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH")
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE")


# =========================
# OPENAI
# =========================

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# =========================
# ARBITRATION API
# =========================

PARSER_API_KEY = os.getenv("PARSER_API_KEY", "").strip()


# =========================
# PROJECT
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")

SESSION_NAME = os.getenv("SESSION_NAME", "telegram_sender").strip()

DATABASE_PATH = "data/local.sqlite"

PROMPTS_DIR = "prompts"

CLIENT_FOLDER_PREFIX = "Клиенты"


# =========================
# EXCEL
# =========================

EXCEL_REQUIRED_COLUMNS = [
    "ИНН",
    "ФИО",
    "Юзернейм",
    "Продукт",
]