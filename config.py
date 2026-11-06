"""Configuration loaded from environment variables (.env)."""
import os

from dotenv import load_dotenv

load_dotenv()


def _int_list(value: str) -> list[int]:
    return [int(x) for x in value.replace(" ", "").split(",") if x]


# --- Required -------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_IDS = _int_list(os.getenv("ADMIN_IDS", ""))

# --- General --------------------------------------------------------------
DATABASE_PATH = os.getenv("DATABASE_PATH", "store.db")
CURRENCY = os.getenv("CURRENCY", "₹")
# Optional channel/group ID where sales and deposits are logged (e.g. -1001234567890)
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID") or 0) or None
# Time zone offset used when showing dates and computing "today" (330 = IST)
TZ_OFFSET_MINUTES = int(os.getenv("TZ_OFFSET_MINUTES", "330"))
# Minutes a UPI payment request stays valid
PAYMENT_TIMEOUT_MIN = int(os.getenv("PAYMENT_TIMEOUT_MIN", "15"))
# Max pending deposits per user at once
MAX_PENDING_DEPOSITS = int(os.getenv("MAX_PENDING_DEPOSITS", "3"))
# Anti-spam: max actions per user in a 10 second window
RATE_LIMIT = int(os.getenv("RATE_LIMIT", "15"))

# --- Optional: automatic OTP fetching via Telethon sessions ---------------
# Get these from https://my.telegram.org
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH", "")

# --- Defaults for settings editable from the admin panel -----------------
DEFAULT_SETTINGS = {
    "store_name": os.getenv("STORE_NAME", "Premium Account Store"),
    "upi_id": os.getenv("UPI_ID", ""),
    "upi_name": os.getenv("UPI_NAME", ""),
    "support": os.getenv("SUPPORT_CONTACT", ""),
    "min_deposit": os.getenv("MIN_DEPOSIT", "50"),
    "ref_percent": os.getenv("REFERRAL_PERCENT", "5"),
    "otp_hours": os.getenv("OTP_WINDOW_HOURS", "24"),
    "maintenance": "0",
}
