import requests
import time
import datetime
import pytz
import pandas as pd
import threading
from kiteconnect import KiteConnect
import config
from telegram_bot import send_message
import csv
import os
import json
import re
import pyotp
from datetime import datetime, timedelta

# ── FIX: CSV header matches log_trade_full() column order exactly ──
if not os.path.exists("trade_log.csv"):
    with open("trade_log.csv", "w") as f:
        f.write("time,instrument,symbol,signal,entry,exit,pnl,probability\n")



bot_started = False
lock = threading.Lock()
IST = pytz.timezone("Asia/Kolkata")
_KITE_API_KEY = os.environ.get("KITE_API_KEY") or config.API_KEY
kite = KiteConnect(api_key=_KITE_API_KEY)

# ─────────────────────────────────────────────────────────────────────────────
# 🔑 AUTO LOGIN — Zerodha TOTP + Kite Connect token refresh
# ─────────────────────────────────────────────────────────────────────────────
# Reads these env vars (set in Railway → service → Variables):
#   ZERODHA_USER_ID       e.g. AB1234
#   ZERODHA_PASSWORD      your Zerodha login password
#   ZERODHA_TOTP_SECRET   TOTP secret from Zerodha authenticator setup
#   KITE_API_SECRET       Kite Connect API secret
# Falls back to config.ACCESS_TOKEN if any env var is missing (backward compat).

def zerodha_auto_login():
    """
    Full automated Zerodha login flow:
      1. POST credentials → get request_id
      2. Generate TOTP from ZERODHA_TOTP_SECRET → complete 2FA
      3. Follow Kite Connect login redirect → extract request_token
      4. kite.generate_session(request_token) → get fresh access_token
      5. Apply token to global kite object
    Returns the new access_token string, or None on failure.
    """
    user_id     = os.environ.get("ZERODHA_USER_ID")
    password    = os.environ.get("ZERODHA_PASSWORD")
    totp_secret = os.environ.get("ZERODHA_TOTP_SECRET")
    api_secret  = (os.environ.get("KITE_API_SECRET") or
                   os.environ.get("API_SECRET") or
                   getattr(config, "API_SECRET", None) or
                   getattr(config, "SECRET", None))

    if not all([user_id, password, totp_secret, api_secret]):
        print("⚠️  Auto-login env vars not set — falling back to config.ACCESS_TOKEN")
        kite.set_access_token(config.ACCESS_TOKEN)
        return None

    session = requests.Session()
    session.headers.update({
        "User-Agent"     : "Mozilla/5.0",
        "Content-Type"   : "application/x-www-form-urlencoded",
        "X-Kite-Version" : "3",
    })

    try:
        # ── Step 1: POST login credentials ──────────────────────────────────
        print("🔐 Auto-login: posting credentials...")
        resp = session.post(
            "https://kite.zerodha.com/api/login",
            data={"user_id": user_id, "password": password},
            timeout=15,
        )
        data = resp.json()
        if data.get("status") != "success":
            raise Exception(f"Credential login failed: {data.get('message')}")

        request_id = data["data"]["request_id"]
        twofa_type = data["data"].get("twofa_type", "totp")
        print(f"   request_id: {request_id}  |  2FA: {twofa_type}")

        # ── Step 2: TOTP 2FA ────────────────────────────────────────────────
        # If we're in the last 3 seconds of the 30-second window, wait for
        # the next window to avoid submitting a code that expires in-flight.
        totp_obj = pyotp.TOTP(totp_secret)
        seconds_remaining = 30 - (int(time.time()) % 30)
        if seconds_remaining <= 3:
            print(f"   ⏳ Near window boundary ({seconds_remaining}s left) — waiting for next window...")
            time.sleep(seconds_remaining + 1)

        totp_value = totp_obj.now()
        print(f"   TOTP generated: {totp_value} (window: {30 - (int(time.time()) % 30)}s remaining)")

        data2 = None
        for attempt in range(1, 4):
            resp2 = session.post(
                "https://kite.zerodha.com/api/twofa",
                data={
                    "user_id"      : user_id,
                    "request_id"   : request_id,
                    "twofa_value"  : totp_obj.now(),   # regenerate on each attempt
                    "twofa_type"   : twofa_type,
                    "skip_session" : "",
                },
                timeout=15,
            )
            data2 = resp2.json()
            print(f"   2FA attempt {attempt}: {data2.get('status')} | code: {totp_obj.now()}")
            if data2.get("status") == "success":
                break
            # Wait for the next 30-second window and retry with a fresh code
            wait_secs = 30 - (int(time.time()) % 30) + 1
            print(f"   ⏳ Waiting {wait_secs}s for next TOTP window (attempt {attempt}/3)...")
            time.sleep(wait_secs)

        if not data2 or data2.get("status") != "success":
            raise Exception(f"2FA failed after 3 attempts: {data2.get('message') if data2 else 'No response'}")
        print(f"   ✅ 2FA passed | full response: {data2}", flush=True)
        print(f"   Session cookies after 2FA: {dict(session.cookies)}", flush=True)
        import sys; sys.stdout.flush()

        # ── Step 3: Get request_token via Kite Connect redirect ─────────────
        import sys
        print("   Getting request_token from Kite Connect redirect...", flush=True)
        _api_key = os.environ.get("KITE_API_KEY") or config.API_KEY
        print(f"   Using api_key: {_api_key[:6]}... (len={len(str(_api_key))})", flush=True)
        login_url = f"https://kite.zerodha.com/connect/login?api_key={_api_key}&v=3"

        request_token = None

        # ── 3a: Hop 1 — /connect/login → /connect/finish ────────────────────
        resp_login = session.get(login_url, allow_redirects=False, timeout=15)
        loc1 = resp_login.headers.get("Location", "")
        print(f"   [hop1] status={resp_login.status_code} | loc={loc1[:150]}", flush=True)
        print(f"   [hop1] body={resp_login.text[:200]}", flush=True)
        sys.stdout.flush()

        m = re.search(r"request_token=([^&]+)", loc1)
        if m:
            request_token = m.group(1)

        # ── 3b: Hop 2 — /connect/finish → app redirect URL ──────────────────
        if not request_token and loc1:
            finish_url = loc1 if loc1.startswith("http") else "https://kite.zerodha.com" + loc1
            resp_finish = session.get(finish_url, allow_redirects=False, timeout=15)
            loc2 = resp_finish.headers.get("Location", "")
            print(f"   [hop2] status={resp_finish.status_code} | loc={loc2[:150]}", flush=True)
            print(f"   [hop2] body={resp_finish.text[:400]}", flush=True)
            sys.stdout.flush()

            m = re.search(r"request_token=([^&\"']+)", loc2)
            if m:
                request_token = m.group(1)

            if not request_token:
                m = re.search(r"request_token[\"']?\s*[:=]\s*[\"']?([A-Za-z0-9]+)", resp_finish.text)
                if m:
                    request_token = m.group(1)

            # ── 3c: Hop 3 — follow app redirect URL ─────────────────────────
            if not request_token and loc2:
                try:
                    app_url = loc2 if loc2.startswith("http") else "https://kite.zerodha.com" + loc2
                    resp_app = session.get(app_url, allow_redirects=False, timeout=10)
                    loc3 = resp_app.headers.get("Location", "")
                    print(f"   [hop3] status={resp_app.status_code} | loc={loc3[:150]}", flush=True)
                    print(f"   [hop3] body={resp_app.text[:400]}", flush=True)
                    sys.stdout.flush()
                    m = re.search(r"request_token=([^&\"']+)", loc3 + resp_app.text)
                    if m:
                        request_token = m.group(1)
                except Exception as e:
                    print(f"   [hop3] error: {e}", flush=True)
                    sys.stdout.flush()

        if not request_token:
            raise Exception(
                "request_token not found. Check hop1/hop2/hop3 debug lines above."
            )
        print(f"   request_token: {request_token[:10]}...", flush=True)

        # ── Step 4: Generate access_token ───────────────────────────────────
        # Re-read api_secret here in case env var wasn't loaded at function entry
        if not api_secret:
            api_secret = (os.environ.get("KITE_API_SECRET") or
                          os.environ.get("API_SECRET") or
                          getattr(config, "API_SECRET", None) or
                          getattr(config, "SECRET", None))
        print(f"   api_secret: {'SET len='+str(len(api_secret)) if api_secret else 'NONE — check KITE_API_SECRET in Railway'}", flush=True)
        print(f"   kite.api_key: {kite.api_key}", flush=True)
        if not api_secret:
            raise Exception("KITE_API_SECRET is not set or empty in Railway env vars.")
        # Ensure kite object has the correct api_key from env var
        kite.api_key = _api_key
        session_data = kite.generate_session(request_token, api_secret=api_secret)
        access_token = session_data["access_token"]
        kite.set_access_token(access_token)
        print(f"✅ Auto-login successful | token: {access_token[:8]}...{access_token[-4:]}")

        # ── Notify ML server of fresh token ──────────────────────────────────
        # Use globals().get() to safely read USE_ML_FILTER and ML_SERVER_URL —
        # this function is called at module load (line ~233) before those
        # constants are defined at line ~257, so a direct reference would raise
        # NameError, get caught by the except below, and silently overwrite the
        # freshly-set access token with the stale config.ACCESS_TOKEN.
        _use_ml  = globals().get("USE_ML_FILTER", False)
        _ml_url  = globals().get("ML_SERVER_URL", "")
        if _use_ml and _ml_url:
            try:
                resp = requests.post(
                    f"{_ml_url}/set_token",
                    json={"access_token": access_token},
                    timeout=5
                )
                print(f"🤖 ML server token update: {resp.json()}", flush=True)
            except Exception as _ml_e:
                print(f"⚠️ Could not notify ML server: {_ml_e}", flush=True)

        return access_token

    except Exception as e:
        import traceback
        print(f"❌ Auto-login failed: {e}", flush=True)
        print(traceback.format_exc(), flush=True)
        # Fallback: use last known token from config so bot can still attempt to run.
        # Only apply fallback if the kite object does NOT already have a fresh token
        # (i.e. the exception happened BEFORE kite.set_access_token was called).
        _current = getattr(kite, "access_token", None)
        _cfg_tok = getattr(config, "ACCESS_TOKEN", None)
        if not _current or _current == _cfg_tok:
            try:
                kite.set_access_token(config.ACCESS_TOKEN)
                print("⚠️  Falling back to config.ACCESS_TOKEN", flush=True)
            except Exception:
                pass
        raise   # re-raise so caller can capture the error message


# ── Initial login at startup ─────────────────────────────────────────────────
try:
    _startup_token = zerodha_auto_login()
except Exception:
    _startup_token = None

# 🌐 PRINT RAILWAY PUBLIC IP
try:
    ip = requests.get("https://api.ipify.org", timeout=5).text.strip()
    print("🌐 Railway Public IP:", ip)
except Exception as e:
    print("❌ IP fetch failed:", e)


# ─────────────────────────────────────────────────────────────────────────────
# 🌐 AUTO IP WHITELIST — updates Kite developer app IP on every deploy
#
# Required Railway env vars:
#   KITE_DEV_USER_ID  — your Zerodha user ID (same as trading login)
#   KITE_DEV_PASSWORD — your Zerodha password
#   KITE_APP_ID       — numeric app ID from developers.kite.trade URL
#                       e.g. https://developers.kite.trade/apps/12345 → 12345
# ─────────────────────────────────────────────────────────────────────────────
def update_kite_ip_whitelist():
    """
    Fetch Railway's current public IP and whitelist it in the Kite Connect
    developer app settings. Safe to call on every startup — if IP hasn't
    changed, Kite simply accepts the same value.
    """
    dev_user = os.environ.get("KITE_DEV_USER_ID")
    dev_pass = os.environ.get("KITE_DEV_PASSWORD")
    app_id   = os.environ.get("KITE_APP_ID")

    if not all([dev_user, dev_pass, app_id]):
        print("⚠️ IP whitelist auto-update skipped — set KITE_DEV_USER_ID / "
              "KITE_DEV_PASSWORD / KITE_APP_ID in Railway env vars", flush=True)
        return False

    try:
        # ── Step 1: Get current Railway public IP ─────────────────────────────
        current_ip = requests.get("https://api.ipify.org", timeout=5).text.strip()
        print(f"🌐 Railway IP for whitelist: {current_ip}", flush=True)

        sess = requests.Session()
        sess.headers.update({"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"})

        # ── Step 2: Login to Kite developer portal ───────────────────────────
        r1 = sess.post(
            "https://developers.kite.trade/api/users/signin",
            json={"user_id": dev_user, "password": dev_pass},
            timeout=10
        )
        d1 = r1.json()
        if d1.get("status") != "success":
            print(f"❌ Kite dev portal login failed: {d1.get('message')}", flush=True)
            return False

        print(f"✅ Kite dev portal logged in", flush=True)

        # ── Step 3: Update IP whitelist for the app ──────────────────────────
        r2 = sess.put(
            f"https://developers.kite.trade/api/apps/{app_id}",
            json={"ip_whitelist": [current_ip]},
            timeout=10
        )
        d2 = r2.json()
        if d2.get("status") == "success":
            print(f"✅ Kite IP whitelist updated → {current_ip}", flush=True)
            try:
                send_message(f"🌐 Railway IP auto-whitelisted\n{current_ip}")
            except Exception:
                pass
            return True
        else:
            print(f"❌ IP whitelist update failed: {d2}", flush=True)
            try:
                send_message(f"❌ IP whitelist update failed\n{d2.get('message', str(d2))}")
            except Exception:
                pass
            return False

    except Exception as e:
        import traceback
        print(f"❌ IP whitelist error: {e}", flush=True)
        print(traceback.format_exc(), flush=True)
        return False


# Run at startup — updates IP immediately when Railway redeploys
threading.Thread(target=update_kite_ip_whitelist, daemon=True).start()

SIGNAL_URL = "https://avi-bot-1.onrender.com/signal"

# ─────────────────────────────────────────────────────────────────────────────
# 🔒 FIXED LOT MODE — set to False to enable balance-based lot sizing
# While True : every order = exactly 1 lot (NIFTY 65 qty, CRUDE 100 qty)
# While False: calculate_lots() uses balance/risk model automatically
# ─────────────────────────────────────────────────────────────────────────────
FIXED_LOT_MODE = True   # ← change to False when ready for balance-based sizing

# ── Strategy Filters ─────────────────────────────────────────────────────────
# Toggle each filter on/off with True/False.
# All filters must pass before an order is placed.
# ─────────────────────────────────────────────────────────────────────────────
USE_ADX_FILTER     = False   # ✅ ACTIVE — only enter when ADX >= ADX_MIN_VALUE
ADX_MIN_VALUE      = 22     # Kite API data gives ~3 pts lower ADX than TradingView
                            # TV shows 16.94 → Kite gives ~13.6; threshold=12 filters
                            # only true choppy/sideways (TV ADX would be ~15 or below)

# ── 9/15 EMA Second Signal Source ────────────────────────────────────────────
# Both HalfTrend AND 9/15 EMA must agree before an order is placed.
# CALL entry: 9 EMA must be above 15 EMA (bullish alignment)
# PUT  entry: 9 EMA must be below 15 EMA (bearish alignment)
# Applies to both NIFTY and CRUDE on 15-min chart.
# Set False to disable and trade on HalfTrend signal alone.
# ─────────────────────────────────────────────────────────────────────────────
USE_EMA_FILTER     = True   # ✅ ACTIVE — 9/15 EMA must confirm HalfTrend signal

USE_MTF_FILTER     = False  # 1-hour HalfTrend confirmation (off — HalfTrend already
                            # handles direction; ADX handles the real failure mode)

USE_VIX_FILTER     = False  # India VIX range filter (off)
VIX_MIN            = 11     # (used only when USE_VIX_FILTER = True)
VIX_MAX            = 22     # (used only when USE_VIX_FILTER = True)

USE_SESSION_FILTER = False  # Session dead-zone filter (off)

# ── Instrument Enable / Disable ───────────────────────────────────────────────
# Set False to completely stop trading that instrument.
# The loop stays running (no restart needed) — just skips all entries.
# Flip back to True to resume immediately on next cycle.
ENABLE_NIFTY      = True    # ✅ NIFTY trading active
ENABLE_BANKNIFTY  = False   # ✅ BANKNIFTY trading active
ENABLE_SENSEX     = False    # ✅ SENSEX trading active
ENABLE_CRUDE      = False    # ✅ CRUDE trading active

# ── Daily Trade Limits ────────────────────────────────────────────────────────
MAX_NIFTY_TRADES_PER_DAY      = 4   # NIFTY:     max 4 trades/day
MAX_BANKNIFTY_TRADES_PER_DAY  = 4   # BANKNIFTY: max 4 trades/day
MAX_SENSEX_TRADES_PER_DAY     = 4   # SENSEX:    max 4 trades/day
MAX_CRUDE_TRADES_PER_DAY      = 3   # CRUDE:     max 3 trades/day

# ── Stop Loss ─────────────────────────────────────────────────────────────────
# Set False to disable ALL SL logic (trailing + hard SL).
# Profit-lock logic is NOT affected — it always stays active.
# Re-enable by setting back to True.
USE_STOP_LOSS = False           # ⛔ SL disabled — profit lock only

# ── ML Signal Filter ──────────────────────────────────────────────────────────
# ml_signal_server.py must be running (separate Railway/Render service).
# The bot calls /signal before each NIFTY entry:
#   • ML must AGREE with HalfTrend direction (CALL/PUT)
#   • ML confidence must be >= ML_MIN_CONFIDENCE
#   • If ML returns HOLD → entry is skipped
#   • If ML server is unreachable and ML_REQUIRED=False → bot trades anyway
# ─────────────────────────────────────────────────────────────────────────────
USE_ML_FILTER      = False  # ⛔ ML filter disabled — HalfTrend signal only
ML_SERVER_URL      = "https://avibot-production.up.railway.app"   # ML signal server URL
ML_MIN_CONFIDENCE  = 50     # minimum ML confidence % to allow entry
ML_REQUIRED        = False  # False = trade even if ML server is down (safe fallback)

# -----------------------------
# STATES
# -----------------------------
nifty_active = False
crude_active = False

trade_in_progress_nifty = False
trade_in_progress_crude = False

# 🔥 TREND MEMORY (NEW)
last_trend_nifty = None
last_trend_crude = None

global_trade_active = False

# -----------------------------
# RISK VARIABLES
# -----------------------------
daily_pnl = 0
trade_count = 0
last_loss_time = None
last_reset_date = None

MAX_DRAWDOWN = -3000   # adjust based on capital
win_streak = 0
loss_streak = 0

last_signal_nifty = None
last_signal_crude = None

last_trade_time_nifty = 0
last_trade_time_crude = 0

SIGNAL_COOLDOWN = 90  
alert_sent = False
last_analysis_time = 0

portfolio_pnl = 0
peak_portfolio = 0
risk_off = False

data_cache = {}
CACHE_TTL = 20  # seconds

report_sent_today = False
max_drawdown = 0
HARD_STOP_LOSS = -5000

trade_alert_sent = {
    "max_trades": False,
    "max_loss": False,
    "target_hit": False
}

instrument_cache = {}

ltp_cache = {}
LTP_TTL = 3  # seconds

quote_cache = {}
QUOTE_TTL = 3

CRUDE_TOKEN = None
NIFTY_FUT_TOKEN = None

ml_cache = {"time": 0, "data": None}
ML_CACHE_TTL = 2  # seconds


last_executed_signal_nifty = None
last_exit_time_crude = 0
last_exit_time_nifty = 0
REENTRY_COOLDOWN = 600  # 10 min
CRUDE_SYMBOL = None
pyramid_done = False

TRADE_LOG_FILE = "trade_log.csv"
last_executed_signal_crude = None
CRUDE_TOKEN = config.CRUDE_TOKEN
BANKNIFTY_TOKEN = getattr(config, "BANKNIFTY_TOKEN", None)
SENSEX_TOKEN    = getattr(config, "SENSEX_TOKEN",    None)
last_log_time = 0
last_running_signal = None
performance_log = []

current_symbol = None
current_qty = 0
current_exchange = None

adaptive_config = {
    "prob_threshold": 38,
    "trend_threshold": 0.0015,
    "risk_multiplier": 1.0
}

strategy_log = {
    "TREND": [],
    "SIDEWAYS": [],
    "VOLATILE": [],
    "NORMAL": []
}

strategy_status = {
    "TREND": True,
    "SIDEWAYS": True,
    "VOLATILE": True,
    "NORMAL": True
}

strategy_weights = {
    "TREND": 1.0,
    "SIDEWAYS": 0.6,
    "VOLATILE": 0.8,
    "NORMAL": 0.9
}

exit_done = False          # kept for backward compat; manage_trade now uses a local copy
partial_booked = False
last_exit_reason = None

# ── Trade generation counters ─────────────────────────────────────────────
# Each counter is incremented every time a NEW trade is started for that
# instrument.  manage_trade captures the counter value at entry and exits
# its while-loop immediately if the counter has moved on (flip happened).
# This prevents a superseded manage_trade thread from calling exit_position
# on a position it no longer owns.
_nifty_trade_gen     = [0]
_banknifty_trade_gen = [0]
_sensex_trade_gen    = [0]
_crude_trade_gen     = [0]

nifty_trade_count     = 0
banknifty_trade_count = 0
sensex_trade_count    = 0
crude_trade_count     = 0
last_reset_day = None

# Per-instrument daily P&L tracking
nifty_daily_pnl      = 0
banknifty_daily_pnl  = 0
sensex_daily_pnl     = 0
crude_daily_pnl      = 0
nifty_daily_wins     = 0
nifty_daily_losses   = 0
banknifty_daily_wins   = 0
banknifty_daily_losses = 0
sensex_daily_wins    = 0
sensex_daily_losses  = 0
crude_daily_wins     = 0
crude_daily_losses   = 0

# Per-instrument position state
banknifty_position   = {"symbol": None, "qty": 0, "exchange": None, "signal": None, "active": False}
banknifty_trade_active = False
last_executed_signal_banknifty = None
last_exit_time_banknifty = 0

sensex_position      = {"symbol": None, "qty": 0, "exchange": None, "signal": None, "active": False}
sensex_trade_active  = False
last_executed_signal_sensex = None
last_exit_time_sensex = 0

# Cache for BankNifty + SENSEX 15-min data
last_fetch_banknifty = 0
cached_banknifty_df  = None
cached_banknifty_ht  = None

last_fetch_sensex    = 0
cached_sensex_df     = None
cached_sensex_ht     = None

# Telegram alert rate-limiting (avoid flooding on same state)
_last_no_signal_alert_nifty     = 0
_last_no_signal_alert_banknifty = 0
_last_no_signal_alert_sensex    = 0
_last_no_signal_alert_crude     = 0
_last_trail_alert_nifty = 0.0
_last_trail_alert_crude = 0.0
NO_SIGNAL_ALERT_INTERVAL = 300   # send "no arrow" alert at most every 5 min
last_no_arrow_log_time = 0
last_logged_trend_nifty = None
last_logged_arrow_nifty = None
last_logged_trend_crude = None
last_logged_arrow_crude = None
last_arrow_index_nifty = None
last_arrow_index_crude = None
history_loaded_crude = False
history_loaded_nifty = False
last_weak_log_time = 0
last_status = None
DEBUG = False

last_fetch_nifty = 0
last_fetch_crude = 0

cached_nifty_df = None
cached_crude_5m = None
cached_crude_15m = None

# HalfTrend indicator cache — recomputed only when underlying data refreshes
cached_nifty_ht = None
cached_crude_ht = None

# Per-instrument trade locks (replaces single global_trade_active for cross-instrument safety)
nifty_trade_active = False
crude_trade_active = False

def is_nifty_trading_time():
    now = datetime.now(IST)

    return (
        (now.hour == 9 and now.minute >= 15) or
        (9 < now.hour < 15) or
        (now.hour == 15 and now.minute <= 30)
    )


def is_crude_trading_time():
    now = datetime.now(IST)

    return (
        (now.hour == 9 and now.minute >= 0) or
        (9 < now.hour < 23)   # CRUDE runs till ~11 PM
    )


def is_trading_time():
    now = datetime.now(IST)

    # Start at 9:00 AM
    if now.hour < 9:
        return False

    # Stop at 11:00 PM
    if now.hour >= 23:
        return False

    return True

def prepare_indicators(df):
    if df is None or len(df) < 2:
        return df

    df = df.copy()

    # VWAP
    df["volume"] = df["volume"].replace(0, 1)
    df["vwap"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()
    df["vwap"] = df["vwap"].fillna(df["close"])

    # EMA
    df["ema9"] = df["close"].ewm(span=9).mean()
    df["ema20"] = df["close"].ewm(span=20).mean()

    return df
import pandas as pd
import numpy as np

# ======================================
# TRUE TradingView ATR (Wilder RMA)
# ta.atr(period)
# ======================================
import numpy as np

def ATR(df, period=100):
    high = df["high"]
    low = df["low"]
    close = df["close"]
    prev_close = close.shift()

    # Vectorized calculation
    tr = np.maximum(
        high - low, 
        np.maximum(
            (high - prev_close).abs(), 
            (low - prev_close).abs()
        )
    )
    
    # Using your existing (correct) RMA logic
    atr = tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()

    return atr


# ──────────────────────────────────────────────────────────────────────────────
# 📊 ADX  —  Average Directional Index (Wilder, period=14)
# Measures trend STRENGTH (not direction).
# ADX > 25 → strong trend → HalfTrend signals have high follow-through
# ADX < 20 → choppy/ranging → HalfTrend gives many false signals
# ──────────────────────────────────────────────────────────────────────────────
def ADX(df, period=14):
    high  = df["high"]
    low   = df["low"]
    close = df["close"]
    prev_close = close.shift(1)
    prev_high  = high.shift(1)
    prev_low   = low.shift(1)

    # True Range
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs()
    ], axis=1).max(axis=1)

    # Directional Movement
    up_move   = high - prev_high
    down_move = prev_low - low

    plus_dm  = pd.Series(
        np.where((up_move > down_move) & (up_move > 0), up_move, 0.0),
        index=df.index)
    minus_dm = pd.Series(
        np.where((down_move > up_move) & (down_move > 0), down_move, 0.0),
        index=df.index)

    # Wilder smoothing — matches TradingView's rma() exactly:
    # rma seeds from bar 1 (no min_periods gap), alpha = 1/period
    alpha  = 1.0 / period
    atr_s  = tr.ewm(alpha=alpha, adjust=False).mean()
    pdi    = 100 * plus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_s
    mdi    = 100 * minus_dm.ewm(alpha=alpha, adjust=False).mean() / atr_s

    dx     = 100 * (pdi - mdi).abs() / (pdi + mdi).replace(0, np.nan)
    adx    = dx.ewm(alpha=alpha, adjust=False).mean()
    return adx


# ──────────────────────────────────────────────────────────────────────────────
# 📈 India VIX  —  fetch live VIX from Kite
# VIX 12–22 → ideal option-buying zone
# VIX < 11  → premiums too cheap to move enough (time decay wins)
# VIX > 22  → gap-risk, chaotic fills, wide spreads
# ──────────────────────────────────────────────────────────────────────────────
_vix_cache = {"value": None, "ts": 0}   # simple 5-min cache so every loop tick
                                        # doesn't hammer the Kite LTP endpoint

def get_india_vix():
    try:
        if time.time() - _vix_cache["ts"] < 300:   # reuse cached value for 5 min
            return _vix_cache["value"]
        q   = kite.ltp(["NSE:INDIA VIX"])
        vix = q["NSE:INDIA VIX"]["last_price"]
        _vix_cache["value"] = vix
        _vix_cache["ts"]    = time.time()
        return vix
    except Exception as _e:
        print(f"⚠️ VIX fetch error: {_e}")
        return None   # return None → VIX filter is skipped gracefully


# ──────────────────────────────────────────────────────────────────────────────
# 🕐 MTF (Multi-Time-Frame) 1-Hour HalfTrend confirmation
# 15-min arrow must agree with the 1-hour trend direction.
# Counter-trend entries on 15-min are the most dangerous in options because
# the larger trend fights you while time decay erodes the premium.
# ──────────────────────────────────────────────────────────────────────────────
_last_fetch_nifty_1h = [0]
_last_fetch_crude_1h = [0]
_cached_nifty_ht_1h  = [None]
_cached_crude_ht_1h  = [None]

def get_mtf_trend(token, instrument):
    """
    Fetches 1-hour HalfTrend and returns the trend direction of the last
    closed 1-hour candle: "CALL" (bullish), "PUT" (bearish), or None on error.
    Cached for 3 minutes to avoid hammering the Kite historical API.
    """
    try:
        if instrument == "NIFTY":
            if time.time() - _last_fetch_nifty_1h[0] > 180 or _cached_nifty_ht_1h[0] is None:
                df_1h = get_cached_data(token, "60minute", 100)
                if df_1h is not None and len(df_1h) >= 30:
                    _cached_nifty_ht_1h[0] = halftrend_tv(df_1h, amplitude=2, channel_deviation=2)
                _last_fetch_nifty_1h[0] = time.time()
            ht_1h = _cached_nifty_ht_1h[0]
        else:
            if time.time() - _last_fetch_crude_1h[0] > 180 or _cached_crude_ht_1h[0] is None:
                df_1h = get_cached_data(token, "60minute", 100)
                if df_1h is not None and len(df_1h) >= 30:
                    _cached_crude_ht_1h[0] = halftrend_tv(df_1h, amplitude=2, channel_deviation=2)
                _last_fetch_crude_1h[0] = time.time()
            ht_1h = _cached_crude_ht_1h[0]

        if ht_1h is None or len(ht_1h) < 5:
            return None

        trend_1h = int(ht_1h.iloc[-2]["trend"])   # last CLOSED 1-hour candle
        return "CALL" if trend_1h == 0 else "PUT"

    except Exception as _e:
        print(f"⚠️ MTF error ({instrument}): {_e}")
        return None   # return None → MTF filter skipped gracefully


# ──────────────────────────────────────────────────────────────────────────────
# 🔍 apply_entry_filters(signal, instrument, df_15m)
# Single function that runs ALL enabled filters.
# ──────────────────────────────────────────────────────────────────────────────
# ML SIGNAL HELPER
# ──────────────────────────────────────────────────────────────────────────────
def get_ml_signal():
    """
    Call the ML signal server and return (signal, confidence, reason).
    signal     : "CALL" | "PUT" | "HOLD"
    confidence : float 0-100
    reason     : string explanation

    If server unreachable:
      • ML_REQUIRED=True  → returns ("HOLD", 0, "ML server down") — blocks entry
      • ML_REQUIRED=False → returns (None, 0, "ML skipped")      — entry allowed
    """
    try:
        resp = requests.get(f"{ML_SERVER_URL}/signal", timeout=3)
        data = resp.json()
        return data.get("signal", "HOLD"), float(data.get("confidence", 0)), data.get("reason", "")
    except Exception as e:
        print(f"⚠️ ML server unreachable: {e}", flush=True)
        if ML_REQUIRED:
            return "HOLD", 0, f"ML server down: {e}"
        return None, 0, "ML server skipped (not required)"


# Returns (passed: bool, reason: str)
# Call this right before order placement in nifty_loop / crude_loop.
# ──────────────────────────────────────────────────────────────────────────────
def apply_entry_filters(signal, instrument, df_15m, token):
    """
    Runs ADX, MTF, VIX, and Session filters.
    Returns True if ALL enabled filters pass, False if any blocks entry.
    Also returns a human-readable reason string for logging / Telegram.
    """
    now_ist = datetime.now(IST)

    # ── 1. Session timing filter (NIFTY + BANKNIFTY — same session hours) ────────
    if USE_SESSION_FILTER and instrument in ("NIFTY", "BANKNIFTY", "SENSEX"):
        _dead = (
            (now_ist.hour == 11 and now_ist.minute >= 30) or
            (now_ist.hour == 12) or
            (now_ist.hour == 13 and now_ist.minute < 30)
        )
        if _dead:
            return False, "⏸️ Session filter: 11:30 AM–1:30 PM dead zone (low momentum)"

    # ── 2. ADX filter (trend strength) ────────────────────────────────────────
    if USE_ADX_FILTER and df_15m is not None:
        try:
            adx_s   = ADX(df_15m, period=14)
            adx_val = adx_s.iloc[-2]   # last CLOSED candle
            if not np.isnan(adx_val) and adx_val < ADX_MIN_VALUE:
                return False, f"⏸️ ADX filter: ADX={adx_val:.1f} below {ADX_MIN_VALUE} (choppy market)"
            _adx_str = f"ADX={adx_val:.1f}" if not np.isnan(adx_val) else "ADX=N/A"
        except Exception as _e:
            _adx_str = f"ADX=err({_e})"
    else:
        _adx_str = "ADX=off"

    # ── 3. MTF 1-hour confirmation ─────────────────────────────────────────────
    if USE_MTF_FILTER:
        trend_1h = get_mtf_trend(token, instrument)
        if trend_1h is not None and trend_1h != signal:
            return False, (f"⏸️ MTF filter: 1h trend={trend_1h} ≠ 15m signal={signal} "
                           f"(counter-trend — skip)")
        _mtf_str = f"1h={trend_1h or 'N/A'}"
    else:
        _mtf_str = "MTF=off"

    # ── 4. India VIX filter ───────────────────────────────────────────────────
    if USE_VIX_FILTER:
        vix = get_india_vix()
        if vix is not None:
            if vix < VIX_MIN:
                return False, f"⏸️ VIX filter: VIX={vix:.1f} < {VIX_MIN} (premiums too cheap)"
            if vix > VIX_MAX:
                return False, f"⏸️ VIX filter: VIX={vix:.1f} > {VIX_MAX} (too volatile)"
            _vix_str = f"VIX={vix:.1f}"
        else:
            _vix_str = "VIX=N/A"
    else:
        _vix_str = "VIX=off"

    # ── 5. 9/15 EMA second signal source (NIFTY + CRUDE) ─────────────────────
    # 9 EMA must be on the correct side of 15 EMA to confirm HalfTrend direction.
    # Uses last CLOSED candle (-2) — same anti-repaint rule as HalfTrend.
    _ema_str = "EMA=off"
    if USE_EMA_FILTER and df_15m is not None:
        try:
            ema9  = df_15m["close"].ewm(span=9,  adjust=False).mean()
            ema15 = df_15m["close"].ewm(span=15, adjust=False).mean()
            e9    = round(float(ema9.iloc[-2]),  2)
            e15   = round(float(ema15.iloc[-2]), 2)

            if signal == "CALL" and e9 < e15:
                return False, (f"⏸️ EMA filter: 9 EMA ({e9:.1f}) below 15 EMA ({e15:.1f}) "
                               f"— bearish EMA, HalfTrend says CALL (disagreement)")
            if signal == "PUT" and e9 > e15:
                return False, (f"⏸️ EMA filter: 9 EMA ({e9:.1f}) above 15 EMA ({e15:.1f}) "
                               f"— bullish EMA, HalfTrend says PUT (disagreement)")

            _ema_str = f"EMA9={e9:.1f} {'>' if e9 > e15 else '<'} EMA15={e15:.1f}"
        except Exception as _e:
            _ema_str = f"EMA=err({_e})"

    # ── 6. ML Signal filter (NIFTY only) ─────────────────────────────────────
    _ml_str = "ML=off"
    if USE_ML_FILTER and instrument == "NIFTY":
        ml_sig, ml_conf, ml_reason = get_ml_signal()

        if ml_sig is None:
            # Server unreachable and ML_REQUIRED=False → allow trade, log it
            _ml_str = "ML=skipped(server down)"
        elif ml_sig == "HOLD":
            return False, f"🤖 ML filter: HOLD — {ml_reason} (confidence: {ml_conf:.0f}%)"
        elif ml_sig != signal:
            return False, (f"🤖 ML filter: disagreement — ML={ml_sig} vs HalfTrend={signal} "
                           f"(confidence: {ml_conf:.0f}%)")
        elif ml_conf < ML_MIN_CONFIDENCE:
            return False, (f"🤖 ML filter: low confidence — {ml_conf:.0f}% below {ML_MIN_CONFIDENCE}% "
                           f"({ml_reason})")
        else:
            _ml_str = f"ML={ml_sig}({ml_conf:.0f}%)"
            print(f"🤖 ML confirmed: {ml_sig} | confidence={ml_conf:.1f}% | {ml_reason}", flush=True)

    return True, f"✅ Filters passed — {_adx_str} | {_ema_str} | {_mtf_str} | {_vix_str} | {_ml_str}"


# ======================================
# TRUE TradingView HalfTrend
# Pine v6 exact logic
# ======================================
import pandas as pd
import numpy as np

def halftrend_tv(df, amplitude=2, channel_deviation=2):
    """
    Exact Python port of the TradingView HalfTrend indicator (Pine Script v6)
    by Alex Orekhov (everget). GPL-3.0 licensed.

    Outputs per bar:
        trend     : 0 = bullish, 1 = bearish
        ht        : HalfTrend line value (up when trend=0, down when trend=1)
        atr2      : half of ATR(100) — used for arrow offset
        atrHigh   : ht + channel_deviation * atr2  (upper channel band)
        atrLow    : ht - channel_deviation * atr2  (lower channel band)
        arrowUp   : non-NaN only on the bar a BUY arrow fires (= up - atr2)
        arrowDown : non-NaN only on the bar a SELL arrow fires (= down + atr2)
        buy       : True on the exact bar the buy arrow fires  → enter CALL
        sell      : True on the exact bar the sell arrow fires → enter PUT
    """
    df = df.copy()
    n = len(df)

    # ── 1. ATR(100) Wilder RMA — matches ta.atr(100) exactly ──────────────
    atr_series = ATR(df, 100)
    atr2_arr   = (atr_series / 2).to_numpy()
    dev_arr    = channel_deviation * atr2_arr

    # ── 2. Rolling stats matching Pine's highestbars / lowestbars / sma ───
    # ta.highestbars(amplitude) → index offset of highest bar in last `amplitude` bars
    # high[math.abs(ta.highestbars(amplitude))] → the high VALUE at that bar
    # This equals rolling(amplitude).max() — numerically identical.
    # Pine's sma(high, amplitude) = rolling mean of high over amplitude bars.
    high_arr = df['high'].to_numpy(dtype=float)
    low_arr  = df['low'].to_numpy(dtype=float)
    close_arr = df['close'].to_numpy(dtype=float)

    hp_arr  = df['high'].rolling(window=amplitude).max().to_numpy(dtype=float)   # highPrice
    lp_arr  = df['low'].rolling(window=amplitude).min().to_numpy(dtype=float)    # lowPrice
    hma_arr = df['high'].rolling(window=amplitude).mean().to_numpy(dtype=float)  # highma
    lma_arr = df['low'].rolling(window=amplitude).mean().to_numpy(dtype=float)   # lowma

    # ── 3. State arrays (all persistent — Pine `var`) ─────────────────────
    trend        = np.zeros(n, dtype=float)
    nextTrend    = np.zeros(n, dtype=float)
    maxLowPrice  = np.zeros(n, dtype=float)
    minHighPrice = np.zeros(n, dtype=float)
    up           = np.zeros(n, dtype=float)
    down         = np.zeros(n, dtype=float)

    # Pine var initialisation:
    #   maxLowPrice = nz(low[1], low)  → low[0] on bar 0
    #   minHighPrice = nz(high[1], high) → high[0] on bar 0
    maxLowPrice[0]  = low_arr[0]
    minHighPrice[0] = high_arr[0]

    # Output arrays for arrow values (NaN = no arrow on that bar)
    arrowUp_arr   = np.full(n, np.nan)
    arrowDown_arr = np.full(n, np.nan)

    for i in range(1, n):
        # ── carry forward persistent vars ────────────────────────────────
        trend[i]        = trend[i-1]
        nextTrend[i]    = nextTrend[i-1]
        maxLowPrice[i]  = maxLowPrice[i-1]
        minHighPrice[i] = minHighPrice[i-1]
        up[i]           = up[i-1]
        down[i]         = down[i-1]

        # Skip until rolling windows are fully populated
        if i < amplitude:
            continue

        close_i     = close_arr[i]
        low_prev    = low_arr[i-1]    # nz(low[1], low)
        high_prev   = high_arr[i-1]   # nz(high[1], high)
        hp          = hp_arr[i]        # highPrice
        lp          = lp_arr[i]        # lowPrice
        hma         = hma_arr[i]       # highma
        lma         = lma_arr[i]       # lowma
        atr2_i      = atr2_arr[i]

        # ── Trend logic (exact Pine if/else) ─────────────────────────────
        if nextTrend[i] == 1:
            maxLowPrice[i] = max(lp, maxLowPrice[i])
            if hma < maxLowPrice[i] and close_i < low_prev:
                trend[i]        = 1
                nextTrend[i]    = 0
                minHighPrice[i] = hp
        else:
            minHighPrice[i] = min(hp, minHighPrice[i])
            if lma > minHighPrice[i] and close_i > high_prev:
                trend[i]       = 0
                nextTrend[i]   = 1
                maxLowPrice[i] = lp

        # ── up / down line + arrow placement ─────────────────────────────
        # Pine: trend[1] means previous bar trend → trend[i-1] in Python
        prev_trend  = trend[i-1]
        prev_up     = up[i-1]
        prev_down   = down[i-1]

        if trend[i] == 0:
            if prev_trend != 0:
                # Trend just switched to bullish
                # Pine: up := na(down[1]) ? down : down[1]
                # In Python: if prev bar's down was 0 (never set), fall back to current down
                up[i]            = prev_down if prev_down != 0 else down[i]
                arrowUp_arr[i]   = up[i] - atr2_i   # Pine: arrowUp := up - atr2
            else:
                # Pine: up := na(up[1]) ? maxLowPrice : math.max(maxLowPrice, up[1])
                up[i] = max(maxLowPrice[i], prev_up) if prev_up != 0 else maxLowPrice[i]
        else:
            if prev_trend != 1:
                # Trend just switched to bearish
                # Pine: down := na(up[1]) ? up : up[1]
                down[i]          = prev_up if prev_up != 0 else up[i]
                arrowDown_arr[i] = down[i] + atr2_i  # Pine: arrowDown := down + atr2
            else:
                # Pine: down := na(down[1]) ? minHighPrice : math.min(minHighPrice, down[1])
                down[i] = min(minHighPrice[i], prev_down) if prev_down != 0 else minHighPrice[i]

    # ── 4. Output columns ─────────────────────────────────────────────────
    ht_arr    = np.where(trend == 0, up, down)
    atrHigh   = ht_arr + dev_arr
    atrLow    = ht_arr - dev_arr

    df["trend"]      = trend
    df["ht"]         = ht_arr
    df["atr2"]       = atr2_arr
    df["atrHigh"]    = atrHigh      # upper channel band (sell ribbon edge)
    df["atrLow"]     = atrLow       # lower channel band (buy ribbon edge)
    df["arrowUp"]    = arrowUp_arr   # NaN except on buy-signal bar
    df["arrowDown"]  = arrowDown_arr # NaN except on sell-signal bar

    # ── 5. Signal flags — exact Pine definition ───────────────────────────
    # Pine: buySignal  = not na(arrowUp)   and trend == 0 and trend[1] == 1
    # Pine: sellSignal = not na(arrowDown) and trend == 1 and trend[1] == 0
    trend_series = df["trend"]
    df["buy"]  = (~np.isnan(arrowUp_arr))   & (trend_series == 0) & (trend_series.shift(1) == 1)
    df["sell"] = (~np.isnan(arrowDown_arr)) & (trend_series == 1) & (trend_series.shift(1) == 0)

    return df
#======
def verify_halftrend(ht_df, name="VERIFY", bars=5):
    try:
        if ht_df is None or len(ht_df) < bars + 1:
            print(f"⚠️ {name}: Not enough data for verification")
            return

        print("\n" + "=" * 90)
        print(f"🔍 HALF TREND VERIFICATION MODE ({name})")
        print("=" * 90)

        check_df = ht_df.tail(bars).copy()

        for i in range(len(check_df)):
            row = check_df.iloc[i]
            ts    = row.name if row.name is not None else i
            trend = "CALL(0)" if row["trend"] == 0 else "PUT(1) "

            signal = "NONE"
            if row["buy"]:
                signal = "BUY ▲"
            elif row["sell"]:
                signal = "SELL ▼"

            arrow_val = ""
            if row["buy"]:
                arrow_val = f"arrowUp={row['arrowUp']:.2f}  atrLow={row['atrLow']:.2f}"
            elif row["sell"]:
                arrow_val = f"arrowDn={row['arrowDown']:.2f}  atrHigh={row['atrHigh']:.2f}"

            print(
                f"🕒 {ts} | "
                f"Trend:{trend} | "
                f"Signal:{signal:6} | "
                f"Close:{row['close']:.2f} | "
                f"HT:{row['ht']:.2f} | "
                f"{arrow_val}"
            )

        last = ht_df.iloc[-2]
        final_signal = None
        if last["buy"]:
            final_signal = f"CALL  (arrowUp={last['arrowUp']:.2f}, enter near atrLow={last['atrLow']:.2f})"
        elif last["sell"]:
            final_signal = f"PUT   (arrowDn={last['arrowDown']:.2f}, enter near atrHigh={last['atrHigh']:.2f})"

        print("-" * 90)
        print(f"🎯 CLOSED CANDLE DECISION → {final_signal if final_signal else 'NO NEW SIGNAL'}")
        print("=" * 90 + "\n")

    except Exception as e:
        print("❌ Verification error:", e)
        
#===


    
# NOTE: detect_market_type() is defined further below (single authoritative version).
# The duplicate that was here has been removed to prevent silent override.



def evaluate_strategies():

    print("📊 Evaluating strategies...")

    for strat, results in strategy_log.items():

        if len(results) < 5:
            continue  # not enough data

        wins = sum(1 for p in results if p > 0)
        win_rate = wins / len(results)
        avg_pnl = sum(results) / len(results)

        print(f"{strat} → WinRate: {win_rate:.2f}, AvgPnL: {avg_pnl:.2f}")

        # 🎯 Adjust weights instead of disabling
        if win_rate < 0.4 or avg_pnl < 0:
            strategy_weights[strat] = max(0.2, strategy_weights[strat] - 0.2)
            print(f"⚠️ Reducing weight for {strat}")

        elif win_rate > 0.6 and avg_pnl > 0:
            strategy_weights[strat] = min(1.5, strategy_weights[strat] + 0.2)
            print(f"🚀 Increasing weight for {strat}")

def log_trade_full(symbol, entry, exit_price, pnl, instrument, signal, probability):

    import csv
    from datetime import datetime

    with open(TRADE_LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)

        writer.writerow([
            datetime.now(),
            instrument,
            symbol,
            signal,
            entry,
            exit_price,
            pnl,
            probability
        ])


def get_nifty_fut_token():
    try:
        instruments = kite.instruments("NFO")

        futures = [
            inst for inst in instruments
            if "NIFTY" in inst["tradingsymbol"]
            and inst["instrument_type"] == "FUT"
        ]

        futures = sorted(futures, key=lambda x: x["expiry"])

        if futures:
            token = futures[0]["instrument_token"]
            print(f"✅ NIFTY FUT TOKEN: {token} ({futures[0]['tradingsymbol']})")
            return token

        return None

    except Exception as e:
        print("❌ NIFTY FUT token error:", e)
        return None


def get_latest_fut_token(symbol, exchange):
    try:
        instruments = kite.instruments(exchange)

        futures = [
            inst for inst in instruments
            if symbol in inst["tradingsymbol"]
            and inst["instrument_type"] == "FUT"
        ]

        # Sort by expiry (nearest first)
        futures = sorted(futures, key=lambda x: x["expiry"])

        if futures:
            token = futures[0]["instrument_token"]
            print(f"✅ {symbol} TOKEN: {token} ({futures[0]['tradingsymbol']})")
            return token

        print(f"❌ No FUT found for {symbol}")
        return None

    except Exception as e:
        print(f"❌ Token fetch error for {symbol}:", e)
        return None


def get_session_config(instrument):

    session = get_market_session(instrument)

    if instrument == "NIFTY":

        if session == "MORNING":
            return {"min_conf": 50, "lot_mult": 1.2}

        elif session == "MIDDAY":
            return {"min_conf": 50, "lot_mult": 0.7}

        elif session == "AFTERNOON":
            return {"min_conf": 60, "lot_mult": 1}

    else:  # CRUDE

        if session == "MORNING":
            return {"min_conf": 55, "lot_mult": 1}

        elif session == "MIDDAY":
            return {"min_conf": 50, "lot_mult": 0.7}

        elif session == "EVENING_TREND":
            return {"min_conf": 50, "lot_mult": 1.5}

        elif session == "VOLATILE_SESSION":
            return {"min_conf": 65, "lot_mult": 1}

    return None


def safe_ltp(symbol):
    global ltp_cache

    # ✅ Protection: invalid symbol
    if not symbol or not isinstance(symbol, str):
        return None

    now = time.time()

    # ✅ Cache hit
    if symbol in ltp_cache:
        ts, price = ltp_cache[symbol]

        if now - ts < LTP_TTL:
            return price

    # ✅ Retry max 2 times
    for _ in range(2):
        try:
            data = kite.ltp([symbol])

            if not data or symbol not in data:
                print("❌ LTP missing for:", symbol)
                return None

            price = data[symbol].get("last_price")

            if price is None:
                return None

            ltp_cache[symbol] = (now, price)
            return price

        except Exception as e:
            print("LTP error:", e)
            time.sleep(0.5)

    return None

# -----------------------------
# MARKET FILTERS
# -----------------------------
def is_market_trending(token, df=None):

    try:
        if df is None:
            df = get_cached_data(token, "5minute", 200)

        if df is None or len(df) < 10:
            return False

        # 🔥 CRITICAL FIX
        df = prepare_indicators(df)

        # 🔒 SAFETY CHECK (ADD THIS)
        if "vwap" not in df.columns:
            print("⚠️ VWAP missing — skipping trend check")
            return False

        last = df.iloc[-1]

        vwap = last["vwap"]
        atr = (df["high"] - df["low"]).rolling(5).mean().iloc[-1]

        print(f"🔥 Trend Check → VWAP Dist: {abs(last['close'] - vwap)}, ATR: {atr}")

        return abs(last["close"] - vwap) > atr * 0.5

    except Exception as e:
        print("Trend error:", e)
        return False

# -----------------------------
# RISK CONTROL
# -----------------------------

def can_trade():

    global daily_pnl, trade_count, last_loss_time, trade_alert_sent
    global loss_streak

    # 🛑 Portfolio protection FIRST
    if not portfolio_safe():
        return False

    # 🚫 Risk OFF
    if risk_off:
        return False

    # 🛑 Bad day stop
    if daily_pnl < config.MAX_DAILY_LOSS:
        return False

    # 🎯 Profit lock
    # 🎯 DAILY PROFIT LOCK
    if daily_pnl >= 10000:
        print("🎯 Target reached — stopping trading")
        return False

    # 🚫 Max trades
    if trade_count >= config.MAX_TRADES:
        return False

    # ⏳ Cooldown after loss
    if last_loss_time and time.time() - last_loss_time < config.COOLDOWN_AFTER_LOSS:
        return False

    # 🚫 Losing streak control — do NOT sleep here; let the loop handle the pause
    if loss_streak >= 3:
        return False

    return True



# -----------------------------
# NIFTY STRATEGIES
# -----------------------------



def pivot_signal(token):
    try:
        now = datetime.now()
        df = get_cached_data(token, "5minute", 20)
        
        
        if df is None or len(df) < 10:
            return "HOLD"

        prev = df.iloc[-2]
        pivot = (prev["high"] + prev["low"] + prev["close"]) / 3
        ltp = safe_ltp("NSE:NIFTY 50")
        if ltp is None:
            return "HOLD"

        return "CALL" if ltp > pivot else "PUT"
    except:
        return "HOLD"


def momentum_signal(token):
    try:
        now = datetime.now()
        df = get_cached_data(token, "5minute", 20)
        
        
        if df is None or len(df) < 10:
            return "HOLD"

        last = df.iloc[-1]
        body = abs(last["close"] - last["open"])
        rng = last["high"] - last["low"]

        if body > rng * 0.6:
            return "CALL" if last["close"] > last["open"] else "PUT"

        return "HOLD"
    except:
        return "HOLD"




# -----------------------------
# PRO CRUDE STRATEGY
# -----------------------------
def get_crude_signal(token):
    
    try:
        now = datetime.now()

        df = get_cached_data(token, "5minute", 20)

        if df is None or len(df) < 20:
            return "HOLD"
            
        df = df.copy()

        df = prepare_indicators(df)
        df["vol_ma"] = df["volume"].rolling(10).mean()

        last = df.iloc[-1]
        prev = df.iloc[-2]

        body = abs(last["close"] - last["open"])
        rng = last["high"] - last["low"]

        strong = body > rng * 0.5
        small = body < rng * 0.3

        if small:
            return "HOLD"

        vol_spike = last["volume"] > last["vol_ma"] * 1.2

        above_vwap = last["close"] > last["vwap"]
        below_vwap = last["close"] < last["vwap"]

        breakout_up = last["close"] > prev["high"]
        breakout_down = last["close"] < prev["low"]

        # -----------------------------
        # 🎯 MAIN LOGIC (SCORING)
        # -----------------------------
        call_score = 0
        put_score = 0

        if breakout_up:
            call_score += 1
        if above_vwap:
            call_score += 1
        if vol_spike:
            call_score += 1
        if strong:
            call_score += 1

        if breakout_down:
            put_score += 1
        if below_vwap:
            put_score += 1
        if vol_spike:
            put_score += 1
        if strong:
            put_score += 1

        if call_score >= 2 and call_score > put_score:
            return "CALL"

        if put_score >= 2 and put_score > call_score:
            return "PUT"


        # -----------------------------
        # ⚡ OPTIONAL BOOST (ADD HERE)
        # -----------------------------
        if strong and above_vwap and last["close"] > prev["close"] and vol_spike:
            return "CALL"

        if strong and below_vwap and last["close"] < prev["close"] and vol_spike:
            return "PUT"

        # 🔥 FALLBACK SIGNAL (VERY IMPORTANT)
        if last["close"] > prev["close"] and abs(last["close"] - prev["close"]) > last["close"] * 0.0005:
            return "CALL"
        elif last["close"] < prev["close"]:
            return "PUT"
            
        # -----------------------------
        # DEFAULT
        # -----------------------------
        return "HOLD"
        
    except Exception as e:
        print("CRUDE SIGNAL ERROR:", e)
        return "HOLD"
        
        
def get_quote(symbol):
    global quote_cache

    now = time.time()

    if symbol in quote_cache:
        ts, data = quote_cache[symbol]
        if now - ts < QUOTE_TTL:
            return data

    try:
        data = kite.quote([symbol])[symbol]
        quote_cache[symbol] = (now, data)
        return data

    except Exception as e:
        print("Quote fetch error:", e)
        return None       

        
def is_liquid_option(symbol, exchange):

    try:
        full_symbol = f"{exchange}:{symbol}"

        data = get_quote(full_symbol)
        if not data:
            return False

        price = data.get("last_price", 0)

        # Basic price sanity
        if price <= 0:
            return False

        # OPTIONAL: fetch OHLC (volume proxy)
        ohlc = data.get("ohlc", {})

        # Avoid extreme low premium options
        if price < 5:
            return False

        # Avoid too high premium (low liquidity sometimes)
        if price > 500:
            return False

        return True

    except:
        return False
        
def score_option(symbol, exchange, token, signal, df=None):

    if df is None:
        df = get_cached_data(token, "5minute", 50)

    if df is None:
        return 0

    df = df.copy()   # ALWAYS COPY
        

    try:
        full_symbol = f"{exchange}:{symbol}"

        price = safe_ltp(full_symbol)
        # 🔥 RELAXED FILTER
        if price is None or price <= 0:
            return 0

        # allow wider range
        if price < 5 or price > 1000:
            return 0

        # -----------------------------
        # 🎯 PRICE OPTIMIZATION
        # -----------------------------
        score = 100 / (abs(price - 100) + 1)

        # -----------------------------
        # 📈 MOMENTUM BOOST
        # -----------------------------
        now = datetime.now()

        if df is None or len(df) < 10:
            return 0
        if len(df) >= 3:
            last = df.iloc[-1]
            prev = df.iloc[-2]

            move = last["close"] - prev["close"]

            if signal == "CALL" and move > 0:
                score *= 1.3

            if signal == "PUT" and move < 0:
                score *= 1.3

        # -----------------------------
        # 🔊 VOLUME BOOST
        # -----------------------------
        if len(df) >= 10:
            df["vol_ma"] = df["volume"].rolling(5).mean()
            if df.iloc[-1]["volume"] > df.iloc[-1]["vol_ma"]:
                score *= 1.2
                
        # Premium sweet spot boost
        if 70 <= price <= 150:
            score *= 1.3

        return score

    except Exception as e:
        print("Score error:", e)
        return 0
        
def is_good_spread(symbol, exchange):

    try:
        full_symbol = f"{exchange}:{symbol}"

        data = get_quote(full_symbol)
        if not data:
            return False

        depth = data.get("depth", {})

        bids = depth.get("buy", [])
        asks = depth.get("sell", [])

        if not bids or not asks:
            return False

        best_bid = bids[0]["price"]
        best_ask = asks[0]["price"]

        spread = best_ask - best_bid

        ltp = data.get("last_price", 0)

        if ltp == 0:
            return False

        spread_pct = (spread / ltp) * 100

        print(f"Spread {symbol}: {spread_pct:.2f}%")

        # ✅ RULE: Reject if spread > 1.5%
        if spread_pct > 1.5:
            return False

        return True

    except Exception as e:
        print("Spread error:", e)
        return False
        

def get_instruments_cached(exchange):
    global instrument_cache

    if exchange in instrument_cache:
        return instrument_cache[exchange]

    try:
        data = kite.instruments(exchange)
        instrument_cache[exchange] = data
        return data
    except Exception as e:
        print("Instrument fetch error:", e)
        return []


def get_crude_fut_symbol():
    try:
        instruments = kite.instruments("MCX")

        futures = [
            inst for inst in instruments
            if inst["name"] == "CRUDEOIL"
            and inst["instrument_type"] == "FUT"
        ]

        futures = sorted(futures, key=lambda x: x["expiry"])

        if futures:
            symbol = f"MCX:{futures[0]['tradingsymbol']}"
            print("✅ Selected FUT:", symbol)
            return symbol

    except Exception as e:
        print("❌ Crude symbol error:", e)

    return None

# -----------------------------
# OPTION SELECTOR
# -----------------------------
def find_option(signal, instrument):
    print("🔍 Entered find_option")

    # =====================================
    # Normalize signal
    # =====================================
    signal = str(signal).strip().upper()

    if signal not in ["CALL", "PUT"]:
        print("❌ Invalid signal:", signal)
        return None, None, None, None

    print("🧠 find_option received:", signal)

    symbol = None
    price = None
    lot = None
    exchange = None

    # =====================================
    # CONFIG
    # =====================================
    if instrument == "NIFTY":
        exchange = "NFO"
        name = "NIFTY"
        step = 50
        token = config.NIFTY_TOKEN
        token_symbol = "NSE:NIFTY 50"
        lot_size = 65            # NIFTY lot size (updated to 15 from Apr 2025)
    elif instrument == "BANKNIFTY":
        exchange = "NFO"
        name = "BANKNIFTY"
        step = 100               # BankNifty strikes move in ₹100 steps
        token = BANKNIFTY_TOKEN
        token_symbol = "NSE:NIFTY BANK"
        lot_size = 30            # BankNifty lot size
    elif instrument == "SENSEX":
        exchange = "BFO"         # BSE F&O exchange
        name = "SENSEX"
        step = 100               # SENSEX strikes move in ₹100 steps
        token = SENSEX_TOKEN
        token_symbol = "BSE:SENSEX"
        lot_size = 20            # SENSEX lot size = 20
    else:
        exchange = "MCX"
        name = "CRUDEOIL"
        step = 100
        token = CRUDE_TOKEN
        token_symbol = get_crude_fut_symbol()
        lot_size = 100

    # =====================================
    # MARKET DATA
    # =====================================
    df = get_cached_data(token, "5minute", 150)

    if df is None or df.empty:
        print("❌ No market data")
        return None, None, None, None

    ltp = safe_ltp(token_symbol)

    if ltp is None or ltp <= 0:
        print("❌ Invalid spot/fut LTP")
        return None, None, None, None

    atm = round(ltp / step) * step

    # =====================================
    # BALANCE BASED SETTINGS
    # =====================================
    balance = get_balance(instrument) or 10000

    if instrument == "CRUDE":
        # MCX Crude Oil lot = 100 barrels. Strikes move in ₹100 steps.
        if balance <= 5000:
            strike_shift = 2
            max_price = 80
        elif balance <= 10000:
            strike_shift = 2
            max_price = 100
        elif balance <= 20000:
            strike_shift = 1
            max_price = 130
        else:
            strike_shift = 1
            max_price = 160
    elif instrument == "BANKNIFTY":
        # BankNifty lot = 15. Strikes move in ₹100 steps.
        # ATM BankNifty option ~₹100-400. Lot value = premium × 15.
        if balance <= 5000:
            strike_shift = 3       # ₹300 OTM
            max_price = 120        # ₹120 × 15 = ₹1,800 per lot
        elif balance <= 10000:
            strike_shift = 2
            max_price = 200        # ₹200 × 15 = ₹3,000 per lot
        elif balance <= 20000:
            strike_shift = 2
            max_price = 300        # ₹300 × 15 = ₹4,500 per lot
        elif balance <= 35000:
            strike_shift = 1
            max_price = 450        # ₹450 × 15 = ₹6,750 per lot
        else:
            strike_shift = 1
            max_price = 600        # ₹600 × 15 = ₹9,000 per lot
    elif instrument == "SENSEX":
        # SENSEX lot = 20. Strikes move in ₹100 steps.
        # SENSEX ~75,000-80,000. ATM option ~₹100-500. Lot value = premium × 20.
        if balance <= 5000:
            strike_shift = 3       # ₹300 OTM
            max_price = 80         # ₹80  × 20 = ₹1,600 per lot
        elif balance <= 10000:
            strike_shift = 2
            max_price = 130        # ₹130 × 20 = ₹2,600 per lot
        elif balance <= 20000:
            strike_shift = 2
            max_price = 200        # ₹200 × 20 = ₹4,000 per lot
        elif balance <= 35000:
            strike_shift = 1
            max_price = 300        # ₹300 × 20 = ₹6,000 per lot
        else:
            strike_shift = 1
            max_price = 450        # ₹450 × 20 = ₹9,000 per lot
    else:
        # NIFTY — lot size 15 (updated Apr 2025)
        # 1 lot value = premium * 65
        # Capital cap: 1 lot must not exceed 40% of balance
        # For ₹25k: 40% = ₹10,000 → max premium = 10000/65 ≈ 153
        # For ₹50k: 40% = ₹20,000 → max premium = 20000/65 ≈ 307
        if balance <= 5000:
            strike_shift = 7
            max_price = 50
        elif balance <= 10000:
            strike_shift = 5      # deep OTM — cheap premium
            max_price = 80        # ₹50 × 65 = ₹3,250 per lot
        elif balance <= 20000:
            strike_shift = 3      # OTM
            max_price = 110       # ₹110 × 65 = ₹7,150 per lot
        elif balance <= 35000:
            strike_shift = 2      # slight OTM
            max_price = 170       # ₹170 × 65 = ₹11,050 per lot
        elif balance <= 50000:
            strike_shift = 1      # near ATM
            max_price = 250       # ₹250 × 65 = ₹16,250 per lot
        else:
            strike_shift = 1      # ATM / 1 strike OTM
            max_price = 380       # ₹380 × 65 = ₹24,700 per lot

    # =====================================
    # OPTION TYPE + CORRECT STRIKE DIRECTION
    # =====================================
    if signal == "CALL":
        opt_type = "CE"
        target_strike = atm + (strike_shift * step)
    else:
        opt_type = "PE"
        target_strike = atm - (strike_shift * step)

    print(f"🎯 Searching option type: {opt_type}")
    print(f"💰 Balance: {balance} | ATM: {atm} | Target Strike: {target_strike} | Max Premium: {max_price}")

    # =====================================
    # LOAD OPTION CHAIN
    # =====================================
    instruments = get_instruments_cached(exchange)
    today = datetime.now().date()

    opts = [
        i for i in instruments
        if i["name"] == name
        and i["instrument_type"] == opt_type
        and i["expiry"] >= today
    ]

    if not opts:
        print("❌ No option contracts found")
        return None, None, None, None

    expiry = sorted(set(i["expiry"] for i in opts))[0]

    # =====================================
    # PRIMARY SEARCH
    # =====================================
    # FIX: sort by strike proximity to target_strike BEFORE slicing to [:20]
    # so the best candidates are always evaluated even in large option chains.
    opts_sorted = sorted(
        [i for i in opts if i["expiry"] == expiry],
        key=lambda x: abs(int(x.get("strike", 0)) - target_strike)
    )

    # Minimum premium floor — filters out near-zero illiquid options
    # CRUDE:  ₹10  (1 lot = 100 barrels — below ₹10 is too illiquid)
    # SENSEX: ₹30  (lot size = 10 — need meaningful premium)
    # NIFTY / BANKNIFTY: ₹20
    if instrument == "CRUDE":
        min_price = 10
    elif instrument == "SENSEX":
        min_price = 30
    else:
        min_price = 20

    candidates = []

    for i in opts_sorted[:20]:   # reduce API load — now sorted by proximity

        try:
            strike = int(i["strike"])
        except:
            continue

        sym = f"{exchange}:{i['tradingsymbol']}"
        p = safe_ltp(sym)

        if p is None or p <= 0:
            continue

        # ── Premium filter (CRUDE strictly ≥ ₹50, NIFTY ≥ ₹20) ──────────────
        if p < min_price or p > max_price:
            continue

        # ── Liquidity check — skip illiquid options ──────────────────────────
        try:
            q = kite.quote([sym])
            if q and sym in q:
                qd        = q[sym]
                vol       = qd.get("volume", 0)
                oi        = qd.get("oi", 0)
                depth     = qd.get("depth", {})
                best_bid  = depth.get("buy",  [{}])[0].get("price", 0) if depth.get("buy")  else 0
                best_ask  = depth.get("sell", [{}])[0].get("price", 0) if depth.get("sell") else 0

                # No market at all — skip
                if best_bid <= 0 or best_ask <= 0:
                    print(f"   ⚠️ No market (bid=0 or ask=0): {sym}")
                    continue

                # Spread > 15% of LTP = illiquid
                spread_pct = (best_ask - best_bid) / p if p > 0 else 1.0
                max_spread = 0.15
                if spread_pct > max_spread:
                    print(f"   ⚠️ Illiquid spread {spread_pct:.0%}: {sym} bid={best_bid} ask={best_ask}")
                    continue

                # Zero volume AND zero OI = completely untouched option
                if vol == 0 and oi == 0:
                    print(f"   ⚠️ Zero volume+OI: {sym}")
                    continue
        except Exception as liq_err:
            print(f"   ⚠️ Liquidity check failed for {sym}: {liq_err} — proceeding")

        diff = abs(strike - target_strike)
        trade_value = p * lot_size

        # Hard affordability: 1 lot must not exceed 40% of available balance
        if trade_value > balance * 0.70:
            continue

        score = score_option(
            i["tradingsymbol"],
            exchange,
            token,
            signal,
            df
        )

        candidates.append({
            "symbol": i["tradingsymbol"],
            "price": p,
            "strike": strike,
            "diff": diff,
            "score": score
        })

    print(f"📊 Candidates found: {len(candidates)}")

    # =====================================
    # BEST PICK
    # =====================================
    if candidates:
        if balance <= 10000:
            # low balance = cheapest first
            best = sorted(
                candidates,
                key=lambda x: (
                    x["price"],
                    x["diff"],
                    -x["score"]
                )
            )[0]
        else:
            # normal mode
            best = sorted(
                candidates,
                key=lambda x: (
                    x["diff"],
                    -x["score"],
                    abs(x["price"] - max_price)
                )
            )[0]

        print(f"🏆 Selected: {best['symbol']} | Strike: {best['strike']} | Price: {best['price']}")

        strong_trend = is_market_trending(token, df)
        lot = calculate_lots(best["price"], exchange, instrument, strong_trend)

        return best["symbol"], best["price"], lot, exchange

    # =====================================
    # FALLBACK SEARCH
    # =====================================
    print("⚠️ No ideal candidate — fallback")

    fallback = []

    # FIX: same proximity-sort fix as primary search
    for i in opts_sorted[:20]:

        try:
            strike = int(i["strike"])
        except:
            continue

        sym = f"{exchange}:{i['tradingsymbol']}"
        p = safe_ltp(sym)

        if p is None or p <= 0:
            continue

        # Fallback also respects min_price (CRUDE ≥ ₹10, NIFTY ≥ ₹20)
        if min_price <= p <= max_price * 1.8:
            # Liquidity check for fallback too
            _liquid = True
            try:
                q = kite.quote([sym])
                if q and sym in q:
                    depth    = q[sym].get("depth", {})
                    best_bid = depth.get("buy",  [{}])[0].get("price", 0) if depth.get("buy")  else 0
                    best_ask = depth.get("sell", [{}])[0].get("price", 0) if depth.get("sell") else 0
                    if best_bid <= 0 or best_ask <= 0:
                        _liquid = False
                    elif p > 0 and (best_ask - best_bid) / p > 0.15:
                        _liquid = False
            except Exception:
                pass
            if not _liquid:
                continue

            fallback.append({
                "symbol": i["tradingsymbol"],
                "price": p,
                "strike": strike,
                "diff": abs(strike - target_strike)
            })

    if fallback:
        if balance <= 10000:
            best = sorted(
                fallback,
                key=lambda x: (
                    x["price"],
                    x["diff"]
                )
            )[0]
        else:
            best = sorted(
                fallback,
                key=lambda x: (
                    x["diff"],
                    abs(x["price"] - max_price)
                )
            )[0]

        print(f"✅ Fallback: {best['symbol']} | Strike: {best['strike']} | Price: {best['price']}")

        strong_trend = is_market_trending(token, df)
        lot = calculate_lots(best["price"], exchange, instrument, strong_trend)

        return best["symbol"], best["price"], lot, exchange

    print("❌ No valid option found")
    return None, None, None, None    



# -----------------------------
# ORDER
# -----------------------------

def place_order(symbol, qty, exchange, instrument):

    print(f"🚀 PLACE ORDER: {symbol}, lot: {qty}, exchange: {exchange}")
    
    now = datetime.now(IST)

    if exchange == "NFO" and not (
        (now.hour == 9 and now.minute >= 15) or
        (9 < now.hour < 15) or
        (now.hour == 15 and now.minute <= 30)
    ):
        print("🚫 Market closed — skipping order")
        return None

    # 🚫 STRICT OPTION ONLY (REPLACE THIS BLOCK)
    if not symbol.endswith(("CE", "PE")):
        print("🚫 BLOCKED: Only CE/PE options allowed")
        return None

    try:
        full_symbol = f"{exchange}:{symbol}"

        # 📊 LTP
        ltp = safe_ltp(full_symbol)
        if ltp is None or ltp <= 0:
            print("❌ Invalid LTP")
            return None

        expected_price = ltp

        # 🔥 SAFE PRICE CALCULATION (NO DEPTH DEPENDENCY)
        spread_buffer = 0.002 if exchange == "NFO" else 0.004
        price = round(ltp * (1 + spread_buffer), 1)

        if price <= 0:
            print(f"❌ Invalid price {price}")
            return None

        quantity = get_quantity(qty, exchange)

        # ✅ LIVE BALANCE SUFFICIENCY CHECK — block order if balance too low
        try:
            live_balance = get_balance(instrument)
            total_cost   = price * quantity          # total ₹ this order will deploy
            min_required = total_cost * 1.02        # 10% buffer for margin/charges

            print(f"💰 Balance check → Available: ₹{live_balance:,.0f}  |  Order cost: ₹{total_cost:,.0f}  |  Required (with buffer): ₹{min_required:,.0f}")

            if live_balance < min_required:
                msg = (f"🚫 Insufficient balance for {symbol}\n"
                       f"Need ₹{min_required:,.0f}, have ₹{live_balance:,.0f}")
                print(msg)
                send_message(msg)
                return None

        except Exception as e:
            print(f"⚠️ Balance check failed: {e} — proceeding with order")

        print(f"➡️ Placing LIMIT order @ {price}  qty={quantity}")

        # 🚀 PLACE ORDER
        order_id = kite.place_order(
            variety="regular",
            exchange=exchange,
            tradingsymbol=symbol,
            transaction_type="BUY",
            quantity=quantity,
            order_type="LIMIT",
            price=price,
            product="MIS" if exchange == "NFO" else "NRML"
        )

        send_message(
            f"📥 Order placed: {symbol}\n"
            f"   Price: ₹{price:.1f}  |  Qty: {quantity}  |  Lots: {qty}\n"
            f"   Total deployed: ₹{price * quantity:,.0f}\n"
            f"   Max risk (25% SL): ₹{price * 0.25 * quantity:,.0f}"
        )

        filled_price = None

        # 🔄 CHECK FILL (LIMITED RETRY)
        for i in range(3):
            time.sleep(1)

            try:
                orders = kite.orders()
            except Exception as e:
                print("⚠️ Order fetch failed:", e)
                continue

            for o in orders:
                if o["order_id"] == order_id:
                    if o["status"] == "COMPLETE":
                        filled_price = o["average_price"]
                        break
                    elif o["status"] in ["CANCELLED", "REJECTED"]:
                        print("❌ Order rejected/cancelled")
                        return None

            if filled_price:
                break

            # 🔥 SMALL CONTROLLED PRICE INCREASE
            new_price = round(min(price * 1.001, expected_price * 1.01), 1)

            if new_price == price:
                continue

            try:
                kite.modify_order(
                    variety="regular",
                    order_id=order_id,
                    price=new_price
                )
                price = new_price
            except Exception as e:
                print("⚠️ Modify failed:", e)

        # ❌ NOT FILLED → CANCEL
        if not filled_price:
            try:
                kite.cancel_order(variety="regular", order_id=order_id)
            except:
                pass

            send_message(f"❌ Order cancelled: {symbol}")
            return None

        # 📉 SLIPPAGE CHECK (STRICT)
        slippage = abs(filled_price - expected_price)

        if slippage > expected_price * 0.012:  # tighter control
            print(f"❌ High slippage: {slippage} — exiting filled position immediately")
            send_message(f"❌ Trade slippage exit\n{symbol} slippage={slippage:.2f}")
            # The order is already FILLED — cancel won't work. Exit the position instead.
            exit_position(symbol, quantity, exchange)
            return None

        print(f"✅ Filled @ {filled_price}")
        return filled_price

    except Exception as e:
        print("❌ ORDER ERROR:", str(e))
        send_message(f"❌ Order error: {e}")
        return None
        
        
def update_streak(pnl):
    global win_streak, loss_streak, last_loss_time

    if pnl > 0:
        win_streak += 1
        loss_streak = 0
    else:
        loss_streak += 1
        win_streak = 0
        last_loss_time = time.time()


def update_exit_time(instrument):
    global last_exit_time_nifty, last_exit_time_crude

    if instrument == "NIFTY":
        last_exit_time_nifty = time.time()
    else:
        last_exit_time_crude = time.time()

# -----------------------------
# TRADE MGMT
# -----------------------------
def manage_trade(symbol, entry, qty, exchange, instrument, signal, probability, market_type,
                 gen_id=None):
    """
    Manages an open option trade until it exits via SL, profit-lock, force-close, or flip.

    gen_id  —  trade-generation counter value captured when this trade was placed.
               If the global counter advances (a new flip trade started), this thread
               detects it at the top of the loop and exits cleanly without touching
               the new trade's position.  Pass None to disable generation checking
               (legacy / unit-test path).
    """
    global global_trade_active
    global daily_pnl, trade_count, last_loss_time
    global win_streak, loss_streak
    global portfolio_pnl, peak_portfolio, risk_off
    global max_drawdown, last_exit_time_nifty, last_exit_time_crude
    global nifty_active, crude_active
    global last_exit_reason
    # exit_done is intentionally LOCAL so two concurrent manage_trade threads
    # (old + new after a flip) do not share state.
    exit_done = False
    local_max_profit = 0

    full_symbol = f"{exchange}:{symbol}"
    actual_qty = get_quantity(qty, exchange)
    remaining_qty = actual_qty

    entry_time = time.time()
    partial_booked = False
    pnl = 0
    ltp = entry

    # 🔥 CORE RISK MODEL — Two-tier SL
    # Tier 1: 25% from entry — first touch → expand (spike/wick buffer, don't exit)
    # Tier 2: 45% from entry — second touch → exit immediately
    SL_TIER1 = 0.25   # initial SL
    SL_TIER2 = 0.45   # expanded SL if tier-1 touched by single candle spike
    risk = entry * SL_TIER1
    sl_expanded = False   # True once tier-1 fires and we widen to tier-2

    # Bot always BUYS options (CE or PE).
    # P&L = option_price_now - entry_price (same formula for both CE and PE).
    # SL fires when option premium drops below entry - risk (same for both).
    sl   = entry - risk   # exit if option premium drops by risk %
    peak = entry          # track highest option premium reached

    # sl and spike_sl are always below entry (buying options → loss = premium drop)
    _spike_sl = entry * (1 - SL_TIER2)   # entry - 45%
    send_message(
        f"🚀 NEW TRADE ENTERED\n"
        f"📌 {instrument} {signal} → {symbol}\n"
        f"💰 Entry: ₹{entry:.1f}  |  Qty: {actual_qty}\n"
        f"🛑 Initial SL : ₹{sl:.1f}  (entry − 25%)\n"
        f"🛡️ Spike SL   : ₹{_spike_sl:.1f}  (entry − 45%, only if 25% wicked)\n"
        f"📊 Deployed: ₹{entry * actual_qty:,.0f}"
    )

    try:
        while True:
            # ─────────────────────────────────────────────────────────────────
            # 🔄 GENERATION CHECK — exit if a newer trade has taken over
            # This happens when the arrow flips: the loop exits the old Kite
            # position and starts a new manage_trade thread with an incremented
            # gen_id.  The superseded thread (this one) detects the mismatch
            # here and breaks cleanly, ensuring it never calls exit_position on
            # a position it no longer owns.
            # ─────────────────────────────────────────────────────────────────
            if gen_id is not None:
                _cur_gen = (_nifty_trade_gen[0]     if instrument == "NIFTY"
                            else _banknifty_trade_gen[0] if instrument == "BANKNIFTY"
                            else _crude_trade_gen[0])
                if gen_id != _cur_gen:
                    print(f"ℹ️ manage_trade [{instrument} {signal} {symbol}]: "
                          f"superseded by gen {_cur_gen} (mine={gen_id}) — exiting cleanly",
                          flush=True)
                    # pnl may not be set if we exit on the very first iteration
                    # before ltp was ever fetched — leave it at default 0.
                    break

            ltp = safe_ltp(full_symbol)

            if ltp is None:
                time.sleep(10)
                continue

            # ─────────────────────────────────────────────────────────────────
            # ⏰ FORCE CLOSE — market hours ended
            # NIFTY: force exit at 3:20 PM IST (10 min before 3:30 PM close)
            # CRUDE: force exit at 11:25 PM IST (5 min before 11:30 PM close)
            # Also exits on weekends (Saturday/Sunday) if trade carried over.
            # ─────────────────────────────────────────────────────────────────
            _now = datetime.now(IST)
            _is_weekend = _now.weekday() >= 5   # Saturday=5, Sunday=6
            _nifty_time_over     = (instrument in ("NIFTY", "BANKNIFTY") and
                                    (_now.hour > 15 or (_now.hour == 15 and _now.minute >= 20)))
            _crude_time_over     = (instrument == "CRUDE" and
                                    (_now.hour == 23 and _now.minute >= 25))
            if (_is_weekend or _nifty_time_over or _crude_time_over) and not exit_done:
                _reason = ("weekend" if _is_weekend
                           else f"{instrument} 3:20 PM force close" if _nifty_time_over
                           else "CRUDE 11:25 PM force close")
                # Compute live P&L for the message (current_pnl computed later in loop)
                _ltp_now = ltp if ltp else entry
                _pnl_now = (_ltp_now - entry if signal == "CALL" else entry - _ltp_now) * remaining_qty
                print(f"⏰ Force close ({_reason}): {symbol}")
                send_message(
                    f"⏰ FORCE CLOSE — {_reason.upper()}\n"
                    f"📌 {instrument} {signal} → {symbol}\n"
                    f"💰 P&L: ₹{_pnl_now:.0f}\n"
                    f"🚪 Exiting to avoid overnight / weekend hold"
                )
                exit_position(symbol, remaining_qty, exchange)
                pnl = _pnl_now
                break

            # ===============================
            # 🔥 ULTRA PRO EXIT SYSTEM
            # ===============================

            # 📊 PROFIT — buying options: P&L = (current premium - entry premium)
            # Same formula for both CE and PE — the bot always BUYS, never sells.
            profit = ltp - entry
            peak   = max(peak, ltp)   # track highest premium reached (for profit lock)

            current_pnl = profit * remaining_qty

           # 💰 SMART PARTIAL BOOKING
            if not partial_booked and current_pnl >= 1200:

                # Skip partial if strong trend
                if is_market_trending(
                    CRUDE_TOKEN if instrument == "CRUDE" else (BANKNIFTY_TOKEN if instrument == "BANKNIFTY" else config.NIFTY_TOKEN)
                ):
                    print("🚀 Strong trend — skipping partial booking")
                else:
                    half_qty = remaining_qty // 2

                    if half_qty > 0:
                        exit_position(symbol, half_qty, exchange)

                        remaining_qty -= half_qty
                        partial_booked = True

                        print(f"💰 Partial booked: {symbol}", flush=True)

            # ===============================
            # 💰 GLOBAL PROFIT PROTECTION
            # ===============================
            

            local_max_profit = max(local_max_profit, current_pnl)

            if local_max_profit >= 1000:

                # 🎯 DYNAMIC LOCK
                if local_max_profit < 1500:
                    lock_pct = 0.5
                elif local_max_profit < 3000:
                    lock_pct = 0.7
                else:
                    lock_pct = 0.8

                lock_level = local_max_profit * lock_pct

                print(f"💰 Lock Active → Peak: {local_max_profit:.0f}, Lock: {lock_level:.0f}")

                if current_pnl < lock_level:
                    send_message(
                        f"💰 PROFIT LOCK EXIT\n"
                        f"📌 {instrument} {signal} → {symbol}\n"
                        f"📈 Peak P&L: ₹{local_max_profit:.0f}  |  Current: ₹{current_pnl:.0f}\n"
                        f"🔒 Lock level ({int(lock_pct*100)}%): ₹{lock_level:.0f} — exiting to protect gains"
                    )
                    print("💰 Profit lock triggered — exit")

                    if not exit_done:
                        exit_position(symbol, remaining_qty, exchange)

                    pnl = current_pnl
                    break

            # ===============================
            # 🧠 ATR BASED TRAILING
            # ===============================
            try:
                df_trail = get_cached_data(
                    CRUDE_TOKEN if instrument == "CRUDE" else (BANKNIFTY_TOKEN if instrument == "BANKNIFTY" else config.NIFTY_TOKEN),
                    "5minute",
                    50
                )

                # FIX: use the ATR() function (Wilder RMA), not a high-low range
                atr_series = ATR(df_trail, period=14)
                atr_value = atr_series.iloc[-1] if not atr_series.isna().iloc[-1] else entry * 0.02

            except:
                atr_value = entry * 0.02

            

            # ===============================
            # 🚀 ATR TRAILING (ADAPTIVE)
            # ===============================
            # IMPORTANT: freeze trailing when sl_expanded=True.
            # After tier-1 SL fires we widen to 45% (entry - 45%).
            # Without this guard, the ATR trail immediately recalculates
            # sl from peak (~entry level) and overwrites the 45% expansion,
            # causing a tier-2 exit 1.5 s later instead of giving real room.
            trail_multiplier = 1.2
            old_sl = sl

            if not sl_expanded:
                # Normal trailing: SL rises as option premium rises
                sl = max(sl, peak - (atr_value * trail_multiplier))
                if abs(sl - old_sl) > entry * 0.005:
                    print(f"📈 {instrument} trailing SL: ₹{old_sl:.1f} → ₹{sl:.1f}  P&L: ₹{current_pnl:.0f}", flush=True)

            # ===============================
            # 🔥 STRONG TREND MODE (LET PROFITS RUN)
            # ===============================
            if current_pnl >= 3000 and not sl_expanded:
                sl = max(sl, peak - (atr_value * 0.8))

            # 🔥 HALF TREND EXIT — Pine-accurate: exit when opposite arrow fires on closed candle
            # NOTE: nifty_loop / crude_loop is the PRIMARY flip handler (runs every 10s).
            # This block is a safety fallback that fires inside manage_trade's 1.5s loop,
            # covering the window between nifty_loop iterations.
            # exit_position() is idempotent — it checks Kite positions before placing the
            # sell order, so double-exit is impossible even if both paths fire.
            try:
                df_ht_exit = get_cached_data(
                    CRUDE_TOKEN if instrument == "CRUDE" else (BANKNIFTY_TOKEN if instrument == "BANKNIFTY" else config.NIFTY_TOKEN),
                    "15minute",
                    120
                )

                if df_ht_exit is None or len(df_ht_exit) < 10:
                    raise ValueError("Insufficient data for HT exit check")

                ht_df_exit = halftrend_tv(df_ht_exit)

                # Use closed candle (-2) — same anti-repaint rule as entry
                last_exit = ht_df_exit.iloc[-2]

                # Pine: buySignal = not na(arrowUp) and trend==0 and trend[1]==1
                # Pine: sellSignal = not na(arrowDown) and trend==1 and trend[1]==0
                exit_signal = None
                if last_exit["buy"]:
                    exit_signal = "CALL"
                elif last_exit["sell"]:
                    exit_signal = "PUT"

                if exit_signal and exit_signal != signal:
                    # Check whether nifty_loop already handled this flip
                    # (pos dict will have been cleared or updated to the new signal)
                    pos_dict = nifty_position if instrument == "NIFTY" else crude_position
                    with lock:
                        already_flipped = (pos_dict.get("symbol") != symbol)

                    if already_flipped:
                        # nifty_loop already exited this trade — just break cleanly
                        print(f"ℹ️ HT exit: flip already handled by loop — breaking cleanly")
                        pnl = current_pnl
                        break

                    print(f"🔄 HalfTrend Exit Triggered — new arrow: {exit_signal}, was in: {signal}")
                    print(f"   HT={last_exit['ht']:.2f}  atrHigh={last_exit['atrHigh']:.2f}  atrLow={last_exit['atrLow']:.2f}")
                    send_message(
                        f"🔄 HALFTREND ARROW FLIP — EXIT\n"
                        f"📌 {instrument}: was {signal} on {symbol}\n"
                        f"🔁 New arrow: {exit_signal}\n"
                        f"💰 Trade P&L: ₹{current_pnl:.0f}  |  LTP: ₹{ltp:.1f}\n"
                        f"📊 HT line: {last_exit['ht']:.2f}"
                    )
                    if not exit_done:
                        exit_position(symbol, remaining_qty, exchange)
                        exit_done = True

                    pnl = current_pnl
                    break

            except Exception as e:
                print("HT exit error:", e)

            # ================================================================
            # 🛑 STOP LOSS EXITS  (checked in priority order every 1.5 s)
            # ================================================================

            # ── 1 & 2. Stop Loss (controlled by USE_STOP_LOSS flag) ──────────
            # Set USE_STOP_LOSS = True to re-enable at any time.
            # Profit-lock above is always active regardless of this flag.
            if USE_STOP_LOSS:
                # ── 1. Trailing SL — Two-tier spike protection ───────────────
                # Tier 1 (25%): first touch → expand to 45% (don't exit)
                # Tier 2 (45%): confirmed move → exit immediately
                # Always buying options → SL fires when premium drops below sl level
                trailing_hit = ltp <= sl

                if trailing_hit and not exit_done:
                    if not sl_expanded:
                        # Tier 1 touched: widen SL to 45%, stay in trade
                        sl_expanded = True
                        risk = entry * SL_TIER2
                        sl = entry - risk   # new SL = entry - 45%
                        print(f"⚠️ SL TIER-1 TOUCHED (25%) — expanding to 45% | new SL=₹{sl:.1f}  LTP=₹{ltp:.1f}", flush=True)
                        send_message(
                            f"⚠️ SL SPIKE BUFFER — {instrument} {signal}\n"
                            f"📌 {symbol}\n"
                            f"📉 LTP: ₹{ltp:.1f} touched 25% SL\n"
                            f"🛡️ Expanding SL to 45% → ₹{sl:.1f}\n"
                            f"💔 P&L so far: ₹{current_pnl:.0f}"
                        )
                        # Do NOT break — continue monitoring at 45% SL
                    else:
                        # Tier 2 (45%) confirmed: exit now
                        last_exit_reason = "TRAILING_SL"
                        print(f"🛑 TRAILING SL HIT (45%) | LTP={ltp:.1f}  SL={sl:.1f}", flush=True)
                        send_message(
                            f"🛑 TRAILING SL HIT — EXITING\n"
                            f"📌 {instrument} {signal} → {symbol}\n"
                            f"📉 LTP: ₹{ltp:.1f}  |  SL (45%): ₹{sl:.1f}\n"
                            f"💔 P&L: ₹{current_pnl:.0f}\n"
                            f"📊 Entry: ₹{entry:.1f}  |  Peak: ₹{peak:.1f}"
                        )
                        exit_position(symbol, remaining_qty, exchange)
                        exit_done = True
                        pnl = current_pnl
                        break

                # ── 2. Hard SL safety net ────────────────────────────────────
                # Fires if trailing SL misses (e.g. gap-down open).
                max_loss = risk * remaining_qty
                if current_pnl <= -max_loss and not exit_done:
                    last_exit_reason = "HARD_SL"
                    sl_pct_lbl = "45%" if sl_expanded else "25%"
                    print(f"🛑 HARD SL HIT ({sl_pct_lbl}) | Loss: ₹{current_pnl:.0f} / Limit: ₹{-max_loss:.0f}", flush=True)
                    send_message(
                        f"🛑 HARD STOP LOSS HIT — EXITING\n"
                        f"📌 {instrument} {signal} → {symbol}\n"
                        f"💔 Loss: ₹{current_pnl:.0f}  |  SL limit: ₹{-max_loss:.0f}\n"
                        f"📉 Entry: ₹{entry:.1f}  |  Exit LTP: ₹{ltp:.1f}\n"
                        f"📊 Hard SL = {sl_pct_lbl} of option premium × qty"
                    )
                    exit_position(symbol, remaining_qty, exchange)
                    exit_done = True
                    pnl = current_pnl
                    break

            # 🚀 Smart time exit (only if NO momentum)
            #if time.time() - entry_time > 900:
                
            #    if abs(ltp - entry) < entry * 0.003:
            #        pnl = profit * remaining_qty
            #        if not exit_done:
            #            exit_position(symbol, remaining_qty, exchange)
            #            exit_done = True
            #        send_message(f"⏱ No momentum exit\n{symbol}")
            #        break

            time.sleep(1.5)

    except Exception as e:
        print("Trade error:", e)

    finally:
        # -----------------------------
        # 📊 FINAL UPDATE BLOCK (SAFE)
        # -----------------------------
        with lock:
            global global_trade_active
            global nifty_daily_pnl, crude_daily_pnl
            global nifty_trade_count, crude_trade_count
            global nifty_daily_wins, nifty_daily_losses
            global crude_daily_wins, crude_daily_losses

            portfolio_pnl += pnl
            daily_pnl += pnl

            # ── Per-instrument accounting ──────────────────────────────────
            if instrument == "NIFTY":
                nifty_daily_pnl += pnl
                nifty_trade_count += 1
                if pnl > 0: nifty_daily_wins   += 1
                else:        nifty_daily_losses += 1
            elif instrument == "BANKNIFTY":
                banknifty_daily_pnl += pnl
                banknifty_trade_count += 1
                if pnl > 0: banknifty_daily_wins   += 1
                else:        banknifty_daily_losses += 1
            elif instrument == "SENSEX":
                sensex_daily_pnl += pnl
                sensex_trade_count += 1
                if pnl > 0: sensex_daily_wins   += 1
                else:        sensex_daily_losses += 1
            else:
                crude_daily_pnl += pnl
                crude_trade_count += 1
                if pnl > 0: crude_daily_wins   += 1
                else:        crude_daily_losses += 1

            update_streak(pnl)
            update_exit_time(instrument)

            # 🔥 FLIP RACE CONDITION FIX:
            # Do NOT unconditionally clear nifty_trade_active / crude_trade_active
            # here.  If a HalfTrend flip happened while this trade was running,
            # nifty_loop already registered a NEW trade in nifty_position with a
            # different symbol and set nifty_trade_active=True for that new trade.
            # Clearing it here would wipe the new trade's flag and allow a third
            # order to be placed immediately.
            #
            # run_trade_wrapper.finally already does this with a symbol guard —
            # clearing flags only when pos_dict["symbol"] still matches THIS trade.
            # So we deliberately leave nifty_active / crude_active / trade_active
            # flag management to run_trade_wrapper.finally exclusively.
            #
            # We DO still need to clear the legacy trade_in_progress flags (they
            # are not used in the flip path so it is safe to clear them here).
            global trade_in_progress_nifty, trade_in_progress_crude

            if instrument == "NIFTY":
                trade_in_progress_nifty = False
            else:
                trade_in_progress_crude = False

            exit_emoji = "✅" if pnl > 0 else "❌"
            print(f"✅ {instrument} trade closed — ready for next")

            # ── Trade closed Telegram summary ─────────────────────────────
            _combined_pnl = nifty_daily_pnl + banknifty_daily_pnl + sensex_daily_pnl + crude_daily_pnl
            send_message(
                f"{exit_emoji} TRADE CLOSED — {instrument} {signal}\n"
                f"📌 {symbol}\n"
                f"💰 P&L : ₹{pnl:+.0f}  ({'PROFIT' if pnl > 0 else 'LOSS'})\n"
                f"📊 Entry: ₹{entry:.1f}  |  Exit: ₹{ltp:.1f}\n"
                f"{'─'*28}\n"
                f"📅 NIFTY      : ₹{nifty_daily_pnl:+.0f}\n"
                f"📅 BANKNIFTY  : ₹{banknifty_daily_pnl:+.0f}\n"
                f"📅 SENSEX     : ₹{sensex_daily_pnl:+.0f}\n"
                f"📅 CRUDE      : ₹{crude_daily_pnl:+.0f}\n"
                f"💼 Combined   : ₹{_combined_pnl:+.0f}"
            )

            log_trade_full(symbol, entry, ltp, pnl, instrument, signal, probability)

            trade_count += 1

            # ✅ THREAD SAFE PERFORMANCE LOG
            performance_log.append({
                "result": "WIN" if pnl > 0 else "LOSS",
                "pnl": pnl,
                "time": time.time()
            })

            if len(performance_log) > 100:
                performance_log.pop(0)
                
            
            

        # 📊 STRATEGY LOG (OUTSIDE LOCK OK)
        if market_type in strategy_log:
            strategy_log[market_type].append(pnl) 


# ─────────────────────────────────────────────────────────────────────────────
# 🔒 KITE POSITION GUARD
# Primary defence against placing a second order while one is already live.
# Uses the actual Kite positions API — not in-memory flags — so it works
# correctly even after a bot restart while a trade is open.
# ─────────────────────────────────────────────────────────────────────────────
_kite_pos_cache: dict = {}   # { instrument: (timestamp, result_dict) }
_KITE_POS_TTL = 5            # seconds — avoid hammering the API

def get_open_kite_position(instrument):
    """
    Query Kite for open net positions belonging to this instrument.

    Returns a dict  { "symbol": str, "qty": int, "exchange": str }
    if an open position is found, otherwise returns None.

    instrument : "NIFTY"  → looks for NFO CE/PE positions
                 "CRUDE"  → looks for MCX CE/PE positions
    """
    global _kite_pos_cache

    now_ts = time.time()
    if instrument in _kite_pos_cache:
        cached_ts, cached_result = _kite_pos_cache[instrument]
        if now_ts - cached_ts < _KITE_POS_TTL:
            return cached_result

    try:
        exchange_map = {"NIFTY": "NFO", "BANKNIFTY": "NFO", "SENSEX": "BFO", "CRUDE": "MCX"}
        # Symbol prefix to distinguish NIFTY vs BANKNIFTY (both NFO options)
        prefix_map   = {"NIFTY": "NIFTY", "BANKNIFTY": "BANKNIFTY", "SENSEX": "SENSEX", "CRUDE": None}
        target_exchange = exchange_map.get(instrument)
        sym_prefix      = prefix_map.get(instrument)

        positions = kite.positions().get("net", [])
        for p in positions:
            # Only consider positions with non-zero open quantity
            if p.get("quantity", 0) == 0:
                continue
            if p.get("exchange") != target_exchange:
                continue
            # Must be an option (CE or PE)
            sym = p.get("tradingsymbol", "")
            if not (sym.endswith("CE") or sym.endswith("PE")):
                continue
            # For NFO instruments, check symbol prefix to separate NIFTY from BANKNIFTY
            if sym_prefix and not sym.startswith(sym_prefix):
                continue

            result = {
                "symbol":   sym,
                "qty":      abs(p["quantity"]),
                "exchange": p["exchange"],
            }
            _kite_pos_cache[instrument] = (now_ts, result)
            return result

        _kite_pos_cache[instrument] = (now_ts, None)
        return None

    except Exception as e:
        print(f"⚠️ get_open_kite_position({instrument}) error: {e}")
        return None   # fail-safe: assume no position rather than blocking forever


def restore_position_state_from_kite():
    """
    Called once at bot startup.
    If Kite already has open positions (e.g. from a previous session),
    restore in-memory flags so the bot doesn't place duplicate orders.
    """
    global nifty_position, banknifty_position, sensex_position, crude_position
    global nifty_trade_active, banknifty_trade_active, sensex_trade_active, crude_trade_active
    global global_trade_active
    global last_executed_signal_nifty, last_executed_signal_banknifty
    global last_executed_signal_sensex, last_executed_signal_crude

    for instrument in ("NIFTY", "BANKNIFTY", "SENSEX", "CRUDE"):
        pos = get_open_kite_position(instrument)
        if pos is None:
            continue

        sym      = pos["symbol"]
        qty      = pos["qty"]
        exchange = pos["exchange"]
        signal   = "CALL" if sym.endswith("CE") else "PUT"

        print(f"⚠️ Existing Kite position found on startup: {instrument} {sym} qty={qty}")
        send_message(
            f"⚠️ EXISTING POSITION DETECTED ON STARTUP\n"
            f"📌 {instrument}: {sym}  qty={qty}\n"
            f"🔄 Restoring in-memory state — bot will manage this trade normally"
        )

        with lock:
            if instrument == "NIFTY":
                nifty_position.update({"symbol": sym, "qty": qty,
                                       "exchange": exchange, "signal": signal,
                                       "active": True})
                nifty_trade_active = True
                last_executed_signal_nifty = signal
            elif instrument == "BANKNIFTY":
                banknifty_position.update({"symbol": sym, "qty": qty,
                                           "exchange": exchange, "signal": signal,
                                           "active": True})
                banknifty_trade_active = True
                last_executed_signal_banknifty = signal
            elif instrument == "SENSEX":
                sensex_position.update({"symbol": sym, "qty": qty,
                                        "exchange": exchange, "signal": signal,
                                        "active": True})
                sensex_trade_active = True
                last_executed_signal_sensex = signal
            else:
                crude_position.update({"symbol": sym, "qty": qty,
                                       "exchange": exchange, "signal": signal,
                                       "active": True})
                crude_trade_active = True
                last_executed_signal_crude = signal

            global_trade_active = True


#exit sell orders
def exit_position(symbol, qty, exchange):
    """
    Exit (sell) an open option position.

    Kite API BLOCKS market orders for NFO/MCX via API:
      "Market orders without market protection are not allowed via API."

    Fix: use LIMIT order at a small discount below LTP.
    We retry up to 4 times, each time lowering the price slightly so it
    fills quickly even if the spread is wide or the market is moving fast.

    Slippage ladder (sell price as % of LTP):
      Attempt 1: LTP × 0.995  (-0.5%)
      Attempt 2: LTP × 0.990  (-1.0%)
      Attempt 3: LTP × 0.982  (-1.8%)
      Attempt 4: LTP × 0.970  (-3.0%)   ← last resort aggressive fill
    """
    try:
        # ===============================
        # 🔍 VERIFY POSITION BEFORE EXIT
        # ===============================
        positions = kite.positions()["net"]
        found = False
        actual_qty = qty
        position_product = None
        for p in positions:
            if p["tradingsymbol"] == symbol and p["quantity"] > 0:
                found = True
                actual_qty = p["quantity"]          # use actual open qty from Kite
                position_product = p.get("product") # read product type from Kite position
                break

        if not found:
            print(f"⚠️ No open position found for {symbol} — already exited?")
            return False

        # Use actual qty from Kite (avoids partial-exit mismatch)
        exit_qty = min(qty, actual_qty)

        # FIX: use the SAME product type as the original position.
        # If we bought MIS but exit with NRML (or vice versa), Kite treats the
        # sell as opening a NEW naked short and demands full margin (~₹1.4 lakh).
        # Reading product from the position ensures both legs always match.
        if position_product:
            exit_product = position_product
        else:
            exit_product = "MIS" if exchange == "NFO" else "NRML"

        print(f"🚪 EXITING: {symbol}, qty: {exit_qty}, exchange: {exchange}, product: {exit_product}")

        # ===============================
        # 💰 GET LTP FOR LIMIT PRICE
        # ===============================
        full_symbol = f"{exchange}:{symbol}"
        ltp = safe_ltp(full_symbol)
        if ltp is None or ltp <= 0:
            # Fallback: try quote API
            try:
                q = kite.quote([full_symbol])
                ltp = q[full_symbol]["last_price"]
            except Exception:
                ltp = None

        if ltp is None or ltp <= 0:
            print(f"❌ Cannot get LTP for {symbol} — aborting exit")
            send_message(f"❌ Exit FAILED — no LTP for {symbol}")
            return False

        # ===============================
        # 🚪 EXIT WITH LIMIT ORDER + RETRY
        # ===============================
        slippage_pcts = [0.995, 0.990, 0.982, 0.970]   # increasingly aggressive

        for attempt, slip in enumerate(slippage_pcts, 1):
            exit_price = round(ltp * slip, 1)
            if exit_price <= 0:
                exit_price = 0.5   # minimum valid option price on Kite

            print(f"🚪 Exit attempt {attempt}/4 — LIMIT @ ₹{exit_price:.1f}  (LTP={ltp:.1f}, slip={slip})")

            try:
                order_id = kite.place_order(
                    variety=kite.VARIETY_REGULAR,
                    exchange=exchange,
                    tradingsymbol=symbol,
                    transaction_type=kite.TRANSACTION_TYPE_SELL,
                    quantity=exit_qty,
                    order_type=kite.ORDER_TYPE_LIMIT,
                    price=exit_price,
                    product=exit_product   # matches the original buy product type
                )
                print(f"   Order placed: {order_id}")
            except Exception as oe:
                print(f"   ⚠️ Order placement failed: {oe}")
                time.sleep(1)
                continue

            # Wait up to 3 seconds for fill confirmation
            filled = False
            for _ in range(3):
                time.sleep(1)
                try:
                    orders = kite.orders()
                    for o in orders:
                        if o["order_id"] == order_id:
                            if o["status"] == "COMPLETE":
                                filled = True
                                filled_price = o["average_price"]
                                break
                            elif o["status"] in ["CANCELLED", "REJECTED"]:
                                print(f"   ❌ Order {o['status']}")
                                break
                except Exception:
                    pass
                if filled:
                    break

            if filled:
                # Invalidate Kite position cache so next loop sees the exit
                _kite_pos_cache.pop("NIFTY" if exchange == "NFO" else "CRUDE", None)
                send_message(
                    f"✅ EXIT FILLED\n"
                    f"📌 {symbol}\n"
                    f"💰 Sell price: ₹{filled_price:.1f}  |  Qty: {exit_qty}\n"
                    f"📊 Slippage: {(1-slip)*100:.1f}% from LTP ₹{ltp:.1f}"
                )
                print(f"✅ Exit filled @ ₹{filled_price:.1f}")
                return True

            # Not filled — cancel and try next slippage level
            try:
                kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=order_id)
                print(f"   ↩️ Cancelled unfilled order — trying next level")
            except Exception:
                pass

            time.sleep(0.5)

        # All attempts exhausted
        print(f"❌ Exit FAILED after 4 attempts — {symbol}")
        send_message(
            f"🚨 EXIT FAILED — {symbol}\n"
            f"4 limit order attempts exhausted.\n"
            f"Please exit manually immediately!\n"
            f"Qty: {exit_qty}  |  Last tried price: ₹{round(ltp * slippage_pcts[-1], 1):.1f}"
        )
        return False

    except Exception as e:
        print(f"❌ Exit order failed: {symbol} | Error: {e}")
        send_message(f"🚨 EXIT ERROR — {symbol}\n{e}\nPlease check and exit manually!")
        return False
            

def tune_strategy():
    global adaptive_config

    if len(performance_log) < 10:
        return

    last_trades = performance_log[-10:]
    wins = sum(1 for t in last_trades if t["result"] == "WIN")
    win_rate = wins / len(last_trades)

    print(f"📊 Adaptive Check → Win rate: {win_rate:.2f}")

    # -----------------------------
    # 🔧 ADJUST PROBABILITY
    # -----------------------------
    if win_rate < 0.5:
        adaptive_config["prob_threshold"] = min(65, adaptive_config["prob_threshold"] + 2)
        print("⚠️ Increasing probability threshold")

    elif win_rate > 0.65:
        adaptive_config["prob_threshold"] = max(50, adaptive_config["prob_threshold"] - 2)
        print("🚀 Lowering threshold (more trades)")

    # -----------------------------
    # 🔧 ADJUST TREND FILTER
    # -----------------------------
    if win_rate < 0.5:
        adaptive_config["trend_threshold"] = min(0.002, adaptive_config["trend_threshold"] + 0.0002)

    elif win_rate > 0.65:
        adaptive_config["trend_threshold"] = max(0.001, adaptive_config["trend_threshold"] - 0.0002)

    # -----------------------------
    # 🔧 ADJUST RISK
    # -----------------------------
    if win_rate < 0.45:
        adaptive_config["risk_multiplier"] = 0.8
        print("🛑 Reducing risk")

    elif win_rate > 0.7:
        adaptive_config["risk_multiplier"] = 1.2
        print("📈 Increasing risk")

    print(f"⚙️ New Config: {adaptive_config}")  
    
            
def run_trade_wrapper(symbol, price, lot, exchange, instrument, signal, probability, market_type,
                      gen_id=None):
    """
    Wrapper around manage_trade that safely clears per-instrument state when the trade ends.

    RACE-CONDITION FIX:
    After a HalfTrend flip, the loop exits the old trade AND immediately places a new
    trade in the opposite direction, writing new trade data into the position dict.
    If we unconditionally clear the position dict in the finally block we would wipe the NEW
    trade's state.  We guard this by checking that the symbol we were managing is still
    the current one — if it has already changed (flip happened) we leave state alone.

    gen_id — forwarded to manage_trade so it can self-terminate when superseded.
    """
    global nifty_active, crude_active
    global nifty_trade_active, crude_trade_active, banknifty_trade_active, sensex_trade_active
    global nifty_position, crude_position, banknifty_position, sensex_position

    try:
        manage_trade(symbol, price, lot, exchange, instrument, signal, probability, market_type,
                     gen_id=gen_id)

    finally:
        with lock:
            global global_trade_active
            global last_executed_signal_nifty, last_executed_signal_crude
            global last_executed_signal_banknifty, last_executed_signal_sensex

            if instrument == "NIFTY":
                pos_dict = nifty_position
            elif instrument == "BANKNIFTY":
                pos_dict = banknifty_position
            elif instrument == "SENSEX":
                pos_dict = sensex_position
            else:
                pos_dict = crude_position

            # Only clear state when this trade's symbol is still the active one.
            if pos_dict.get("symbol") == symbol:
                pos_dict.update({"symbol": None, "qty": 0, "exchange": None,
                                 "signal": None, "active": False})

                if instrument == "NIFTY":
                    nifty_active = False
                    nifty_trade_active = False
                    last_executed_signal_nifty = None
                    nifty_loop._carryover_done = None
                    nifty_loop._sig_alerted    = None
                elif instrument == "BANKNIFTY":
                    banknifty_trade_active = False
                    last_executed_signal_banknifty = None
                    banknifty_loop._carryover_done = None
                    banknifty_loop._sig_alerted    = None
                elif instrument == "SENSEX":
                    sensex_trade_active = False
                    last_executed_signal_sensex = None
                    sensex_loop._carryover_done = None
                    sensex_loop._sig_alerted    = None
                else:
                    crude_active = False
                    crude_trade_active = False
                    last_executed_signal_crude = None
                    crude_loop._carryover_done = None
                    crude_loop._sig_alerted    = None

                print(f"♻️ {instrument} re-entry unblocked — will re-enter if arrow still active",
                      flush=True)

            else:
                print(f"ℹ️ run_trade_wrapper: symbol changed ({symbol} → {pos_dict.get('symbol')}) "
                      f"— flip trade active, not clearing new position state")

            # ── Always recompute global_trade_active from all instrument flags ───
            global_trade_active = nifty_trade_active or banknifty_trade_active or sensex_trade_active or crude_trade_active

            
            
def analyze_performance():

    import pandas as pd

    try:
        df = pd.read_csv(TRADE_LOG_FILE)

        if len(df) < 20:
            return

        win_rate = (df["pnl"] > 0).mean()
        avg_profit = df[df["pnl"] > 0]["pnl"].mean() or 0
        avg_loss = df[df["pnl"] <= 0]["pnl"].mean() or 0

        print(f"""
        📊 PERFORMANCE:
        Win Rate: {win_rate:.2f}
        Avg Profit: {avg_profit}
        Avg Loss: {avg_loss}
        """)

        return win_rate, avg_profit, avg_loss

    except Exception as e:
        print("Analysis error:", e)
            
            
            
def get_trade_probability(token, signal, df):
    df = prepare_indicators(df)
    try:
        score = 0
        last = df.iloc[-1]
        prev = df.iloc[-2]

        # VWAP alignment
        vwap = df.iloc[-1]["vwap"]
        if signal == "CALL" and last["close"] > vwap:
            score += 20
        elif signal == "PUT" and last["close"] < vwap:
            score += 20

        # Breakout strength
        if signal == "CALL" and last["close"] > prev["high"]:
            score += 25
        elif signal == "PUT" and last["close"] < prev["low"]:
            score += 25
            
        # Momentum fallback boost
        if signal == "CALL" and last["close"] > prev["close"]:
            score += 10

        if signal == "PUT" and last["close"] < prev["close"]:
            score += 10

        # Volume spike
        vol_ma = df["volume"].rolling(5).mean().iloc[-1]
        if last["volume"] > vol_ma * 1.3:
            score += 20

        # Candle strength
        body = abs(last["close"] - last["open"])
        rng = last["high"] - last["low"]

        if rng > 0 and body > rng * 0.6:
            score += 15

        # 🔥 BOOST BASE PROBABILITY
        base = 40

        final_score = base + score

        return min(final_score, 100)

    except:
        return 0
        
        
def ai_trade_filter(token, signal, df):

    # ❌ News volatility
    if is_news_volatility(token):
        print("🚫 Skipping — news volatility")
        return False

    # ❌ Fake breakout
    if is_false_breakout(token, signal):
        print("🚫 Skipping — fake breakout")
        return False

    # ❌ Reversal trap
    if is_reversal_trap(token, signal):
        print("🚫 Skipping — reversal trap")
        return False

    return True
    
 
 
# -----------------------------
# LAST ACTIVE SIGNAL RESOLVER
# -----------------------------
def get_last_active_signal(ht_df):
    """
    Solves the MIS carry-over problem.

    MIS positions are auto-squared at 3:20 PM every day.
    Next morning the HalfTrend trend may still be active (bullish/bearish)
    but NO new arrow fires — because the trend didn't change overnight.
    The bot would sit idle all day even though the signal is clear.

    This function:
      1. First checks the last closed candle for a fresh arrow (normal path).
      2. If no fresh arrow, scans backward through closed candles to find
         the most recent arrow that still matches the CURRENT trend direction.
      3. Returns that signal so the bot can re-enter at market open.

    Safety rules:
      - Only looks back MAX_LOOKBACK_BARS candles (default 120 = ~5 trading days on 15-min).
      - Signal must AGREE with current trend (trend==0 → CALL, trend==1 → PUT).
      - If the last arrow found disagrees with current trend (reversal happened
        but no re-entry arrow yet), returns None — do not trade.
      - Returns a tuple: (signal, arrow_bar_index, is_fresh)
          signal         : "CALL" | "PUT" | None
          arrow_bar_index: integer index in ht_df of the arrow bar
          is_fresh       : True if arrow is on iloc[-2] (same-day),
                           False if it is a carried-over signal from prior bars
    """
    # 1 trading day ≈ 25 bars (9:15–3:30).  120 bars ≈ 5 trading days (1 full week).
    # Increased from 60 → 120 so arrows from up to ~1 week ago are still detected.
    MAX_LOOKBACK_BARS = 120   # ~5 trading days on 15-min chart

    n = len(ht_df)
    if n < 4:
        return None, None, False

    # Current trend direction from the last closed candle
    current_trend = int(ht_df.iloc[-2]["trend"])   # 0=bullish, 1=bearish
    expected_signal = "CALL" if current_trend == 0 else "PUT"

    # Scan from most-recent closed candle backward.
    #
    # Rule: return the FIRST arrow that matches the current trend direction.
    # BUT — if we encounter an OPPOSITE arrow before finding a matching one,
    # STOP immediately. That opposite arrow means the trend reversed between
    # the old signal and now. Entering on the older arrow would be trading
    # against the reversal.
    #
    # Example that caused the bug (DO NOT revert):
    #   Bar -28: BUY arrow (old crash-bottom signal)
    #   Bar -5:  SELL arrow (trend flipped bearish at 1 PM)
    #   Bar -2:  still shows trend=BULLISH (1-bar lag on last closed candle)
    #   → Without this break, scan skips SELL arrow, finds old BUY → enters CALL ❌
    #   → With this break, scan hits SELL arrow → stops → returns None ✅
    #
    # NOTE: We break on ARROWS (meaningful reversals), NOT on every opposite-trend
    # bar. A single opposite-trend bar with no arrow is just a 1-bar flicker and
    # is ignored — this avoids the original over-sensitivity problem.
    for offset in range(2, min(n, MAX_LOOKBACK_BARS + 2)):
        bar = ht_df.iloc[-offset]

        # ── Matching arrow found — valid entry ───────────────────────────────
        if bar["buy"] and expected_signal == "CALL":
            is_fresh = (offset == 2)
            return "CALL", n - offset, is_fresh

        if bar["sell"] and expected_signal == "PUT":
            is_fresh = (offset == 2)
            return "PUT", n - offset, is_fresh

        # ── Opposite arrow found — trend reversed, stop scanning ─────────────
        if bar["sell"] and expected_signal == "CALL":
            # A sell arrow sits between now and any older buy arrow.
            # That sell arrow invalidates the older buy — do not enter.
            break

        if bar["buy"] and expected_signal == "PUT":
            # A buy arrow sits between now and any older sell arrow.
            # That buy arrow invalidates the older sell — do not enter.
            break

    # No valid arrow found within lookback window
    return None, None, False


# Per-trade state — written only inside lock, read by both loop and manage_trade thread
# These replace the shared current_symbol/qty/exchange globals for flip safety
nifty_position = {"symbol": None, "qty": 0, "exchange": None, "signal": None, "active": False}
crude_position = {"symbol": None, "qty": 0, "exchange": None, "signal": None, "active": False}

# -----------------------------
# THREADS
# -----------------------------
# =========================
# 🔥 NIFTY LOOP (UPDATED)
# =========================

def nifty_loop():
    global last_running_signal, current_symbol, current_qty, current_exchange
    global last_executed_signal_nifty, global_trade_active
    global last_status, last_weak_log_time
    global last_fetch_nifty, cached_nifty_df, cached_nifty_ht
    global nifty_trade_active, nifty_position
    global win_streak, loss_streak

    _nifty_weekend_msg_sent = [False]   # send "sleeping" msg only once per weekend
    _nifty_wakeup_msg_sent  = [False]   # send "waking up" msg only once per Monday

    while True:
        try:
            # ── Instrument kill-switch ────────────────────────────────────────
            if not ENABLE_NIFTY:
                if last_status != "NIFTY_DISABLED":
                    print("⛔ NIFTY trading disabled (ENABLE_NIFTY=False)", flush=True)
                    last_status = "NIFTY_DISABLED"
                time.sleep(30)
                continue

            now_dt = datetime.now(IST)

            # ── Weekend: sleep and do nothing ────────────────────────────────
            if now_dt.weekday() >= 5:   # Saturday=5, Sunday=6
                if not _nifty_weekend_msg_sent[0]:
                    send_message("😴 NIFTY: Weekend — bot sleeping until Monday 8:55 AM IST")
                    _nifty_weekend_msg_sent[0] = True
                    _nifty_wakeup_msg_sent[0]  = False   # reset so Monday msg fires
                time.sleep(300)   # check every 5 min
                continue

            # ── Pre-market: before 9:00 AM on weekdays ───────────────────────
            if now_dt.hour < 9:
                _nifty_weekend_msg_sent[0] = False   # reset for next weekend
                if not _nifty_wakeup_msg_sent[0] and now_dt.weekday() == 0:
                    send_message("🌅 NIFTY: Monday — bot active, waiting for 9:15 AM market open")
                    _nifty_wakeup_msg_sent[0] = True
                time.sleep(60)
                continue

            # ── After market close: sleep until next day ──────────────────────
            # DO NOT break — just sleep so the thread survives to trade tomorrow.
            if now_dt.hour > 15 or (now_dt.hour == 15 and now_dt.minute > 30):
                print("🛑 NIFTY market closed — sleeping until tomorrow 9:00 AM")
                time.sleep(60)   # check every minute, loop will skip until morning
                continue

            # Reset daily stats at start of new trading day
            reset_daily_pnl()

            # Loss streak cooldown — pause then RESET (same fix as CRUDE).
            if loss_streak >= 3:
                print("⚠️ Loss streak >= 3 — pausing NIFTY 15 min then resetting streak", flush=True)
                send_message("❌ NIFTY: 3 consecutive losses — pausing 15 min")
                time.sleep(900)   # 15-minute cooldown
                loss_streak = 0   # reset so trading can resume
                print("♻️ NIFTY loss streak reset — resuming trading", flush=True)
                continue

            # Refresh data cache every 30 seconds
            if time.time() - last_fetch_nifty > 30 or cached_nifty_df is None:
                cached_nifty_df = get_cached_data(config.NIFTY_TOKEN, "15minute", 600)
                if cached_nifty_df is not None and len(cached_nifty_df) >= 120:
                    cached_nifty_ht = halftrend_tv(cached_nifty_df, amplitude=2, channel_deviation=2)
                last_fetch_nifty = time.time()

            if cached_nifty_df is None or len(cached_nifty_df) < 120 or cached_nifty_ht is None:
                time.sleep(10)
                continue

            ht_df = cached_nifty_ht
            current_trend = int(ht_df.iloc[-2]["trend"])
            print("🧠 Current Trend:", "CALL" if current_trend == 0 else "PUT")

            # ── Signal Detection (fresh arrow + carry-over) ───────────────────
            signal, arrow_idx, is_fresh = get_last_active_signal(ht_df)

            arrow_level = None
            if signal is not None and arrow_idx is not None:
                arrow_bar = ht_df.iloc[arrow_idx]
                arrow_level = arrow_bar["atrLow"] if signal == "CALL" else arrow_bar["atrHigh"]
                if is_fresh:
                    tag = "🟢 FRESH" if signal == "CALL" else "🔴 FRESH"
                    print(f"{tag} NIFTY {signal} @ {arrow_level:.2f}  HT={arrow_bar['ht']:.2f}")
                else:
                    bars_ago = len(ht_df) - arrow_idx - 2
                    tag = "🟢 CARRY-OVER" if signal == "CALL" else "🔴 CARRY-OVER"
                    print(f"{tag} NIFTY {signal} — {bars_ago} bars ({bars_ago*15} min) ago @ {arrow_level:.2f}")

            if signal is None:
                status = "NO_ARROW_NIFTY"
                if last_status != status or time.time() - last_weak_log_time > 60:
                    trend_name = "BULLISH" if int(ht_df.iloc[-2]["trend"]) == 0 else "BEARISH"
                    print(f"⏸️ NIFTY: trend={trend_name} but no valid arrow in last 120 bars — waiting")
                    last_status = status
                    last_weak_log_time = time.time()
                time.sleep(10)
                continue

            # ── Daily trade limit check ───────────────────────────────────────
            if nifty_trade_count >= MAX_NIFTY_TRADES_PER_DAY:
                _msg = f"🔒 NIFTY daily limit reached ({nifty_trade_count}/{MAX_NIFTY_TRADES_PER_DAY} trades) — no more trades today"
                if last_status != "NIFTY_LIMIT":
                    print(_msg, flush=True)
                    send_message(f"🔒 NIFTY: {MAX_NIFTY_TRADES_PER_DAY} trades done for today. Resuming tomorrow.")
                    last_status = "NIFTY_LIMIT"
                time.sleep(60)
                continue

            # ── Signal detected — log only, no Telegram ──────────────────────
            if signal is not None and arrow_idx is not None:
                arrow_bar = ht_df.iloc[arrow_idx]
                _level = arrow_bar["atrLow"] if signal == "CALL" else arrow_bar["atrHigh"]
                _bars_ago = len(ht_df) - arrow_idx - 2
                _freshness = "FRESH" if is_fresh else f"CARRY-OVER ({_bars_ago * 15} min ago)"
                print(f"🔔 NIFTY {signal} {_freshness} @ ₹{_level:.2f}", flush=True)

            # ── Carry-over: enter only once per day ───────────────────────────
            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            carryover_key = f"NIFTY_{signal}_{today_str}"
            if not is_fresh:
                if getattr(nifty_loop, "_carryover_done", None) == carryover_key:
                    time.sleep(10)
                    continue

            # ══════════════════════════════════════════════════════════════
            # 📊  STRATEGY FILTERS  (ADX / MTF / VIX / Session)
            # All filters must pass before order placement.
            # ══════════════════════════════════════════════════════════════
            _filter_ok, _filter_reason = apply_entry_filters(
                signal, "NIFTY", cached_nifty_df, config.NIFTY_TOKEN)

            if not _filter_ok:
                print(f"🚫 NIFTY entry blocked — {_filter_reason}", flush=True)
                # Alert once per 15-min candle; re-fires if block reason changes
                _fkey = f"NIFTY_f_{datetime.now(IST).strftime('%Y-%m-%d_%H%M')[:13]}_{_filter_reason[:30]}"
                if getattr(nifty_loop, "_filter_alerted", None) != _fkey:
                    try:
                        send_message(f"🚫 NIFTY ORDER BLOCKED\n{_filter_reason}")
                        nifty_loop._filter_alerted = _fkey
                    except Exception as _e:
                        print(f"⚠️ Filter alert send failed: {_e}", flush=True)
                time.sleep(30)
                continue

            print(f"   {_filter_reason}", flush=True)

            # ══════════════════════════════════════════════════════════════
            # 🔒  ONE-ORDER-AT-A-TIME GUARD (3-layer defence)
            # ══════════════════════════════════════════════════════════════
            # Layer 1 — Kite ground truth (handles bot restarts / flag drift)
            kite_pos = get_open_kite_position("NIFTY")
            if kite_pos:
                kite_sig = "CALL" if kite_pos["symbol"].endswith("CE") else "PUT"

                if kite_sig == signal:
                    # Already have a live NFO position in the same direction — skip
                    with lock:
                        if not nifty_position["active"]:
                            nifty_position.update({
                                "symbol":   kite_pos["symbol"],
                                "qty":      kite_pos["qty"],
                                "exchange": kite_pos["exchange"],
                                "signal":   kite_sig,
                                "active":   True,
                            })
                            nifty_trade_active = True
                    time.sleep(10)
                    continue

                else:
                    # Open position in opposite direction → flip
                    print(f"🔁 NIFTY FLIP (Kite): {kite_sig} → {signal}")
                    exit_ok = exit_position(kite_pos["symbol"], kite_pos["qty"],
                                            kite_pos["exchange"])
                    if exit_ok:
                        send_message(
                            f"🔁 NIFTY flip exit\n"
                            f"Closed: {kite_sig} ({kite_pos['symbol']})\n"
                            f"New signal: {signal}"
                        )
                        _kite_pos_cache.pop("NIFTY", None)
                    else:
                        print("⚠️ NIFTY flip exit failed — retrying next tick")
                        time.sleep(5)
                        continue

                    with lock:
                        nifty_position.update({"symbol": None, "qty": 0,
                                               "exchange": None, "signal": None,
                                               "active": False})
                        nifty_trade_active = False
                        global_trade_active = nifty_trade_active or banknifty_trade_active or sensex_trade_active or crude_trade_active
                        last_executed_signal_nifty = None
                    time.sleep(3)

            else:
                # No live Kite position — sync in-memory if drifted
                with lock:
                    if nifty_position["active"]:
                        print("⚠️ NIFTY in-memory says active but Kite shows no position — resetting")
                        nifty_position.update({"symbol": None, "qty": 0,
                                               "exchange": None, "signal": None,
                                               "active": False})
                        nifty_trade_active = False

            # Layer 2 — in-memory flag (fast path)
            with lock:
                pos_active = nifty_position["active"]
                pos_signal = nifty_position["signal"]

            if pos_active and pos_signal == signal:
                time.sleep(10)
                continue

            # Layer 3 — duplicate prevention for same fresh signal
            if is_fresh and signal == last_executed_signal_nifty:
                time.sleep(10)
                continue

            # ══════════════════════════════════════════════════════════════
            # 🚀  ENTRY — all guards passed, place the order
            # ══════════════════════════════════════════════════════════════
            # FIX: check flag then release lock; sleep OUTSIDE the lock.
            with lock:
                if nifty_trade_active:
                    already_active = True
                else:
                    already_active = False
                    nifty_trade_active = True
                    global_trade_active = True

            if already_active:
                time.sleep(10)
                continue

            print(f"🧠 NIFTY entering: {signal}")
            symbol, price, lot, exchange = find_option(signal, "NIFTY")

            if not symbol or price is None:
                with lock:
                    nifty_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or sensex_trade_active or crude_trade_active
                time.sleep(10)
                continue

            filled_price = place_order(symbol, lot, exchange, "NIFTY")

            if filled_price:
                with lock:
                    # Increment generation INSIDE the lock so manage_trade's
                    # generation check is consistent with position state.
                    _nifty_trade_gen[0] += 1
                    _nifty_gen_id = _nifty_trade_gen[0]
                    nifty_position.update({
                        "symbol":   symbol,
                        "qty":      get_quantity(lot, exchange),
                        "exchange": exchange,
                        "signal":   signal,
                        "active":   True,
                    })
                    last_executed_signal_nifty = signal
                    # NOTE: last_running_signal / current_symbol / current_qty /
                    # current_exchange are legacy shared globals. We no longer
                    # write them here so that concurrent CRUDE trades (morning
                    # session) do not overwrite NIFTY's values and vice versa.
                    # All position state lives in nifty_position / crude_position.

                _kite_pos_cache.pop("NIFTY", None)   # invalidate position cache

                if not is_fresh:
                    nifty_loop._carryover_done = carryover_key
                    send_message(
                        f"♻️ NIFTY carry-over entry\n"
                        f"Signal: {signal} (trend continuing)\n"
                        f"{symbol} @ ₹{filled_price}"
                    )
                else:
                    send_message(f"🆕 NIFTY {signal} entry\n{symbol} @ ₹{filled_price}")

                threading.Thread(
                    target=run_trade_wrapper,
                    args=(symbol, filled_price, lot, exchange, "NIFTY", signal, 0, "TREND"),
                    kwargs={"gen_id": _nifty_gen_id},
                    daemon=True
                ).start()
                print(f"🎯 NIFTY Trade: {symbol} @ ₹{filled_price}  lots={lot}  gen={_nifty_gen_id}")
            else:
                with lock:
                    nifty_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or sensex_trade_active or crude_trade_active

        except Exception as e:
            print("❌ NIFTY LOOP ERROR:", e)
            try:
                if get_open_kite_position("NIFTY") is None:
                    with lock:
                        nifty_trade_active = False
            except Exception:
                pass

        time.sleep(10)


# =====================================================
# 🔥 CRUDE LOOP (ARROW ONLY MODE)
# =====================================================

def crude_loop():
    global last_running_signal, current_symbol, current_qty, current_exchange
    global last_executed_signal_crude, global_trade_active
    global last_status, last_weak_log_time
    global last_fetch_crude, cached_crude_15m, cached_crude_ht
    global crude_trade_active
    global win_streak, loss_streak

    _crude_weekend_msg_sent = [False]

    while True:
        try:
            # ── Instrument kill-switch ────────────────────────────────────────
            if not ENABLE_CRUDE:
                if last_status != "CRUDE_DISABLED":
                    print("⛔ CRUDE trading disabled (ENABLE_CRUDE=False)", flush=True)
                    last_status = "CRUDE_DISABLED"
                time.sleep(30)
                continue

            now_dt = datetime.now(IST)

            # ── Weekend: sleep and do nothing ────────────────────────────────
            if now_dt.weekday() >= 5:   # Saturday=5, Sunday=6
                if not _crude_weekend_msg_sent[0]:
                    send_message("😴 CRUDE: Weekend — bot sleeping until Monday evening session")
                    _crude_weekend_msg_sent[0] = True
                time.sleep(300)
                continue

            # Reset weekend flag on weekdays
            _crude_weekend_msg_sent[0] = False

            # ── Crude trades only during evening session ──────────────────────
            # Session: 3:31 PM – 11:30 PM IST.  Sleep during off-hours.
            if now_dt.hour < 15 or (now_dt.hour == 15 and now_dt.minute <= 30):
                time.sleep(30)
                continue

            # ── After session close: sleep until tomorrow ─────────────────────
            if now_dt.hour == 23 and now_dt.minute >= 31:
                time.sleep(60)
                continue

            # Reset daily stats at start of new day
            reset_daily_pnl()

            # Loss streak cooldown — pause then RESET so CRUDE can resume trading.
            # Without reset, bot loops on this check forever (streak never clears
            # unless a trade wins, but no trades are placed = permanent deadlock).
            if loss_streak >= 3:
                print("⚠️ Loss streak >= 3 — pausing CRUDE 15 min then resetting streak", flush=True)
                send_message("❌ CRUDE: 3 consecutive losses — pausing 15 min")
                time.sleep(900)   # 15-minute cooldown
                loss_streak = 0   # reset so trading can resume after cooldown
                print("♻️ CRUDE loss streak reset — resuming trading", flush=True)
                continue

            # Refresh data cache every 20 seconds
            if time.time() - last_fetch_crude > 20 or cached_crude_15m is None:
                cached_crude_15m = get_cached_data(CRUDE_TOKEN, "15minute", 600)
                # Recompute HalfTrend only when data refreshes
                if cached_crude_15m is not None and len(cached_crude_15m) >= 50:
                    cached_crude_ht = halftrend_tv(cached_crude_15m, amplitude=2, channel_deviation=2)
                last_fetch_crude = time.time()

            if cached_crude_15m is None or len(cached_crude_15m) < 50 or cached_crude_ht is None:
                time.sleep(10)
                continue

            ht_df = cached_crude_ht

            # ── Signal Detection (same carry-over logic as Nifty) ─────────────
            signal, arrow_idx, is_fresh = get_last_active_signal(ht_df)

            arrow_level = None
            if signal is not None and arrow_idx is not None:
                arrow_bar = ht_df.iloc[arrow_idx]
                arrow_level = arrow_bar["atrLow"] if signal == "CALL" else arrow_bar["atrHigh"]
                if is_fresh:
                    print(f"{'🟢' if signal=='CALL' else '🔴'} FRESH CRUDE {signal} @ {arrow_level:.2f}")
                else:
                    bars_ago = len(ht_df) - arrow_idx - 2
                    print(f"{'🟢' if signal=='CALL' else '🔴'} CARRY-OVER CRUDE {signal} — {bars_ago} bars ago @ {arrow_level:.2f}")

            if signal is None:
                time.sleep(10)
                continue

            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            carryover_key = f"CRUDE_{signal}_{today_str}"

            # FIX: Check carry-over guard BEFORE sending Telegram alert.
            # Previously the alert was sent here, but the carry-over guard below
            # would silently skip the order — causing "alert sent but no order" bug.
            if not is_fresh:
                if getattr(crude_loop, "_carryover_done", None) == carryover_key:
                    time.sleep(10)
                    continue

            # ── Daily trade limit check ───────────────────────────────────────
            if crude_trade_count >= MAX_CRUDE_TRADES_PER_DAY:
                _msg = f"🔒 CRUDE daily limit reached ({crude_trade_count}/{MAX_CRUDE_TRADES_PER_DAY} trades) — no more trades today"
                if last_status != "CRUDE_LIMIT":
                    print(_msg, flush=True)
                    send_message(f"🔒 CRUDE: {MAX_CRUDE_TRADES_PER_DAY} trades done for today. Resuming tomorrow.")
                    last_status = "CRUDE_LIMIT"
                time.sleep(60)
                continue

            # ── Signal detected — log only, no Telegram ──────────────────────
            if signal is not None and arrow_idx is not None:
                _arrow_bar_c = ht_df.iloc[arrow_idx]
                _level_c = _arrow_bar_c["atrLow"] if signal == "CALL" else _arrow_bar_c["atrHigh"]
                _bars_ago_c = len(ht_df) - arrow_idx - 2
                _freshness_c = "FRESH" if is_fresh else f"CARRY-OVER ({_bars_ago_c * 15} min ago)"
                print(f"🔔 CRUDE {signal} {_freshness_c} @ ₹{_level_c:.2f}", flush=True)

            # ══════════════════════════════════════════════════════════════
            # 📊  STRATEGY FILTERS  (ADX / MTF / VIX)
            # Note: Session filter not applied for CRUDE (evening session
            # has no lunch dead-zone equivalent).
            # ══════════════════════════════════════════════════════════════
            _filter_ok, _filter_reason = apply_entry_filters(
                signal, "CRUDE", cached_crude_15m, CRUDE_TOKEN)

            if not _filter_ok:
                print(f"🚫 CRUDE entry blocked — {_filter_reason}", flush=True)
                _fkey = f"CRUDE_f_{datetime.now(IST).strftime('%Y-%m-%d_%H%M')[:13]}_{_filter_reason[:30]}"
                if getattr(crude_loop, "_filter_alerted", None) != _fkey:
                    try:
                        send_message(f"🚫 CRUDE ORDER BLOCKED\n{_filter_reason}")
                        crude_loop._filter_alerted = _fkey
                    except Exception as _e:
                        print(f"⚠️ Filter alert send failed: {_e}", flush=True)
                time.sleep(30)
                continue

            print(f"   {_filter_reason}", flush=True)

            # ══════════════════════════════════════════════════════════════
            # 🔒  ONE-ORDER-AT-A-TIME GUARD (3-layer defence)
            # ══════════════════════════════════════════════════════════════
            # Layer 1 — Kite ground truth (handles bot restarts / flag drift)
            # FIX: Layer 1 must run FIRST so that in-memory state is synced
            # from Kite on restart before any duplicate-prevention logic fires.
            kite_pos = get_open_kite_position("CRUDE")
            if kite_pos:
                kite_sig = "CALL" if kite_pos["symbol"].endswith("CE") else "PUT"

                if kite_sig == signal:
                    # Already have an open position in the same direction — skip
                    # Sync in-memory state in case it drifted
                    with lock:
                        if not crude_position["active"]:
                            crude_position.update({
                                "symbol":   kite_pos["symbol"],
                                "qty":      kite_pos["qty"],
                                "exchange": kite_pos["exchange"],
                                "signal":   kite_sig,
                                "active":   True,
                            })
                            crude_trade_active = True
                    time.sleep(10)
                    continue

                else:
                    # Position exists in OPPOSITE direction → flip
                    print(f"🔁 CRUDE FLIP (Kite): {kite_sig} → {signal}")
                    exit_ok = exit_position(kite_pos["symbol"], kite_pos["qty"],
                                            kite_pos["exchange"])
                    if exit_ok:
                        send_message(
                            f"🔁 CRUDE flip exit\n"
                            f"Closed: {kite_sig} ({kite_pos['symbol']})\n"
                            f"New signal: {signal}"
                        )
                        _kite_pos_cache.pop("CRUDE", None)   # invalidate cache
                    else:
                        print("⚠️ CRUDE flip exit failed — retrying next tick")
                        time.sleep(5)
                        continue

                    with lock:
                        crude_position.update({"symbol": None, "qty": 0,
                                               "exchange": None, "signal": None,
                                               "active": False})
                        crude_trade_active = False
                        global_trade_active = nifty_trade_active or banknifty_trade_active or sensex_trade_active or crude_trade_active
                        last_executed_signal_crude = None   # FIX: reset so flip re-entry isn't blocked by Layer 3
                    # FIX: reset carryover guard so new direction isn't blocked
                    crude_loop._carryover_done = None
                    crude_loop._sig_alerted = None
                    time.sleep(3)   # let Kite process exit before re-entry

            else:
                # No open Kite position — sync in-memory state if it drifted
                with lock:
                    if crude_position["active"]:
                        print("⚠️ CRUDE in-memory says active but Kite shows no position — resetting")
                        crude_position.update({"symbol": None, "qty": 0,
                                               "exchange": None, "signal": None,
                                               "active": False})
                        crude_trade_active = False

            # Layer 2 — in-memory flag (fast path, no API call)
            with lock:
                pos_active   = crude_position["active"]
                pos_signal   = crude_position["signal"]

            if pos_active and pos_signal == signal:
                time.sleep(10)
                continue

            # Layer 3 — duplicate prevention for same fresh signal
            # FIX: moved here (after Layer 1 Kite sync) so that a bot restart
            # with an open Kite position still syncs in-memory state correctly
            # before the duplicate-signal check short-circuits the loop.
            if is_fresh and signal == last_executed_signal_crude:
                time.sleep(10)
                continue

            # ══════════════════════════════════════════════════════════════
            # 🚀  ENTRY — all guards passed, place the order
            # ══════════════════════════════════════════════════════════════
            # FIX: acquire lock → check flag → set flag → release lock.
            # Do NOT sleep inside the lock (that holds the lock and blocks
            # run_trade_wrapper's finally block from clearing state).
            with lock:
                if crude_trade_active:
                    already_active = True
                else:
                    already_active = False
                    crude_trade_active = True
                    global_trade_active = True

            if already_active:
                time.sleep(10)   # sleep OUTSIDE lock
                continue

            print(f"🧠 CRUDE Arrow Detected: {signal}")
            symbol, price, lot, exchange = find_option(signal, "CRUDE")
            print(f"   find_option → symbol={symbol} price={price} lot={lot} exchange={exchange}")

            if not symbol:
                # Send alert only once per 15-min candle to avoid Telegram spam
                _noopt_key = f"CRUDE_noopt_{datetime.now(IST).strftime('%Y-%m-%d_%H%M')[:14]}"
                if getattr(crude_loop, "_noopt_alerted", None) != _noopt_key:
                    send_message(
                        f"⚠️ CRUDE {signal}: No suitable option found\n"
                        f"Check option chain — price may be out of range or expiry unavailable"
                    )
                    crude_loop._noopt_alerted = _noopt_key
                with lock:
                    crude_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or sensex_trade_active or crude_trade_active
                time.sleep(30)
                continue

            if symbol:
                filled_price = place_order(symbol, lot, exchange, "CRUDE")
                if filled_price:
                    with lock:
                        # Increment generation INSIDE the lock so manage_trade's
                        # generation check is consistent with position state.
                        _crude_trade_gen[0] += 1
                        _crude_gen_id = _crude_trade_gen[0]
                        crude_position.update({
                            "symbol":   symbol,
                            "qty":      get_quantity(lot, exchange),
                            "exchange": exchange,
                            "signal":   signal,
                            "active":   True,
                        })
                        last_executed_signal_crude = signal
                        # NOTE: legacy shared globals (last_running_signal,
                        # current_symbol, current_qty, current_exchange) are no
                        # longer written here — see nifty_loop comment above.

                    _kite_pos_cache.pop("CRUDE", None)   # invalidate position cache

                    if not is_fresh:
                        crude_loop._carryover_done = carryover_key
                        send_message(
                            f"♻️ CRUDE carry-over entry\n"
                            f"Signal: {signal} (trend continuing)\n"
                            f"{symbol} @ ₹{filled_price}"
                        )

                    threading.Thread(
                        target=run_trade_wrapper,
                        args=(symbol, filled_price, lot, exchange, "CRUDE", signal, 0, "TREND"),
                        kwargs={"gen_id": _crude_gen_id},
                        daemon=True
                    ).start()
                    print(f"🎯 CRUDE Trade: {symbol} @ ₹{filled_price}  lots={lot}  gen={_crude_gen_id}")
                else:
                    with lock:
                        crude_trade_active = False
                        global_trade_active = nifty_trade_active or banknifty_trade_active or sensex_trade_active or crude_trade_active
            else:
                with lock:
                    crude_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or sensex_trade_active or crude_trade_active

        except Exception as e:
            print("❌ CRUDE LOOP ERROR:", e)
            # Safety reset: if flag was set True before the exception,
            # only reset it if Kite confirms no open position
            try:
                if get_open_kite_position("CRUDE") is None:
                    with lock:
                        crude_trade_active = False
            except Exception:
                pass

        time.sleep(10)

# =====================================================
# 🏦 BANKNIFTY LOOP
# =====================================================

def banknifty_loop():
    global last_executed_signal_banknifty, global_trade_active
    global last_status, last_weak_log_time
    global last_fetch_banknifty, cached_banknifty_df, cached_banknifty_ht
    global banknifty_trade_active, banknifty_position
    global win_streak, loss_streak

    _bn_weekend_msg_sent = [False]
    _bn_wakeup_msg_sent  = [False]

    while True:
        try:
            # ── Instrument kill-switch ────────────────────────────────────────
            if not ENABLE_BANKNIFTY:
                if last_status != "BANKNIFTY_DISABLED":
                    print("⛔ BANKNIFTY trading disabled (ENABLE_BANKNIFTY=False)", flush=True)
                    last_status = "BANKNIFTY_DISABLED"
                time.sleep(30)
                continue

            now_dt = datetime.now(IST)

            # ── Weekend: sleep and do nothing ────────────────────────────────
            if now_dt.weekday() >= 5:
                if not _bn_weekend_msg_sent[0]:
                    send_message("😴 BANKNIFTY: Weekend — bot sleeping until Monday 8:55 AM IST")
                    _bn_weekend_msg_sent[0] = True
                    _bn_wakeup_msg_sent[0]  = False
                time.sleep(300)
                continue

            # ── Pre-market: before 9:00 AM on weekdays ───────────────────────
            if now_dt.hour < 9:
                _bn_weekend_msg_sent[0] = False
                if not _bn_wakeup_msg_sent[0] and now_dt.weekday() == 0:
                    send_message("🌅 BANKNIFTY: Monday — bot active, waiting for 9:15 AM market open")
                    _bn_wakeup_msg_sent[0] = True
                time.sleep(60)
                continue

            # ── After market close: sleep until next day ──────────────────────
            if now_dt.hour > 15 or (now_dt.hour == 15 and now_dt.minute > 30):
                print("🛑 BANKNIFTY market closed — sleeping until tomorrow 9:00 AM")
                time.sleep(60)
                continue

            # Reset daily stats at start of new trading day
            reset_daily_pnl()

            # Loss streak cooldown
            if loss_streak >= 3:
                print("⚠️ Loss streak >= 3 — pausing BANKNIFTY 15 min then resetting streak", flush=True)
                send_message("❌ BANKNIFTY: 3 consecutive losses — pausing 15 min")
                time.sleep(900)
                loss_streak = 0
                print("♻️ BANKNIFTY loss streak reset — resuming trading", flush=True)
                continue

            # Refresh data cache every 30 seconds
            if time.time() - last_fetch_banknifty > 30 or cached_banknifty_df is None:
                cached_banknifty_df = get_cached_data(BANKNIFTY_TOKEN, "15minute", 600)
                if cached_banknifty_df is not None and len(cached_banknifty_df) >= 120:
                    cached_banknifty_ht = halftrend_tv(cached_banknifty_df, amplitude=2, channel_deviation=2)
                last_fetch_banknifty = time.time()

            if cached_banknifty_df is None or len(cached_banknifty_df) < 120 or cached_banknifty_ht is None:
                time.sleep(10)
                continue

            ht_df = cached_banknifty_ht
            current_trend = int(ht_df.iloc[-2]["trend"])
            print("🧠 BANKNIFTY Trend:", "CALL" if current_trend == 0 else "PUT")

            # ── Signal Detection (fresh arrow + carry-over) ───────────────────
            signal, arrow_idx, is_fresh = get_last_active_signal(ht_df)

            arrow_level = None
            if signal is not None and arrow_idx is not None:
                arrow_bar = ht_df.iloc[arrow_idx]
                arrow_level = arrow_bar["atrLow"] if signal == "CALL" else arrow_bar["atrHigh"]
                if is_fresh:
                    tag = "🟢 FRESH" if signal == "CALL" else "🔴 FRESH"
                    print(f"{tag} BANKNIFTY {signal} @ {arrow_level:.2f}  HT={arrow_bar['ht']:.2f}")
                else:
                    bars_ago = len(ht_df) - arrow_idx - 2
                    tag = "🟢 CARRY-OVER" if signal == "CALL" else "🔴 CARRY-OVER"
                    print(f"{tag} BANKNIFTY {signal} — {bars_ago} bars ({bars_ago*15} min) ago @ {arrow_level:.2f}")

            if signal is None:
                status = "NO_ARROW_BANKNIFTY"
                if last_status != status or time.time() - last_weak_log_time > 60:
                    trend_name = "BULLISH" if int(ht_df.iloc[-2]["trend"]) == 0 else "BEARISH"
                    print(f"⏸️ BANKNIFTY: trend={trend_name} but no valid arrow in last 120 bars — waiting")
                    last_status = status
                    last_weak_log_time = time.time()
                time.sleep(10)
                continue

            # ── Daily trade limit check ───────────────────────────────────────
            if banknifty_trade_count >= MAX_BANKNIFTY_TRADES_PER_DAY:
                _msg = f"🔒 BANKNIFTY daily limit reached ({banknifty_trade_count}/{MAX_BANKNIFTY_TRADES_PER_DAY} trades) — no more trades today"
                if last_status != "BANKNIFTY_LIMIT":
                    print(_msg, flush=True)
                    send_message(f"🔒 BANKNIFTY: {MAX_BANKNIFTY_TRADES_PER_DAY} trades done for today. Resuming tomorrow.")
                    last_status = "BANKNIFTY_LIMIT"
                time.sleep(60)
                continue

            # ── Signal detected — log only, no Telegram ──────────────────────
            if signal is not None and arrow_idx is not None:
                arrow_bar = ht_df.iloc[arrow_idx]
                _level = arrow_bar["atrLow"] if signal == "CALL" else arrow_bar["atrHigh"]
                _bars_ago = len(ht_df) - arrow_idx - 2
                _freshness = "FRESH" if is_fresh else f"CARRY-OVER ({_bars_ago * 15} min ago)"
                print(f"🔔 BANKNIFTY {signal} {_freshness} @ ₹{_level:.2f}", flush=True)

            # ── Carry-over: enter only once per day ───────────────────────────
            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            carryover_key = f"BANKNIFTY_{signal}_{today_str}"
            if not is_fresh:
                if getattr(banknifty_loop, "_carryover_done", None) == carryover_key:
                    time.sleep(10)
                    continue

            # ══════════════════════════════════════════════════════════════
            # 📊  STRATEGY FILTERS  (ADX / MTF / VIX / Session)
            # ══════════════════════════════════════════════════════════════
            _filter_ok, _filter_reason = apply_entry_filters(
                signal, "BANKNIFTY", cached_banknifty_df, BANKNIFTY_TOKEN)

            if not _filter_ok:
                print(f"🚫 BANKNIFTY entry blocked — {_filter_reason}", flush=True)
                _fkey = f"BANKNIFTY_f_{datetime.now(IST).strftime('%Y-%m-%d_%H%M')[:13]}_{_filter_reason[:30]}"
                if getattr(banknifty_loop, "_filter_alerted", None) != _fkey:
                    try:
                        send_message(f"🚫 BANKNIFTY ORDER BLOCKED\n{_filter_reason}")
                        banknifty_loop._filter_alerted = _fkey
                    except Exception as _e:
                        print(f"⚠️ Filter alert send failed: {_e}", flush=True)
                time.sleep(30)
                continue

            print(f"   {_filter_reason}", flush=True)

            # ══════════════════════════════════════════════════════════════
            # 🔒  ONE-ORDER-AT-A-TIME GUARD (3-layer defence)
            # ══════════════════════════════════════════════════════════════
            # Layer 1 — Kite ground truth
            kite_pos = get_open_kite_position("BANKNIFTY")
            if kite_pos:
                kite_sig = "CALL" if kite_pos["symbol"].endswith("CE") else "PUT"

                if kite_sig == signal:
                    with lock:
                        if not banknifty_position["active"]:
                            banknifty_position.update({
                                "symbol":   kite_pos["symbol"],
                                "qty":      kite_pos["qty"],
                                "exchange": kite_pos["exchange"],
                                "signal":   kite_sig,
                                "active":   True,
                            })
                            banknifty_trade_active = True
                    time.sleep(10)
                    continue

                else:
                    # Open position in opposite direction → flip
                    print(f"🔁 BANKNIFTY FLIP (Kite): {kite_sig} → {signal}")
                    exit_ok = exit_position(kite_pos["symbol"], kite_pos["qty"],
                                            kite_pos["exchange"])
                    if exit_ok:
                        send_message(
                            f"🔁 BANKNIFTY flip exit\n"
                            f"Closed: {kite_sig} ({kite_pos['symbol']})\n"
                            f"New signal: {signal}"
                        )
                        _kite_pos_cache.pop("BANKNIFTY", None)
                    else:
                        print("⚠️ BANKNIFTY flip exit failed — retrying next tick")
                        time.sleep(5)
                        continue

                    with lock:
                        banknifty_position.update({"symbol": None, "qty": 0,
                                                   "exchange": None, "signal": None,
                                                   "active": False})
                        banknifty_trade_active = False
                        global_trade_active = nifty_trade_active or banknifty_trade_active or sensex_trade_active or crude_trade_active
                        last_executed_signal_banknifty = None
                    banknifty_loop._carryover_done = None
                    banknifty_loop._sig_alerted    = None
                    time.sleep(3)

            else:
                # No live Kite position — sync in-memory if drifted
                with lock:
                    if banknifty_position["active"]:
                        print("⚠️ BANKNIFTY in-memory says active but Kite shows no position — resetting")
                        banknifty_position.update({"symbol": None, "qty": 0,
                                                   "exchange": None, "signal": None,
                                                   "active": False})
                        banknifty_trade_active = False

            # Layer 2 — in-memory flag (fast path)
            with lock:
                pos_active = banknifty_position["active"]
                pos_signal = banknifty_position["signal"]

            if pos_active and pos_signal == signal:
                time.sleep(10)
                continue

            # Layer 3 — duplicate prevention for same fresh signal
            if is_fresh and signal == last_executed_signal_banknifty:
                time.sleep(10)
                continue

            # ══════════════════════════════════════════════════════════════
            # 🚀  ENTRY — all guards passed, place the order
            # ══════════════════════════════════════════════════════════════
            with lock:
                if banknifty_trade_active:
                    already_active = True
                else:
                    already_active = False
                    banknifty_trade_active = True
                    global_trade_active = True

            if already_active:
                time.sleep(10)
                continue

            print(f"🧠 BANKNIFTY entering: {signal}")
            symbol, price, lot, exchange = find_option(signal, "BANKNIFTY")

            if not symbol or price is None:
                with lock:
                    banknifty_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or sensex_trade_active or crude_trade_active
                time.sleep(10)
                continue

            filled_price = place_order(symbol, lot, exchange, "BANKNIFTY")

            if filled_price:
                with lock:
                    _banknifty_trade_gen[0] += 1
                    _bn_gen_id = _banknifty_trade_gen[0]
                    banknifty_position.update({
                        "symbol":   symbol,
                        "qty":      get_quantity(lot, exchange, "BANKNIFTY"),
                        "exchange": exchange,
                        "signal":   signal,
                        "active":   True,
                    })
                    last_executed_signal_banknifty = signal

                _kite_pos_cache.pop("BANKNIFTY", None)

                if not is_fresh:
                    banknifty_loop._carryover_done = carryover_key
                    send_message(
                        f"♻️ BANKNIFTY carry-over entry\n"
                        f"Signal: {signal} (trend continuing)\n"
                        f"{symbol} @ ₹{filled_price}"
                    )
                else:
                    send_message(f"🆕 BANKNIFTY {signal} entry\n{symbol} @ ₹{filled_price}")

                threading.Thread(
                    target=run_trade_wrapper,
                    args=(symbol, filled_price, lot, exchange, "BANKNIFTY", signal, 0, "TREND"),
                    kwargs={"gen_id": _bn_gen_id},
                    daemon=True
                ).start()
                print(f"🎯 BANKNIFTY Trade: {symbol} @ ₹{filled_price}  lots={lot}  gen={_bn_gen_id}")
            else:
                with lock:
                    banknifty_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or sensex_trade_active or crude_trade_active

        except Exception as e:
            print("❌ BANKNIFTY LOOP ERROR:", e)
            try:
                if get_open_kite_position("BANKNIFTY") is None:
                    with lock:
                        banknifty_trade_active = False
            except Exception:
                pass

        time.sleep(10)


# =====================================================
# 📊 SENSEX LOOP (BSE F&O — same session as NIFTY)
# =====================================================

def sensex_loop():
    global last_executed_signal_sensex, global_trade_active
    global last_status, last_weak_log_time
    global last_fetch_sensex, cached_sensex_df, cached_sensex_ht
    global sensex_trade_active, sensex_position
    global win_streak, loss_streak

    _sx_weekend_msg_sent = [False]
    _sx_wakeup_msg_sent  = [False]

    while True:
        try:
            # ── Instrument kill-switch ────────────────────────────────────────
            if not ENABLE_SENSEX:
                if last_status != "SENSEX_DISABLED":
                    print("⛔ SENSEX trading disabled (ENABLE_SENSEX=False)", flush=True)
                    last_status = "SENSEX_DISABLED"
                time.sleep(30)
                continue

            now_dt = datetime.now(IST)

            # ── Weekend: sleep and do nothing ────────────────────────────────
            if now_dt.weekday() >= 5:
                if not _sx_weekend_msg_sent[0]:
                    send_message("😴 SENSEX: Weekend — bot sleeping until Monday 8:55 AM IST")
                    _sx_weekend_msg_sent[0] = True
                    _sx_wakeup_msg_sent[0]  = False
                time.sleep(300)
                continue

            # ── Pre-market: before 9:00 AM on weekdays ───────────────────────
            if now_dt.hour < 9:
                _sx_weekend_msg_sent[0] = False
                if not _sx_wakeup_msg_sent[0] and now_dt.weekday() == 0:
                    send_message("🌅 SENSEX: Monday — bot active, waiting for 9:15 AM market open")
                    _sx_wakeup_msg_sent[0] = True
                time.sleep(60)
                continue

            # ── After market close: sleep until next day ──────────────────────
            if now_dt.hour > 15 or (now_dt.hour == 15 and now_dt.minute > 30):
                print("🛑 SENSEX market closed — sleeping until tomorrow 9:00 AM")
                time.sleep(60)
                continue

            # Reset daily stats at start of new trading day
            reset_daily_pnl()

            # Loss streak cooldown
            if loss_streak >= 3:
                print("⚠️ Loss streak >= 3 — pausing SENSEX 15 min then resetting streak", flush=True)
                send_message("❌ SENSEX: 3 consecutive losses — pausing 15 min")
                time.sleep(900)
                loss_streak = 0
                print("♻️ SENSEX loss streak reset — resuming trading", flush=True)
                continue

            # Refresh data cache every 30 seconds
            if time.time() - last_fetch_sensex > 30 or cached_sensex_df is None:
                cached_sensex_df = get_cached_data(SENSEX_TOKEN, "15minute", 600)
                if cached_sensex_df is not None and len(cached_sensex_df) >= 120:
                    cached_sensex_ht = halftrend_tv(cached_sensex_df, amplitude=2, channel_deviation=2)
                last_fetch_sensex = time.time()

            if cached_sensex_df is None or len(cached_sensex_df) < 120 or cached_sensex_ht is None:
                time.sleep(10)
                continue

            ht_df = cached_sensex_ht
            current_trend = int(ht_df.iloc[-2]["trend"])
            print("🧠 SENSEX Trend:", "CALL" if current_trend == 0 else "PUT")

            # ── Signal Detection (fresh arrow + carry-over) ───────────────────
            signal, arrow_idx, is_fresh = get_last_active_signal(ht_df)

            arrow_level = None
            if signal is not None and arrow_idx is not None:
                arrow_bar = ht_df.iloc[arrow_idx]
                arrow_level = arrow_bar["atrLow"] if signal == "CALL" else arrow_bar["atrHigh"]
                if is_fresh:
                    tag = "🟢 FRESH" if signal == "CALL" else "🔴 FRESH"
                    print(f"{tag} SENSEX {signal} @ {arrow_level:.2f}  HT={arrow_bar['ht']:.2f}")
                else:
                    bars_ago = len(ht_df) - arrow_idx - 2
                    tag = "🟢 CARRY-OVER" if signal == "CALL" else "🔴 CARRY-OVER"
                    print(f"{tag} SENSEX {signal} — {bars_ago} bars ({bars_ago*15} min) ago @ {arrow_level:.2f}")

            if signal is None:
                status = "NO_ARROW_SENSEX"
                if last_status != status or time.time() - last_weak_log_time > 60:
                    trend_name = "BULLISH" if int(ht_df.iloc[-2]["trend"]) == 0 else "BEARISH"
                    print(f"⏸️ SENSEX: trend={trend_name} but no valid arrow in last 120 bars — waiting")
                    last_status = status
                    last_weak_log_time = time.time()
                time.sleep(10)
                continue

            # ── Daily trade limit check ───────────────────────────────────────
            if sensex_trade_count >= MAX_SENSEX_TRADES_PER_DAY:
                _msg = f"🔒 SENSEX daily limit reached ({sensex_trade_count}/{MAX_SENSEX_TRADES_PER_DAY} trades) — no more trades today"
                if last_status != "SENSEX_LIMIT":
                    print(_msg, flush=True)
                    send_message(f"🔒 SENSEX: {MAX_SENSEX_TRADES_PER_DAY} trades done for today. Resuming tomorrow.")
                    last_status = "SENSEX_LIMIT"
                time.sleep(60)
                continue

            # ── Signal detected — log only, no Telegram ──────────────────────
            if signal is not None and arrow_idx is not None:
                arrow_bar = ht_df.iloc[arrow_idx]
                _level = arrow_bar["atrLow"] if signal == "CALL" else arrow_bar["atrHigh"]
                _bars_ago = len(ht_df) - arrow_idx - 2
                _freshness = "FRESH" if is_fresh else f"CARRY-OVER ({_bars_ago * 15} min ago)"
                print(f"🔔 SENSEX {signal} {_freshness} @ ₹{_level:.2f}", flush=True)

            # ── Carry-over: enter only once per day ───────────────────────────
            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            carryover_key = f"SENSEX_{signal}_{today_str}"
            if not is_fresh:
                if getattr(sensex_loop, "_carryover_done", None) == carryover_key:
                    time.sleep(10)
                    continue

            # ══════════════════════════════════════════════════════════════
            # 📊  STRATEGY FILTERS  (ADX / MTF / VIX / Session)
            # ══════════════════════════════════════════════════════════════
            _filter_ok, _filter_reason = apply_entry_filters(
                signal, "SENSEX", cached_sensex_df, SENSEX_TOKEN)

            if not _filter_ok:
                print(f"🚫 SENSEX entry blocked — {_filter_reason}", flush=True)
                _fkey = f"SENSEX_f_{datetime.now(IST).strftime('%Y-%m-%d_%H%M')[:13]}_{_filter_reason[:30]}"
                if getattr(sensex_loop, "_filter_alerted", None) != _fkey:
                    try:
                        send_message(f"🚫 SENSEX ORDER BLOCKED\n{_filter_reason}")
                        sensex_loop._filter_alerted = _fkey
                    except Exception as _e:
                        print(f"⚠️ Filter alert send failed: {_e}", flush=True)
                time.sleep(30)
                continue

            print(f"   {_filter_reason}", flush=True)

            # ══════════════════════════════════════════════════════════════
            # 🔒  ONE-ORDER-AT-A-TIME GUARD (3-layer defence)
            # ══════════════════════════════════════════════════════════════
            # Layer 1 — Kite ground truth
            kite_pos = get_open_kite_position("SENSEX")
            if kite_pos:
                kite_sig = "CALL" if kite_pos["symbol"].endswith("CE") else "PUT"

                if kite_sig == signal:
                    with lock:
                        if not sensex_position["active"]:
                            sensex_position.update({
                                "symbol":   kite_pos["symbol"],
                                "qty":      kite_pos["qty"],
                                "exchange": kite_pos["exchange"],
                                "signal":   kite_sig,
                                "active":   True,
                            })
                            sensex_trade_active = True
                    time.sleep(10)
                    continue

                else:
                    # Open position in opposite direction → flip
                    print(f"🔁 SENSEX FLIP (Kite): {kite_sig} → {signal}")
                    exit_ok = exit_position(kite_pos["symbol"], kite_pos["qty"],
                                            kite_pos["exchange"])
                    if exit_ok:
                        send_message(
                            f"🔁 SENSEX flip exit\n"
                            f"Closed: {kite_sig} ({kite_pos['symbol']})\n"
                            f"New signal: {signal}"
                        )
                        _kite_pos_cache.pop("SENSEX", None)
                    else:
                        print("⚠️ SENSEX flip exit failed — retrying next tick")
                        time.sleep(5)
                        continue

                    with lock:
                        sensex_position.update({"symbol": None, "qty": 0,
                                                "exchange": None, "signal": None,
                                                "active": False})
                        sensex_trade_active = False
                        global_trade_active = nifty_trade_active or banknifty_trade_active or sensex_trade_active or crude_trade_active
                        last_executed_signal_sensex = None
                    sensex_loop._carryover_done = None
                    sensex_loop._sig_alerted    = None
                    time.sleep(3)

            else:
                # No live Kite position — sync in-memory if drifted
                with lock:
                    if sensex_position["active"]:
                        print("⚠️ SENSEX in-memory says active but Kite shows no position — resetting")
                        sensex_position.update({"symbol": None, "qty": 0,
                                                "exchange": None, "signal": None,
                                                "active": False})
                        sensex_trade_active = False

            # Layer 2 — in-memory flag (fast path)
            with lock:
                pos_active = sensex_position["active"]
                pos_signal = sensex_position["signal"]

            if pos_active and pos_signal == signal:
                time.sleep(10)
                continue

            # Layer 3 — duplicate prevention for same fresh signal
            if is_fresh and signal == last_executed_signal_sensex:
                time.sleep(10)
                continue

            # ══════════════════════════════════════════════════════════════
            # 🚀  ENTRY — all guards passed, place the order
            # ══════════════════════════════════════════════════════════════
            with lock:
                if sensex_trade_active:
                    already_active = True
                else:
                    already_active = False
                    sensex_trade_active = True
                    global_trade_active = True

            if already_active:
                time.sleep(10)
                continue

            print(f"🧠 SENSEX entering: {signal}")
            symbol, price, lot, exchange = find_option(signal, "SENSEX")

            if not symbol or price is None:
                with lock:
                    sensex_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or sensex_trade_active or crude_trade_active
                time.sleep(10)
                continue

            filled_price = place_order(symbol, lot, exchange, "SENSEX")

            if filled_price:
                with lock:
                    _sensex_trade_gen[0] += 1
                    _sx_gen_id = _sensex_trade_gen[0]
                    sensex_position.update({
                        "symbol":   symbol,
                        "qty":      get_quantity(lot, exchange, "SENSEX"),
                        "exchange": exchange,
                        "signal":   signal,
                        "active":   True,
                    })
                    last_executed_signal_sensex = signal

                _kite_pos_cache.pop("SENSEX", None)

                if not is_fresh:
                    sensex_loop._carryover_done = carryover_key
                    send_message(
                        f"♻️ SENSEX carry-over entry\n"
                        f"Signal: {signal} (trend continuing)\n"
                        f"{symbol} @ ₹{filled_price}"
                    )
                else:
                    send_message(f"🆕 SENSEX {signal} entry\n{symbol} @ ₹{filled_price}")

                threading.Thread(
                    target=run_trade_wrapper,
                    args=(symbol, filled_price, lot, exchange, "SENSEX", signal, 0, "TREND"),
                    kwargs={"gen_id": _sx_gen_id},
                    daemon=True
                ).start()
                print(f"🎯 SENSEX Trade: {symbol} @ ₹{filled_price}  lots={lot}  gen={_sx_gen_id}")
            else:
                with lock:
                    sensex_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or sensex_trade_active or crude_trade_active

        except Exception as e:
            print("❌ SENSEX LOOP ERROR:", e)
            try:
                if get_open_kite_position("SENSEX") is None:
                    with lock:
                        sensex_trade_active = False
            except Exception:
                pass

        time.sleep(10)


#==================
def get_strike_mode(token):

    try:
        now = datetime.now()

        df = get_cached_data(token, "5minute", 20)

        if df is None or len(df) < 20:
            return "ATM"

        df = df.copy()

        # -----------------------------
        # VWAP
        # -----------------------------
        df["vwap"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()

        last = df.iloc[-1]

        vwap_distance = abs(last["close"] - last["vwap"])

        # -----------------------------
        # MOMENTUM
        # -----------------------------
        body = abs(last["close"] - last["open"])
        rng = last["high"] - last["low"]

        strong_candle = body > rng * 0.6

        # -----------------------------
        # VOLATILITY
        # -----------------------------
        day_range = df["high"].max() - df["low"].min()

        # -----------------------------
        # DECISION LOGIC
        # -----------------------------
        if vwap_distance > 20 and strong_candle:
            return "OTM"   # strong trend

        if vwap_distance > 10:
            return "ATM"   # normal

        return "ITM"       # weak / sideways

    except:
        return "ATM"




def run_ml_server():
    try:
        from ml_signal_server import app
        print("🚀 Starting ML server thread...")
        app.run(host="0.0.0.0", port=10000)
    except Exception as e:
        print("❌ ML server failed:", e)
        
def performance_loop():
    while True:
        analyze_performance()
        time.sleep(1800)
        
def confirm_entry(token, signal, df=None):
    
    try:
        if df is None:
            df = get_cached_data(token, "5minute", 200)
            

        if df is None or len(df) < 10:
            return False
            
        df = prepare_indicators(df)
            

        last = df.iloc[-1]
        prev = df.iloc[-2]

        # Strong candle required
        body = abs(last["close"] - last["open"])
        rng = last["high"] - last["low"]

        if rng == 0 or body < rng * 0.4:
            return False

        if signal == "CALL":
            return last["close"] > last["vwap"] and last["close"] > prev["high"]

        if signal == "PUT":
            return last["close"] < last["vwap"] and last["close"] < prev["low"]

        return False

    except:
        return False
    
def get_quantity(lots, exchange, instrument=None):
    if exchange == "MCX":          # CRUDE OIL
        return lots * 1
    if instrument == "BANKNIFTY":
        return lots * 15           # BankNifty lot size = 15
    if instrument == "SENSEX":
        return lots * 20           # SENSEX lot size = 20
    if exchange in ("NFO", "BFO"): # NIFTY — current lot size = 65
        return lots * 65
    return lots
    
def get_balance(instrument):
    """
    Returns the live available cash balance from Kite for the given instrument.
    NIFTY → equity segment
    CRUDE → commodity segment
    """
    try:
        margin = kite.margins()
        if instrument == "NIFTY":
            seg = margin.get("equity", {}).get("available", {})
        else:
            seg = margin.get("equity", {}).get("available", {})

        # Kite returns live_balance when intraday, cash otherwise
        balance = seg.get("live_balance") or seg.get("cash") or 0
        balance = float(balance)

        if balance <= 0:
            print(f"⚠️ get_balance: zero or negative balance for {instrument}")

        return balance

    except Exception as e:
        print(f"❌ get_balance error for {instrument}: {e}")
        return 0
        
        
def calculate_lots(price, exchange, instrument, strong_trend=False):
    """
    Balance-aware lot sizing for Nifty (NFO) and Crude (MCX).

    Logic:
      1. Fetch live available balance from Kite.
      2. Risk amount = balance * RISK_PCT (5% by default).
      3. SL is assumed at 25% of option premium (i.e. exit if premium drops 25%).
      4. risk_per_lot = SL_points * lot_size
      5. lots = floor(risk_amount / risk_per_lot)
      6. Hard cap: total trade value (premium * lot_size * lots) <= MAX_CAPITAL_PCT of balance.
      7. Streak and drawdown adjustments applied last.

    Nifty lot size = 75 (as of 2024 revision — update if SEBI changes it again).
    Crude lot size = 100 bbls.
    """
    global win_streak, loss_streak
    global portfolio_pnl, peak_portfolio

    # ── Risk parameters ──────────────────────────────────────────────────
    RISK_PCT         = 0.05    # 5% of balance risked per trade
    SL_PCT           = 0.25    # assume SL at 25% drop in option premium
    MAX_CAPITAL_PCT  = 0.70    # never deploy more than 40% of balance in one trade
    MAX_LOTS_NIFTY   = 5       # hard ceiling — adjust to your comfort
    MAX_LOTS_CRUDE   = 3

    # ── Fixed lot mode — 1 lot for both instruments until strategy confirmed ──
    if FIXED_LOT_MODE:
        return 1   # ← remove FIXED_LOT_MODE flag above to enable balance sizing

    # ── Lot sizes ─────────────────────────────────────────────────────────
    if instrument == "NIFTY":
        lot_size = 65          # Current Nifty F&O lot size
        max_lots = MAX_LOTS_NIFTY
    else:
        lot_size = 100         # Crude Oil MCX lot size
        max_lots = MAX_LOTS_CRUDE

    # ── 1. Live balance ───────────────────────────────────────────────────
    try:
        balance = get_balance(instrument)
        if not balance or balance <= 0:
            print("⚠️ Balance fetch failed — defaulting to 1 lot")
            return 1
    except Exception as e:
        print(f"⚠️ Balance error: {e} — defaulting to 1 lot")
        return 1

    print(f"💰 Live balance ({instrument}): ₹{balance:,.0f}")

    # ── 2. Risk amount ────────────────────────────────────────────────────
    risk_amount = balance * RISK_PCT * adaptive_config["risk_multiplier"]
    print(f"🎯 Risk amount (5%): ₹{risk_amount:,.0f}")

    # ── 3. Risk per lot ───────────────────────────────────────────────────
    sl_points      = price * SL_PCT          # points lost if SL hit
    risk_per_lot   = sl_points * lot_size    # ₹ loss per lot if SL hit
    trade_value_1  = price * lot_size        # ₹ deployed per lot

    if risk_per_lot <= 0 or trade_value_1 <= 0:
        print("⚠️ Invalid price for lot calculation — defaulting to 1 lot")
        return 1

    print(f"📊 Option premium: ₹{price:.1f}  |  SL pts: ₹{sl_points:.1f}  |  Risk/lot: ₹{risk_per_lot:.0f}  |  Deploy/lot: ₹{trade_value_1:.0f}")

    # ── 4. Lots from risk model ───────────────────────────────────────────
    lots_by_risk = int(risk_amount / risk_per_lot)

    # ── 5. Lots from capital cap (never deploy > 40% of balance) ─────────
    max_deployable   = balance * MAX_CAPITAL_PCT
    lots_by_capital  = int(max_deployable / trade_value_1)

    lots = min(lots_by_risk, lots_by_capital)
    print(f"📐 Lots by risk={lots_by_risk}  |  Lots by capital cap={lots_by_capital}  |  Chosen={lots}")

    # ── 6. Streak adjustments ─────────────────────────────────────────────
    if win_streak >= 3:
        lots = int(lots * 1.3)
        print(f"🚀 Win streak {win_streak} → scale up to {lots} lots")
    elif win_streak >= 2:
        lots = int(lots * 1.15)
        print(f"📈 Win streak {win_streak} → slight scale up to {lots} lots")

    if loss_streak >= 3:
        lots = 1
        print(f"🛑 Loss streak {loss_streak} → forced to 1 lot")
    elif loss_streak >= 2:
        lots = max(1, int(lots * 0.6))
        print(f"⚠️ Loss streak {loss_streak} → scale down to {lots} lots")

    # ── 7. Drawdown protection ────────────────────────────────────────────
    drawdown = peak_portfolio - portfolio_pnl
    if drawdown > abs(config.MAX_DRAWDOWN) * 0.5:
        lots = 1
        print(f"🚫 Drawdown ₹{drawdown:.0f} > 50% of max — forced to 1 lot")

    # ── 8. Trend boost (only when already profitable today) ──────────────
    if strong_trend and win_streak >= 2 and daily_pnl > 0:
        lots = int(lots * 1.2)
        print(f"📈 Strong trend + winning day → boost to {lots} lots")

    # ── 9. Hard floor and ceiling ─────────────────────────────────────────
    lots = max(1, lots)
    lots = min(lots, max_lots)

    print(f"✅ Final lots: {lots}  |  Total deployed: ₹{lots * trade_value_1:,.0f}  |  Max risk: ₹{lots * risk_per_lot:,.0f}")
    return lots

def is_strong_trend_day(token, df=None):

    try:
        if df is None:
            df = get_cached_data(token, "5minute", 200)
            

        if df is None or len(df) < 10:
            return False

        move = abs(df.iloc[-1]["close"] - df.iloc[0]["close"])

        return move > df.iloc[-1]["close"] * 0.01

    except:
        return False
        
def is_reversal_trap(token, signal):

    try:
        now = datetime.now()

        df = get_cached_data(token, "5minute", 20)
        
        
        if df is None or len(df) < 10:
            return False

        if len(df) < 5:
            return False

        last = df.iloc[-1]

        body = abs(last["close"] - last["open"])
        candle_range = last["high"] - last["low"]

        upper_wick = last["high"] - max(last["open"], last["close"])
        lower_wick = min(last["open"], last["close"]) - last["low"]

        if candle_range == 0:
            return False

        upper_ratio = upper_wick / candle_range
        lower_ratio = lower_wick / candle_range

        print(f"Trap Check → Upper: {upper_ratio:.2f}, Lower: {lower_ratio:.2f}")

        # -----------------------------
        # CALL TRAP (bull trap)
        # -----------------------------
        if signal == "CALL":
            if upper_ratio > 0.5:
                return True

        # -----------------------------
        # PUT TRAP (bear trap)
        # -----------------------------
        if signal == "PUT":
            if lower_ratio > 0.5:
                return True

        return False

    except Exception as e:
        print("Trap detection error:", e)
        return False
        
def is_news_volatility(token):

    try:
        now = datetime.now()

        df = get_cached_data(token, "5minute", 20)
        
        
        if df is None or len(df) < 10:
            return False

        if len(df) < 10:
            return False
            
        df = df.copy()

        # Candle range
        df["range"] = df["high"] - df["low"]

        # Current candle
        last = df.iloc[-1]

        # Average volatility
        avg_range = df["range"].rolling(10).mean().iloc[-1]

        current_range = last["high"] - last["low"]

        print(f"News Check → Current: {current_range}, Avg: {avg_range}")

        # -----------------------------
        # VOLATILITY SPIKE CONDITION
        # -----------------------------
        if current_range > avg_range * 2:
            return True

        return False

    except Exception as e:
        print("News volatility error:", e)
        return False
        
def reset_daily_pnl():

    global daily_pnl, trade_count, last_reset_date
    global win_streak, loss_streak
    global trade_alert_sent
    global report_sent_today, max_drawdown
    global portfolio_pnl, peak_portfolio   # ✅ CORRECT VARIABLES

    from datetime import date
    today = date.today()

    if last_reset_date != today:
        print("🔄 Resetting daily stats")

        trade_alert_sent = {
            "max_trades": False,
            "max_loss": False,
            "target_hit": False
        }

        daily_pnl = 0
        trade_count = 0

        # ✅ FIXED VARIABLES
        portfolio_pnl = 0
        peak_portfolio = 0

        win_streak = 0
        loss_streak = 0
        report_sent_today = False
        max_drawdown = 0

        # ── Per-instrument reset ─────────────────────────────────────────
        global nifty_daily_pnl, banknifty_daily_pnl, sensex_daily_pnl, crude_daily_pnl
        global nifty_trade_count, banknifty_trade_count, sensex_trade_count, crude_trade_count
        global nifty_daily_wins, nifty_daily_losses
        global banknifty_daily_wins, banknifty_daily_losses
        global sensex_daily_wins, sensex_daily_losses
        global crude_daily_wins, crude_daily_losses
        global _last_no_signal_alert_nifty, _last_no_signal_alert_banknifty
        global _last_no_signal_alert_sensex, _last_no_signal_alert_crude

        nifty_daily_pnl = 0
        banknifty_daily_pnl = 0
        sensex_daily_pnl = 0
        crude_daily_pnl = 0
        nifty_trade_count = 0
        banknifty_trade_count = 0
        sensex_trade_count = 0
        crude_trade_count = 0
        nifty_daily_wins = 0
        nifty_daily_losses = 0
        banknifty_daily_wins = 0
        banknifty_daily_losses = 0
        sensex_daily_wins = 0
        sensex_daily_losses = 0
        crude_daily_wins = 0
        crude_daily_losses = 0
        _last_no_signal_alert_nifty = 0
        _last_no_signal_alert_banknifty = 0
        _last_no_signal_alert_sensex = 0
        _last_no_signal_alert_crude = 0

        # Clear stale option chain cache from prior trading day
        instrument_cache.clear()
        _data_cache_store.clear()   # also flush historical data cache

        last_reset_date = today
        
 
def get_trade_confidence(token, signal, df=None, strong_trend=False):

    try:
        if df is None:
            df = get_cached_data(token, "5minute", 20)
            

        if df is None or len(df) < 10:
            return 0

        df = df.copy()
        df["vwap"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()
        df["vol_ma"] = df["volume"].rolling(5).mean()

        last = df.iloc[-1]
        prev = df.iloc[-2]

        score = 0

        # VWAP
        if signal == "CALL" and last["close"] > last["vwap"]:
            score += 20
        elif signal == "PUT" and last["close"] < last["vwap"]:
            score += 20

        # Breakout
        if signal == "CALL" and last["close"] > prev["high"]:
            score += 25
        elif signal == "PUT" and last["close"] < prev["low"]:
            score += 25

        # Volume
        if last["volume"] > last["vol_ma"] * 1.3:
            score += 20

        # Candle strength
        body = abs(last["close"] - last["open"])
        rng = last["high"] - last["low"]

        if rng > 0 and body > rng * 0.6:
            score += 15

        # Trend bonus
        if strong_trend:
            score += 10

        return min(score, 100)

    except:
        return 0

 
def is_false_breakout(token, signal):

    try:
        now = datetime.now()

        df = get_cached_data(token, "5minute", 20)
        
        
        if df is None or len(df) < 10:
            return False
            
        df = df.copy()

        df["vol_ma"] = df["volume"].rolling(5).mean()

        last = df.iloc[-1]
        prev = df.iloc[-2]

        # -----------------------------
        # CANDLE ANALYSIS
        # -----------------------------
        body = abs(last["close"] - last["open"])
        rng = last["high"] - last["low"]

        upper_wick = last["high"] - max(last["open"], last["close"])
        lower_wick = min(last["open"], last["close"]) - last["low"]

        # -----------------------------
        # CONDITIONS
        # -----------------------------

        # Weak breakout (small body)
        weak_body = body < (rng * 0.4)

        # No volume support
        low_volume = last["volume"] < last["vol_ma"]

        # Rejection candle
        rejection = upper_wick > body * 1.5 if signal == "CALL" else lower_wick > body * 1.5

        # No follow-through
        no_break = (
            signal == "CALL" and last["close"] <= prev["high"]
        ) or (
            signal == "PUT" and last["close"] >= prev["low"]
        )

        # 🔥 SUPER SAFE MODE (STRONG FILTER)
        if weak_body and low_volume and rejection:
            print("🚫 Strong fake breakout (super filter)")
            return True

        if rejection:
            print("🚫 Rejection candle")
            return True

        if no_break:
            print("🚫 No breakout follow-through")
            return True

        return False

    except Exception as e:
        print("False breakout error:", e)
        return False
        
        
def get_market_session(instrument):

    now = datetime.now(IST)

    # -----------------------------
    # NIFTY (NSE)
    # -----------------------------
    if instrument == "NIFTY":

        if 9 <= now.hour < 11:
            return "MORNING"

        elif 11 <= now.hour < 13:
            return "MIDDAY"

        elif 13 <= now.hour < 15:
            return "AFTERNOON"

        else:
            return "CLOSED"

    # -----------------------------
    # CRUDE (MCX)
    # -----------------------------
    else:

        if 9 <= now.hour < 12:
            return "MORNING"

        elif 12 <= now.hour < 17:
            return "MIDDAY"

        elif 17 <= now.hour < 21:
            return "EVENING_TREND"

        elif 21 <= now.hour < 23:
            return "VOLATILE_SESSION"

        else:
            return "CLOSED"
            

def vwap_signal(token, df=None):
    try:
        now = datetime.now()

        if df is None:
            df = get_cached_data(token, "5minute", 20)
        
        
        if df is None or len(df) < 10:
            return "HOLD"
            
        df = df.copy()    

        df["vwap"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()
        last = df.iloc[-1]

        if last["close"] > last["vwap"]:
            return "CALL"
        elif last["close"] < last["vwap"]:
            return "PUT"

        return "HOLD"

    except:
        return "HOLD"
        
def breakout_signal(token, df=None):
    try:
        now = datetime.now()

        if df is None:
            df = get_cached_data(token, "5minute", 20)
        
        
        if df is None or len(df) < 10:
            return "HOLD"

        last = df.iloc[-1]
        prev = df.iloc[-2]

        if last["close"] > prev["high"]:
            return "CALL"
        elif last["close"] < prev["low"]:
            return "PUT"

        return "HOLD"

    except:
        return "HOLD"
        
def pullback_signal(token, df=None):

    try:
        now = datetime.now()

        if df is None:
            df = get_cached_data(token, "5minute", 20)

        if df is None or len(df) < 10:
            return "HOLD"

        df = df.copy()

        df["ema"] = df["close"].ewm(span=9).mean()

        last = df.iloc[-1]

        if last["close"] > last["ema"]:
            return "CALL"
        elif last["close"] < last["ema"]:
            return "PUT"

        return "HOLD"

    except:
        return "HOLD"
        
def get_ml_cached():
    global ml_cache

    now = time.time()

    if ml_cache["data"] and (now - ml_cache["time"] < ML_CACHE_TTL):
        return ml_cache["data"]

    try:
        data = requests.get(SIGNAL_URL, timeout=1).json()
        ml_cache["time"] = now
        ml_cache["data"] = data
        return data
    except:
        return None
        
        
# -----------------------------
# ELITE SIGNAL (NEW)
# -----------------------------
def elite_signal(df):
    import pandas as pd
    
    if "vwap" not in df.columns:
        df = prepare_indicators(df)

    # 🔒 BASIC SAFETY
    if df is None or len(df) < 2:
        return "HOLD"

    if "ema9" not in df.columns or "ema20" not in df.columns:
        df = prepare_indicators(df)

    last = df.iloc[-1]
    prev = df.iloc[-2]

    # 🔒 VWAP SAFETY
    if pd.isna(last.get("vwap", None)):
        last["vwap"] = last["close"]

    move = abs(last["close"] - prev["close"])
    threshold = last["close"] * 0.0003

    # -----------------------------
    # 🔥 CANDLE STRENGTH (PRO ADD)
    # -----------------------------
    body = abs(last["close"] - last["open"])
    range_ = last["high"] - last["low"]
    strong_candle = body > (range_ * 0.5) if range_ > 0 else False

    # -----------------------------
    # 🔊 VOLUME CONFIRMATION
    # -----------------------------
    volume_ok = True
    if "volume" in df.columns and len(df) >= 5:
        vol_ma = df["volume"].rolling(5).mean().iloc[-1]
        if pd.notna(vol_ma):
            volume_ok = last["volume"] > vol_ma * 1.2

    # -----------------------------
    # 🧠 TREND (EMA PRIORITY)
    # -----------------------------
    bullish = last["ema9"] > last["ema20"]
    bearish = last["ema9"] < last["ema20"]

    # -----------------------------
    # 🥇 STRONG BREAKOUT
    # -----------------------------
    if bullish and last["close"] > prev["high"] and strong_candle:
        if volume_ok:
            return "CALL"

    if bearish and last["close"] < prev["low"] and strong_candle:
        if volume_ok:
            return "PUT"

    # -----------------------------
    # 🥈 PULLBACK (BEST ENTRY)
    # -----------------------------
    if bullish and last["close"] < prev["close"] and last["close"] > last["vwap"]:
        return "CALL"

    if bearish and last["close"] > prev["close"] and last["close"] < last["vwap"]:
        return "PUT"

    # -----------------------------
    # 🥉 CONTINUATION
    # -----------------------------
    if bullish and last["close"] > prev["close"]:
        return "CALL"

    if bearish and last["close"] < prev["close"]:
        return "PUT"

    # -----------------------------
    # ⚡ MOMENTUM
    # -----------------------------
    if move > threshold:
        if last["close"] > prev["close"]:
            return "CALL"
        elif last["close"] < prev["close"]:
            return "PUT"

    return "HOLD"

        
def multi_strategy_signal(token, instrument, df=None):
    
    if df is None:
        df = get_cached_data(token, "5minute", 20)

    df = prepare_indicators(df)

    signals = []
    ml_conf = 50  # default safe fallback

    # -----------------------------
    # CORE STRATEGIES
    # -----------------------------
    signals.append(vwap_signal(token, df))
    signals.append(breakout_signal(token, df))
    signals.append(pullback_signal(token, df))

    # -----------------------------
    # ML (SAFE HANDLING)
    # -----------------------------
    try:
        data = get_ml_cached()
        
        if not data:
            print("⚠️ ML API failed — using fallback")

        # ✅ only use ML if VALID
        if isinstance(data, dict):
            ml_signal = data.get("signal", "HOLD")
            ml_conf = data.get("confidence", 50)

            # only trust ML if strong
            if ml_conf >= 55:
                signals.append(ml_signal)

        else:
            ml_conf = 50  # fallback

    except Exception as e:
        print(f"⚠️ ML error: {e}")
        ml_conf = 50  # fallback

    # -----------------------------
    # PRIMARY LOGIC
    # -----------------------------
    call_count = signals.count("CALL")
    put_count = signals.count("PUT")

    if call_count >= 2:
        return "CALL", ml_conf

    if put_count >= 2:
        return "PUT", ml_conf

    # -----------------------------
    # ⚡ BALANCED MODE (SAFE OVERRIDE)
    # -----------------------------
    if ml_conf >= 65:   # slightly stricter
        if call_count >= 1:
            print("⚡ ML assisted CALL")
            return "CALL", ml_conf
        elif put_count >= 1:
            print("⚡ ML assisted PUT")
            return "PUT", ml_conf

    # -----------------------------
    # DEFAULT
    # -----------------------------
    return "HOLD", ml_conf
    
    
def adjust_strategy():

    global SIGNAL_COOLDOWN

    result = analyze_performance()

    if not result:
        return

    win_rate, avg_profit, avg_loss = result

    if win_rate < 0.45:
        SIGNAL_COOLDOWN += 10
        print("⚠️ Low win rate → reducing trades")

    elif win_rate > 0.60:
        SIGNAL_COOLDOWN = max(30, SIGNAL_COOLDOWN - 5)
        print("🚀 High win rate → increasing trades")
        
        
def save_best_settings(instrument, mode):

    file = f"{instrument}_settings.json"

    with open(file, "w") as f:
        json.dump({"mode": mode}, f)


def load_best_settings(instrument):

    file = f"{instrument}_settings.json"

    if not os.path.exists(file):
        return None

    with open(file, "r") as f:
        data = json.load(f)

    return data.get("mode")
    
def portfolio_safe():

    global portfolio_pnl, peak_portfolio, risk_off

    # -----------------------------
    # MAX LOSS
    # -----------------------------
    if portfolio_pnl <= config.MAX_PORTFOLIO_LOSS:
        print("🚫 Portfolio max loss hit")
        return False

    # -----------------------------
    # DRAWDOWN CONTROL (FIXED)
    # -----------------------------
    drawdown = peak_portfolio - portfolio_pnl

    if drawdown >= abs(config.MAX_DRAWDOWN):
        print("🚫 Max drawdown hit")

        if config.RISK_OFF_AFTER_LOSS:
            risk_off = True

        return False

    return True
    
def is_low_range_market(token):
    try:
        now = datetime.now()

        df = get_cached_data(token, "5minute", 20)
        
        
        if df is None or len(df) < 10:
            return True

        day_range = df["high"].max() - df["low"].min()

        # Tune for instruments
        last_price = df["close"].iloc[-1]
        day_range = df["high"].max() - df["low"].min()

        if day_range < last_price * 0.003:
            return True

        return False

    except:
        return True
        
        
def detect_market_type(df):
    last = df.iloc[-1]
    recent = df.iloc[-10:]

    range_ = recent["high"].max() - recent["low"].min()
    avg_candle = (recent["high"] - recent["low"]).mean()

    trend = abs(last["close"] - recent["close"].iloc[0])

    # 📊 Classify
    if trend > last["close"] * 0.004:
        return "TREND"

    elif range_ < last["close"] * 0.002:
        return "SIDEWAYS"

    elif avg_candle > last["close"] * 0.003:
        return "VOLATILE"

    else:
        return "NORMAL"
        
        
def choose_best_strategy(df, token):
    market_type = detect_market_type(df)

    print(f"🧠 Market Type: {market_type}")

    # 🚫 Strategy disabled → fallback
    if strategy_weights.get(market_type, 1.0) < 0.3:
        return "HOLD", market_type

    # -----------------------------
    # TREND
    # -----------------------------
    if market_type == "TREND":
        signal = elite_signal(df)

    # -----------------------------
    # SIDEWAYS — mean reversion: fade the breakout
    # -----------------------------
    elif market_type == "SIDEWAYS":
        last = df.iloc[-1]
        prev = df.iloc[-2]

        if last["close"] < prev["low"]:
            signal = "PUT"   # price broke below support — bearish
        elif last["close"] > prev["high"]:
            signal = "CALL"  # price broke above resistance — bullish
        else:
            signal = "HOLD"

    # -----------------------------
    # VOLATILE
    # -----------------------------
    elif market_type == "VOLATILE":
        last = df.iloc[-1]

        if last["close"] > last["open"]:
            signal = "CALL"
        elif last["close"] < last["open"]:
            signal = "PUT"
        else:
            signal = "HOLD"

    else:
        signal = elite_signal(df)

    return signal, market_type        
        
from datetime import datetime, timedelta
import pandas as pd

_data_cache_store: dict = {}   # { (token, interval): (timestamp, df) }  — populated by get_cached_data
_DATA_CACHE_TTL = 20           # seconds — re-fetch if older than this

def get_cached_data(token, interval="15minute", count=200):
    """Fetch historical data from Kite with a 20-second in-memory cache."""
    global _data_cache_store

    cache_key = (token, interval)
    now_ts = time.time()

    # ── Cache hit ───────────────────────────────────────────────────────────
    if cache_key in _data_cache_store:
        cached_ts, cached_df = _data_cache_store[cache_key]
        if now_ts - cached_ts < _DATA_CACHE_TTL:
            return cached_df.tail(count) if not cached_df.empty else None

    # ── Cache miss → fetch from Kite ────────────────────────────────────────
    try:
        # CRITICAL: Railway server runs in UTC. datetime.now() returns UTC time.
        # Kite historical_data API expects IST (UTC+5:30) naive datetimes.
        # Sending UTC time makes Kite think it's 5h30m in the past → returns
        # yesterday's/previous session's data instead of today's live candles.
        # Fix: always derive to_date in IST, strip tzinfo so Kite gets a naive IST datetime.
        to_date   = datetime.now(IST).replace(tzinfo=None)   # IST naive — correct for Kite API
        from_date = to_date - timedelta(days=30)              # 30 days back in IST

        data = kite.historical_data(token, from_date, to_date, interval)
        df   = pd.DataFrame(data)

        if df.empty:
            print(f"⚠️ get_cached_data: empty response for token={token}, interval={interval}")
            return None

        _data_cache_store[cache_key] = (now_ts, df)
        return df.tail(count)

    except Exception as e:
        print(f"❌ Data fetch error (token={token}, interval={interval}): {e}")
        return None



        
def backtest_full(token, instrument, days=5):

    print(f"📊 Running backtest for {instrument}")

    from datetime import timedelta
    now = datetime.now()

    df = pd.DataFrame(kite.historical_data(
        token,
        now - timedelta(days=days),
        now,
        "5minute"
    ))

    wins = 0
    losses = 0
    total_pnl = 0

    for i in range(20, len(df)-10):

        slice_df = df.iloc[:i]

        # Fake current price
        current_price = slice_df.iloc[-1]["close"]

        # Simulate signal
        signal = "CALL" if slice_df.iloc[-1]["close"] > slice_df.iloc[-2]["close"] else "PUT"

        if signal == "HOLD":
            print("Backtest Signal:", signal)
            continue
            

        entry = current_price
        sl = entry - (entry * 0.15)
        target = entry * 1.20

        future = df.iloc[i:i+10]

        exit_price = entry

        for _, row in future.iterrows():

            price = row["close"]

            if price >= target:
                exit_price = price
                wins += 1
                break

            if price <= sl:
                exit_price = price
                losses += 1
                break

        pnl = exit_price - entry
        total_pnl += pnl

    print(f"""
📊 BACKTEST RESULT ({instrument})

Trades: {wins + losses}
Wins: {wins}
Losses: {losses}
Win Rate: {round((wins/(wins+losses))*100 if (wins+losses)>0 else 0,2)}%
Total PnL: {round(total_pnl,2)}
""")
    
def _instrument_report_section(today_df_inst, instrument_name, daily_pnl_val):
    """Helper — builds a single-instrument section for the daily report."""
    if today_df_inst.empty:
        return f"\n📊 {instrument_name}: No trades today\n"

    wins   = int((today_df_inst["pnl"] > 0).sum())
    losses = int((today_df_inst["pnl"] <= 0).sum())
    total  = wins + losses
    wr     = (wins / total * 100) if total > 0 else 0
    best   = float(today_df_inst["pnl"].max())
    worst  = float(today_df_inst["pnl"].min())

    return (
        f"\n{'='*30}\n"
        f"📌 {instrument_name} REPORT\n"
        f"{'='*30}\n"
        f"💰 Net P&L   : ₹{daily_pnl_val:,.0f}\n"
        f"📈 Trades    : {total}  (✅ {wins} wins  ❌ {losses} losses)\n"
        f"🎯 Win Rate  : {wr:.1f}%\n"
        f"🏆 Best trade: ₹{best:,.0f}\n"
        f"💔 Worst trade: ₹{worst:,.0f}\n"
    )


def send_daily_report():
    """
    Per-instrument daily report sent after market close.
    Nifty section  → sent at 3:31 PM (end of equity session).
    Crude section  → included when called after 11 PM (end of MCX session).
    The report scheduler calls this function; it handles both instruments.
    """
    global report_sent_today
    global portfolio_pnl, max_drawdown
    global nifty_daily_pnl, crude_daily_pnl

    report_sent_today = True

    if not os.path.exists(TRADE_LOG_FILE):
        send_message("📊 Daily Report: No trades recorded today")
        return

    try:
        df = pd.read_csv(TRADE_LOG_FILE)

        # Ensure correct column names (8-column format)
        expected_cols = ["time", "instrument", "symbol", "signal", "entry", "exit", "pnl", "probability"]
        if list(df.columns) != expected_cols:
            # Legacy 5-column file — rebuild header gracefully
            df.columns = expected_cols[:len(df.columns)]

        df["time"] = pd.to_datetime(df["time"])
        from datetime import date
        today = date.today()
        today_df = df[df["time"].dt.date == today]

        # ── Per-instrument split ───────────────────────────────────────────
        nifty_df = today_df[today_df["instrument"].str.upper() == "NIFTY"] if "instrument" in today_df.columns else pd.DataFrame()
        crude_df = today_df[today_df["instrument"].str.upper() == "CRUDE"] if "instrument" in today_df.columns else pd.DataFrame()

        nifty_section = _instrument_report_section(nifty_df, "NIFTY", nifty_daily_pnl)
        crude_section = _instrument_report_section(crude_df, "CRUDE OIL", crude_daily_pnl)

        total_pnl  = nifty_daily_pnl + crude_daily_pnl
        total_trades = len(today_df)

        report = (
            f"📅 DAILY TRADING REPORT — {today.strftime('%d %b %Y')}\n"
            f"{'='*30}\n"
            f"🏦 Combined Net P&L : ₹{total_pnl:,.0f}\n"
            f"📊 Total Trades     : {total_trades}\n"
            f"📉 Max Drawdown     : ₹{max_drawdown:,.0f}\n"
            + nifty_section
            + crude_section +
            f"\n{'='*30}\n"
            f"⏰ Report time: {datetime.now(IST).strftime('%H:%M:%S IST')}"
        )

        send_message(report)
        print("📊 Daily report sent")

    except Exception as e:
        print("Report error:", e)
        send_message(f"❌ Daily report error: {e}")


def _read_today_csv(instrument):
    """
    Read today's closed trades for the given instrument from trade_log.csv.
    Returns (pnl, wins, losses, trade_count).
    Uses CSV as source of truth — survives Railway restarts mid-day.
    """
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        if not os.path.exists(TRADE_LOG_FILE):
            return 0, 0, 0, 0
        df = pd.read_csv(TRADE_LOG_FILE)
        if df.empty or "time" not in df.columns:
            return 0, 0, 0, 0
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        df = df[df["time"].dt.strftime("%Y-%m-%d") == today_str]
        if "instrument" in df.columns:
            df = df[df["instrument"].str.upper() == instrument.upper()]
        if df.empty:
            return 0, 0, 0, 0
        pnl    = float(df["pnl"].sum())
        wins   = int((df["pnl"] > 0).sum())
        losses = int((df["pnl"] <= 0).sum())
        count  = len(df)
        return pnl, wins, losses, count
    except Exception as e:
        print(f"⚠️ _read_today_csv({instrument}): {e}", flush=True)
        return 0, 0, 0, 0


def send_nifty_eod_report():
    """Sent at 3:31 PM. Reads from CSV so restarts don't wipe the numbers."""
    csv_pnl, csv_wins, csv_losses, csv_count = _read_today_csv("NIFTY")

    # Merge CSV data with in-memory counters — take whichever is larger
    # (covers edge case where a trade closed after CSV flush but before report)
    pnl    = max(csv_pnl,    nifty_daily_pnl,    key=abs) if csv_count or nifty_trade_count else 0
    wins   = max(csv_wins,   nifty_daily_wins)
    losses = max(csv_losses, nifty_daily_losses)
    count  = max(csv_count,  nifty_trade_count)

    total = wins + losses
    wr = (wins / total * 100) if total > 0 else 0

    send_message(
        f"🔔 NIFTY SESSION CLOSED\n"
        f"{'='*28}\n"
        f"💰 Net P&L    : ₹{pnl:,.0f}\n"
        f"📈 Trades     : {total}  (✅ {wins} wins  ❌ {losses} losses)\n"
        f"🎯 Win Rate   : {wr:.1f}%\n"
        f"📊 Trade count: {count}\n"
        f"⏰ Nifty session ended 3:30 PM IST"
    )


def send_crude_eod_report():
    """Sent at 11:31 PM. Reads from CSV so restarts don't wipe the numbers."""
    csv_pnl, csv_wins, csv_losses, csv_count = _read_today_csv("CRUDE")

    pnl    = max(csv_pnl,    crude_daily_pnl,    key=abs) if csv_count or crude_trade_count else 0
    wins   = max(csv_wins,   crude_daily_wins)
    losses = max(csv_losses, crude_daily_losses)
    count  = max(csv_count,  crude_trade_count)

    total = wins + losses
    wr = (wins / total * 100) if total > 0 else 0

    send_message(
        f"🔔 CRUDE OIL SESSION CLOSED\n"
        f"{'='*28}\n"
        f"💰 Net P&L    : ₹{pnl:,.0f}\n"
        f"📈 Trades     : {total}  (✅ {wins} wins  ❌ {losses} losses)\n"
        f"🎯 Win Rate   : {wr:.1f}%\n"
        f"📊 Trade count: {count}\n"
        f"⏰ Crude session ended 11:30 PM IST"
    )


def send_sensex_eod_report():
    """Sent at 3:33 PM. Reads from CSV so restarts don't wipe the numbers."""
    csv_pnl, csv_wins, csv_losses, csv_count = _read_today_csv("SENSEX")

    pnl    = max(csv_pnl,    sensex_daily_pnl,    key=abs) if csv_count or sensex_trade_count else 0
    wins   = max(csv_wins,   sensex_daily_wins)
    losses = max(csv_losses, sensex_daily_losses)
    count  = max(csv_count,  sensex_trade_count)

    total = wins + losses
    wr = (wins / total * 100) if total > 0 else 0

    send_message(
        f"🔔 SENSEX SESSION CLOSED\n"
        f"{'='*28}\n"
        f"💰 Net P&L    : ₹{pnl:,.0f}\n"
        f"📈 Trades     : {total}  (✅ {wins} wins  ❌ {losses} losses)\n"
        f"🎯 Win Rate   : {wr:.1f}%\n"
        f"📊 Trade count: {count}\n"
        f"⏰ SENSEX session ended 3:30 PM IST"
    )


def send_banknifty_eod_report():
    """Sent at 3:31 PM alongside Nifty report. Reads from CSV so restarts don't wipe the numbers."""
    csv_pnl, csv_wins, csv_losses, csv_count = _read_today_csv("BANKNIFTY")

    pnl    = max(csv_pnl,    banknifty_daily_pnl,    key=abs) if csv_count or banknifty_trade_count else 0
    wins   = max(csv_wins,   banknifty_daily_wins)
    losses = max(csv_losses, banknifty_daily_losses)
    count  = max(csv_count,  banknifty_trade_count)

    total = wins + losses
    wr = (wins / total * 100) if total > 0 else 0

    send_message(
        f"🔔 BANKNIFTY SESSION CLOSED\n"
        f"{'='*28}\n"
        f"💰 Net P&L    : ₹{pnl:,.0f}\n"
        f"📈 Trades     : {total}  (✅ {wins} wins  ❌ {losses} losses)\n"
        f"🎯 Win Rate   : {wr:.1f}%\n"
        f"📊 Trade count: {count}\n"
        f"⏰ BankNifty session ended 3:30 PM IST"
    )
        
        

def backtest_df(df):
    capital = 10000
    position = None
    entry_price = 0
    trades = []

    for i in range(20, len(df)):
        window = df.iloc[:i]
        last = window.iloc[-1]

        # Simple breakout logic
        prev = window.iloc[-2]

        if last["close"] > prev["high"]:
            signal = "CALL"
        elif last["close"] < prev["low"]:
            signal = "PUT"
        else:
            signal = "HOLD"

        if signal == "HOLD":
            continue

        prob = get_trade_probability(None, signal, window)

        if prob < 55:
            continue

        if not confirm_entry(None, signal, window):
            continue

        entry = last["close"]
        sl = entry * 0.85
        target = entry * 1.2

        # simulate next candles
        for j in range(i+1, len(df)):
            price = df.iloc[j]["close"]

            if price <= sl:
                trades.append(-1)
                break

            if price >= target:
                trades.append(2)
                break

    win_rate = sum(1 for t in trades if t > 0) / len(trades) if trades else 0

    print("Trades:", len(trades))
    print("Win rate:", win_rate)      
        
        
  
# -----------------------------
# MAIN
# -----------------------------
if __name__ == "__main__":

    import time
    import os
    import atexit
    import threading

    # -----------------------------
    # 📄 CREATE TRADE LOG FILE (FIX)
    # -----------------------------
    # Note: top-level check at module load already creates this file with the
    # correct 8-column header.  This block is kept as a safety net only — and
    # now uses the same 8-column format so both paths are consistent.
    if not os.path.exists("trade_log.csv"):
        with open("trade_log.csv", "w") as f:
            f.write("time,instrument,symbol,signal,entry,exit,pnl,probability\n")

    # -----------------------------
    # 🎯 TOKEN INITIALIZATION
    # -----------------------------
    CRUDE_TOKEN = get_latest_fut_token("CRUDEOIL", "MCX")
    NIFTY_FUT_TOKEN = get_nifty_fut_token()

    if CRUDE_TOKEN is None:
        print("🚨 CRUDE DISABLED — TOKEN NOT FOUND")

    if NIFTY_FUT_TOKEN is None:
        print("⚠️ NIFTY FUT TOKEN NOT FOUND")

    # -----------------------------
    # 🔍 API TEST
    # -----------------------------
    try:
        print("🔍 Testing Kite API...")
        test = kite.ltp("NSE:NIFTY 50")
        print("✅ Kite API working:", test)
    except Exception as e:
        print("❌ Kite API FAILED:", e)

    # -----------------------------
    # 🔒 LOCK FILE HANDLING
    # -----------------------------
    LOCK_FILE = "bot.lock"

    def remove_lock():
        if os.path.exists(LOCK_FILE):
            os.remove(LOCK_FILE)

    if os.path.exists(LOCK_FILE):
        print("⚠️ Removing stale lock file")
        os.remove(LOCK_FILE)

    with open(LOCK_FILE, "w") as f:
        f.write("running")

    atexit.register(remove_lock)

    # -----------------------------
    # 🚀 START TRADING LOOPS
    # -----------------------------
    # ── Restore any open positions from previous session ────────────────────
    print("🔍 Checking Kite for existing open positions...")
    restore_position_state_from_kite()

    # ── Daily report scheduler ──────────────────────────────────────────────
    _nifty_eod_sent      = [False]   # mutable so inner function can write
    _banknifty_eod_sent  = [False]
    _sensex_eod_sent     = [False]
    _crude_eod_sent      = [False]
    _full_report_sent    = [False]

    def daily_report_scheduler():
        """
        Sends end-of-day reports at precise times:
          3:31 PM IST  → Nifty session closed report
          3:32 PM IST  → BankNifty session closed report
          3:33 PM IST  → SENSEX session closed report
         11:31 PM IST  → Crude session closed report  (after 11:25 PM force-close)
         11:32 PM IST  → Combined NIFTY + BANKNIFTY + SENSEX + CRUDE daily report
        Resets sent-flags at midnight.
        """
        while True:
            try:
                now = datetime.now(IST)

                # Reset flags at midnight
                if now.hour == 0 and now.minute < 2:
                    _nifty_eod_sent[0]      = False
                    _banknifty_eod_sent[0]  = False
                    _sensex_eod_sent[0]     = False
                    _crude_eod_sent[0]      = False
                    _full_report_sent[0]    = False

                # 3:31 PM — Nifty EOD report
                if now.hour == 15 and now.minute == 31 and not _nifty_eod_sent[0]:
                    send_nifty_eod_report()
                    _nifty_eod_sent[0] = True

                # 3:32 PM — BankNifty EOD report (same session as NIFTY)
                if now.hour == 15 and now.minute == 32 and not _banknifty_eod_sent[0]:
                    send_banknifty_eod_report()
                    _banknifty_eod_sent[0] = True

                # 3:33 PM — SENSEX EOD report (same session as NIFTY/BANKNIFTY)
                if now.hour == 15 and now.minute == 33 and not _sensex_eod_sent[0]:
                    send_sensex_eod_report()
                    _sensex_eod_sent[0] = True

                # 11:31 PM — Crude EOD report (after force-close at 11:25 PM, all trades done)
                if now.hour == 23 and now.minute == 31 and not _crude_eod_sent[0]:
                    send_crude_eod_report()
                    _crude_eod_sent[0] = True

                # 11:32 PM — Combined daily report (all instruments together)
                if now.hour == 23 and now.minute == 32 and not _full_report_sent[0]:
                    send_daily_report()
                    _full_report_sent[0] = True

            except Exception as e:
                print(f"❌ Report scheduler error: {e}")

            time.sleep(30)   # check every 30 seconds

    threading.Thread(target=daily_report_scheduler, daemon=True, name="ReportScheduler").start()

    threading.Thread(target=nifty_loop, daemon=True).start()

    if BANKNIFTY_TOKEN:
        threading.Thread(target=banknifty_loop, daemon=True).start()
    else:
        print("⚠️ BANKNIFTY LOOP SKIPPED — token not found")

    if SENSEX_TOKEN:
        threading.Thread(target=sensex_loop, daemon=True).start()
    else:
        print("⚠️ SENSEX LOOP SKIPPED — token not found")

    if CRUDE_TOKEN:
        threading.Thread(target=crude_loop, daemon=True).start()
    else:
        print("⚠️ CRUDE LOOP SKIPPED")

    print("🚀 Trading engine started")

    # -----------------------------
    # 📢 START MESSAGE
    # -----------------------------
    time.sleep(10)
    try:
        crude_status    = f"✅ Token: {CRUDE_TOKEN}"    if CRUDE_TOKEN    else "⚠️ DISABLED (token not found)"
        nifty_status    = f"✅ Token: {config.NIFTY_TOKEN}"
        bn_status       = f"✅ Token: {BANKNIFTY_TOKEN}" if BANKNIFTY_TOKEN else "⚠️ DISABLED (token not found)"
        sx_status       = f"✅ Token: {SENSEX_TOKEN}"    if SENSEX_TOKEN    else "⚠️ DISABLED (token not found)"
        send_message(
            f"🚀 HALFTREND BOT STARTED\n"
            f"{'='*28}\n"
            f"📌 NIFTY     : {nifty_status}\n"
            f"   Hours     : 9:15 AM – 3:30 PM IST | Lot: 65 qty\n"
            f"\n"
            f"🏦 BANKNIFTY : {bn_status}\n"
            f"   Hours     : 9:15 AM – 3:30 PM IST | Lot: 15 qty\n"
            f"\n"
            f"📊 SENSEX    : {sx_status}\n"
            f"   Hours     : 9:15 AM – 3:30 PM IST | Lot: 20 qty\n"
            f"\n"
            f"🛢️ CRUDE     : {crude_status}\n"
            f"   Hours     : 3:30 PM – 11 PM IST\n"
            f"\n"
            f"⚙️ Signal : HalfTrend 15-min (all instruments)\n"
            f"⚙️ SL     : 25% → 45% two-tier\n"
            f"⚙️ Trail  : ATR-based adaptive trailing\n"
            f"⚙️ Flip   : Immediate exit + re-entry on arrow reversal\n"
            f"📅 Reports: NIFTY@3:31 | BankNifty@3:32 | SENSEX@3:33 | Crude@11:31 | Combined@11:32"
        )
    except Exception as e:
        print("Startup telegram failed:", e)

    # -----------------------------
    # 🔁 DAILY TOKEN REFRESH
    # -----------------------------
    def refresh_tokens():
        data_cache.clear()
        ltp_cache.clear()
        instrument_cache.clear()  # Clear stale option chains from prior day
        global CRUDE_TOKEN, NIFTY_FUT_TOKEN
        global CRUDE_SYMBOL
        CRUDE_SYMBOL = None

        _access_token_refreshed_today = [False]   # mutable flag for closure
        _last_refresh_date = [None]

        while True:
            now = datetime.now(IST)
            today = now.date()

            # Reset daily flag at midnight
            if _last_refresh_date[0] != today:
                _access_token_refreshed_today[0] = False
                _last_refresh_date[0] = today

            # ── 8:00 AM weekdays — refresh Kite access token (before market opens) ──
            # weekday() 0=Mon … 4=Fri, 5=Sat, 6=Sun — skip weekends
            if now.hour == 8 and now.minute < 5 and now.weekday() < 5 and not _access_token_refreshed_today[0]:
                print("🔑 Daily access token refresh starting (8:00 AM)...", flush=True)
                _refresh_error = [None]
                _orig_except = None

                # Update IP whitelist before login (Railway may assign new IP after restart)
                threading.Thread(target=update_kite_ip_whitelist, daemon=True).start()

                # Patch zerodha_auto_login to capture the error message
                try:
                    new_token = zerodha_auto_login()
                except Exception as _ex:
                    new_token = None
                    _refresh_error[0] = str(_ex)

                # Also try to read the last printed error from the function
                _access_token_refreshed_today[0] = True
                ist_str = now.strftime("%d %b %Y %H:%M IST")
                if new_token:
                    print(f"✅ Kite token refreshed at {ist_str}", flush=True)
                    try:
                        send_message(
                            f"✅ KITE TOKEN REFRESHED\n"
                            f"🕐 {ist_str}\n"
                            f"📈 Bot ready for market open at 9:15 AM"
                        )
                    except Exception:
                        pass
                else:
                    err_detail = _refresh_error[0] or "Check Railway logs for details"
                    try:
                        send_message(
                            f"❌ KITE TOKEN REFRESH FAILED\n"
                            f"{'='*28}\n"
                            f"🕐 Time  : {ist_str}\n"
                            f"❗ Error : {err_detail[:200]}\n"
                            f"⚠️ Please refresh token manually!"
                        )
                    except Exception:
                        pass

            # ── 9:00–9:05 AM — refresh instrument tokens ────────────────────
            if now.hour == 9 and now.minute < 5:
                print("🔄 Refreshing instrument tokens...")
                CRUDE_TOKEN = get_latest_fut_token("CRUDEOIL", "MCX")
                NIFTY_FUT_TOKEN = get_nifty_fut_token()
                data_cache.clear()
                ltp_cache.clear()
                instrument_cache.clear()
                print("✅ Instrument tokens refreshed")

            time.sleep(60)

    threading.Thread(target=refresh_tokens, daemon=True).start()

    # -----------------------------
    # 🔁 KEEP ALIVE
    # -----------------------------
    while True:
        time.sleep(60)


    # end of __main__ block