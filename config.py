import os

# -----------------------------
# 🔐 ZERODHA API
# -----------------------------
# Reads from Railway env vars — supports both naming conventions
API_KEY    = os.getenv("KITE_API_KEY")    or os.getenv("API_KEY")
API_SECRET = os.getenv("KITE_API_SECRET") or os.getenv("API_SECRET")
ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")

# -----------------------------
# 🔐 TELEGRAM
# -----------------------------
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# -----------------------------
# 🔐 AUTO LOGIN
# -----------------------------
USER_ID    = os.getenv("ZERODHA_USER_ID")   or os.getenv("USER_ID")
PASSWORD   = os.getenv("ZERODHA_PASSWORD")  or os.getenv("PASSWORD")
PIN        = os.getenv("PIN")
API_SECRET = os.getenv("KITE_API_SECRET")   or os.getenv("API_SECRET")

# -----------------------------
# 📊 TRADING CONFIG
# -----------------------------
NIFTY_LOT  = 1
CRUDE_LOT  = 1

STOP_LOSS = 0.30   # 30%
TARGET    = 0.50   # 50%

# -----------------------------
# 📈 INSTRUMENT TOKENS
# -----------------------------
NIFTY_TOKEN     = 256265
CRUDE_TOKEN     = 124544519
BANKNIFTY_TOKEN = 260105
SENSEX_TOKEN    = 265

USE_SCREENER            = False
SCREENER_SESSION_COOKIE = "tg3e1qszm56un498a6xvu0738zwhztbq"
SCREENER_SCREEN_ID      = "3617311"

# -----------------------------
# 💰 OPTION SELECTION
# -----------------------------
MIN_PREMIUM = 50
MAX_PREMIUM = 120

# -----------------------------
# RISK MANAGEMENT
# -----------------------------
MAX_DAILY_LOSS      = -3000
DAILY_TARGET        = 10000
MAX_TRADES          = 8
COOLDOWN_AFTER_LOSS = 300    # seconds

RISK_PER_TRADE = 0.02   # 2%
MAX_LOTS       = 1

RUN_BACKTEST = True

STRIKE_MODE     = "ATM"
MIN_CONFIDENCE  = 55

# -----------------------------
# PORTFOLIO RISK
# -----------------------------
MAX_PORTFOLIO_LOSS  = -5000
MAX_DRAWDOWN        = -3000
RISK_OFF_AFTER_LOSS = True
