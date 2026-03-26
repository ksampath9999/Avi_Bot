import os

# -----------------------------
# 🔐 ZERODHA API
# -----------------------------
API_KEY = os.getenv("API_KEY")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")   # used in cloud OR local

# -----------------------------
# 🔐 TELEGRAM
# -----------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# -----------------------------
# 🔐 AUTO LOGIN (LOCAL ONLY)
# ⚠️ DO NOT USE IN CLOUD
# -----------------------------
USER_ID = os.getenv("USER_ID")
PASSWORD = os.getenv("PASSWORD")
PIN = os.getenv("PIN")
API_SECRET = os.getenv("API_SECRET")

# -----------------------------
# 📊 TRADING CONFIG
# -----------------------------
LOT_SIZE = 50   # NIFTY lot size (keep updated)

MAX_TRADES = 3

RISK_PER_TRADE = 0.02   # 2%
MAX_DAILY_LOSS = 3000

STOP_LOSS = 0.30   # 30%
TARGET = 0.50      # 50%

# -----------------------------
# 📈 INSTRUMENT
# -----------------------------
NIFTY_TOKEN = 256265

# -----------------------------
# 💰 OPTION SELECTION
# -----------------------------
MIN_PREMIUM = 50
MAX_PREMIUM = 120
