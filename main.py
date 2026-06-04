import requests
import time
import pytz
import pandas as pd
import threading
from kiteconnect import KiteConnect
import config
import csv
import os
import json
import re
import pyotp
from datetime import datetime, timedelta
from telegram_bot import send_message as _raw_send_message

def send_message(text):
    """
    HTML-safe wrapper around telegram_bot.send_message.
    Replaces < and > with safe equivalents so Telegram never
    throws 'Unsupported start tag' errors regardless of what
    filter reason strings or error messages contain.
    """
    try:
        safe = str(text).replace("<", "‹").replace(">", "›")
        _raw_send_message(safe)
    except Exception as _tg_err:
        print(f"⚠️ Telegram send failed: {_tg_err}", flush=True)

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

    def _safe_json(response, step_name):
        """Parse JSON safely — raise a clear error if body is empty or not JSON."""
        body = response.text.strip() if response.text else ""
        if not body:
            raise Exception(
                f"{step_name} returned empty response body "
                f"(HTTP {response.status_code}). "
                f"Possible causes: rate-limit, IP block, or Zerodha server issue. "
                f"Headers: {dict(response.headers)}"
            )
        try:
            return response.json()
        except Exception:
            raise Exception(
                f"{step_name} returned non-JSON (HTTP {response.status_code}). "
                f"Body preview: {body[:300]}"
            )

    try:
        # ── Step 1: POST login credentials (with retry) ──────────────────────
        print("🔐 Auto-login: posting credentials...")
        data = None
        for _login_attempt in range(1, 4):
            try:
                resp = session.post(
                    "https://kite.zerodha.com/api/login",
                    data={"user_id": user_id, "password": password},
                    timeout=20,
                )
                print(f"   [login attempt {_login_attempt}] HTTP {resp.status_code} | "
                      f"body_len={len(resp.text)} | body_preview={resp.text[:120]}", flush=True)
                data = _safe_json(resp, "Step-1 /api/login")
                break   # success — stop retrying
            except Exception as _step1_err:
                print(f"   ⚠️ Login attempt {_login_attempt}/3 failed: {_step1_err}", flush=True)
                if _login_attempt < 3:
                    _wait = 30 * _login_attempt        # 30s, then 60s
                    print(f"   ⏳ Retrying in {_wait}s...", flush=True)
                    time.sleep(_wait)
                else:
                    raise   # re-raise on final attempt

        if data is None:
            raise Exception("Step-1 /api/login: no data after 3 attempts")
        if data.get("status") != "success":
            raise Exception(f"Credential login failed: {data.get('message')} | full: {data}")

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
                timeout=20,
            )
            print(f"   [2FA attempt {attempt}] HTTP {resp2.status_code} | "
                  f"body_preview={resp2.text[:120]}", flush=True)
            try:
                data2 = _safe_json(resp2, f"Step-2 /api/twofa attempt {attempt}")
            except Exception as _2fa_parse_err:
                print(f"   ⚠️ 2FA parse error: {_2fa_parse_err}", flush=True)
                data2 = None
            if data2 and data2.get("status") == "success":
                print(f"   2FA attempt {attempt}: success | code: {totp_obj.now()}")
                break
            print(f"   2FA attempt {attempt}: {data2.get('status') if data2 else 'NO_DATA'} | "
                  f"msg: {data2.get('message') if data2 else 'N/A'}")
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

        # Capture all cookies from the 2FA session — these MUST be sent with
        # every redirect call otherwise Kite resets the session to /connect/login
        _cookies = dict(session.cookies)
        print(f"   Session cookies available: {list(_cookies.keys())}", flush=True)

        # Build cookie header string explicitly
        _cookie_hdr = "; ".join(f"{k}={v}" for k, v in _cookies.items())

        # Dedicated session for redirect flow with all cookies pre-loaded
        _rs = requests.Session()
        _rs.cookies.update(_cookies)
        _rs.headers.update({
            "User-Agent"    : "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "X-Kite-Version": "3",
            "Referer"       : "https://kite.zerodha.com/",
            "Cookie"        : _cookie_hdr,
        })

        login_url = f"https://kite.zerodha.com/connect/login?api_key={_api_key}&v=3"
        request_token = None

        # ── 3a: Hop 1 ────────────────────────────────────────────────────────
        resp_login = _rs.get(login_url, allow_redirects=False, timeout=15)
        loc1 = resp_login.headers.get("Location", "")
        print(f"   [hop1] status={resp_login.status_code} | loc={loc1[:150]}", flush=True)
        sys.stdout.flush()

        m = re.search(r"request_token=([^&]+)", loc1)
        if m:
            request_token = m.group(1)

        # If hop1 redirected back to /connect/login — session not carried
        if not request_token and "/connect/login" in loc1:
            print("   ⚠️ hop1 redirected back to /connect/login — retrying with enctoken header", flush=True)
            _enctoken_val = _cookies.get("enctoken", "")
            if _enctoken_val:
                _rs.headers.update({
                    "Authorization": f"enctoken {_enctoken_val}",
                })
            resp_login = _rs.get(login_url, allow_redirects=False, timeout=15)
            loc1 = resp_login.headers.get("Location", "")
            print(f"   [hop1-retry] status={resp_login.status_code} | loc={loc1[:150]}", flush=True)
            m = re.search(r"request_token=([^&]+)", loc1)
            if m:
                request_token = m.group(1)

        # ── 3b: Hop 2 ────────────────────────────────────────────────────────
        if not request_token and loc1 and "/connect/login" not in loc1:
            finish_url = loc1 if loc1.startswith("http") else "https://kite.zerodha.com" + loc1
            resp_finish = _rs.get(finish_url, allow_redirects=False, timeout=15)
            loc2 = resp_finish.headers.get("Location", "")
            print(f"   [hop2] status={resp_finish.status_code} | loc={loc2[:150]}", flush=True)
            sys.stdout.flush()

            # Extract token from loc2 directly — handles localhost and 127.0.0.1 redirects
            m = re.search(r"request_token=([^&\"']+)", loc2)
            if m:
                request_token = m.group(1)
                print(f"   [hop2] ✅ request_token found in redirect URL", flush=True)

            # ── 3c: Hop 3 — only follow if NOT a localhost/127 redirect ──────
            # If loc2 points to localhost or 127.0.0.1, the token should already
            # be in the URL above. Fetching localhost would fail (nothing running there).
            if not request_token and loc2:
                _is_local = any(x in loc2 for x in ["localhost", "127.0.0.1", "127.0.0"])
                if _is_local:
                    print(f"   [hop3] skipping fetch — localhost redirect, token not in URL", flush=True)
                    print(f"   ⚠️ Fix: change your Kite app redirect URL from 'http://localhost' to 'https://127.0.0.1'", flush=True)
                else:
                    try:
                        app_url = loc2 if loc2.startswith("http") else "https://kite.zerodha.com" + loc2
                        resp_app = _rs.get(app_url, allow_redirects=False, timeout=10)
                        loc3 = resp_app.headers.get("Location", "")
                        print(f"   [hop3] status={resp_app.status_code} | loc={loc3[:150]}", flush=True)
                        sys.stdout.flush()
                        m = re.search(r"request_token=([^&\"']+)", loc3 + resp_app.text)
                        if m:
                            request_token = m.group(1)

                        # ── Hop3b: /connect/authorize SPA ────────────────────
                        if not request_token and resp_app.status_code == 200 and "authorize" in app_url:
                            print("   [hop3b] /connect/authorize SPA — trying JS API...", flush=True)
                            _sess_id = re.search(r"sess_id=([^&]+)", app_url)
                            if _sess_id:
                                _sid = _sess_id.group(1)
                                resp_auth2 = _rs.post(
                                    "https://kite.zerodha.com/connect/authorize",
                                    json={"api_key": _api_key, "sess_id": _sid},
                                    headers={"Content-Type": "application/json",
                                             "X-Kite-Version": "3",
                                             "Accept": "application/json"},
                                    allow_redirects=False,
                                    timeout=15
                                )
                                loc3b = resp_auth2.headers.get("Location", "")
                                print(f"   [hop3b] status={resp_auth2.status_code} | loc={loc3b[:200]}", flush=True)
                                m = re.search(r"request_token=([^&\"']+)", loc3b + resp_auth2.text)
                                if m:
                                    request_token = m.group(1)

                    except Exception as e:
                        print(f"   [hop3] error: {e}", flush=True)
                        sys.stdout.flush()

        if not request_token:
            # Final fallback: try enctoken from session cookies
            _enctoken = _cookies.get("enctoken", "")
            if _enctoken:
                print(f"   [enctoken final fallback] using enctoken (len={len(_enctoken)})", flush=True)
                kite.set_access_token(_enctoken)
                try:
                    _test = kite.profile()
                    print(f"   [enctoken] ✅ works — user: {_test.get('user_name', '?')}", flush=True)
                    return _enctoken
                except Exception as _enc_err:
                    print(f"   [enctoken] ❌ failed: {_enc_err}", flush=True)

            raise Exception(
                "request_token not found. "
                "Go to developers.kite.trade → your app → "
                "change Redirect URL from 'http://localhost' to 'https://127.0.0.1' → save. "
                "Then whitelist your Railway IP. Then redeploy."
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
# Print which env vars are set (masked) so you can diagnose missing values
print("\n🔑 Checking Railway environment variables:", flush=True)
_ev_checks = {
    "ZERODHA_USER_ID":     os.environ.get("ZERODHA_USER_ID"),
    "ZERODHA_PASSWORD":    os.environ.get("ZERODHA_PASSWORD"),
    "ZERODHA_TOTP_SECRET": os.environ.get("ZERODHA_TOTP_SECRET"),
    "KITE_API_KEY":        os.environ.get("KITE_API_KEY"),
    "KITE_API_SECRET":     os.environ.get("KITE_API_SECRET") or os.environ.get("API_SECRET"),
}
for _k, _v in _ev_checks.items():
    if _v:
        _masked = _v[:4] + "****" + _v[-2:] if len(_v) > 6 else "****"
        print(f"   ✅ {_k} = {_masked} (len={len(_v)})", flush=True)
    else:
        print(f"   ❌ {_k} = NOT SET ← fix this in Railway Variables", flush=True)
print(flush=True)

try:
    _startup_token = zerodha_auto_login()
    print("✅ Auto-login successful", flush=True)
except Exception as _startup_err:
    _startup_token = None
    print(f"❌ Startup login failed: {_startup_err}", flush=True)
    try:
        send_message(
            f"❌ KITE LOGIN FAILED AT STARTUP\n"
            f"Error: {str(_startup_err)[:200]}\n"
            f"Bot is running with expired/old token — orders will be rejected.\n"
            f"Fix: Check TOTP secret, password, IP whitelist then redeploy."
        )
    except Exception:
        pass

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

    Railway Hobby plan uses a STATIC IP — set RAILWAY_STATIC_IP env var to skip
    the ipify.org lookup and always use the known fixed IP (faster + more reliable).
    Your current static IP: 35.236.200.109
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
        # Railway Hobby plan has a STATIC IP — use it directly if configured.
        _static_ip = os.environ.get("RAILWAY_STATIC_IP", "").strip()
        if _static_ip:
            current_ip = _static_ip
            print(f"🌐 Using static Railway IP: {current_ip}", flush=True)
        else:
            current_ip = requests.get("https://api.ipify.org", timeout=5).text.strip()
            print(f"🌐 Railway dynamic IP: {current_ip}", flush=True)

        sess = requests.Session()
        sess.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-Requested-With": "XMLHttpRequest",
        })

        # ── Step 2: Login to Kite developer portal ───────────────────────────
        r1 = sess.post(
            "https://developers.kite.trade/api/users/signin",
            json={"user_id": dev_user, "password": dev_pass},
            timeout=10
        )

        # Kite dev portal sometimes returns empty body (HTML redirect, CSRF block,
        # or invalid credentials) — handle gracefully so the bot doesn't crash.
        if not r1.text.strip():
            print(f"❌ IP whitelist: Kite dev portal returned empty response "
                  f"(HTTP {r1.status_code}). "
                  f"Check KITE_DEV_USER_ID / KITE_DEV_PASSWORD env vars. "
                  f"Bot will continue without IP whitelisting.", flush=True)
            print(f"   ℹ️  Manually whitelist {current_ip} at: "
                  f"https://developers.kite.trade/apps/{app_id}", flush=True)
            return False

        try:
            d1 = r1.json()
        except Exception:
            print(f"❌ IP whitelist: Kite dev portal login response is not JSON. "
                  f"HTTP {r1.status_code} | Body: {r1.text[:200]!r}", flush=True)
            print(f"   ℹ️  Manually whitelist {current_ip} at: "
                  f"https://developers.kite.trade/apps/{app_id}", flush=True)
            return False

        if d1.get("status") != "success":
            print(f"❌ Kite dev portal login failed: {d1.get('message', d1)}", flush=True)
            return False

        print(f"✅ Kite dev portal logged in", flush=True)

        # ── Step 3: Update IP whitelist for the app ──────────────────────────
        r2 = sess.put(
            f"https://developers.kite.trade/api/apps/{app_id}",
            json={"ip_whitelist": [current_ip]},
            timeout=10
        )

        if not r2.text.strip():
            print(f"❌ IP whitelist update: empty response from Kite (HTTP {r2.status_code}). "
                  f"Manually add {current_ip} at: "
                  f"https://developers.kite.trade/apps/{app_id}", flush=True)
            return False

        try:
            d2 = r2.json()
        except Exception:
            print(f"❌ IP whitelist update response is not JSON. "
                  f"HTTP {r2.status_code} | Body: {r2.text[:200]!r}", flush=True)
            return False

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
USE_ADX_FILTER     = False   # OFF — using first-candle range filter instead
ADX_MIN_VALUE      = 20

# ── Daily profit target ───────────────────────────────────────────────────────
# Read from Railway env var DAILY_PROFIT_TARGET so each bot can have its own.
# Set to 0 to disable. Default = 0 (disabled) if env var not set.
# Bot 1 (NWW864): set DAILY_PROFIT_TARGET=1000 in Railway Variables
# Bot 2 (REW397): leave unset or set to 0 for no limit
DAILY_PROFIT_TARGET = int(os.environ.get("DAILY_PROFIT_TARGET", "0"))

# Profit protection mode — after daily target hit, allow new trades
# but stop if P&L drops below the target (protect the gains)
USE_PROFIT_PROTECTION = os.environ.get("USE_PROFIT_PROTECTION", "false").lower() == "true"
_profit_protection_floor = 0.0   # set when target first hit


def daily_profit_target_monitor():
    """
    Background thread — checks combined P&L every 5 seconds.
    When P&L >= DAILY_PROFIT_TARGET:
      1. Exit ALL active positions immediately
      2. Send Telegram alert
      3. Block new entries for rest of day (via apply_entry_filters check)
    Runs only when DAILY_PROFIT_TARGET > 0.
    """
    global _daily_target_exited, _profit_protection_floor, _daily_max_loss_hit
    global nifty_trade_active, banknifty_trade_active, finnifty_trade_active
    global sensex_trade_active, crude_trade_active
    global nifty_position, banknifty_position, finnifty_position, sensex_position, crude_position
    global global_trade_active, last_executed_signal_nifty
    global last_executed_signal_banknifty, last_executed_signal_finnifty
    global last_executed_signal_sensex, last_executed_signal_crude

    if DAILY_PROFIT_TARGET <= 0:
        return   # disabled — exit thread immediately

    print(f"🎯 Daily profit target monitor started — target: ₹{DAILY_PROFIT_TARGET}", flush=True)

    _last_reset_date = None

    while True:
        try:
            time.sleep(5)

            now_ist = datetime.now(IST)

            # Only run during market hours (9:15 AM to 3:30 PM weekdays)
            # Prevents midnight/weekend false triggers
            if now_ist.weekday() >= 5:
                continue   # weekend — skip
            _hour, _min = now_ist.hour, now_ist.minute
            _in_market = (
                (_hour == 9  and _min >= 15) or
                (9 < _hour < 15) or
                (_hour == 15 and _min <= 35)
            )
            if not _in_market:
                continue   # outside market hours — skip

            # Reset flag at start of new trading day
            _today = now_ist.date()
            if _last_reset_date != _today:
                _daily_target_exited     = False
                _profit_protection_floor = 0.0
                _daily_max_loss_hit      = False
                daily_profit_target_monitor._protection_triggered = False
                daily_profit_target_monitor._target_hit_time      = 0
                daily_profit_target_monitor._floor_tick           = 0
                _last_reset_date         = _today
                # Also directly reset all P&L vars to avoid stale yesterday data
                with lock:
                    global nifty_daily_pnl, banknifty_daily_pnl, finnifty_daily_pnl
                    global sensex_daily_pnl, crude_daily_pnl
                    nifty_daily_pnl     = 0.0
                    banknifty_daily_pnl = 0.0
                    finnifty_daily_pnl  = 0.0
                    sensex_daily_pnl    = 0.0
                    crude_daily_pnl     = 0.0
                print(f"🎯 Target monitor: new day reset + P&L cleared at {now_ist.strftime('%H:%M')}", flush=True)
                continue   # skip this tick

            # Wait until 9:25 AM before checking P&L — loops need time to reset
            if _hour == 9 and _min < 25:
                continue   # too early — instrument loops haven't reset yet

            # ── Calculate combined P&L — closed trades + live unrealised ─────
            _closed_pnl = (nifty_daily_pnl + banknifty_daily_pnl +
                           finnifty_daily_pnl + sensex_daily_pnl + crude_daily_pnl)
            _combined   = _closed_pnl
            _live_pnl   = 0.0

            try:
                _net_positions = kite.positions().get("net", [])
                for _p in _net_positions:
                    if _p.get("quantity", 0) > 0:
                        _live_pnl += float(_p.get("pnl", 0) or 0)
                _combined += _live_pnl
            except Exception as _lp_err:
                print(f"⚠️ Live P&L fetch error: {_lp_err}", flush=True)

            if (_live_pnl != 0 or _closed_pnl != 0) and _combined > DAILY_PROFIT_TARGET * 0.5:
                print(f"🎯 Target monitor: closed=₹{_closed_pnl:.0f} + "
                      f"live=₹{_live_pnl:.0f} = combined=₹{_combined:.0f} "
                      f"/ target=₹{DAILY_PROFIT_TARGET}", flush=True)

            # ── Daily max loss — stop ALL trading if loss too deep ────────────
            if DAILY_MAX_LOSS > 0 and not _daily_max_loss_hit:
                if _combined <= -DAILY_MAX_LOSS:
                    _daily_max_loss_hit = True
                    print(f"🛑 DAILY MAX LOSS HIT: ₹{_combined:.0f} <= -₹{DAILY_MAX_LOSS}", flush=True)
                    send_message(
                        f"🛑 DAILY MAX LOSS REACHED\n"
                        f"💸 Combined P&L: ₹{_combined:.0f} (closed+live)\n"
                        f"🔒 Limit: -₹{DAILY_MAX_LOSS}\n"
                        f"🛑 All trading stopped for today\n"
                        f"📅 Resumes tomorrow at 9:20 AM"
                    )
                    for _inst, _pos, _ in [
                        ("NIFTY",     nifty_position,    nifty_trade_active),
                        ("BANKNIFTY", banknifty_position, banknifty_trade_active),
                        ("FINNIFTY",  finnifty_position,  finnifty_trade_active),
                        ("SENSEX",    sensex_position,    sensex_trade_active),
                        ("CRUDE",     crude_position,     crude_trade_active),
                    ]:
                        with lock:
                            _sym  = _pos.get("symbol")
                            _qty  = _pos.get("qty", 0)
                            _exc  = _pos.get("exchange")
                            _active = _pos.get("active", False)
                        if _active and _sym and _qty > 0:
                            print(f"   🔴 Max-loss exit {_inst}: {_sym}", flush=True)
                            exit_position(_sym, _qty, _exc)

            if _daily_max_loss_hit:
                continue   # silent — block all new entries

            if _daily_target_exited and not USE_PROFIT_PROTECTION:
                continue   # target hit normal mode — skip

            # ── PROFIT PROTECTION MODE ────────────────────────────────────────
            if _daily_target_exited and USE_PROFIT_PROTECTION:

                # If protection already triggered — silently block entries
                if getattr(daily_profit_target_monitor, '_protection_triggered', False):
                    time.sleep(5)
                    continue

                # Grace period — wait 60s after target hit for exits to settle
                _hit_time = getattr(daily_profit_target_monitor, '_target_hit_time', 0)
                if time.time() - _hit_time < 60:
                    print(f"🛡️ Protection grace period — waiting for exits to settle", flush=True)
                    continue

                # Dynamic trailing floor — rises as profit grows, never drops
                if _combined > _profit_protection_floor:
                    _new_floor = max(
                        DAILY_PROFIT_TARGET,
                        _combined * 0.85
                    )
                    if _new_floor > _profit_protection_floor:
                        print(f"🛡️ Protection floor raised: ₹{_profit_protection_floor:.0f} → ₹{_new_floor:.0f} "
                              f"(85% of peak ₹{_combined:.0f})", flush=True)
                        _profit_protection_floor = _new_floor

                if _combined < _profit_protection_floor:
                    # First time protection triggers — alert once and exit
                    daily_profit_target_monitor._protection_triggered = True
                    print(f"🛡️ PROFIT PROTECTION TRIGGERED: ₹{_combined:.0f} < floor ₹{_profit_protection_floor:.0f}", flush=True)
                    send_message(
                        f"🛡️ PROFIT PROTECTION TRIGGERED\n"
                        f"💰 Combined P&L: ₹{_combined:.0f} (live included)\n"
                        f"🔒 Floor: ₹{_profit_protection_floor:.0f}\n"
                        f"📉 P&L dropped below protection floor\n"
                        f"🛑 Exiting all positions — no more trades today\n"
                        f"(This alert will not repeat)"
                    )
                    for _inst, _pos, _ in [
                        ("NIFTY",     nifty_position,     nifty_trade_active),
                        ("BANKNIFTY", banknifty_position,  banknifty_trade_active),
                        ("FINNIFTY",  finnifty_position,   finnifty_trade_active),
                        ("SENSEX",    sensex_position,     sensex_trade_active),
                        ("CRUDE",     crude_position,      crude_trade_active),
                    ]:
                        with lock:
                            _sym  = _pos.get("symbol")
                            _qty  = _pos.get("qty", 0)
                            _exc  = _pos.get("exchange")
                            _active = _pos.get("active", False)
                        if _active and _sym and _qty > 0:
                            print(f"   🔴 Protection exit {_inst}: {_sym}", flush=True)
                            _ep_ok = exit_position(_sym, _qty, _exc)
                            if not _ep_ok:
                                send_message(f"🚨 PROTECTION EXIT FAILED — EXIT {_sym} MANUALLY")
                    # Floor = 0 so apply_entry_filters blocks new entries
                    _profit_protection_floor = 0.0
                else:
                    # Above floor — allow new trades, print only every ~30 ticks
                    if not hasattr(daily_profit_target_monitor, '_floor_tick'):
                        daily_profit_target_monitor._floor_tick = 0
                    daily_profit_target_monitor._floor_tick += 1
                    if daily_profit_target_monitor._floor_tick % 30 == 0:
                        print(f"🛡️ Protection: ₹{_combined:.0f} above floor ₹{_profit_protection_floor:.0f} ✅", flush=True)
                continue

            # ── First time target hit ─────────────────────────────────────────
            if _combined < DAILY_PROFIT_TARGET:
                continue

            _daily_target_exited = True
            _profit_protection_floor = DAILY_PROFIT_TARGET   # protect the target amount
            daily_profit_target_monitor._target_hit_time = time.time()   # grace period start
            print(f"🎯 DAILY TARGET HIT ₹{_combined:.0f} — exiting all positions", flush=True)

            _exited = []

            for _inst, _pos, _token_flag in [
                ("NIFTY",     nifty_position,     nifty_trade_active),
                ("BANKNIFTY", banknifty_position,  banknifty_trade_active),
                ("FINNIFTY",  finnifty_position,   finnifty_trade_active),
                ("SENSEX",    sensex_position,     sensex_trade_active),
                ("CRUDE",     crude_position,      crude_trade_active),
            ]:
                with lock:
                    _sym = _pos.get("symbol")
                    _qty = _pos.get("qty", 0)
                    _exc = _pos.get("exchange")
                    _active = _pos.get("active", False)

                if _active and _sym and _qty > 0:
                    print(f"   🔴 Exiting {_inst}: {_sym} qty={_qty}", flush=True)
                    try:
                        _ok = exit_position(_sym, _qty, _exc)
                        if _ok:
                            _exited.append(f"{_inst}: {_sym}")
                        else:
                            _exited.append(f"{_inst}: {_sym} ❌ EXIT FAILED — EXIT MANUALLY!")
                            send_message(
                                f"🚨 DAILY TARGET EXIT FAILED\n"
                                f"📌 {_inst}: {_sym}\n"
                                f"📦 Qty: {_qty}\n"
                                f"⚠️ IP may be blocked — exit manually on Kite!\n"
                                f"🔧 Fix: developers.kite.trade → Profile → IP Whitelist → Clear all"
                            )
                    except Exception as _ex_err:
                        print(f"   ⚠️ Exit error {_inst}: {_ex_err}", flush=True)
                        _exited.append(f"{_inst}: {_sym} ❌ ERROR")

                    with lock:
                        _pos.update({"symbol": None, "qty": 0,
                                     "exchange": None, "signal": None,
                                     "active": False})
                        if _inst == "NIFTY":
                            nifty_trade_active = False
                            last_executed_signal_nifty = None
                        elif _inst == "BANKNIFTY":
                            banknifty_trade_active = False
                            last_executed_signal_banknifty = None
                        elif _inst == "FINNIFTY":
                            finnifty_trade_active = False
                            last_executed_signal_finnifty = None
                        elif _inst == "SENSEX":
                            sensex_trade_active = False
                            last_executed_signal_sensex = None
                        else:
                            crude_trade_active = False
                            last_executed_signal_crude = None

            with lock:
                global_trade_active = (nifty_trade_active or banknifty_trade_active or
                                       finnifty_trade_active or sensex_trade_active or crude_trade_active)

            _exit_summary = "\n".join(_exited) if _exited else "No active positions"

            if USE_PROFIT_PROTECTION:
                send_message(
                    f"🎯 DAILY TARGET HIT ₹{_combined:.0f}\n"
                    f"🛡️ PROFIT PROTECTION MODE ON\n"
                    f"🔒 Floor: ₹{_profit_protection_floor:.0f} — new trades allowed\n"
                    f"⚠️ Will stop if combined drops below ₹{_profit_protection_floor:.0f}\n"
                    f"🔴 Closed:\n{_exit_summary}"
                )
            else:
                send_message(
                    f"🎯 DAILY TARGET HIT — ALL TRADES CLOSED\n"
                    f"💰 Combined P&L: ₹{_combined:.0f}\n"
                    f"🎉 Target: ₹{DAILY_PROFIT_TARGET:.0f}\n"
                    f"📊 NIFTY:     ₹{nifty_daily_pnl:.0f}\n"
                    f"📊 BANKNIFTY: ₹{banknifty_daily_pnl:.0f}\n"
                    f"📊 FINNIFTY:  ₹{finnifty_daily_pnl:.0f}\n"
                    f"📊 SENSEX:    ₹{sensex_daily_pnl:.0f}\n"
                    f"📊 CRUDE:     ₹{crude_daily_pnl:.0f}\n"
                    f"🔴 Closed:\n{_exit_summary}\n"
                    f"🛑 No new entries for rest of day"
                )

        except Exception as _mon_err:
            print(f"⚠️ Target monitor error: {_mon_err}", flush=True)

# ── Support & Resistance Filter ───────────────────────────────────────────────
USE_SR_FILTER      = os.environ.get("USE_SR_FILTER", "true").lower() == "true"
SR_BLOCK_PCT       = 0.003   # 0.3% proximity for PDH/PDL/Pivot (when enabled)
SR_ALGO_BLOCK_PCT  = 0.001   # 0.1% proximity for Algo SZ/RZ — tighter to avoid blocking valid trades
                               # 0.1% = ~24 pts on Nifty 24000, ~75 pts on SENSEX 75000

# Which SR methods to use — enable/disable independently
SR_USE_PDH_PDL     = False   # PDH/PDL — good but blocks many valid breakouts
SR_USE_PIVOTS      = False   # Pivot R1/R2/S1/S2 — same issue
SR_USE_ROUND       = False   # Round numbers — too frequent on 50-pt grid
SR_USE_ALGO_OI     = True    # Algo SZ/RZ from OI — major institutional levels only

# ── 9/15 EMA Second Signal Source ────────────────────────────────────────────
# Both HalfTrend AND 9/15 EMA must agree before an order is placed.
# CALL entry: 9 EMA must be above 15 EMA (bullish alignment)
# PUT  entry: 9 EMA must be below 15 EMA (bearish alignment)
# Applies to both NIFTY and CRUDE on 15-min chart.
# Set False to disable and trade on HalfTrend signal alone.
# ─────────────────────────────────────────────────────────────────────────────
USE_EMA_FILTER     = False   # OFF — disabled

USE_MTF_FILTER     = False  # 1-hour HalfTrend confirmation (off — HalfTrend already
                            # handles direction; ADX handles the real failure mode)

USE_VIX_FILTER     = False  # India VIX range filter (off)
VIX_MIN            = 11     # (used only when USE_VIX_FILTER = True)
VIX_MAX            = 22     # (used only when USE_VIX_FILTER = True)

USE_SESSION_FILTER = False  # Session dead-zone filter (off)

# ── Instrument Enable / Disable ───────────────────────────────────────────────
# Control each instrument via Railway Variables — no code change needed.
# Default: NIFTY and SENSEX on, rest off.
ENABLE_NIFTY      = os.environ.get("ENABLE_NIFTY",      "true").lower()  == "true"
ENABLE_BANKNIFTY  = os.environ.get("ENABLE_BANKNIFTY",  "false").lower() == "true"
ENABLE_FINNIFTY   = os.environ.get("ENABLE_FINNIFTY",   "false").lower() == "true"
ENABLE_SENSEX     = os.environ.get("ENABLE_SENSEX",     "true").lower()  == "true"
LOW_BALANCE_THRESHOLD = 1500   # if equity balance < ₹1,500 → only SENSEX allowed

# ── Lot sizes — configurable via Railway Variables ────────────────────────────
# Change without redeploying: set NIFTY_LOT_SIZE=65 in Railway Variables
NIFTY_LOT_SIZE     = int(os.environ.get("NIFTY_LOT_SIZE",     "65"))
BANKNIFTY_LOT_SIZE = int(os.environ.get("BANKNIFTY_LOT_SIZE", "30"))
FINNIFTY_LOT_SIZE  = int(os.environ.get("FINNIFTY_LOT_SIZE",  "60"))
SENSEX_LOT_SIZE    = int(os.environ.get("SENSEX_LOT_SIZE",    "20"))
CRUDE_LOT_SIZE     = int(os.environ.get("CRUDE_LOT_SIZE",     "100"))

# ── Number of lots per trade — configurable via Railway Variables ─────────────
# 0 = auto (calculated by calculate_lots based on balance)
# 1,2,3 = fixed number of lots regardless of balance
NIFTY_NUM_LOTS     = int(os.environ.get("NIFTY_NUM_LOTS",     "0"))
BANKNIFTY_NUM_LOTS = int(os.environ.get("BANKNIFTY_NUM_LOTS", "0"))
FINNIFTY_NUM_LOTS  = int(os.environ.get("FINNIFTY_NUM_LOTS",  "0"))
SENSEX_NUM_LOTS    = int(os.environ.get("SENSEX_NUM_LOTS",    "0"))
CRUDE_NUM_LOTS     = int(os.environ.get("CRUDE_NUM_LOTS",     "0"))

# ── Max loss per trade — configurable via Railway Variables ───────────────────
# MAX_LOSS_PER_TRADE: fixed ₹ loss limit regardless of lot size
# MAX_LOSS_PER_LOT:   per-lot ₹ loss limit (e.g. ₹800/lot × 2 lots = ₹1,600)
# If both set, uses whichever is LOWER (more conservative)
# Set to 0 to disable that check
MAX_LOSS_PER_TRADE = int(os.environ.get("MAX_LOSS_PER_TRADE", "800"))   # ₹800 default
MAX_LOSS_PER_LOT   = int(os.environ.get("MAX_LOSS_PER_LOT",   "800"))   # ₹800 per lot default

# ── HalfTrend settings ───────────────────────────────────────────────────────
HT_AMPLITUDE       = int(os.environ.get("HT_AMPLITUDE",      "4"))    # amplitude (1=sensitive, 4=smooth)
HT_LOOKBACK_CANDLES = int(os.environ.get("HT_LOOKBACK_CANDLES", "400")) # candles (200=~2.5 days on 15min)

# ── Spike reversal exit settings ──────────────────────────────────────────────
_SPIKE_MIN_PROFIT  = int(os.environ.get("SPIKE_MIN_PROFIT",  "400"))    # activate at ₹400 profit
_SPIKE_DROP_PCT    = float(os.environ.get("SPIKE_DROP_PCT",  "0.40"))   # exit if drops 40% from peak
_SPIKE_WINDOW_SECS = int(os.environ.get("SPIKE_WINDOW_SECS", "120"))    # within 2 min

# Daily max loss — stop ALL trading if combined loss exceeds this
# Prevents catastrophic days. Set 0 to disable.
DAILY_MAX_LOSS     = int(os.environ.get("DAILY_MAX_LOSS",     "1500"))  # ₹1,500 default
_daily_max_loss_hit = False   # flag — set True when daily loss limit hit


print(f"📦 Lot sizes: NIFTY={NIFTY_LOT_SIZE} BN={BANKNIFTY_LOT_SIZE} "
      f"FN={FINNIFTY_LOT_SIZE} SENSEX={SENSEX_LOT_SIZE} CRUDE={CRUDE_LOT_SIZE}", flush=True)
print(f"📊 Num lots:  NIFTY={NIFTY_NUM_LOTS or 'auto'} BN={BANKNIFTY_NUM_LOTS or 'auto'} "
      f"FN={FINNIFTY_NUM_LOTS or 'auto'} SENSEX={SENSEX_NUM_LOTS or 'auto'} "
      f"CRUDE={CRUDE_NUM_LOTS or 'auto'}", flush=True)
ENABLE_CRUDE      = os.environ.get("ENABLE_CRUDE",      "false").lower() == "true"
ENABLE_SWING      = os.environ.get("ENABLE_SWING",      "false").lower() == "true"

# ── Daily Trade Limits ────────────────────────────────────────────────────────
MAX_NIFTY_TRADES_PER_DAY      = int(os.environ.get("MAX_NIFTY_TRADES",     "4"))
MAX_BANKNIFTY_TRADES_PER_DAY  = int(os.environ.get("MAX_BANKNIFTY_TRADES", "4"))
MAX_FINNIFTY_TRADES_PER_DAY   = int(os.environ.get("MAX_FINNIFTY_TRADES",  "4"))
MAX_SENSEX_TRADES_PER_DAY     = int(os.environ.get("MAX_SENSEX_TRADES",    "4"))
MAX_CRUDE_TRADES_PER_DAY      = int(os.environ.get("MAX_CRUDE_TRADES",     "3"))

# ── Swing Trade Settings ──────────────────────────────────────────────────────
SWING_STOCKS_FILE        = "stocks.txt"  # one NSE symbol per line (e.g. RELIANCE)
SWING_SL_PCT             = 0.05          # 5%  hard stop-loss below entry
SWING_TARGET_PCT         = 0.10          # 10% profit target above entry
SWING_CAPITAL_PER_STOCK  = 10000         # ₹10,000 deployed per stock position
MAX_SWING_POSITIONS      = 5             # max concurrent open swing positions

# ── Stock Options Settings ────────────────────────────────────────────────────
# Signal: last closed daily candle HalfTrend → buy CE (CALL) or PE (PUT)
# Execution: MIS (intraday), force-close at 3:15 PM
ENABLE_STOCK_OPTIONS         = False
STOCK_OPTIONS_FILE           = "stock_options.txt"  # one NSE symbol per line
STOCK_OPTIONS_SL_PCT         = 0.30   # 30% SL on option premium
STOCK_OPTIONS_TARGET_PCT     = 0.50   # 50% profit target on premium
STOCK_OPTIONS_CAPITAL        = 5000   # ₹5,000 max deployed per stock option
MAX_STOCK_OPTIONS_POSITIONS  = 5      # max concurrent stock option positions
STOCK_OPT_FORCE_CLOSE_HOUR   = 15     # force-close hour (IST)
STOCK_OPT_FORCE_CLOSE_MIN    = 15     # force-close minute (IST) → 3:15 PM

# ── Screener.in Integration ───────────────────────────────────────────────────
# Set USE_SCREENER = True and add credentials to config.py to auto-populate
# stock lists each morning from your saved Screener.in screen.
# Setup: 1) Create the screen on screener.in  2) Note the ID from URL
#        e.g. https://www.screener.in/screens/123456/ → SCREENER_SCREEN_ID = "123456"
USE_SCREENER              = getattr(config, "USE_SCREENER", False)
SCREENER_SESSION_COOKIE   = getattr(config, "SCREENER_SESSION_COOKIE", "")   # 'sessionid' cookie value
SCREENER_SCREEN_ID        = getattr(config, "SCREENER_SCREEN_ID", "")        # numeric ID from URL

# ── Higher Timeframe (30-min) Trend Filter ────────────────────────────────────
# When True, a 15-min signal is only taken if the 30-min HalfTrend trend
# direction AGREES.  Filters out counter-trend entries on 15-min.
# Set False to trade all 15-min signals regardless of 30-min trend.
USE_HTF_FILTER = False           # ✅ 30-min direction filter enabled

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

global_trade_active = False

# -----------------------------
# RISK VARIABLES
# -----------------------------
daily_pnl = 0
trade_count = 0
last_loss_time = None
last_reset_date = None

MAX_DRAWDOWN = -3000   # adjust based on capital
win_streak  = 0
loss_streak = 0   # global — kept for backward compat only
# Per-instrument consecutive loss counters — independent for each instrument
_loss_streak = {
    "NIFTY": 0, "BANKNIFTY": 0, "FINNIFTY": 0, "SENSEX": 0, "CRUDE": 0
}
_win_streak = {
    "NIFTY": 0, "BANKNIFTY": 0, "FINNIFTY": 0, "SENSEX": 0, "CRUDE": 0
}


last_trade_time_nifty = 0
last_trade_time_crude = 0

SIGNAL_COOLDOWN = 90  
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

NIFTY_FUT_TOKEN = None

ml_cache = {"time": 0, "data": None}
ML_CACHE_TTL = 2  # seconds


last_executed_signal_nifty = None
last_exit_time_crude = 0
last_exit_time_nifty = 0
REENTRY_COOLDOWN = 600  # 10 min
CRUDE_SYMBOL = None

TRADE_LOG_FILE = "trade_log.csv"
last_executed_signal_crude = None
CRUDE_TOKEN = config.CRUDE_TOKEN
BANKNIFTY_TOKEN = getattr(config, "BANKNIFTY_TOKEN", None)
SENSEX_TOKEN    = getattr(config, "SENSEX_TOKEN",    None)
FINNIFTY_TOKEN  = getattr(config, "FINNIFTY_TOKEN",  None)
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
_finnifty_trade_gen  = [0]
_sensex_trade_gen    = [0]
_crude_trade_gen     = [0]

nifty_trade_count     = 0
banknifty_trade_count = 0
finnifty_trade_count  = 0
sensex_trade_count    = 0
crude_trade_count     = 0

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

finnifty_position    = {"symbol": None, "qty": 0, "exchange": None, "signal": None, "active": False}
finnifty_trade_active  = False
last_executed_signal_finnifty = None
last_exit_time_finnifty = 0
finnifty_daily_pnl   = 0
finnifty_daily_wins  = 0
finnifty_daily_losses = 0
finnifty_trade_count = 0

sensex_position      = {"symbol": None, "qty": 0, "exchange": None, "signal": None, "active": False}
sensex_trade_active  = False
last_executed_signal_sensex = None
last_exit_time_sensex = 0

# Cache for BankNifty + SENSEX 15-min data
last_fetch_banknifty = 0
cached_banknifty_df  = None
cached_banknifty_ht  = None

last_fetch_finnifty  = 0
cached_finnifty_df   = None
cached_finnifty_ht   = None

last_fetch_sensex    = 0
cached_sensex_df     = None
cached_sensex_ht     = None

# ── Swing trade state ─────────────────────────────────────────────────────────
# { symbol: {"entry": float, "qty": int, "sl": float, "target": float,
#            "signal": "CALL", "entry_time": datetime} }
swing_positions      = {}
swing_positions_lock = threading.Lock()
swing_daily_pnl      = 0
swing_daily_wins     = 0
swing_daily_losses   = 0
swing_trade_count    = 0
_swing_token_cache   = {}   # {symbol: token_int}
_swing_data_cache    = {}   # {symbol: (timestamp, df)}  — 4-hour TTL

# ── Screener.in cache ─────────────────────────────────────────────────────────
_screener_session         = None          # requests.Session (reused across calls)
_screener_stocks_today    = []            # all NSE symbols from screen today
_screener_fo_stocks_today = []            # F&O-eligible subset (have NFO options)
_screener_refresh_date    = None          # date object — re-fetches once per day

# ── Disabled-instrument log suppression ──────────────────────────────────────
# Each flag flips to True after the first "disabled" print — never prints again.
_crude_disabled_logged    = False
_banknifty_disabled_logged = False
_finnifty_disabled_logged  = False
_sensex_disabled_logged   = False

# ── Insufficient balance alert rate-limiter ───────────────────────────────────
# Stores last alert timestamp per instrument — alerts at most once per 30 min.
_insufficient_balance_alerted = {}   # instrument -> float (time.time())

# ── Progressive daily profit lock ────────────────────────────────────────────
# Tracks the highest daily P&L reached so far and the active lock floor.
# Tiers:  ₹1000 → lock 80%  (floor = ₹800)
#         ₹2000 → lock 85%  (floor = ₹1700)
#         ₹3000 → lock 90%  (floor = ₹2700)
# Once a tier is activated the floor only ever rises — never drops back.
_peak_daily_pnl   = 0.0   # highest daily_pnl seen today
_profit_lock_floor = 0.0  # minimum P&L we must stay above to keep trading
_profit_lock_tier  = 0    # 0 = none, 1 = 80%, 2 = 85%, 3 = 90%

# ── Last exited symbol per instrument — same-strike re-entry guard ────────────
# After exiting, the symbol is stored here.  The loop blocks re-entry of the
# exact same symbol until a NEW arrow fires (is_fresh=True) or trend flips.
_last_exited_symbol = {}   # instrument -> str e.g. "NIFTY2650523900PE"
_ip_blocked          = False   # True when Kite rejects orders due to IP
_ip_alert_sent       = False   # one-time alert flag — prevents spam
_blocked_strikes    = {    # strikes blocked after max-loss exit — cleared daily
    "NIFTY": set(), "BANKNIFTY": set(), "FINNIFTY": set(),
    "SENSEX": set(), "CRUDE": set()
}
# Whipsaw detection — track recent flip timestamps per instrument
_flip_timestamps    = {    # {instrument: [timestamp, ...]} — last N flip times
    "NIFTY": [], "BANKNIFTY": [], "FINNIFTY": [], "SENSEX": [], "CRUDE": []
}
WHIPSAW_WINDOW_SECS  = 1800   # 30-min window
WHIPSAW_MAX_FLIPS    = 3      # if >= 3 flips in 30 min → whipsaw detected
WHIPSAW_PAUSE_SECS   = 1800   # pause 30 min after whipsaw detected
USE_WHIPSAW_FILTER   = os.environ.get("USE_WHIPSAW_FILTER", "true").lower() == "true"
_whipsaw_pause_until = {}    # {instrument: timestamp}

# ── Profit-lock exit cooldown ─────────────────────────────────────────────────
# After a per-trade profit lock exit, block new entries for 15 minutes to avoid
# immediately jumping back into the same move and giving gains back.
_profit_lock_exit_time = {}   # instrument -> float (time.time())

# ── Stock Options state ───────────────────────────────────────────────────────
# { underlying_symbol: {"option_symbol": str, "entry": float, "qty": int,
#                        "sl": float, "target": float, "signal": str, "exchange": "NFO"} }
stock_options_positions      = {}
stock_options_positions_lock = threading.Lock()
stock_options_daily_pnl      = 0
stock_options_daily_wins     = 0
stock_options_daily_losses   = 0
stock_options_trade_count    = 0

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
        (now.hour == 9 and now.minute >= 20) or   # entries from 9:20 AM
        (9 < now.hour < 15) or
        (now.hour == 15 and now.minute < 20)
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
# ======================================
# TRUE TradingView ATR (Wilder RMA)
# ta.atr(period)
# ======================================

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

# 30-min HalfTrend cache for HTF direction filter
# { token: {"ht": DataFrame, "ts": float} }  — refreshed every 5 min
_htf_30m_cache = {}

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
# 📐 check_htf_filter(signal, token)
# 30-minute HalfTrend direction filter.
# Only allows entry when the 30-min trend agrees with the 15-min signal.
# ──────────────────────────────────────────────────────────────────────────────
def check_htf_filter(signal, token):
    """
    Returns (allowed: bool, reason: str).

    Rule:
      30-min trend == 0 (bullish) → only CALL entries allowed
      30-min trend == 1 (bearish) → only PUT entries allowed

    Caches the 30-min HalfTrend DataFrame for 5 minutes to avoid
    recomputing on every loop iteration (30-min candles barely change
    within a 5-min window).

    If disabled (USE_HTF_FILTER=False) or data unavailable → always passes.
    """
    if not USE_HTF_FILTER:
        return True, "HTF filter off"

    now_ts = time.time()
    cached = _htf_30m_cache.get(token)

    try:
        # Refresh cache if missing or older than 5 minutes
        if cached is None or (now_ts - cached["ts"]) > 300:
            df_30m = get_cached_data(token, "30minute", 200)
            if df_30m is None or len(df_30m) < 50:
                return True, "HTF: not enough 30-min bars — filter skipped"
            ht_30m = halftrend_tv(df_30m, amplitude=2, channel_deviation=2)
            _htf_30m_cache[token] = {"ht": ht_30m, "ts": now_ts}
        else:
            ht_30m = cached["ht"]

        # Use last CLOSED 30-min candle (anti-repaint, same rule as 15-min)
        last_30m        = ht_30m.iloc[-2]
        trend_30m       = int(last_30m["trend"])   # 0 = bullish, 1 = bearish
        htf_direction   = "CALL" if trend_30m == 0 else "PUT"
        htf_ht_val      = last_30m["ht"]

        if htf_direction == signal:
            return True, f"✅ 30-min HT: {htf_direction} (HT={htf_ht_val:.1f}) — agrees with {signal}"
        else:
            return False, (
                f"🚫 HTF BLOCK: 30-min trend is {htf_direction} "
                f"(HT={htf_ht_val:.1f}) — counter-trend {signal} entry skipped"
            )

    except Exception as e:
        print(f"⚠️ HTF filter error: {e}", flush=True)
        return True, f"HTF filter error ({e}) — skipped"


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
_algo_sz_rz_cache = {}   # {instrument: (timestamp, rz_strike, sz_strike)}
_ALGO_OI_TTL      = 1800  # 30 min cache — OI doesn't change rapidly


def _get_algo_sz_rz(instrument, cur_price):
    """
    Returns (algo_rz, algo_sz) — the strike prices with highest CALL OI
    and highest PUT OI for the current expiry.

    algo_rz = max CALL OI strike = institutional resistance (Algo Resistance Zone)
    algo_sz = max PUT  OI strike = institutional support    (Algo Support Zone)

    Uses Kite's quote API to get OI for all strikes near ATM.
    Cached for 30 minutes to avoid excessive API calls.
    """
    global _algo_sz_rz_cache

    # Check cache
    _cached = _algo_sz_rz_cache.get(instrument)
    if _cached and (time.time() - _cached[0]) < _ALGO_OI_TTL:
        return _cached[1], _cached[2]

    try:
        # Determine exchange and index name
        if instrument == "NIFTY":
            _exchange  = "NFO"
            _name      = "NIFTY"
            _step      = 50
            _atm_range = 10
        elif instrument == "BANKNIFTY":
            _exchange  = "NFO"
            _name      = "BANKNIFTY"
            _step      = 100
            _atm_range = 10
        elif instrument == "FINNIFTY":
            _exchange  = "NFO"
            _name      = "FINNIFTY"
            _step      = 50
            _atm_range = 10
        elif instrument == "SENSEX":
            _exchange  = "BFO"
            _name      = "SENSEX"
            _step      = 100
            _atm_range = 10
        else:
            return None, None   # CRUDE options OI not relevant

        # Get current expiry instruments
        _instruments = kite.instruments(_exchange)

        # Find nearest expiry
        from datetime import date as _date
        _today = _date.today()
        _expiries = sorted(set(
            i["expiry"] for i in _instruments
            if i["name"] == _name
            and i["instrument_type"] in ("CE", "PE")
            and i["expiry"] >= _today
        ))
        if not _expiries:
            return None, None
        _near_expiry = _expiries[0]

        # Get ATM strike
        _atm = round(cur_price / _step) * _step

        # Build list of strikes to check (ATM ± range)
        _strikes = [_atm + (_step * i)
                    for i in range(-_atm_range, _atm_range + 1)]

        # Filter instruments for near expiry and our strikes
        _opt = [
            i for i in _instruments
            if i["name"] == _name
            and i["expiry"] == _near_expiry
            and i["strike"] in _strikes
            and i["instrument_type"] in ("CE", "PE")
        ]

        if not _opt:
            return None, None

        # Fetch OI via quote API (batch)
        _symbols = [f"{_exchange}:{i['tradingsymbol']}" for i in _opt]

        # Quote in batches of 100 (Kite limit)
        _quotes = {}
        for _i in range(0, len(_symbols), 100):
            _batch = _symbols[_i:_i+100]
            try:
                _q = kite.quote(_batch)
                _quotes.update(_q)
            except Exception:
                pass

        # Find max CALL OI and max PUT OI strikes
        _call_oi = {}  # strike → OI
        _put_oi  = {}  # strike → OI

        for _inst in _opt:
            _sym = f"{_exchange}:{_inst['tradingsymbol']}"
            _q   = _quotes.get(_sym, {})
            _oi  = _q.get("oi", 0) or 0
            _strike = _inst["strike"]
            if _inst["instrument_type"] == "CE":
                _call_oi[_strike] = _call_oi.get(_strike, 0) + _oi
            else:
                _put_oi[_strike]  = _put_oi.get(_strike, 0) + _oi

        if not _call_oi or not _put_oi:
            return None, None

        # Max CALL OI → Algo RZ (resistance)
        _algo_rz = max(_call_oi, key=_call_oi.get)
        # Max PUT  OI → Algo SZ (support)
        _algo_sz = max(_put_oi,  key=_put_oi.get)

        _top_call = sorted(_call_oi.items(), key=lambda x: -x[1])[:3]
        _top_put  = sorted(_put_oi.items(),  key=lambda x: -x[1])[:3]
        print(f"📊 OI [{instrument}] expiry={_near_expiry} | "
              f"Top CALL OI: {_top_call} | Top PUT OI: {_top_put}", flush=True)

        # Cache result
        _algo_sz_rz_cache[instrument] = (time.time(), _algo_rz, _algo_sz)
        return float(_algo_rz), float(_algo_sz)

    except Exception as _e:
        print(f"⚠️ _get_algo_sz_rz({instrument}): {_e}", flush=True)
        return None, None


def record_flip_and_check_whipsaw(instrument):
    """
    Records a HalfTrend flip timestamp for the instrument.
    Returns (is_whipsaw, pause_remaining_secs).
    If >= WHIPSAW_MAX_FLIPS flips in WHIPSAW_WINDOW_SECS → whipsaw detected.
    """
    global _flip_timestamps, _whipsaw_pause_until

    if not USE_WHIPSAW_FILTER:
        return False, 0   # disabled — no whipsaw detection

    now = time.time()

    # Check if currently in whipsaw pause
    pause_until = _whipsaw_pause_until.get(instrument, 0)
    if now < pause_until:
        return True, int(pause_until - now)

    # Record this flip
    _flip_timestamps[instrument].append(now)

    # Remove flips outside the window
    _flip_timestamps[instrument] = [
        t for t in _flip_timestamps[instrument]
        if now - t <= WHIPSAW_WINDOW_SECS
    ]

    flip_count = len(_flip_timestamps[instrument])
    print(f"🔄 {instrument} flip #{flip_count} in last {WHIPSAW_WINDOW_SECS//60} min", flush=True)

    # Whipsaw detected
    if flip_count >= WHIPSAW_MAX_FLIPS:
        _whipsaw_pause_until[instrument] = now + WHIPSAW_PAUSE_SECS
        _flip_timestamps[instrument].clear()  # reset after pause set
        print(f"⚠️ WHIPSAW DETECTED [{instrument}]: {flip_count} flips in "
              f"{WHIPSAW_WINDOW_SECS//60} min — pausing {WHIPSAW_PAUSE_SECS//60} min", flush=True)
        send_message(
            f"⚠️ WHIPSAW DETECTED — {instrument}\n"
            f"📊 {flip_count} arrow flips in {WHIPSAW_WINDOW_SECS//60} minutes\n"
            f"🛑 Choppy market — pausing entries for {WHIPSAW_PAUSE_SECS//60} minutes\n"
            f"⏰ Resuming at {datetime.fromtimestamp(now + WHIPSAW_PAUSE_SECS, IST).strftime('%H:%M')} IST"
        )
        return True, WHIPSAW_PAUSE_SECS

    return False, 0


def apply_entry_filters(signal, instrument, df_15m, token, **kwargs):
    """
    All entry filters — each independently controlled by a True/False flag.

    Filters (in order):
      1. Session dead zone   — USE_SESSION_FILTER
      2. ADX trend strength  — USE_ADX_FILTER        (ADX >= ADX_MIN_VALUE)
      3. MTF 1-hour          — USE_MTF_FILTER        (1h HalfTrend agrees)
      4. India VIX           — USE_VIX_FILTER
      5. EMA 9/15 stack      — USE_EMA_FILTER
      6. ML signal           — USE_ML_FILTER         (NIFTY only)
      7. Hull Suite          — USE_HULL_FILTER        ← NEW
         Hull color must match HalfTrend signal on the same 15-min closed candle.
         Green band (hull > hull[2]) → CALL only
         Red band   (hull < hull[2]) → PUT  only

    Returns (passed: bool, reason: str).
    """
    global _daily_target_exited, _daily_max_loss_hit   # must be global so reset propagates
    global _ip_blocked, _ip_alert_sent

    # ── IP blocked — test recovery every 2 min ───────────────────────────────
    if _ip_blocked:
        # Try a harmless API call to check if IP unblocked
        _last_check = getattr(apply_entry_filters, '_ip_last_check', 0)
        if time.time() - _last_check > 120:   # check every 2 min
            setattr(apply_entry_filters, '_ip_last_check', time.time())
            try:
                kite.profile()   # lightweight call to test IP
                _ip_blocked    = False
                _ip_alert_sent = False
                print("✅ IP block cleared — trading resumed", flush=True)
                send_message("✅ IP unblocked — trading resumed automatically")
            except Exception as _ip_e:
                if "not allowed" in str(_ip_e).lower():
                    print(f"🔕 IP still blocked — next check in 2 min", flush=True)
                else:
                    _ip_blocked    = False
                    _ip_alert_sent = False
                    print(f"✅ IP check passed — resuming", flush=True)
        return False, "🚫 IP blocked — fix whitelist at developers.kite.trade"
    now_ist = datetime.now(IST)

    # ── Daily max loss — stop all trading if loss too deep ───────────────────
    if DAILY_MAX_LOSS > 0 and _daily_max_loss_hit:
        return False, f"🛑 Daily max loss -₹{DAILY_MAX_LOSS} reached — no more trades today"

    # ── Whipsaw guard — pause entries if too many flips in short window ───────
    if USE_WHIPSAW_FILTER:
        _ws_pause = _whipsaw_pause_until.get(instrument, 0)
        if time.time() < _ws_pause:
            _mins_left = int((_ws_pause - time.time()) / 60) + 1
            return False, (
                f"⚠️ Whipsaw pause — choppy market detected, "
                f"waiting {_mins_left} more min before new entries"
            )

    # ── Daily profit target — stop new entries once hit ───────────────────────
    if DAILY_PROFIT_TARGET > 0 and _daily_target_exited:
        if USE_PROFIT_PROTECTION and _profit_protection_floor > 0:
            pass   # protection mode active — monitor handles floor, allow new entries
        else:
            # Re-check actual combined P&L (closed + live) before blocking
            # Flag may be stale if trades closed at a loss after target was hit
            _combined_pnl = (nifty_daily_pnl + banknifty_daily_pnl +
                             finnifty_daily_pnl + sensex_daily_pnl + crude_daily_pnl)
            # Also include live unrealised P&L
            try:
                _net = kite.positions().get("net", [])
                for _p in _net:
                    if _p.get("quantity", 0) > 0:
                        _combined_pnl += float(_p.get("pnl", 0) or 0)
            except Exception:
                pass

            if _combined_pnl >= DAILY_PROFIT_TARGET:
                return False, (
                    f"🎯 Daily target hit — combined P&L Rs.{_combined_pnl:.0f} "
                    f">= target Rs.{DAILY_PROFIT_TARGET:.0f} — no new entries today"
                )
            else:
                # P&L dropped below target — reset flag and allow new entries
                print(f"🔄 Daily target flag reset: P&L ₹{_combined_pnl:.0f} < target ₹{DAILY_PROFIT_TARGET:.0f} — resuming", flush=True)
                _daily_target_exited = False

    # ── 0. First candle range filter — rangebound inside-day detection ─────────
    # If price is still inside the 9:15 AM first candle's high/low range,
    # the market has no breakout direction — skip all entries.
    if df_15m is not None and len(df_15m) >= 2:
        _fc_ok, _fc_reason = check_first_candle_range(signal, df_15m, instrument)
        if not _fc_ok:
            return False, _fc_reason

    # ── 1. Session dead zone ──────────────────────────────────────────────────
    if USE_SESSION_FILTER and instrument in ("NIFTY", "BANKNIFTY", "SENSEX"):
        _dead = (
            (now_ist.hour == 11 and now_ist.minute >= 30) or
            (now_ist.hour == 12) or
            (now_ist.hour == 13 and now_ist.minute < 30)
        )
        if _dead:
            return False, "⏸️ Session filter: 11:30 AM–1:30 PM dead zone (low momentum)"

    # ── 2. ADX trend strength ─────────────────────────────────────────────────
    _adx_str = "ADX=off"
    if USE_ADX_FILTER and df_15m is not None:
        try:
            adx_val = ADX(df_15m, period=14).iloc[-2]
            if not np.isnan(adx_val) and adx_val < ADX_MIN_VALUE:
                reason = (f"📊 ADX={adx_val:.1f} below {ADX_MIN_VALUE} — "
                          f"market is rangebound/choppy, skipping entry")
                print(f"🚫 ADX BLOCK: {reason}", flush=True)
                return False, reason
            _adx_str = f"ADX={adx_val:.1f}" if not np.isnan(adx_val) else "ADX=N/A"
        except Exception as _e:
            _adx_str = f"ADX=err"

    # ── 3. MTF 1-hour confirmation ────────────────────────────────────────────
    _mtf_str = "MTF=off"
    if USE_MTF_FILTER:
        trend_1h = get_mtf_trend(token, instrument)
        if trend_1h is not None and trend_1h != signal:
            return False, f"⏸️ MTF filter: 1h={trend_1h} disagrees with 15m={signal}"
        _mtf_str = f"1h={trend_1h or 'N/A'}"

    # ── 4. India VIX ──────────────────────────────────────────────────────────
    _vix_str = "VIX=off"
    if USE_VIX_FILTER:
        vix = get_india_vix()
        if vix is not None:
            if vix < VIX_MIN:
                return False, f"⏸️ VIX filter: VIX={vix:.1f} below {VIX_MIN} (premiums too cheap)"
            if vix > VIX_MAX:
                return False, f"⏸️ VIX filter: VIX={vix:.1f} > {VIX_MAX} (too volatile)"
            _vix_str = f"VIX={vix:.1f}"

    # ── 5. EMA 9/15 stack confirmation ────────────────────────────────────────
    _ema_str = "EMA=off"
    if USE_EMA_FILTER and df_15m is not None and len(df_15m) >= 20:
        try:
            e9  = round(float(df_15m["close"].ewm(span=9,  adjust=False).mean().iloc[-2]), 2)
            e15 = round(float(df_15m["close"].ewm(span=15, adjust=False).mean().iloc[-2]), 2)
            if signal == "CALL" and e9 < e15:
                return False, f"⏸️ EMA filter: 9 EMA ({e9:.1f}) below 15 EMA ({e15:.1f}) — bearish stack vs CALL"
            if signal == "PUT" and e9 > e15:
                return False, f"⏸️ EMA filter: 9 EMA ({e9:.1f}) > 15 EMA ({e15:.1f}) — bullish stack vs PUT"
            _ema_str = f"EMA9={e9:.1f} {'above' if e9>e15 else 'below'} EMA15={e15:.1f}"
        except Exception as _e:
            _ema_str = "EMA=err"

    # ── 6. ML signal (NIFTY only) ─────────────────────────────────────────────
    _ml_str = "ML=off"
    if USE_ML_FILTER and instrument == "NIFTY":
        ml_sig, ml_conf, ml_reason = get_ml_signal()
        if ml_sig is None:
            _ml_str = "ML=skipped(server down)"
        elif ml_sig == "HOLD":
            return False, f"🤖 ML filter: HOLD — {ml_reason} ({ml_conf:.0f}%)"
        elif ml_sig != signal:
            return False, f"🤖 ML filter: ML={ml_sig} vs HT={signal} ({ml_conf:.0f}%)"
        elif ml_conf < ML_MIN_CONFIDENCE:
            return False, f"🤖 ML filter: low confidence {ml_conf:.0f}% below {ML_MIN_CONFIDENCE}%"
        else:
            _ml_str = f"ML={ml_sig}({ml_conf:.0f}%)"

    # ── 7. Hull Suite — colour must match HalfTrend AND band must be wide enough
    # EXCEPTIONS:
    # 1. Volume spike — bypass colour check, HalfTrend alone decides
    # Hull colour must ALWAYS match HalfTrend — no bypasses allowed
    # Bypasses were causing CALL entries during falling markets
    _hull_str = "Hull=off"
    if USE_HULL_FILTER and df_15m is not None:
        try:
            hull_sig, hval, h2val, bw_pct = get_hull_signal(
                df_15m, mode=HULL_MODE, length=HULL_LENGTH)

            _is_flip = kwargs.get("is_flip_reentry", False)

            if hull_sig is None or hval is None:
                # Hull transitioning — BLOCK entry, don't bypass
                # Entering during transition = entering during flip chaos
                reason = "🌊 Hull transitioning — waiting for colour confirmation"
                print(f"🚫 HULL BLOCK: {reason}", flush=True)
                return False, reason

            # ── Band width check ──────────────────────────────────────────────
            _now_ist = datetime.now(IST)
            _mins_since_open = (_now_ist.hour - 9) * 60 + _now_ist.minute - 15
            _morning_bypass  = (0 <= _mins_since_open <= HULL_MORNING_BYPASS_MINS)

            if USE_HULL_BAND_FILTER and HULL_MIN_BAND_WIDTH_PCT > 0 and bw_pct < HULL_MIN_BAND_WIDTH_PCT and not _morning_bypass:
                pts    = abs(hval - h2val)
                reason = (
                    f"🌊 Hull filter: band too thin — "
                    f"width={bw_pct*100:.3f}% ({pts:.1f} pts) "
                    f"min {HULL_MIN_BAND_WIDTH_PCT*100:.3f}% — "
                    f"trend transitioning, skipping signal"
                )
                print(f"🚫 HULL BLOCK: {reason}", flush=True)
                return False, reason
            elif USE_HULL_BAND_FILTER and _morning_bypass and bw_pct < HULL_MIN_BAND_WIDTH_PCT:
                print(f"🌅 Hull morning bypass active ({_mins_since_open} min) — "
                      f"band={bw_pct*100:.3f}% skipping width check only", flush=True)

            # ── Colour check — NO bypasses, always required ───────────────────
            if hull_sig != signal:
                hull_color = "🟢 GREEN" if hull_sig == "CALL" else "🔴 RED"
                ht_color   = "🟢 GREEN" if signal   == "CALL" else "🔴 RED"
                reason = (
                    f"🌊 Hull filter: Hull={hull_color} vs HalfTrend={ht_color} — "
                    f"colours must match (hull={hval:.1f}, hull2={h2val:.1f}, "
                    f"band={bw_pct*100:.3f}%)"
                )
                print(f"🚫 HULL BLOCK: {reason}", flush=True)
                return False, reason

            band_color = "🟢" if signal == "CALL" else "🔴"
            _hull_str  = (
                f"Hull={band_color}({hval:.1f} vs {h2val:.1f}) "
                f"band={bw_pct*100:.3f}%"
            )

        except Exception as _hull_e:
            _hull_str = f"Hull=err({_hull_e})"

    # ── SuperTrend Filter — must agree with HalfTrend signal ─────────────────
    _st_str = "ST=off"
    if USE_SUPERTREND_FILTER and df_15m is not None and len(df_15m) >= ST_PERIOD + 5:
        try:
            _st_signal, _st_val = get_supertrend_signal(df_15m, ST_PERIOD, ST_MULTIPLIER)
            if _st_signal is None:
                _st_str = "ST=transitioning"
                print(f"⚠️ SuperTrend transitioning — bypassing", flush=True)
            elif _st_signal != signal:
                _st_color = "🟢" if _st_signal == "CALL" else "🔴"
                _ht_color = "🟢" if signal == "CALL" else "🔴"
                reason = (f"📈 SuperTrend filter: ST={_st_color} {_st_signal} "
                          f"vs HalfTrend={_ht_color} {signal} — "
                          f"not aligned (ST line=₹{_st_val:.1f})")
                print(f"🚫 ST BLOCK: {reason}", flush=True)
                return False, reason
            else:
                _st_color = "🟢" if _st_signal == "CALL" else "🔴"
                _st_str = f"ST={_st_color}({_st_val:.1f})"
        except Exception as _st_e:
            _st_str = f"ST=err({_st_e})"

    # ── 8. Support & Resistance proximity block ───────────────────────────────
    # If price is AT or NEAR a key resistance → block CALL entry (rejection risk)
    # If price is AT or NEAR a key support    → block PUT entry  (bounce risk)
    #
    # Logic:
    #   - Find swing highs (resistance) and swing lows (support) in last 50 bars
    #   - "Near" = within SR_BLOCK_PCT % of current price
    #   - Only block if the S&R level has been tested 2+ times (confirmed level)
    #   - If price has already broken ABOVE resistance → allow CALL (breakout)
    #   - If price has already broken BELOW support   → allow PUT  (breakdown)
    #
    _sr_str = "SR=off"
    if USE_SR_FILTER and df_15m is not None and len(df_15m) >= 30:
        try:
            cur_close = float(df_15m["close"].iloc[-2])
            cur_high  = float(df_15m["high"].iloc[-2])
            prox      = cur_close * SR_BLOCK_PCT

            # ── Method 1: Previous Day High / Low (PDH / PDL) ────────────────
            # Fetch daily candles to get yesterday's OHLC
            # We derive PDH/PDL from the 5-min data itself (first/last bars of prev day)
            df_15m["date_only"] = pd.to_datetime(df_15m["date"]).dt.date
            _unique_days = sorted(df_15m["date_only"].unique())
            pdh = pdl = pdc = None
            if len(_unique_days) >= 2:
                _prev_day = _unique_days[-2]
                _prev_bars = df_15m[df_15m["date_only"] == _prev_day]
                if len(_prev_bars) > 0:
                    pdh = float(_prev_bars["high"].max())
                    pdl = float(_prev_bars["low"].min())
                    pdc = float(_prev_bars["close"].iloc[-1])

            # ── Method 2: Pivot Points (Standard) ────────────────────────────
            # Calculated once from previous day's H/L/C
            r1 = r2 = s1 = s2 = pivot = None
            if pdh and pdl and pdc:
                pivot = (pdh + pdl + pdc) / 3
                r1    = (2 * pivot) - pdl
                r2    = pivot + (pdh - pdl)
                s1    = (2 * pivot) - pdh
                s2    = pivot - (pdh - pdl)

            # ── Method 3: Round Number / Psychological Levels ─────────────────
            # Nifty: 50-pt round numbers (24000, 24050, 24100...)
            # BankNifty/SENSEX: 100-pt round numbers
            if instrument in ("BANKNIFTY", "SENSEX"):
                _round_step = 100
            elif instrument == "CRUDE":
                _round_step = 50
            else:
                _round_step = 50   # Nifty 50-pt strikes

            _nearest_round_below = (cur_close // _round_step) * _round_step
            _nearest_round_above = _nearest_round_below + _round_step

            # ── Build complete resistance and support level lists ──────────────
            resistance_levels = []
            support_levels    = []

            # PDH → resistance, PDL → support
            if SR_USE_PDH_PDL:
                if pdh and pdh > cur_close:
                    resistance_levels.append(("PDH", pdh))
                if pdl and pdl < cur_close:
                    support_levels.append(("PDL", pdl))
                if pdc:
                    if pdc > cur_close:
                        resistance_levels.append(("PDC", pdc))
                    elif pdc < cur_close:
                        support_levels.append(("PDC", pdc))

            # Pivot levels
            if SR_USE_PIVOTS:
                for _name, _val in [("R1", r1), ("R2", r2), ("S1", s1),
                                     ("S2", s2), ("Pivot", pivot)]:
                    if _val is None: continue
                    if _val > cur_close:
                        resistance_levels.append((_name, _val))
                    elif _val < cur_close:
                        support_levels.append((_name, _val))

            # Round numbers
            if SR_USE_ROUND:
                if _nearest_round_above > cur_close:
                    resistance_levels.append(("Round", _nearest_round_above))
                if _nearest_round_below < cur_close:
                    support_levels.append(("Round", _nearest_round_below))

            # ── Method 4: Algo SZ/RZ — OI-based institutional levels ─────────
            if SR_USE_ALGO_OI:
                try:
                    _oi_res, _oi_sup = _get_algo_sz_rz(instrument, cur_close)
                    if _oi_res and _oi_res > cur_close:
                        resistance_levels.append(("AlgoRZ", _oi_res))
                        print(f"📊 Algo RZ [{instrument}]: ₹{_oi_res:.0f} (max CALL OI strike)", flush=True)
                    if _oi_sup and _oi_sup < cur_close:
                        support_levels.append(("AlgoSZ", _oi_sup))
                        print(f"📊 Algo SZ [{instrument}]: ₹{_oi_sup:.0f} (max PUT OI strike)", flush=True)
                except Exception as _oi_err:
                    print(f"⚠️ OI levels error: {_oi_err}", flush=True)

            # ── Find nearest levels ───────────────────────────────────────────
            nearest_res = min(resistance_levels, key=lambda x: x[1]) if resistance_levels else None
            nearest_sup = max(support_levels,    key=lambda x: x[1]) if support_levels    else None

            _sr_blocked = False

            # PUT near support → block (unless price already broke below it)
            if signal == "PUT" and nearest_sup:
                _name, _level = nearest_sup
                _pct = SR_ALGO_BLOCK_PCT if "Algo" in _name else SR_BLOCK_PCT
                dist_pct = (cur_close - _level) / cur_close

                # Special Algo SZ logic: if price already BELOW AlgoSZ → broken support → allow PUT
                if "Algo" in _name and cur_close < _level:
                    print(f"✅ Price broke below {_name} ₹{_level:.0f} — PUT allowed (broken support)", flush=True)
                elif dist_pct <= _pct:
                    _sr_blocked = True
                    reason = (
                        f"🧱 SR filter: PUT blocked — price ₹{cur_close:.1f} within "
                        f"{dist_pct*100:.2f}% of {_name} support ₹{_level:.1f} — bounce risk"
                    )
                    print(f"🚫 SR BLOCK: {reason}", flush=True)
                    return False, reason

            # CALL near resistance → block (unless price already broke above it)
            if signal == "CALL" and nearest_res:
                _name, _level = nearest_res
                _pct = SR_ALGO_BLOCK_PCT if "Algo" in _name else SR_BLOCK_PCT
                dist_pct = (_level - cur_close) / cur_close

                # Special Algo RZ logic: if price already ABOVE AlgoRZ → broken resistance → allow CALL
                if "Algo" in _name and cur_close > _level:
                    print(f"✅ Price broke above {_name} ₹{_level:.0f} — CALL allowed (broken resistance)", flush=True)
                elif dist_pct <= _pct:
                    _sr_blocked = True
                    reason = (
                        f"🧱 SR filter: CALL blocked — price ₹{cur_close:.1f} within "
                        f"{dist_pct*100:.2f}% of {_name} resistance ₹{_level:.1f} — rejection risk"
                    )
                    print(f"🚫 SR BLOCK: {reason}", flush=True)
                    return False, reason

            if not _sr_blocked:
                _r = f"{nearest_res[0]}=₹{nearest_res[1]:.0f}" if nearest_res else "none"
                _s = f"{nearest_sup[0]}=₹{nearest_sup[1]:.0f}" if nearest_sup else "none"
                _sr_str = f"SR=clear(res:{_r},sup:{_s})"

        except Exception as _sr_e:
            _sr_str = f"SR=err({_sr_e})"

    # ── Candle Pattern Confirmation ───────────────────────────────────────────
    # Analyses the last closed candle (iloc[-2]) for pattern confirmation.
    # Patterns that CONFIRM the signal → boost confidence
    # Patterns that CONTRADICT the signal → block entry
    USE_CANDLE_PATTERN_FILTER = os.environ.get("USE_CANDLE_FILTER", "true").lower() == "true"
    _candle_str = "CP=off"

    # Skip candle pattern on flip re-entry — the candle that caused the flip
    # is always in the OLD direction, not the new one. Checking it would
    # always block the re-entry incorrectly.
    _is_flip = kwargs.get("is_flip_reentry", False)
    if _is_flip:
        _candle_str = "CP=skip(flip-reentry)"
    elif USE_CANDLE_PATTERN_FILTER and df_15m is not None and len(df_15m) >= 4:
        try:
            _bar  = df_15m.iloc[-2]   # last closed candle
            _prev = df_15m.iloc[-3]   # candle before that

            _o = float(_bar["open"])
            _h = float(_bar["high"])
            _l = float(_bar["low"])
            _c = float(_bar["close"])

            _range  = _h - _l
            _body   = abs(_c - _o)
            _upper  = _h - max(_c, _o)
            _lower  = min(_c, _o) - _l

            _is_green = _c > _o
            _is_red   = _c < _o

            # Classify pattern
            _pattern = "NEUTRAL"
            _pattern_bias = None   # "BULLISH" or "BEARISH"

            if _range > 0:
                _body_pct  = _body  / _range
                _upper_pct = _upper / _range
                _lower_pct = _lower / _range

                # ── Bearish patterns ──────────────────────────────────────────
                # Shooting Star: small body at bottom, long upper wick
                if _upper_pct > 0.55 and _body_pct < 0.3 and _is_red:
                    _pattern = "ShootingStar"
                    _pattern_bias = "BEARISH"

                # Bearish Marubozu: big red body, tiny wicks
                elif _is_red and _body_pct > 0.80:
                    _pattern = "BearishMarubozu"
                    _pattern_bias = "BEARISH"

                # Bearish Engulfing: red body engulfs previous green
                elif (_is_red and float(_prev["close"]) > float(_prev["open"])
                      and _o > float(_prev["close"])
                      and _c < float(_prev["open"])):
                    _pattern = "BearishEngulfing"
                    _pattern_bias = "BEARISH"

                # ── Bullish patterns ──────────────────────────────────────────
                # Hammer: small body at top, long lower wick
                elif _lower_pct > 0.55 and _body_pct < 0.3 and _is_green:
                    _pattern = "Hammer"
                    _pattern_bias = "BULLISH"

                # Bullish Marubozu: big green body, tiny wicks
                elif _is_green and _body_pct > 0.80:
                    _pattern = "BullishMarubozu"
                    _pattern_bias = "BULLISH"

                # Bullish Engulfing: green body engulfs previous red
                elif (_is_green and float(_prev["close"]) < float(_prev["open"])
                      and _o < float(_prev["close"])
                      and _c > float(_prev["open"])):
                    _pattern = "BullishEngulfing"
                    _pattern_bias = "BULLISH"

                # Doji: open ≈ close, body very small
                elif _body_pct < 0.1:
                    _pattern = "Doji"
                    _pattern_bias = None   # neutral — no block

                # Inside Bar: current H/L inside previous H/L
                elif (_h <= float(_prev["high"]) and _l >= float(_prev["low"])):
                    _pattern = "InsideBar"
                    _pattern_bias = None   # continuation — no block

            # ── Decision ─────────────────────────────────────────────────────
            if _pattern_bias is not None:
                _signal_bias = "BULLISH" if signal == "CALL" else "BEARISH"

                if _pattern_bias == _signal_bias:
                    # Pattern CONFIRMS signal → good entry
                    _candle_str = f"CP=✅{_pattern}({_pattern_bias})"
                    print(f"🕯️ Candle pattern CONFIRMS {signal}: {_pattern}", flush=True)
                else:
                    # Pattern CONTRADICTS signal → block
                    _candle_str = f"CP=❌{_pattern}({_pattern_bias}vs{signal})"
                    reason = (
                        f"🕯️ Candle pattern contradicts signal — "
                        f"{_pattern} ({_pattern_bias}) vs {signal} entry"
                    )
                    print(f"🚫 CANDLE BLOCK: {reason}", flush=True)
                    return False, reason
            else:
                _candle_str = f"CP={_pattern}"

        except Exception as _cp_e:
            _candle_str = f"CP=err({_cp_e})"
    _claude_str = "Claude=off"
    if not USE_CLAUDE_FILTER:
        _claude_str = "Claude=off(set USE_CLAUDE_FILTER=true in Railway)"
    elif not os.environ.get("ANTHROPIC_API_KEY", ""):
        _claude_str = "Claude=no-key"
    elif df_15m is not None:
        try:
            _ht_df = kwargs.get("ht_df")
            _band  = kwargs.get("hull_band_pct", 0)
            if _ht_df is None:
                _claude_str = "Claude=no-ht-df"
            else:
                _ok, _reason, _conf = claude_trade_filter(
                    signal, instrument, df_15m, _ht_df, _band)
                _claude_str = f"Claude={_conf}%"
                if not _ok:
                    return False, f"🤖 Signal filter: {_reason} (confidence={_conf}%)"
        except Exception as _ce:
            _claude_str = f"Claude=err({_ce})"

    return True, f"✅ Filters passed — {_adx_str} | {_ema_str} | {_mtf_str} | {_vix_str} | {_ml_str} | {_hull_str} | {_st_str} | {_sr_str} | {_candle_str} | {_claude_str}"



# ──────────────────────────────────────────────────────────────────────────────
# 🌊  HULL SUITE  —  exact Python port of InSilico's Pine Script v4 indicator
# ──────────────────────────────────────────────────────────────────────────────
#
# Pine source:  "Hull Suite by InSilico"
# Logic:
#   HMA  = WMA(2 × WMA(src, n/2) − WMA(src, n),  round(√n))
#   EHMA = EMA(2 × EMA(src, n/2) − EMA(src, n),  round(√n))
#   THMA = WMA(3×WMA(src,n/3) − WMA(src,n/2) − WMA(src,n),  n)
#
#   Color rule (Pine):  HULL > HULL[2]  →  green (bullish)
#                       HULL < HULL[2]  →  red   (bearish)
#   MHULL = HULL[0]  (current bar)
#   SHULL = HULL[2]  (2 bars ago — used for crossover detection)
#
#   Signal for bot:
#     CALL  when MHULL > SHULL  (green band)
#     PUT   when MHULL < SHULL  (red band)
#
# Parameters (matching Pine defaults used in the image):
#   mode   = "Hma"   (default)
#   length = 55      (swing entry default in Pine)
# ──────────────────────────────────────────────────────────────────────────────

USE_HULL_FILTER  = os.environ.get("USE_HULL_FILTER", "true").lower() == "true"
HULL_MODE        = "Hma"  # "Hma" | "Ehma" | "Thma"

# ── SuperTrend Filter ──────────────────────────────────────────────────────────
USE_SUPERTREND_FILTER = os.environ.get("USE_SUPERTREND_FILTER", "false").lower() == "true"
ST_PERIOD             = int(os.environ.get("ST_PERIOD",     "10"))   # ATR period
ST_MULTIPLIER         = float(os.environ.get("ST_MULTIPLIER", "3.0")) # band multiplier
HULL_LENGTH      = 55     # Pine default for swing entry

# Minimum band width as % of price.
# Band width = abs(MHULL - SHULL) / price
# When band is thin → trend is weak / transitioning → ignore signal.
# 0.001 = 0.1% of price  (e.g. on Nifty 23000 → min gap of 23 pts)
# 0.002 = 0.2% of price  (e.g. on Nifty 23000 → min gap of 46 pts) ← recommended
# Set to 0.0 to disable the width check.
HULL_MIN_BAND_WIDTH_PCT  = 0.0003   # 0.03% — minimum band width to confirm trend
                                     # On 15-min candles: ~7pts on Nifty 23000
                                     # Thin band = sideways market = skip entry
USE_HULL_BAND_FILTER = os.environ.get("USE_HULL_BAND_FILTER", "true").lower() == "true"  # enable/disable Hull band width check independently
HULL_MORNING_BYPASS_MINS = 75

# ── First 5-min candle range filter ──────────────────────────────────────────
# Strategy: The first 5-min candle of the day (9:15–9:20 AM) sets the
# opening range. If subsequent candles stay INSIDE this range, the market
# is in a balance/inside-day mode — no directional bias → skip all entries.
# Only enter when price BREAKS OUT of the first candle's high or low.
#
# USE_FIRST_CANDLE_FILTER = True  → active
# FIRST_CANDLE_BUFFER_PCT  = small buffer to avoid false breakout triggers
#   e.g. 0.001 = 0.1% → on Nifty 24000, buffer = 24 pts above/below first candle
USE_FIRST_CANDLE_FILTER  = True
FIRST_CANDLE_BUFFER_PCT  = 0.0   # No buffer — any close outside first candle range = breakout

# Per-instrument first candle cache — reset daily
_fc_breakout_done: dict = {}       # { "NIFTY_2026-05-14": "PUT" } — once broken, always broken
_first_candle_cache: dict = {}     # { "NIFTY_2026-05-14": {high, low, time} }
_first_candle_alert_sent: dict = {} # throttle — timestamp of last alert per instrument


def get_first_candle(df, instrument):
    """
    Returns the high and low of the FIRST 5-min candle (9:15–9:20 AM).
    Always uses 5-min candles regardless of main loop timeframe.
    Returns (high, low, candle_time) or (None, None, None) if not available.
    """
    global _first_candle_cache

    try:
        today     = datetime.now(IST).date()
        cache_key = f"{instrument}_{today}"

        if cache_key in _first_candle_cache:
            c = _first_candle_cache[cache_key]
            return c["high"], c["low"], c["time"]

        # Always fetch 5-min data for FC — regardless of main loop timeframe
        _token_map = {
            "NIFTY":     config.NIFTY_TOKEN,
            "BANKNIFTY": BANKNIFTY_TOKEN,
            "FINNIFTY":  FINNIFTY_TOKEN,
            "SENSEX":    SENSEX_TOKEN,
            "CRUDE":     CRUDE_TOKEN,
        }
        _token = _token_map.get(instrument)
        if _token:
            df_5m = get_cached_data(_token, "5minute", 30)
            if df_5m is not None and len(df_5m) >= 2:
                df = df_5m   # use 5-min data for FC

        if df is None or len(df) < 2:
            return None, None, None

        df_copy      = df.copy()
        df_copy["_dt"] = pd.to_datetime(df_copy["date"])
        if df_copy["_dt"].dt.tz is None:
            df_copy["_dt"] = df_copy["_dt"].dt.tz_localize(IST)
        else:
            df_copy["_dt"] = df_copy["_dt"].dt.tz_convert(IST)

        # Get today's bars
        today_bars = df_copy[df_copy["_dt"].dt.date == today]
        if len(today_bars) == 0:
            return None, None, None

        first_bar = today_bars.iloc[0]
        bar_time  = first_bar["_dt"]

        # Must be the 9:15 AM 5-min candle
        if bar_time.hour != 9 or bar_time.minute != 15:
            return None, None, None

        fc_high = float(first_bar["high"])
        fc_low  = float(first_bar["low"])

        _first_candle_cache[cache_key] = {
            "high": fc_high,
            "low":  fc_low,
            "time": bar_time
        }
        print(f"📊 First 5-min candle [{instrument}] 09:15-09:20 → "
              f"High=₹{fc_high:.1f}  Low=₹{fc_low:.1f}  "
              f"Range={fc_high-fc_low:.1f} pts", flush=True)
        return fc_high, fc_low, bar_time

    except Exception as e:
        print(f"⚠️ get_first_candle({instrument}) error: {e}", flush=True)
        return None, None, None


def check_first_candle_range(signal, df, instrument):
    """
    Returns (passed: bool, reason: str)

    Once price breaks the first candle range in ANY direction,
    the filter is permanently disabled for that instrument for the rest of the day.
    No re-checking — one breakout = always unlocked.
    """
    global _first_candle_alert_sent, _fc_breakout_done

    if not USE_FIRST_CANDLE_FILTER:
        return True, "FC=off"

    try:
        _now_ist = datetime.now(IST)
        _mins_since_open = (_now_ist.hour - 9) * 60 + _now_ist.minute - 15
        if _mins_since_open > 120:
            return True, f"FC=expired(market open {_mins_since_open} min ago)"

        today_str  = str(_now_ist.date())
        _break_key = f"{instrument}_{today_str}"

        # ── Already broke out today — check if direction updated ────────────
        _broke = _fc_breakout_done.get(_break_key)
        if _broke:
            fc_high, fc_low, fc_time = get_first_candle(df, instrument)
            if fc_high is not None and fc_low is not None:
                cur_close = float(df["close"].iloc[-2])
                buffer    = cur_close * FIRST_CANDLE_BUFFER_PCT

                if _broke == "DOWN" and cur_close >= fc_high + buffer:
                    # Price broke back ABOVE FC high → update to UP
                    _fc_breakout_done[_break_key] = "UP"
                    _broke = "UP"
                    print(f"🔄 FC direction updated: DOWN→UP [{instrument}]", flush=True)

                elif _broke == "UP" and cur_close <= fc_low - buffer:
                    # Price broke back BELOW FC low → update to DOWN
                    _fc_breakout_done[_break_key] = "DOWN"
                    _broke = "DOWN"
                    print(f"🔄 FC direction updated: UP→DOWN [{instrument}]", flush=True)

                elif _broke == "DOWN" and cur_close > fc_low:
                    # Price recovered back INSIDE FC range — allow both directions
                    print(f"🔄 FC: price back inside range [{instrument}] — allowing both directions", flush=True)
                    _fc_breakout_done.pop(_break_key, None)
                    return True, f"FC=recovered(price ₹{cur_close:.0f} back inside range)"

                elif _broke == "UP" and cur_close < fc_high:
                    # Price fell back INSIDE FC range — allow both directions
                    print(f"🔄 FC: price back inside range [{instrument}] — allowing both directions", flush=True)
                    _fc_breakout_done.pop(_break_key, None)
                    return True, f"FC=recovered(price ₹{cur_close:.0f} back inside range)"

            if (_broke == "UP"   and signal == "CALL") or \
               (_broke == "DOWN" and signal == "PUT"):
                return True, f"FC=unlocked(broke {_broke} earlier today)"
            else:
                return False, (f"FC=broke {_broke} but signal is {signal} — "
                               f"direction mismatch")

        fc_high, fc_low, fc_time = get_first_candle(df, instrument)

        if fc_high is None or fc_low is None:
            return True, "FC=N/A"

        cur_close = float(df["close"].iloc[-2])
        buffer    = cur_close * FIRST_CANDLE_BUFFER_PCT

        breakout_high = fc_high + buffer
        breakout_low  = fc_low  - buffer

        # Breakout detected — lock permanently for today regardless of direction
        if cur_close >= breakout_high:
            _fc_breakout_done[_break_key] = "UP"
            print(f"🔓 FC BREAKOUT [{instrument}] ↑ — permanently unlocked for today", flush=True)
            if signal == "CALL":
                return True, f"FC=breakout↑ close=₹{cur_close:.1f} above FC_high=₹{fc_high:.1f}"
            else:
                return False, f"FC=broke UP but signal is PUT — waiting for HT to confirm"

        if cur_close <= breakout_low:
            _fc_breakout_done[_break_key] = "DOWN"
            print(f"🔓 FC BREAKOUT [{instrument}] ↓ — permanently unlocked for today", flush=True)
            if signal == "PUT":
                return True, f"FC=breakout↓ close=₹{cur_close:.1f} below FC_low=₹{fc_low:.1f}"
            else:
                return False, f"FC=broke DOWN but signal is CALL — waiting for HT to confirm"

        # Price still inside first candle range → rangebound
        alert_key     = f"{instrument}_{today_str}_range"
        _last_sent_ts = _first_candle_alert_sent.get(alert_key, 0)
        _thirty_mins  = 30 * 60
        if time.time() - _last_sent_ts >= _thirty_mins:
            _first_candle_alert_sent[alert_key] = time.time()
            msg = (
                f"📦 {instrument}: market inside opening range\n"
                f"Opening range: High=₹{fc_high:.1f}  Low=₹{fc_low:.1f}  "
                f"Range={fc_high-fc_low:.1f} pts\n"
                f"Current close: ₹{cur_close:.1f}\n"
                f"No orders until price breaks {'above ₹' + str(round(breakout_high,1)) if signal=='CALL' else 'below ₹' + str(round(breakout_low,1))}"
            )
            print(f"🚫 FC RANGE BLOCK [{instrument}]: {msg}", flush=True)
            try:
                send_message(msg)
            except Exception:
                pass

        return False, (
            f"📦 FC range block — close=₹{cur_close:.1f} inside "
            f"[₹{fc_low:.1f} – ₹{fc_high:.1f}] "
            f"(breakout needs close {'above' if signal=='CALL' else 'below'} "
            f"₹{breakout_high if signal=='CALL' else breakout_low:.1f})"
        )

    except Exception as e:
        print(f"⚠️ check_first_candle_range error: {e}", flush=True)
        return True, f"FC=err({e})"

# ── Volume spike config ───────────────────────────────────────────────────────
# When HalfTrend flips AND the flip candle has unusually high volume,
# skip the Hull colour check and enter on HalfTrend alone.
# Logic: volume_spike = current_volume > VOLUME_SPIKE_MULTIPLIER × avg_volume(lookback)
# Set VOLUME_SPIKE_MULTIPLIER = 0.0 to disable volume override entirely.
VOLUME_SPIKE_MULTIPLIER = 2.0   # current bar volume must be 2× the 20-bar average
VOLUME_SPIKE_LOOKBACK   = 20    # bars used to compute average volume


def is_volume_spike(df, multiplier=VOLUME_SPIKE_MULTIPLIER,
                    lookback=VOLUME_SPIKE_LOOKBACK):
    """
    Returns (is_spike: bool, current_vol: float, avg_vol: float, ratio: float)

    Checks the last CLOSED candle (iloc[-2]) volume against the
    rolling average of the prior `lookback` candles (iloc[-lookback-2:-2]).

    A spike means: current_volume >= multiplier × avg_volume
    """
    try:
        if df is None or len(df) < lookback + 3:
            return False, 0.0, 0.0, 0.0

        if "volume" not in df.columns:
            return False, 0.0, 0.0, 0.0

        current_vol = float(df["volume"].iloc[-2])
        avg_vol     = float(df["volume"].iloc[-lookback-2:-2].mean())

        if avg_vol <= 0:
            return False, current_vol, avg_vol, 0.0

        ratio    = current_vol / avg_vol
        is_spike = ratio >= multiplier

        return is_spike, current_vol, avg_vol, round(ratio, 2)

    except Exception as e:
        print(f"⚠️ Volume spike check error: {e}")
        return False, 0.0, 0.0, 0.0


def _wma(series: pd.Series, period: int) -> pd.Series:
    """Weighted Moving Average — matches Pine's wma()."""
    weights = np.arange(1, period + 1, dtype=float)
    return series.rolling(period).apply(
        lambda x: np.dot(x, weights) / weights.sum(), raw=True
    )


def supertrend(df, period=10, multiplier=3.0):
    """
    SuperTrend indicator — matches TradingView Pine Script output.
    direction:  1 = bullish (price above ST → CALL)
               -1 = bearish (price below ST → PUT)
    """
    try:
        high  = df["high"].astype(float).values
        low   = df["low"].astype(float).values
        close = df["close"].astype(float).values
        n     = len(df)

        if n < period + 2:
            return None

        # True Range
        tr = np.zeros(n)
        for i in range(1, n):
            tr[i] = max(high[i] - low[i],
                        abs(high[i] - close[i-1]),
                        abs(low[i]  - close[i-1]))

        # ATR via Wilder's RMA (same as Pine's ta.rma)
        atr = np.zeros(n)
        atr[period] = np.mean(tr[1:period+1])
        for i in range(period+1, n):
            atr[i] = (atr[i-1] * (period - 1) + tr[i]) / period

        # Basic bands
        hl2         = (high + low) / 2.0
        basic_upper = hl2 + multiplier * atr
        basic_lower = hl2 - multiplier * atr

        # Final bands + SuperTrend line
        final_upper = basic_upper.copy()
        final_lower = basic_lower.copy()
        st          = np.zeros(n)
        direction   = np.ones(n, dtype=int)   # 1=bullish, -1=bearish

        for i in range(1, n):
            # Upper band stays high unless new lower high appears
            final_upper[i] = (basic_upper[i]
                              if basic_upper[i] < final_upper[i-1] or close[i-1] > final_upper[i-1]
                              else final_upper[i-1])
            # Lower band stays low unless new higher low appears
            final_lower[i] = (basic_lower[i]
                              if basic_lower[i] > final_lower[i-1] or close[i-1] < final_lower[i-1]
                              else final_lower[i-1])

            # Direction: 1=bullish (price above ST), -1=bearish (price below ST)
            if direction[i-1] == -1:
                direction[i] = 1  if close[i] > final_upper[i] else -1
            else:
                direction[i] = -1 if close[i] < final_lower[i] else  1

            # ST line: lower band when bullish, upper band when bearish
            st[i] = final_lower[i] if direction[i] == 1 else final_upper[i]

        result = df.copy()
        result["st"]           = st
        result["st_direction"] = direction
        return result

    except Exception as e:
        print(f"⚠️ SuperTrend error: {e}", flush=True)
        return None


def get_supertrend_signal(df, period=10, multiplier=3.0):
    """
    Returns (signal, st_value) from last CLOSED candle (anti-repaint).
    signal: 'CALL' (direction=1, bullish), 'PUT' (direction=-1, bearish), None
    """
    try:
        st_df = supertrend(df, period=period, multiplier=multiplier)
        if st_df is None or len(st_df) < period + 2:
            return None, None

        last      = st_df.iloc[-2]   # closed candle
        direction = int(last["st_direction"])
        st_val    = float(last["st"])

        if st_val == 0:
            return None, None   # not yet calculated

        signal = "CALL" if direction == 1 else "PUT"
        return signal, st_val

    except Exception as e:
        print(f"⚠️ get_supertrend_signal error: {e}", flush=True)
        return None, None


def hull_suite(df, mode=HULL_MODE, length=HULL_LENGTH):
    """
    Computes Hull Suite on the given OHLCV DataFrame.

    Returns a DataFrame with extra columns:
        hull      : the Hull MA line value (MHULL = HULL[0])
        hull_2    : HULL shifted 2 bars back (SHULL = HULL[2])
        hull_bull : True when hull > hull_2  (green band — bullish)
        hull_bear : True when hull < hull_2  (red band   — bearish)
        hull_signal: "CALL" | "PUT" | None
    """
    df = df.copy()
    src = df["close"]
    n   = int(length)

    if mode == "Hma":
        # Pine: wma(2 * wma(src, n/2) - wma(src, n), round(sqrt(n)))
        half  = max(1, n // 2)
        sqn   = max(1, round(np.sqrt(n)))
        raw   = 2 * _wma(src, half) - _wma(src, n)
        hull_series = _wma(raw, sqn)

    elif mode == "Ehma":
        # Pine: ema(2 * ema(src, n/2) - ema(src, n), round(sqrt(n)))
        half  = max(1, n // 2)
        sqn   = max(1, round(np.sqrt(n)))
        raw   = 2 * src.ewm(span=half, adjust=False).mean() \
                  - src.ewm(span=n,    adjust=False).mean()
        hull_series = raw.ewm(span=sqn, adjust=False).mean()

    elif mode == "Thma":
        # Pine: wma(wma(src,n/3)*3 - wma(src,n/2) - wma(src,n), n)
        # Note: Pine passes length/2 to Mode() for Thma, so effective n = n//2
        n2    = max(1, n // 2)
        third = max(1, n2 // 3)
        half2 = max(1, n2 // 2)
        raw   = 3 * _wma(src, third) - _wma(src, half2) - _wma(src, n2)
        hull_series = _wma(raw, n2)

    else:
        raise ValueError(f"hull_suite: unknown mode '{mode}'")

    df["hull"]    = hull_series
    df["hull_2"]  = hull_series.shift(2)   # Pine: HULL[2]

    # Color rule: HULL > HULL[2] → green (bullish), else red (bearish)
    df["hull_bull"] = df["hull"] > df["hull_2"]
    df["hull_bear"] = df["hull"] < df["hull_2"]

    df["hull_signal"] = np.where(
        df["hull_bull"], "CALL",
        np.where(df["hull_bear"], "PUT", None)
    )

    return df


def get_hull_signal(df_15m, mode=HULL_MODE, length=HULL_LENGTH):
    """
    Returns the Hull Suite signal on the last CLOSED candle (iloc[-2]).
    Requires Hull colour to be CONSISTENT for at least 2 bars to avoid
    false signals during transitions (like the chart shows — Hull briefly
    flips green during a downtrend before resuming red).

    Returns: (signal, hull_value, hull_2_value, band_width_pct)
    """
    try:
        if df_15m is None or len(df_15m) < length + 5:
            return None, None, None, None

        ht  = hull_suite(df_15m, mode=mode, length=length)

        bar      = ht.iloc[-2]   # last CLOSED candle
        prev_bar = ht.iloc[-3]   # candle before that

        sig   = bar["hull_signal"]
        hval  = round(float(bar["hull"]),   2)
        h2val = round(float(bar["hull_2"]), 2)
        price = round(float(bar["close"]),  2)

        band_width_pct = abs(hval - h2val) / price if price > 0 else 0.0

        # ── Consistency check — both last 2 closed bars must agree ───────────
        # Prevents false colour flip during transition (your chart scenario):
        # Hull briefly turns green at bottom of downtrend → would wrongly block PUT
        prev_sig = prev_bar["hull_signal"]
        if sig != prev_sig:
            # Hull just flipped on this bar — treat as transitioning → return None
            print(f"⚠️ Hull transition detected: prev={prev_sig} → cur={sig} — treating as None", flush=True)
            return None, hval, h2val, round(band_width_pct, 6)

        return sig, hval, h2val, round(band_width_pct, 6)

    except Exception as e:
        print(f"⚠️ Hull Suite error: {e}")
        return None, None, None, None


# ──────────────────────────────────────────────────────────────────────────────
# END HULL SUITE
# ──────────────────────────────────────────────────────────────────────────────


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
    ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
    with open(TRADE_LOG_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([ts, instrument, symbol, signal, entry, exit_price, pnl, probability])


def _log_trade_settings(instrument, signal, pnl):
    """
    Approach 1 — Auto-tuning data collection.
    Logs current indicator settings alongside trade outcome.
    Weekly analysis finds which settings produce best win rate.
    """
    try:
        _settings_file = "trade_settings_log.csv"
        _ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")
        _row = {
            "time":                _ts,
            "instrument":          instrument,
            "signal":              signal,
            "pnl":                 round(pnl, 2),
            "won":                 1 if pnl > 0 else 0,
            "hull_min_band_pct":   HULL_MIN_BAND_WIDTH_PCT,
            "hull_morning_bypass": HULL_MORNING_BYPASS_MINS,
            "fc_buffer_pct":       FIRST_CANDLE_BUFFER_PCT,
            "hull_length":         HULL_LENGTH,
            "hull_mode":           HULL_MODE,
            "profit_lock_min":     1000,
            "hour_of_day":         datetime.now(IST).hour,
            "day_of_week":         datetime.now(IST).weekday(),
        }
        import csv as _csv
        _write_header = not os.path.exists(_settings_file)
        with open(_settings_file, "a", newline="") as _f:
            _w = _csv.DictWriter(_f, fieldnames=list(_row.keys()))
            if _write_header:
                _w.writeheader()
            _w.writerow(_row)
    except Exception as _e:
        print(f"⚠️ _log_trade_settings: {_e}", flush=True)


def auto_tune_parameters():
    """
    Approach 1 — Auto-tuning.
    Reads trade_settings_log.csv, finds which parameter combinations
    produced the best win rate over the last 30 days.
    Called weekly by a background thread.
    Returns dict of recommended settings (does NOT apply them automatically —
    just logs recommendations so you can review and decide).
    """
    try:
        _settings_file = "trade_settings_log.csv"
        if not os.path.exists(_settings_file):
            return

        import csv as _csv
        rows = []
        with open(_settings_file, "r") as _f:
            reader = _csv.DictReader(_f)
            for row in reader:
                rows.append(row)

        if len(rows) < 10:
            print("⚠️ Auto-tune: not enough data yet (need 10+ trades)", flush=True)
            return

        # Filter last 30 days
        _cutoff = (datetime.now(IST) - timedelta(days=30)).strftime("%Y-%m-%d")
        rows = [r for r in rows if r["time"] >= _cutoff]

        if not rows:
            return

        # Analyse by hull_min_band_pct
        from collections import defaultdict
        band_stats = defaultdict(lambda: {"wins": 0, "total": 0, "pnl": 0})
        for r in rows:
            try:
                k = r["hull_min_band_pct"]
                band_stats[k]["total"] += 1
                band_stats[k]["wins"]  += int(r["won"])
                band_stats[k]["pnl"]   += float(r["pnl"])
            except Exception:
                pass

        # Find best band setting
        best_band = max(band_stats.items(),
                        key=lambda x: x[1]["wins"] / max(x[1]["total"], 1))

        # Analyse by hour of day — find worst hours
        hour_stats = defaultdict(lambda: {"wins": 0, "total": 0})
        for r in rows:
            try:
                h = int(r["hour_of_day"])
                hour_stats[h]["total"] += 1
                hour_stats[h]["wins"]  += int(r["won"])
            except Exception:
                pass

        worst_hours = [h for h, s in hour_stats.items()
                       if s["total"] >= 3 and s["wins"] / s["total"] < 0.3]

        total   = len(rows)
        wins    = sum(int(r["won"]) for r in rows)
        avg_pnl = sum(float(r["pnl"]) for r in rows) / max(total, 1)

        report = (
            f"🤖 AUTO-TUNE WEEKLY REPORT\n"
            f"{'='*30}\n"
            f"📊 Trades analysed: {total} (last 30 days)\n"
            f"🏆 Win rate: {wins}/{total} = {wins/max(total,1)*100:.1f}%\n"
            f"💰 Avg P&L per trade: ₹{avg_pnl:.0f}\n"
            f"{'='*30}\n"
            f"⚙️ Best hull_min_band_pct: {best_band[0]} "
            f"(win rate: {best_band[1]['wins']}/{best_band[1]['total']})\n"
            + (f"⏰ Worst hours (avoid): {worst_hours}\n" if worst_hours else "") +
            f"{'='*30}\n"
            f"Current settings:\n"
            f"  HULL_MIN_BAND_WIDTH_PCT = {HULL_MIN_BAND_WIDTH_PCT}\n"
            f"  HULL_MORNING_BYPASS_MINS = {HULL_MORNING_BYPASS_MINS}\n"
            f"  FIRST_CANDLE_BUFFER_PCT = {FIRST_CANDLE_BUFFER_PCT}"
        )
        print(report, flush=True)
        send_message(report)

    except Exception as _e:
        print(f"⚠️ auto_tune_parameters: {_e}", flush=True)


def auto_tune_scheduler():
    """Background thread — runs auto_tune_parameters every Sunday at 6 PM."""
    while True:
        try:
            now = datetime.now(IST)
            # Run every Sunday at 6 PM IST
            if now.weekday() == 6 and now.hour == 18 and now.minute < 5:
                print("🤖 Running weekly auto-tune analysis...", flush=True)
                auto_tune_parameters()
                time.sleep(300)  # avoid running twice in same 5-min window
            time.sleep(60)
        except Exception as _e:
            print(f"⚠️ auto_tune_scheduler: {_e}", flush=True)
            time.sleep(60)


# ──────────────────────────────────────────────────────────────────────────────
# 🤖  APPROACH 2 — CLAUDE API TRADE FILTER
# Before placing any order, ask Claude API if the trade looks good.
# Uses last 5 trades + current indicators as context.
# ──────────────────────────────────────────────────────────────────────────────

USE_CLAUDE_FILTER    = os.environ.get("USE_CLAUDE_FILTER", "false").lower() == "true"
CLAUDE_MIN_CONFIDENCE = 65   # minimum confidence % to allow trade
_claude_filter_cache  = {}   # throttle — one call per instrument per signal per hour
_claude_flip_counter  = {}   # {instrument: count} — increments on each HT flip to bust cache


def _get_recent_trades(instrument, n=5):
    """Read last N trades for instrument from trade_log.csv."""
    try:
        import csv as _csv
        trades = []
        if not os.path.exists(TRADE_LOG_FILE):
            return []
        with open(TRADE_LOG_FILE, "r") as _f:
            reader = _csv.DictReader(_f)
            for row in reader:
                if row.get("instrument", "").upper() == instrument.upper():
                    trades.append(row)
        return trades[-n:] if len(trades) >= n else trades
    except Exception:
        return []


def claude_trade_filter(signal, instrument, df, ht_df, hull_band_pct):
    """
    Approach 2 — Claude API trade filter.
    Sends current market context to Claude and gets a confidence score.
    Returns (allowed: bool, reason: str, confidence: int)

    Requires ANTHROPIC_API_KEY env var and USE_CLAUDE_FILTER=true.
    """
    if not USE_CLAUDE_FILTER:
        return True, "Claude filter disabled", 100

    _api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not _api_key:
        print("⚠️ Signal filter: ANTHROPIC_API_KEY not set — skipping", flush=True)
        return True, "Signal filter: no API key", 100

    # Cache key — one call per instrument per signal per hour per flip
    # Flip counter ensures each HT direction change gets a fresh Claude evaluation
    _flip_n    = _claude_flip_counter.get(instrument, 0)
    _cache_key = f"{instrument}_{signal}_{datetime.now(IST).strftime('%Y-%m-%d_%H')}_{_flip_n}"
    if _claude_filter_cache.get(_cache_key):
        cached = _claude_filter_cache[_cache_key]
        _age = time.time() - cached.get("cached_at", 0)
        if _age < 300:   # 5-min cache
            # Max block duration — if Claude keeps blocking >30 min, bypass
            _first_blocked = cached.get("first_blocked_at", cached.get("cached_at", 0))
            _total_block   = time.time() - _first_blocked
            if _total_block > 900:  # 15 min max
                print(f"🤖 Claude block auto-expired after {_total_block/60:.0f} min — bypassing", flush=True)
                _claude_filter_cache.pop(_cache_key, None)
            else:
                print(f"🤖 Claude cached [{instrument} {signal}]: {cached['confidence']}% "
                      f"(blocked {_age/60:.1f} min, total {_total_block/60:.0f} min) — {cached['reason']}", flush=True)
                return cached["allowed"], cached["reason"], cached["confidence"]
        else:
            _claude_filter_cache.pop(_cache_key, None)
            print(f"🤖 Claude cache expired [{instrument}] — calling fresh", flush=True)

    try:
        # ── Pre-check: consecutive losses → block without calling Claude ─────
        _recent = _get_recent_trades(instrument, n=5)
        if len(_recent) >= 3:
            _last3 = [float(t.get("pnl", 0)) for t in _recent[-3:]]
            if all(p < 0 for p in _last3):
                _msg = f"3 consecutive losses (₹{_last3[-3]:.0f}, ₹{_last3[-2]:.0f}, ₹{_last3[-1]:.0f}) — strategy not working today"
                print(f"🤖 Claude pre-check [{instrument}]: BLOCKED — {_msg}", flush=True)
                send_message(
                    f"🚫 SIGNAL FILTER BLOCKED\n"
                    f"📌 {instrument} {signal}\n"
                    f"📊 Confidence: 0% — pre-check failed\n"
                    f"💭 {_msg}"
                )
                return False, _msg, 0

        # Build context for Claude API call
        _recent = _get_recent_trades(instrument, n=5)
        _trade_summary = []
        for t in _recent:
            _pnl = float(t.get("pnl", 0))
            _trade_summary.append(
                f"  {t.get('time','')[:16]} {t.get('signal','')} → "
                f"₹{_pnl:+.0f} ({'WIN' if _pnl > 0 else 'LOSS'})"
            )

        _cur_close = float(df["close"].iloc[-2]) if df is not None and len(df) > 2 else 0
        _ht_trend  = "BULLISH" if int(ht_df.iloc[-2]["trend"]) == 0 else "BEARISH"
        _now       = datetime.now(IST).strftime("%H:%M")

        # ── DETERMINISTIC RULES (replaces Claude API call) ────────────────────
        # Pure Python — no AI judgment, no hallucinated penalties
        # Rules are exact and predictable every time
        _now_ist = datetime.now(IST)
        _confidence = 75
        _allowed    = True
        _reason     = "All rules pass — signal accepted"

        # RULE 1: 3 consecutive losses → block
        _inst_streak = _loss_streak.get(instrument, 0)
        if _inst_streak >= 3:
            _confidence = 40
            _allowed    = False
            _reason     = f"3 consecutive losses on {instrument} — strategy not working today"

        # RULE 2: After 3:10 PM → block
        elif _now_ist.hour > 15 or (_now_ist.hour == 15 and _now_ist.minute >= 10):
            _confidence = 40
            _allowed    = False
            _reason     = f"After 3:10 PM IST ({_now_ist.strftime('%H:%M')}) — no new entries"

        # RULE 3: Hull band too thin → block
        elif hull_band_pct is not None and hull_band_pct < 0.0002:
            _confidence = 40
            _allowed    = False
            _reason     = f"Hull band {hull_band_pct*100:.3f}% below 0.02% — trend too weak"

        # All rules pass → allow with 75% confidence
        confidence = _confidence
        allowed    = _allowed
        reason     = _reason

        # Only cache BLOCKED results for 5 min — rechecked if conditions improve
        if not allowed:
            _existing  = _claude_filter_cache.get(_cache_key, {})
            _now_ts    = time.time()
            _claude_filter_cache[_cache_key] = {
                "allowed":          allowed,
                "reason":           reason,
                "confidence":       confidence,
                "cached_at":        _now_ts,
                "first_blocked_at": _existing.get("first_blocked_at", _now_ts),
            }

        print(f"🔍 Signal filter [{instrument} {signal}]: "
              f"confidence={confidence}% allowed={allowed} — {reason}", flush=True)

        if not allowed:
            send_message(
                f"🚫 SIGNAL FILTER BLOCKED\n"
                f"📌 {instrument} {signal}\n"
                f"📊 Confidence: {confidence}% (min {CLAUDE_MIN_CONFIDENCE}%)\n"
                f"💭 {reason}"
            )

        return allowed, reason, confidence

    except Exception as _e:
        print(f"⚠️ Claude filter error: {_e} — allowing trade", flush=True)
        return True, f"Claude filter error: {_e}", 100


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
    global _peak_daily_pnl, _profit_lock_floor, _profit_lock_tier

    # 🛑 Portfolio protection FIRST
    if not portfolio_safe():
        return False

    # 🚫 Risk OFF
    if risk_off:
        return False

    # 🛑 Bad day stop
    if daily_pnl < config.MAX_DAILY_LOSS:
        return False

    # ─────────────────────────────────────────────────────────────────────────
    # 🎯 PROGRESSIVE DAILY PROFIT LOCK
    # Tiers:  ₹1000 peak → lock 80%  (floor = ₹800)
    #         ₹2000 peak → lock 85%  (floor = ₹1700)
    #         ₹3000 peak → lock 90%  (floor = ₹2700)
    # The peak and floor only ever move up — never down.
    # ─────────────────────────────────────────────────────────────────────────
    if daily_pnl > _peak_daily_pnl:
        _peak_daily_pnl = daily_pnl   # update running peak

        # Activate / upgrade tier based on new peak
        if _peak_daily_pnl >= 3000 and _profit_lock_tier < 3:
            _profit_lock_tier  = 3
            _profit_lock_floor = round(_peak_daily_pnl * 0.90, 2)
            print(f"🔒 Profit lock TIER-3 (90%): peak=₹{_peak_daily_pnl:.0f}  floor=₹{_profit_lock_floor:.0f}", flush=True)
            send_message(f"🔒 Profit lock activated: 90% of ₹{_peak_daily_pnl:.0f}\nFloor = ₹{_profit_lock_floor:.0f} — trading stops if P&L drops below this")
        elif _peak_daily_pnl >= 2000 and _profit_lock_tier < 2:
            _profit_lock_tier  = 2
            _profit_lock_floor = round(_peak_daily_pnl * 0.85, 2)
            print(f"🔒 Profit lock TIER-2 (85%): peak=₹{_peak_daily_pnl:.0f}  floor=₹{_profit_lock_floor:.0f}", flush=True)
            send_message(f"🔒 Profit lock activated: 85% of ₹{_peak_daily_pnl:.0f}\nFloor = ₹{_profit_lock_floor:.0f} — trading stops if P&L drops below this")
        elif _peak_daily_pnl >= 1000 and _profit_lock_tier < 1:
            _profit_lock_tier  = 1
            _profit_lock_floor = round(_peak_daily_pnl * 0.80, 2)
            print(f"🔒 Profit lock TIER-1 (80%): peak=₹{_peak_daily_pnl:.0f}  floor=₹{_profit_lock_floor:.0f}", flush=True)
            send_message(f"🔒 Profit lock activated: 80% of ₹{_peak_daily_pnl:.0f}\nFloor = ₹{_profit_lock_floor:.0f} — trading stops if P&L drops below this")

        # When peak grows within the same tier, raise the floor proportionally
        elif _profit_lock_tier == 3:
            new_floor = round(_peak_daily_pnl * 0.90, 2)
            if new_floor > _profit_lock_floor:
                _profit_lock_floor = new_floor
                print(f"🔒 Profit lock floor raised → ₹{_profit_lock_floor:.0f} (90% of ₹{_peak_daily_pnl:.0f})", flush=True)
        elif _profit_lock_tier == 2:
            new_floor = round(_peak_daily_pnl * 0.85, 2)
            if new_floor > _profit_lock_floor:
                _profit_lock_floor = new_floor
                print(f"🔒 Profit lock floor raised → ₹{_profit_lock_floor:.0f} (85% of ₹{_peak_daily_pnl:.0f})", flush=True)
        elif _profit_lock_tier == 1:
            new_floor = round(_peak_daily_pnl * 0.80, 2)
            if new_floor > _profit_lock_floor:
                _profit_lock_floor = new_floor
                print(f"🔒 Profit lock floor raised → ₹{_profit_lock_floor:.0f} (80% of ₹{_peak_daily_pnl:.0f})", flush=True)

    # Enforce the floor — stop new entries if P&L has given back too much
    if _profit_lock_tier > 0 and daily_pnl < _profit_lock_floor:
        _pct = {1: "80%", 2: "85%", 3: "90%"}.get(_profit_lock_tier, "?")
        print(f"🔒 Profit lock HIT: daily_pnl=₹{daily_pnl:.0f} < floor=₹{_profit_lock_floor:.0f} ({_pct} of peak ₹{_peak_daily_pnl:.0f}) — no new entries", flush=True)
        return False

    # 🚫 Max trades
    if trade_count >= config.MAX_TRADES:
        return False

    # ⏳ Cooldown after loss
    if last_loss_time and time.time() - last_loss_time < config.COOLDOWN_AFTER_LOSS:
        return False

    # 🚫 Losing streak control — do NOT sleep here; let the loop handle the pause
    _inst_streak = _loss_streak.get(instrument, loss_streak)
    if _inst_streak >= 3:
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
    global _blocked_strikes
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
        lot_size = NIFTY_LOT_SIZE
    elif instrument == "BANKNIFTY":
        exchange = "NFO"
        name = "BANKNIFTY"
        step = 100
        token = BANKNIFTY_TOKEN
        token_symbol = "NSE:NIFTY BANK"
        lot_size = 30
    elif instrument == "FINNIFTY":
        exchange = "NFO"
        name = "FINNIFTY"
        step = 50                # FINNIFTY strikes move in ₹50 steps
        token = FINNIFTY_TOKEN
        token_symbol = "NSE:NIFTY FIN SERVICE"
        lot_size = 60            # FINNIFTY lot size = 60
    elif instrument == "SENSEX":
        exchange = "BFO"         # BSE F&O exchange
        name = "SENSEX"
        step = 100               # SENSEX strikes move in ₹100 steps
        token = SENSEX_TOKEN
        token_symbol = "BSE:SENSEX"
        lot_size = SENSEX_LOT_SIZE
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
        # BankNifty lot = 30. Strikes move in ₹100 steps.
        if balance <= 5000:
            strike_shift = 2
            max_price = 120
        elif balance <= 10000:
            strike_shift = 1
            max_price = 200
        elif balance <= 20000:
            strike_shift = 1
            max_price = 300
        elif balance <= 35000:
            strike_shift = 1
            max_price = 450
        else:
            strike_shift = 1
            max_price = 600
    elif instrument == "FINNIFTY":
        # FINNIFTY lot = 60. Strikes move in ₹50 steps.
        # FINNIFTY ~23,000. ATM option ~₹50-300. Lot value = premium × 60.
        if balance <= 5000:
            strike_shift = 2       # 100 OTM (2 × ₹50 steps)
            max_price = 60         # ₹60 × 40 = ₹2,400 per lot
        elif balance <= 10000:
            strike_shift = 2
            max_price = 100        # ₹100 × 40 = ₹4,000 per lot
        elif balance <= 20000:
            strike_shift = 1
            max_price = 150        # ₹150 × 40 = ₹6,000 per lot
        elif balance <= 35000:
            strike_shift = 1
            max_price = 220        # ₹220 × 40 = ₹8,800 per lot
        else:
            strike_shift = 1
            max_price = 300        # ₹300 × 40 = ₹12,000 per lot
    elif instrument == "SENSEX":
        # SENSEX lot = 20. Strikes move in ₹100 steps.
        # SENSEX ~74,000. 200pt OTM option costs ~₹150-250. Lot cost = premium × 20.
        if balance <= 2500:
            strike_shift = 4       # 400 OTM — cheaper deep OTM
            max_price = 80         # ₹80 × 20 = ₹1,600 per lot
        elif balance <= 5000:
            strike_shift = 3       # 300 OTM
            max_price = 150        # ₹150 × 20 = ₹3,000 per lot
        elif balance <= 10000:
            strike_shift = 2       # 200 OTM
            max_price = 220        # ₹220 × 20 = ₹4,400 per lot
        elif balance <= 20000:
            strike_shift = 1       # 100 OTM
            max_price = 350        # ₹350 × 20 = ₹7,000 per lot
        elif balance <= 35000:
            strike_shift = 1
            max_price = 500        # ₹500 × 20 = ₹10,000 per lot
        else:
            strike_shift = 1
            max_price = 700        # ₹700 × 20 = ₹14,000 per lot
    else:
        # NIFTY lot = 65. Strikes move in ₹50 steps.
        # Minimum viable balance: ₹10 premium × 65 = ₹650
        if balance <= 1500:
            strike_shift = 5      # 250 OTM — very deep, cheapest options
            max_price = 15        # ₹15 × 65 = ₹975 per lot
        elif balance <= 3000:
            strike_shift = 4      # 200 OTM
            max_price = 30        # ₹30 × 65 = ₹1,950 per lot
        elif balance <= 5000:
            strike_shift = 3      # 150 OTM
            max_price = 60        # ₹60 × 65 = ₹3,900 per lot
        elif balance <= 10000:
            strike_shift = 2      # 100 OTM
            max_price = 100       # ₹100 × 65 = ₹6,500 per lot
        elif balance <= 20000:
            strike_shift = 2      # 100 OTM
            max_price = 130       # ₹130 × 65 = ₹8,450 per lot
        elif balance <= 35000:
            strike_shift = 1      # 50 OTM
            max_price = 200       # ₹200 × 65 = ₹13,000 per lot
        elif balance <= 50000:
            strike_shift = 1
            max_price = 280       # ₹280 × 65 = ₹18,200 per lot
        else:
            strike_shift = 1
            max_price = 400       # ₹400 × 65 = ₹26,000 per lot

    # ── Minimum balance check ─────────────────────────────────────────────────
    min_balance_map = {
        "NIFTY": 650,      # ₹10 × 65 = ₹650 minimum
        "BANKNIFTY": 600,  # ₹20 × 30 = ₹600 minimum
        "FINNIFTY": 600,   # ₹10 × 60 = ₹600 minimum
        "SENSEX": 400,     # ₹20 × 20 = ₹400 minimum
        "CRUDE": 1000,     # ₹10 × 100 = ₹1000 minimum
    }
    _min_bal = min_balance_map.get(instrument, 500)
    if balance < _min_bal:
        msg = (f"⚠️ Balance ₹{balance:.0f} too low to trade {instrument} "
               f"(minimum ₹{_min_bal}) — skipping")
        print(msg, flush=True)
        send_message(f"💸 INSUFFICIENT BALANCE\n"
                     f"📌 {instrument}: ₹{balance:.0f} < min ₹{_min_bal}\n"
                     f"💡 Add funds to trade {instrument}")
        return None, None, None, None
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
        tradingsym = i['tradingsymbol']

        # ── Block strikes stopped out by max-loss today ───────────────────────
        if tradingsym in _blocked_strikes.get(instrument, set()):
            print(f"   🚫 Strike blocked (max-loss exit today): {tradingsym}", flush=True)
            continue

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

        # Override with fixed lot count from Railway Variable if set (>0)
        _fixed = {"NIFTY": NIFTY_NUM_LOTS, "BANKNIFTY": BANKNIFTY_NUM_LOTS,
                  "FINNIFTY": FINNIFTY_NUM_LOTS, "SENSEX": SENSEX_NUM_LOTS,
                  "CRUDE": CRUDE_NUM_LOTS}.get(instrument, 0)
        if _fixed > 0:
            lot = _fixed
            print(f"📊 Fixed lots: {lot} [{instrument}] (Railway Variable)", flush=True)

        return best["symbol"], best["price"], lot, exchange

    # =====================================
    # =====================================
    # FALLBACK SEARCH
    # =====================================
    print("⚠️ No ideal candidate — fallback")

    # Get live balance for actual affordability check
    try:
        _margin = kite.margins()
        _seg    = _margin.get("equity", {}).get("available", {})
        _live   = float(_seg.get("live_balance", 0) or 0)
    except Exception:
        _live = balance

    fallback = []

    for i in opts_sorted[:20]:
        try:
            strike = int(i["strike"])
        except:
            continue

        sym = f"{exchange}:{i['tradingsymbol']}"

        # Block max-loss strikes in fallback too
        if i['tradingsymbol'] in _blocked_strikes.get(instrument, set()):
            continue

        p = safe_ltp(sym)

        if p is None or p <= 0:
            continue

        # Fallback: strict max_price cap + actual affordability check
        _lot_size = {"SENSEX": SENSEX_LOT_SIZE, "BANKNIFTY": BANKNIFTY_LOT_SIZE, "FINNIFTY": FINNIFTY_LOT_SIZE, "CRUDE": CRUDE_LOT_SIZE}.get(instrument, NIFTY_LOT_SIZE)
        _cost = p * _lot_size * 1.05   # 5% buffer
        _affordable = _cost <= _live   # must fit in live balance

        if min_price <= p <= max_price and _affordable:
            # Liquidity check
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

        # Override with fixed lot count from Railway Variable if set (>0)
        _fixed = {"NIFTY": NIFTY_NUM_LOTS, "BANKNIFTY": BANKNIFTY_NUM_LOTS,
                  "FINNIFTY": FINNIFTY_NUM_LOTS, "SENSEX": SENSEX_NUM_LOTS,
                  "CRUDE": CRUDE_NUM_LOTS}.get(instrument, 0)
        if _fixed > 0:
            lot = _fixed
            print(f"📊 Fixed lots: {lot} [{instrument}] (Railway Variable fallback)", flush=True)

        return best["symbol"], best["price"], lot, exchange

    print("❌ No valid option found")
    return None, None, None, None    



# -----------------------------
# ORDER
# -----------------------------

def place_order(symbol, qty, exchange, instrument):

    print(f"🚀 PLACE ORDER: {symbol}, lot: {qty}, exchange: {exchange}")
    
    now = datetime.now(IST)

    if exchange in ("NFO", "BFO") and not (
        (now.hour == 9 and now.minute >= 20) or   # no orders before 9:20 AM
        (9 < now.hour < 15) or
        (now.hour == 15 and now.minute < 20)
    ):
        print("🚫 Market closed or before 9:20 AM — skipping order")
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

        # ✅ Pass instrument so SENSEX gets lot_size=20, not 1
        quantity = get_quantity(qty, exchange, instrument)

        # ✅ LIVE BALANCE SUFFICIENCY CHECK — block order if balance too low
        # Alert is rate-limited to once per 30 min per instrument to avoid Telegram spam.
        try:
            live_balance = get_balance(instrument)
            total_cost   = price * quantity          # total ₹ this order will deploy
            min_required = total_cost * 1.02         # 2% buffer for margin/charges

            print(f"💰 Balance check → Available: ₹{live_balance:,.0f}  |  Order cost: ₹{total_cost:,.0f}  |  Required (with buffer): ₹{min_required:,.0f}")

            if live_balance < min_required:
                print(f"🚫 Insufficient balance: need ₹{min_required:,.0f}, have ₹{live_balance:,.0f}")
                _now_ts = time.time()
                _last_alert = _insufficient_balance_alerted.get(instrument, 0)
                if _now_ts - _last_alert > 1800:   # alert at most once per 30 min
                    send_message(
                        f"🚫 Insufficient balance for {symbol}\n"
                        f"Need ₹{min_required:,.0f}, have ₹{live_balance:,.0f}\n"
                        f"(Alerts suppressed for 30 min)"
                    )
                    _insufficient_balance_alerted[instrument] = _now_ts
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
            product="MIS" if exchange in ("NFO", "BFO") else "NRML"
        )

        print(f"   Order ID: {order_id} — waiting for fill...", flush=True)

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
                        print(f"❌ Order {o['status']} — will retry with fresh LTP")
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
            print(f"⚠️ Order not filled — cancelled {symbol}", flush=True)
            return None

        # ✅ FILLED — now send single confirmation message
        send_message(
            f"📥 Order placed: {symbol}\n"
            f"   Price: ₹{filled_price:.1f}  |  Qty: {quantity}  |  Lots: {qty}\n"
            f"   Total deployed: ₹{filled_price * quantity:,.0f}\n"
            f"   Max risk (45% SL): ₹{filled_price * 0.45 * quantity:,.0f}"
        )

        # Log slippage for reference only — no exit triggered.
        # Cheap options (₹20–₹50) have bid-ask spreads of ₹0.50–₹1.50 which
        # is normal and should NOT cause an immediate exit. Real exits are
        # handled by manage_trade() via profit lock / HalfTrend flip / SL.
        slippage = abs(filled_price - expected_price)
        slippage_pct = (slippage / expected_price * 100) if expected_price else 0
        print(f"✅ Filled @ {filled_price}  (slippage ₹{slippage:.2f} = {slippage_pct:.1f}%)")
        return filled_price

    except Exception as e:
        err_str = str(e)
        print("❌ ORDER ERROR:", err_str, flush=True)

        # Always send the raw error alert
        send_message(f"❌ Order error: {err_str[:200]}")

        # ── IP whitelist error — one-time alert, then pause ──────────────────
        if "not allowed" in err_str.lower() or ("ip" in err_str.lower() and "allowed" in err_str.lower()):
            global _ip_blocked, _ip_alert_sent
            _ip_blocked = True
            if not _ip_alert_sent:
                _ip_alert_sent = True
                _manual_msg = (
                    f"🚨 IP BLOCKED — PLACE MANUALLY\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📌 Instrument : {instrument}\n"
                    f"📊 Signal     : {signal if 'signal' in dir() else 'CHECK CHART'}\n"
                    f"🏷️ Symbol     : {symbol}\n"
                    f"💰 Price (LTP): ₹{price if price else 'CHECK KITE'}\n"
                    f"📦 Quantity   : {qty} lots ({get_quantity(qty, exchange) if qty and exchange else 'CHECK'} shares)\n"
                    f"🏦 Exchange   : {exchange}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"⚡ Open Kite app → place BUY MIS order\n"
                    f"🔧 Fix: developers.kite.trade → My Apps → IP Whitelist → DELETE ALL\n"
                    f"⚠️ No more alerts until fixed (spam prevented)"
                )
                send_message(_manual_msg)
                print(_manual_msg, flush=True)
                # Auto-retry whitelist
                try:
                    print("🔄 Auto-whitelisting IP...", flush=True)
                    _wl = update_kite_ip_whitelist()
                    if _wl:
                        _ip_blocked    = False
                        _ip_alert_sent = False
                        send_message("✅ IP auto-whitelisted — trading resumed automatically")
                except Exception as _wl_e:
                    print(f"⚠️ Auto whitelist failed: {_wl_e}", flush=True)
            else:
                print(f"🔕 IP still blocked — alert already sent, skipping spam", flush=True)

        return None
        
        
def update_streak(pnl, instrument=None):
    global win_streak, loss_streak, last_loss_time, _loss_streak, _win_streak

    if pnl > 0:
        win_streak += 1
        loss_streak = 0
        if instrument and instrument in _loss_streak:
            _win_streak[instrument]  += 1
            _loss_streak[instrument]  = 0
    else:
        loss_streak += 1
        win_streak = 0
        last_loss_time = time.time()
        if instrument and instrument in _loss_streak:
            _loss_streak[instrument] += 1
            _win_streak[instrument]   = 0


def update_exit_time(instrument):
    global last_exit_time_nifty, last_exit_time_crude
    global last_exit_time_banknifty, last_exit_time_finnifty, last_exit_time_sensex

    now = time.time()
    if instrument == "NIFTY":
        last_exit_time_nifty = now
    elif instrument == "BANKNIFTY":
        last_exit_time_banknifty = now
    elif instrument == "FINNIFTY":
        last_exit_time_finnifty = now
    elif instrument == "SENSEX":
        last_exit_time_sensex = now
    else:
        last_exit_time_crude = now

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
    global _daily_target_exited, _profit_protection_floor
    global _whipsaw_pause_until, _flip_timestamps
    global _loss_streak, _win_streak, _blocked_strikes
    global portfolio_pnl, peak_portfolio, risk_off
    global max_drawdown, last_exit_time_nifty, last_exit_time_crude
    global nifty_active, crude_active
    global last_exit_reason
    global nifty_trade_active, banknifty_trade_active, finnifty_trade_active
    global sensex_trade_active, crude_trade_active
    global nifty_position, banknifty_position, finnifty_position
    global sensex_position, crude_position
    global _blocked_strikes
    # exit_done is intentionally LOCAL so two concurrent manage_trade threads
    # (old + new after a flip) do not share state.
    exit_done        = False
    local_max_profit = 0
    partial_pnl      = 0.0
    _exit_attempted  = False   # prevents profit lock from re-firing after failed exit
    exit_fill_price  = None    # actual exit fill price from exit_position()

    full_symbol = f"{exchange}:{symbol}"
    actual_qty    = get_quantity(qty, exchange, instrument)
    remaining_qty = actual_qty

    entry_time = time.time()
    partial_booked = False
    pnl = 0
    ltp = entry

    # Spike reversal detection
    _spike_peak_profit   = 0.0    # highest profit seen in a short window
    _spike_peak_time     = 0.0    # when peak was seen
    SPIKE_MIN_PROFIT     = _SPIKE_MIN_PROFIT
    SPIKE_DROP_PCT       = _SPIKE_DROP_PCT
    SPIKE_WINDOW_SECS    = _SPIKE_WINDOW_SECS

    # 🔥 CORE RISK MODEL — Two-tier SL
    # Single 45% SL — exit immediately when option drops 45% from entry
    SL_TIER2 = 0.45
    risk = entry * SL_TIER2
    sl   = entry - risk   # exit if option premium drops 45%
    peak = entry          # track highest option premium reached

    # Bot always BUYS options (CE or PE).
    # P&L = option_price_now - entry_price (same formula for both CE and PE).
    # SL fires when option premium drops below entry - risk.
    _spike_sl = sl   # same level — single tier
    send_message(
        f"🚀 NEW TRADE ENTERED\n"
        f"📌 {instrument} {signal} → {symbol}\n"
        f"💰 Entry: ₹{entry:.1f}  |  Qty: {actual_qty}\n"
        f"🛑 SL: ₹{sl:.1f}  (entry − 45%)\n"
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
                _cur_gen = (_nifty_trade_gen[0]      if instrument == "NIFTY"
                            else _banknifty_trade_gen[0] if instrument == "BANKNIFTY"
                            else _finnifty_trade_gen[0]  if instrument == "FINNIFTY"
                            else _sensex_trade_gen[0]    if instrument == "SENSEX"
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
            _nifty_time_over     = (instrument in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX") and
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
                _fc_ok = exit_position(symbol, remaining_qty, exchange)
                if not _fc_ok:
                    # Check if already gone
                    try:
                        _net_fc = kite.positions().get("net", [])
                        _fc_open = any(p.get("tradingsymbol") == symbol and p.get("quantity", 0) > 0 for p in _net_fc)
                    except Exception:
                        _fc_open = False
                    if not _fc_open:
                        print(f"✅ {symbol} not in positions — already exited", flush=True)
                    else:
                        send_message(f"🚨 FORCE CLOSE FAILED — EXIT {symbol} qty={remaining_qty} MANUALLY NOW")
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
                    CRUDE_TOKEN         if instrument == "CRUDE"
                    else BANKNIFTY_TOKEN if instrument == "BANKNIFTY"
                    else FINNIFTY_TOKEN  if instrument == "FINNIFTY"
                    else SENSEX_TOKEN    if instrument == "SENSEX"
                    else config.NIFTY_TOKEN
                ):
                    print("🚀 Strong trend — skipping partial booking", flush=True)
                else:
                    # Lot-size aware partial exit — must be multiple of lot size
                    if instrument == "SENSEX":
                        one_lot = SENSEX_LOT_SIZE
                    elif instrument == "BANKNIFTY":
                        one_lot = BANKNIFTY_LOT_SIZE
                    elif instrument == "FINNIFTY":
                        one_lot = FINNIFTY_LOT_SIZE
                    elif instrument == "CRUDE":
                        one_lot = CRUDE_LOT_SIZE
                    else:
                        one_lot = NIFTY_LOT_SIZE

                    total_lots = remaining_qty // one_lot
                    exit_lots  = max(1, total_lots // 2)
                    half_qty   = exit_lots * one_lot

                    if half_qty > 0 and half_qty < remaining_qty:
                        # Calculate partial P&L: profit = ltp - entry (already computed above)
                        _partial_pnl = profit * half_qty
                        _exit_ok_partial = exit_position(symbol, half_qty, exchange)

                        if _exit_ok_partial:
                            remaining_qty -= half_qty
                            partial_booked = True
                            partial_pnl   += _partial_pnl

                            # Reset profit lock baseline to POST-partial P&L
                            local_max_profit = profit * remaining_qty
                            manage_trade._partial_just_booked = True

                            print(f"💰 Partial booked: {half_qty} units | "
                                  f"Remaining: {remaining_qty} | "
                                  f"Partial P&L: ₹{_partial_pnl:.0f} | "
                                  f"Lock baseline reset to ₹{local_max_profit:.0f}", flush=True)
                            send_message(
                                f"💰 PARTIAL BOOKING\n"
                                f"📌 {instrument} {signal} → {symbol}\n"
                                f"📤 Exited {half_qty} units ({exit_lots} lot{'s' if exit_lots>1 else ''})\n"
                                f"📊 Remaining: {remaining_qty} units\n"
                                f"💰 Partial P&L: ₹{_partial_pnl:.0f} | Total so far: ₹{current_pnl:.0f}"
                            )
                        else:
                            print(f"⚠️ Partial booking exit failed — skipping partial", flush=True)

            # ===============================
            # 💰 GLOBAL PROFIT PROTECTION
            # ===============================

            # Skip max() update the tick right after partial booking
            # to prevent the reset from being overwritten
            if not getattr(manage_trade, "_partial_just_booked", False):
                local_max_profit = max(local_max_profit, current_pnl)
            manage_trade._partial_just_booked = False

            # ================================================================
            # ⚡ SPIKE REVERSAL EXIT
            # If profit spikes rapidly then drops 35% from spike peak → exit
            # Catches sudden reversals like the chart shows
            # ================================================================
            if current_pnl >= SPIKE_MIN_PROFIT and not exit_done:
                if current_pnl > _spike_peak_profit:
                    _spike_peak_profit = current_pnl
                    _spike_peak_time   = time.time()
                else:
                    _spike_age = time.time() - _spike_peak_time
                    if (_spike_age <= SPIKE_WINDOW_SECS and
                            _spike_peak_profit > 0 and
                            current_pnl < _spike_peak_profit * (1 - SPIKE_DROP_PCT)):
                        print(f"⚡ SPIKE REVERSAL: peak=₹{_spike_peak_profit:.0f} "
                              f"dropped to ₹{current_pnl:.0f} "
                              f"({(1 - current_pnl/_spike_peak_profit)*100:.0f}% drop in {_spike_age:.0f}s)",
                              flush=True)
                        send_message(
                            f"⚡ SPIKE REVERSAL EXIT\n"
                            f"📌 {instrument} {signal} → {symbol}\n"
                            f"📈 Peak profit: ₹{_spike_peak_profit:.0f}\n"
                            f"📉 Current:     ₹{current_pnl:.0f}\n"
                            f"⏱️ Reversed in {_spike_age:.0f}s — locking gains"
                        )
                        _sr_fill = exit_position(symbol, remaining_qty, exchange)
                        if not _sr_fill:
                            send_message(f"🚨 SPIKE EXIT FAILED — EXIT {symbol} MANUALLY")
                        else:
                            exit_fill_price = _sr_fill if isinstance(_sr_fill, float) else None
                            ltp = exit_fill_price or ltp
                        pnl = current_pnl
                        exit_done = True
                        break

            if local_max_profit >= 1000:

                # 🎯 DYNAMIC LOCK TIERS:
                # ₹1,000 – ₹1,499 → lock 80%
                # ₹1,500 – ₹2,299 → lock 85%
                # ₹2,300 – ₹2,999 → lock 90%
                # ₹3,000+          → lock 92%
                if local_max_profit < 1500:
                    lock_pct = 0.80
                elif local_max_profit < 2300:
                    lock_pct = 0.85
                elif local_max_profit < 3000:
                    lock_pct = 0.90
                else:
                    lock_pct = 0.92

                lock_level = local_max_profit * lock_pct

                print(f"💰 Lock Active → Peak: {local_max_profit:.0f}, Lock: {lock_level:.0f}")

                if current_pnl < lock_level and not _exit_attempted:
                    _exit_attempted = True   # prevent re-firing every 1.5s
                    _total_pnl = current_pnl + partial_pnl
                    send_message(
                        f"💰 PROFIT LOCK EXIT\n"
                        f"📌 {instrument} {signal} → {symbol}\n"
                        f"📈 Peak P&L: ₹{local_max_profit:.0f}  |  Current: ₹{current_pnl:.0f}\n"
                        f"🔒 Lock level ({int(lock_pct*100)}%): ₹{lock_level:.0f} — exiting to protect gains\n"
                        + (f"💰 Partial booked: ₹{partial_pnl:.0f} | Total trade P&L: ₹{_total_pnl:.0f}\n" if partial_pnl else "") +
                        f"⏳ Next entry in 5 min"
                    )
                    print("💰 Profit lock triggered — exit")
                    _profit_lock_exit_time[instrument] = time.time()

                    if not exit_done:
                        _exit_fill = exit_position(symbol, remaining_qty, exchange)
                        if _exit_fill:
                            exit_fill_price = _exit_fill if isinstance(_exit_fill, float) else None
                            ltp = exit_fill_price or ltp
                            pnl = (ltp - entry) * remaining_qty
                            exit_done = True
                            break
                        else:
                            # Exit failed — try market order as last resort
                            print(f"⚠️ Profit lock exit failed — trying MARKET order", flush=True)
                            try:
                                kite.place_order(
                                    variety=kite.VARIETY_REGULAR,
                                    exchange=exchange,
                                    tradingsymbol=symbol,
                                    transaction_type=kite.TRANSACTION_TYPE_SELL,
                                    quantity=remaining_qty,
                                    order_type=kite.ORDER_TYPE_LIMIT,
                                    price=round(ltp * 0.94, 1),   # 6% below LTP — aggressive fill
                                    product=kite.PRODUCT_MIS,
                                )
                                send_message(
                                    f"🚨 AGGRESSIVE LIMIT EXIT — {symbol}\n"
                                    f"Price: ₹{round(ltp * 0.94, 1)} (6% below LTP)\n"
                                    f"Qty: {remaining_qty} | Check Kite for fill"
                                )
                                exit_done = True
                                pnl = current_pnl
                                break
                            except Exception as _me:
                                print(f"❌ Market order also failed: {_me}", flush=True)
                                send_message(
                                    f"🚨 CRITICAL: ALL EXITS FAILED — {symbol}\n"
                                    f"Qty: {remaining_qty}\n"
                                    f"EXIT MANUALLY NOW on Kite!\n"
                                    f"(This alert will not repeat)"
                                )
                                # Don't break — keep monitoring but don't re-alert
                    else:
                        pnl = current_pnl
                        break

                elif current_pnl < lock_level and _exit_attempted and not exit_done:
                    # Exit was attempted but failed — re-check if position still open
                    try:
                        _net = kite.positions().get("net", [])
                        _still_open = any(
                            p.get("tradingsymbol") == symbol and p.get("quantity", 0) > 0
                            for p in _net
                        )
                    except Exception:
                        _still_open = True  # assume still open if API fails

                    if not _still_open:
                        # Position gone — manually exited or already filled
                        print(f"✅ {symbol} no longer in Kite positions — treating as exited", flush=True)
                        exit_done = True
                        pnl = current_pnl
                        break
                    else:
                        # Still open — retry exit silently
                        _exit_fill = exit_position(symbol, remaining_qty, exchange)
                        if _exit_fill:
                            exit_fill_price = _exit_fill if isinstance(_exit_fill, float) else None
                            ltp = exit_fill_price or ltp
                            pnl = (ltp - entry) * remaining_qty
                            exit_done = True
                            break

            # ===============================
            # 🧠 ATR BASED TRAILING
            # ===============================
            try:
                df_trail = get_cached_data(
                    CRUDE_TOKEN      if instrument == "CRUDE"
                    else BANKNIFTY_TOKEN if instrument == "BANKNIFTY"
                    else FINNIFTY_TOKEN  if instrument == "FINNIFTY"
                    else SENSEX_TOKEN    if instrument == "SENSEX"
                    else config.NIFTY_TOKEN,
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
            trail_multiplier = 1.2
            old_sl = sl

            # Normal trailing: SL rises as option premium rises
            sl = max(sl, peak - (atr_value * trail_multiplier))
            if abs(sl - old_sl) > entry * 0.005:
                print(f"📈 {instrument} trailing SL: ₹{old_sl:.1f} → ₹{sl:.1f}  P&L: ₹{current_pnl:.0f}", flush=True)

            # ===============================
            # 🔥 STRONG TREND MODE (LET PROFITS RUN)
            # ===============================
            if current_pnl >= 3000:
                sl = max(sl, peak - (atr_value * 0.8))

            # 🔥 HALFTREND EXIT — fires when arrow flips on closed candle
            # This is the PRIMARY exit path — runs every 1.5s inside the trade.
            # The loop's Layer 1 is a backup that runs every 10s.
            # Rule: if HalfTrend trend direction changes → exit immediately.
            # No filters apply to exits — only to new entries.
            try:
                _exit_tf = "15minute" if instrument == "CRUDE" else "5minute"
                _exit_token = (CRUDE_TOKEN         if instrument == "CRUDE"
                               else BANKNIFTY_TOKEN if instrument == "BANKNIFTY"
                               else FINNIFTY_TOKEN  if instrument == "FINNIFTY"
                               else SENSEX_TOKEN    if instrument == "SENSEX"
                               else config.NIFTY_TOKEN)
                df_ht_exit = get_cached_data(_exit_token, _exit_tf, 120)

                if df_ht_exit is None or len(df_ht_exit) < 10:
                    raise ValueError("Insufficient data for HT exit check")

                ht_df_exit = halftrend_tv(df_ht_exit, amplitude=HT_AMPLITUDE, channel_deviation=2)
                last_exit  = ht_df_exit.iloc[-2]   # last CLOSED candle — anti-repaint

                # Exit on TREND CHANGE — faster than waiting for arrow
                # trend=0 (bullish) → signal should be CALL
                # trend=1 (bearish) → signal should be PUT
                current_ht_trend = int(last_exit["trend"])
                expected_trend   = 0 if signal == "CALL" else 1

                if current_ht_trend != expected_trend:
                    # Trend has flipped — check if loop already handled it
                    pos_dict = (nifty_position    if instrument == "NIFTY"
                                else banknifty_position if instrument == "BANKNIFTY"
                                else finnifty_position  if instrument == "FINNIFTY"
                                else sensex_position    if instrument == "SENSEX"
                                else crude_position)
                    with lock:
                        already_flipped = (pos_dict.get("symbol") != symbol)

                    if already_flipped:
                        print(f"ℹ️ {instrument} HT exit: loop already handled flip — breaking cleanly", flush=True)
                        pnl = current_pnl
                        break

                    new_dir = "CALL" if current_ht_trend == 0 else "PUT"

                    # Only alert and attempt exit once — prevent spam
                    if not getattr(manage_trade, f"_ht_flip_{symbol}", False):
                        setattr(manage_trade, f"_ht_flip_{symbol}", True)
                        print(f"🔄 {instrument} HalfTrend FLIP → was {signal}, now {new_dir} — exiting", flush=True)
                        send_message(
                            f"🔄 HALFTREND FLIP EXIT\n"
                            f"📌 {instrument}: {signal} → {new_dir}\n"
                            f"🏷️ {symbol}\n"
                            f"💰 P&L: ₹{current_pnl:.0f}  |  LTP: ₹{ltp:.1f}\n"
                            f"📊 HT={last_exit['ht']:.2f}"
                        )

                    if not exit_done:
                        _exit_fill = exit_position(symbol, remaining_qty, exchange)
                        if _exit_fill:
                            exit_fill_price = _exit_fill if isinstance(_exit_fill, float) else None
                            ltp = exit_fill_price or ltp
                            pnl = (ltp - entry) * remaining_qty
                            exit_done = True
                            # Clear flag on successful exit
                            setattr(manage_trade, f"_ht_flip_{symbol}", False)
                            break
                        else:
                            # Exit failed — try aggressive limit as last resort
                            print(f"⚠️ HT flip exit failed — trying aggressive limit order", flush=True)
                            try:
                                _ltp_now = safe_ltp(f"{exchange}:{symbol}") or ltp
                                _aggressive_price = max(0.5, round(_ltp_now * 0.94, 1))
                                kite.place_order(
                                    variety=kite.VARIETY_REGULAR,
                                    exchange=exchange,
                                    tradingsymbol=symbol,
                                    transaction_type=kite.TRANSACTION_TYPE_SELL,
                                    quantity=remaining_qty,
                                    order_type=kite.ORDER_TYPE_LIMIT,
                                    price=_aggressive_price,
                                    product=kite.PRODUCT_MIS,
                                )
                                exit_done = True
                                pnl = current_pnl
                                setattr(manage_trade, f"_ht_flip_{symbol}", False)
                                break
                            except Exception as _me:
                                print(f"❌ Market order failed: {_me}", flush=True)
                                if not getattr(manage_trade, f"_ht_mkt_alerted_{symbol}", False):
                                    setattr(manage_trade, f"_ht_mkt_alerted_{symbol}", True)
                                    send_message(
                                        f"🚨 EXIT MANUALLY NOW — {symbol}\n"
                                        f"qty={remaining_qty}\n"
                                        f"(This alert will not repeat)"
                                    )
                        # Don't break — keep monitoring silently
                        # But first check if position still exists
                        try:
                            _net2 = kite.positions().get("net", [])
                            _still_open2 = any(
                                p.get("tradingsymbol") == symbol and p.get("quantity", 0) > 0
                                for p in _net2
                            )
                        except Exception:
                            _still_open2 = True

                        if not _still_open2:
                            print(f"✅ {symbol} no longer open — treating as manually exited", flush=True)
                            setattr(manage_trade, f"_ht_flip_{symbol}", False)
                            setattr(manage_trade, f"_ht_mkt_alerted_{symbol}", False)
                            exit_done = True
                            pnl = current_pnl
                            break

            except Exception as e:
                print(f"HT exit error [{instrument}]: {e}", flush=True)

            # ================================================================
            # 🛑 STOP LOSS EXITS  (checked in priority order every 1.5 s)
            # ================================================================

            # ── Stop Loss — Single 45% tier ──────────────────────────────────
            # Set USE_STOP_LOSS = True to enable.
            # Exits immediately when option premium drops 45% from entry.
            if USE_STOP_LOSS:
                sl = entry * (1 - SL_TIER2)   # 45% of entry price
                trailing_hit = ltp <= sl

                if trailing_hit and not exit_done:
                    last_exit_reason = "TRAILING_SL"
                    print(f"🛑 SL HIT (45%) | entry=₹{entry:.1f} SL=₹{sl:.1f} LTP=₹{ltp:.1f}", flush=True)
                    send_message(
                        f"🛑 STOP LOSS HIT — EXITING\n"
                        f"📌 {instrument} {signal} → {symbol}\n"
                        f"📉 LTP: ₹{ltp:.1f}  |  SL (45%): ₹{sl:.1f}\n"
                        f"💔 P&L: ₹{current_pnl:.0f}\n"
                        f"📊 Entry: ₹{entry:.1f}  |  Loss: {((ltp-entry)/entry*100):.1f}%"
                    )
                    _sl_fill = exit_position(symbol, remaining_qty, exchange)
                    if not _sl_fill:
                        send_message(f"🚨 SL EXIT FAILED — EXIT {symbol} MANUALLY NOW")
                    else:
                        exit_fill_price = _sl_fill if isinstance(_sl_fill, float) else None
                        ltp = exit_fill_price or ltp
                    pnl = (ltp - entry) * remaining_qty
                    exit_done = True
                    break
            # ================================================================
            # 💔 MAX LOSS EXIT — exit immediately if loss hits limit
            # MAX_LOSS_PER_TRADE: fixed ₹ limit (e.g. ₹1,600 regardless of lots)
            # MAX_LOSS_PER_LOT:   per-lot limit (e.g. ₹800 × 2 lots = ₹1,600)
            # Uses whichever is lower (most conservative)
            # ================================================================
            _lot_count   = remaining_qty // _get_lot_size(instrument)
            _max_by_lot  = (MAX_LOSS_PER_LOT * _lot_count) if MAX_LOSS_PER_LOT > 0 else 999999
            _max_by_flat = MAX_LOSS_PER_TRADE if MAX_LOSS_PER_TRADE > 0 else 999999
            _effective_max_loss = min(_max_by_lot, _max_by_flat)

            if not exit_done and current_pnl <= -_effective_max_loss:
                print(f"💔 MAX LOSS ₹{current_pnl:.0f} <= -₹{_effective_max_loss:.0f} "
                      f"(flat=₹{MAX_LOSS_PER_TRADE} per_lot=₹{MAX_LOSS_PER_LOT}×{_lot_count}lots) "
                      f"— exiting {symbol}", flush=True)
                send_message(
                    f"💔 MAX LOSS EXIT — ₹{_effective_max_loss:.0f} LIMIT HIT\n"
                    f"📌 {instrument} {signal} → {symbol}\n"
                    f"📉 Loss: ₹{current_pnl:.0f}\n"
                    f"📊 Entry: ₹{entry:.1f}  |  LTP: ₹{ltp:.1f}\n"
                    f"🚫 {symbol} blocked for rest of day — same strike won't re-enter"
                )
                _ml_fill = exit_position(symbol, remaining_qty, exchange)
                if not _ml_fill:
                    send_message(f"🚨 MAX LOSS EXIT FAILED — EXIT {symbol} MANUALLY NOW")
                else:
                    ltp = _ml_fill if isinstance(_ml_fill, float) else ltp
                # Block this exact strike for rest of day
                _blocked_strikes[instrument].add(symbol)
                exit_done = True
                pnl = current_pnl
                break

            # ================================================================
            # ⏱️ THETA DECAY EXIT — exit if trade held too long with no momentum
            # Options lose value rapidly after 90 min due to time decay.
            # Only exits if:
            #   1. Trade held > 90 min
            #   2. Option moved < 10% (no momentum)
            #   3. Profit lock NOT already triggered
            # ================================================================
            _trade_age_mins = (time.time() - entry_time) / 60
            _USE_TIME_EXIT  = os.environ.get("USE_THETA_EXIT", "true").lower() == "true"
            _TIME_EXIT_MINS = int(os.environ.get("THETA_EXIT_MINS", "90"))
            _MOMENTUM_PCT   = 0.10    # option must have moved >10% to stay in trade

            if (_USE_TIME_EXIT
                    and not exit_done
                    and _trade_age_mins >= _TIME_EXIT_MINS
                    and local_max_profit < 1000):    # profit lock not active
                _moved_pct = abs(ltp - entry) / entry if entry > 0 else 0
                if _moved_pct < _MOMENTUM_PCT:
                    # Re-entry logic after theta exit:
                    # Loss < ₹500  → re-enter immediately (just theta decay)
                    # Loss >= ₹500 → block re-entry (option falling, not just decay)
                    _will_reenter = current_pnl > -500
                    print(f"⏱️ THETA EXIT: {_trade_age_mins:.0f} min held, "
                          f"moved {_moved_pct*100:.1f}%, P&L=₹{current_pnl:.0f}, "
                          f"re-enter={_will_reenter}", flush=True)
                    send_message(
                        f"⏱️ THETA DECAY EXIT\n"
                        f"📌 {instrument} {signal} → {symbol}\n"
                        f"🕐 Held: {_trade_age_mins:.0f} minutes (>{_TIME_EXIT_MINS} min)\n"
                        f"📊 Option moved only {_moved_pct*100:.1f}% — no momentum\n"
                        f"💰 P&L: ₹{current_pnl:.0f}\n"
                        f"💡 Exiting to avoid theta decay erosion\n"
                        + (f"♻️ Will re-enter if signal still active (loss < ₹500)"
                           if _will_reenter else
                           f"⏳ Loss ₹{current_pnl:.0f} >= ₹500 — waiting for strong candle move before re-entry")
                    )

                    # If big loss — block same strike re-entry until HT flips and comes back
                    if not _will_reenter:
                        _blocked_strikes[instrument].add(symbol)
                        print(f"🚫 {symbol} blocked for re-entry — loss too large", flush=True)
                    _te_fill = exit_position(symbol, remaining_qty, exchange)
                    if not _te_fill:
                        send_message(f"🚨 THETA EXIT FAILED — EXIT {symbol} MANUALLY")
                    else:
                        exit_fill_price = _te_fill if isinstance(_te_fill, float) else None
                        ltp = exit_fill_price or ltp
                    exit_done = True
                    pnl = current_pnl
                    break

            time.sleep(1.5)

    except Exception as e:
        print("Trade error:", e)

    finally:
        # -----------------------------
        # 📊 FINAL UPDATE BLOCK (SAFE)
        # -----------------------------
        with lock:
            global global_trade_active
            global nifty_daily_pnl,     sensex_daily_pnl
            global banknifty_daily_pnl, finnifty_daily_pnl, crude_daily_pnl
            global nifty_trade_count,     crude_trade_count
            global banknifty_trade_count, finnifty_trade_count, sensex_trade_count
            global nifty_daily_wins,     nifty_daily_losses
            global crude_daily_wins,     crude_daily_losses
            global banknifty_daily_wins, banknifty_daily_losses
            global finnifty_daily_wins,  finnifty_daily_losses
            global sensex_daily_wins,    sensex_daily_losses

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
            elif instrument == "FINNIFTY":
                finnifty_daily_pnl += pnl
                finnifty_trade_count += 1
                if pnl > 0: finnifty_daily_wins   += 1
                else:        finnifty_daily_losses += 1
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

            update_streak(pnl, instrument)
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
            _combined_pnl = nifty_daily_pnl + banknifty_daily_pnl + finnifty_daily_pnl + sensex_daily_pnl + crude_daily_pnl
            _total_trade_pnl = pnl + partial_pnl
            send_message(
                f"{exit_emoji} TRADE CLOSED — {instrument} {signal}\n"
                f"📌 {symbol}\n"
                f"💰 Final P&L : ₹{pnl:+.0f}  ({'PROFIT' if pnl > 0 else 'LOSS'})\n"
                + (f"💰 Partial   : ₹{partial_pnl:+.0f}  |  Total: ₹{_total_trade_pnl:+.0f}\n" if partial_pnl else "") +
                f"📊 Entry: ₹{entry:.1f}  |  Exit: ₹{exit_fill_price or ltp:.1f}\n"
                f"{'─'*28}\n"
                f"📅 NIFTY      : ₹{nifty_daily_pnl:+.0f}\n"
                f"📅 FINNIFTY   : ₹{finnifty_daily_pnl:+.0f}\n"
                f"📅 BANKNIFTY  : ₹{banknifty_daily_pnl:+.0f}\n"
                f"📅 SENSEX     : ₹{sensex_daily_pnl:+.0f}\n"
                f"📅 CRUDE      : ₹{crude_daily_pnl:+.0f}\n"
                f"💼 Combined   : ₹{_combined_pnl:+.0f}"
            )

            log_trade_full(symbol, entry, exit_fill_price or ltp, pnl, instrument, signal, probability)

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
        exchange_map = {
            "NIFTY":     "NFO",
            "BANKNIFTY": "NFO",
            "FINNIFTY":  "NFO",
            "SENSEX":    "BFO",
            "CRUDE":     "MCX",
        }
        prefix_map = {
            "NIFTY":     "NIFTY",
            "BANKNIFTY": "BANKNIFTY",
            "FINNIFTY":  "FINNIFTY",
            "SENSEX":    "SENSEX",
            "CRUDE":     "CRUDEOIL",   # MCX Crude Oil symbol starts with CRUDEOIL
        }
        target_exchange = exchange_map.get(instrument)
        sym_prefix      = prefix_map.get(instrument)

        positions = kite.positions().get("net", [])
        for p in positions:
            if p.get("quantity", 0) == 0:
                continue
            if p.get("exchange") != target_exchange:
                continue
            sym = p.get("tradingsymbol", "")
            if not (sym.endswith("CE") or sym.endswith("PE")):
                continue
            # Strictly match prefix — NIFTY must NOT match BANKNIFTY or FINNIFTY
            if sym_prefix:
                if not sym.startswith(sym_prefix):
                    continue
                # Extra check for NIFTY — exclude BANKNIFTY and FINNIFTY
                if instrument == "NIFTY" and (sym.startswith("BANKNIFTY") or sym.startswith("FINNIFTY")):
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


def _get_lot_size(instrument):
    """Returns lot size for instrument — used in position restore."""
    return {"SENSEX": 20, "BANKNIFTY": 30, "FINNIFTY": 60, "CRUDE": 100}.get(instrument, 65)


def restore_position_state_from_kite():
    """
    Called once at bot startup.
    If Kite already has open positions (e.g. from a previous session),
    restore in-memory flags AND start manage_trade so exits/SL/profit-lock work.
    """
    global nifty_position, banknifty_position, finnifty_position, sensex_position, crude_position
    global nifty_trade_active, banknifty_trade_active, finnifty_trade_active, sensex_trade_active, crude_trade_active
    global global_trade_active
    global last_executed_signal_nifty, last_executed_signal_banknifty
    global last_executed_signal_finnifty, last_executed_signal_sensex, last_executed_signal_crude

    for instrument in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "CRUDE"):
        pos = get_open_kite_position(instrument)
        if pos is None:
            continue

        sym      = pos["symbol"]
        qty      = pos["qty"]
        exchange = pos["exchange"]
        signal   = "CALL" if sym.endswith("CE") else "PUT"

        # ── Get actual entry price from Kite positions (average_price) ────────
        # NEVER use LTP as entry — option price changes dramatically intraday.
        # Kite's positions["net"] contains the actual average buy price.
        entry_price = 0.0
        try:
            _positions = kite.positions()
            _net = _positions.get("net", [])
            for _p in _net:
                if _p.get("tradingsymbol") == sym:
                    _avg = float(_p.get("average_price", 0))
                    if _avg > 0:
                        entry_price = _avg
                        qty = abs(int(_p.get("quantity", qty)))
                    break
            if entry_price == 0.0:
                # Fallback to LTP only if average_price not found
                _ex = "BFO" if instrument == "SENSEX" else ("MCX" if instrument == "CRUDE" else "NFO")
                _ltp_data = kite.ltp(f"{_ex}:{sym}")
                entry_price = float(_ltp_data[f"{_ex}:{sym}"]["last_price"])
                print(f"⚠️ average_price not found — using LTP ₹{entry_price:.1f} as fallback", flush=True)
        except Exception as _ep_err:
            print(f"⚠️ Could not get entry price: {_ep_err}", flush=True)
            entry_price = 0.0

        print(f"⚠️ Existing Kite position found on startup: {instrument} {sym} qty={qty} avg_entry=₹{entry_price:.1f}", flush=True)
        send_message(
            f"⚠️ EXISTING POSITION DETECTED ON STARTUP\n"
            f"📌 {instrument}: {sym}  qty={qty}\n"
            f"💰 Avg Entry: ₹{entry_price:.1f}\n"
            f"🔄 Restoring — manage_trade started for SL/profit-lock/exit"
        )

        with lock:
            if instrument == "NIFTY":
                nifty_position.update({"symbol": sym, "qty": qty, "exchange": exchange, "signal": signal, "active": True})
                nifty_trade_active = True
                last_executed_signal_nifty = signal
            elif instrument == "BANKNIFTY":
                banknifty_position.update({"symbol": sym, "qty": qty, "exchange": exchange, "signal": signal, "active": True})
                banknifty_trade_active = True
                last_executed_signal_banknifty = signal
            elif instrument == "FINNIFTY":
                finnifty_position.update({"symbol": sym, "qty": qty, "exchange": exchange, "signal": signal, "active": True})
                finnifty_trade_active = True
                last_executed_signal_finnifty = signal
            elif instrument == "SENSEX":
                sensex_position.update({"symbol": sym, "qty": qty, "exchange": exchange, "signal": signal, "active": True})
                sensex_trade_active = True
                last_executed_signal_sensex = signal
            else:
                crude_position.update({"symbol": sym, "qty": qty, "exchange": exchange, "signal": signal, "active": True})
                crude_trade_active = True
                last_executed_signal_crude = signal

            global_trade_active = True

        # ── Start manage_trade thread for restored position ───────────────
        # Without this, nifty_trade_active stays True forever and blocks
        # all new entries. manage_trade will monitor SL, profit-lock, HT exit.
        import threading
        _restore_thread = threading.Thread(
            target=run_trade_wrapper,
            args=(sym, entry_price, qty // _get_lot_size(instrument),
                  exchange, instrument, signal, 50, "TREND"),
            daemon=True,
            name=f"Restore_{instrument}"
        )
        _restore_thread.start()
        print(f"✅ manage_trade started for restored {instrument} position", flush=True)


def restore_daily_state():
    """
    Called once at startup/redeploy.
    Reads today's closed trades from Kite positions API and restores
    in-memory counters so that:
      • Daily trade limits work correctly (bot doesn't bypass MAX_X_TRADES_PER_DAY)
      • Trade-closed Telegram messages show the correct running daily P&L
      • Loss streak detection continues from the right count
    """
    global nifty_trade_count,     nifty_daily_pnl,     nifty_daily_wins,     nifty_daily_losses
    global banknifty_trade_count, banknifty_daily_pnl, banknifty_daily_wins, banknifty_daily_losses
    global finnifty_trade_count,  finnifty_daily_pnl,  finnifty_daily_wins,  finnifty_daily_losses
    global sensex_trade_count,    sensex_daily_pnl,    sensex_daily_wins,    sensex_daily_losses
    global crude_trade_count,     crude_daily_pnl,     crude_daily_wins,     crude_daily_losses
    global daily_pnl, portfolio_pnl

    print("🔄 Restoring today's trade counters from Kite...", flush=True)
    restored_any = False

    for instrument, prefix in [
        ("NIFTY",     "nifty"),
        ("BANKNIFTY", "banknifty"),
        ("FINNIFTY",  "finnifty"),
        ("SENSEX",    "sensex"),
        ("CRUDE",     "crude"),
    ]:
        try:
            pnl, wins, losses, count = _kite_day_pnl(instrument)
            if count > 0:
                globals()[f"{prefix}_trade_count"]  = count
                globals()[f"{prefix}_daily_pnl"]    = pnl
                globals()[f"{prefix}_daily_wins"]   = wins
                globals()[f"{prefix}_daily_losses"] = losses
                print(
                    f"  ✅ {instrument}: {count} trade(s) restored — "
                    f"P&L=₹{pnl:.0f}  W{wins}/L{losses}",
                    flush=True
                )
                restored_any = True
        except Exception as e:
            print(f"  ⚠️ {instrument} restore failed: {e}", flush=True)

    if restored_any:
        # Sync combined daily P&L with restored per-instrument totals
        daily_pnl = nifty_daily_pnl + banknifty_daily_pnl + finnifty_daily_pnl + sensex_daily_pnl + crude_daily_pnl
        portfolio_pnl = daily_pnl
        print(f"✅ Daily state restored — combined P&L today: ₹{daily_pnl:.0f}", flush=True)
        send_message(
            f"🔄 BOT REDEPLOYED — STATE RESTORED\n"
            f"{'='*28}\n"
            f"📌 NIFTY     : {nifty_trade_count} trades  ₹{nifty_daily_pnl:,.0f}\n"
            f"📌 BANKNIFTY : {banknifty_trade_count} trades  ₹{banknifty_daily_pnl:,.0f}\n"
            f"📌 FINNIFTY  : {finnifty_trade_count} trades  ₹{finnifty_daily_pnl:,.0f}\n"
            f"📌 SENSEX    : {sensex_trade_count} trades  ₹{sensex_daily_pnl:,.0f}\n"
            f"📌 CRUDE     : {crude_trade_count} trades  ₹{crude_daily_pnl:,.0f}\n"
            f"{'='*28}\n"
            f"🏦 Combined  : ₹{daily_pnl:,.0f}"
        )
    else:
        print("✅ No prior trades today — counters start from zero", flush=True)


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
            # Re-fetch LTP on each attempt — price moves during retries
            try:
                _fresh_ltp = safe_ltp(f"{exchange}:{symbol}")
                if _fresh_ltp and _fresh_ltp > 0:
                    ltp = _fresh_ltp
            except Exception:
                pass  # keep using last known ltp

            # For very cheap options (< ₹15), use bigger absolute slippage
            if ltp < 15:
                exit_price = max(0.5, round(ltp - (attempt * 0.5), 1))
            elif ltp < 50:
                exit_price = max(0.5, round(ltp * slip - 0.5, 1))
            else:
                exit_price = round(ltp * slip, 1)

            if exit_price <= 0:
                exit_price = 0.5

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
                _last_order_err = str(oe)
                print(f"   ⚠️ Order placement failed: {oe}")
                # If timeout — wait longer before retry
                if "timed out" in _last_order_err.lower() or "timeout" in _last_order_err.lower():
                    print(f"   ⏳ Timeout detected — waiting 3s before retry", flush=True)
                    time.sleep(3)
                else:
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
                for _inst_key in ("NIFTY", "BANKNIFTY", "FINNIFTY", "SENSEX", "CRUDE"):
                    _kite_pos_cache.pop(_inst_key, None)
                send_message(
                    f"✅ EXIT FILLED\n"
                    f"📌 {symbol}\n"
                    f"💰 Sell price: ₹{filled_price:.1f}  |  Qty: {exit_qty}\n"
                    f"📊 Slippage: {(1-slip)*100:.1f}% from LTP ₹{ltp:.1f}"
                )
                print(f"✅ Exit filled @ ₹{filled_price:.1f}")
                return filled_price   # return actual fill price

            # Not filled — cancel and try next slippage level
            try:
                kite.cancel_order(variety=kite.VARIETY_REGULAR, order_id=order_id)
                print(f"   ↩️ Cancelled unfilled order — trying next level")
            except Exception:
                pass

            time.sleep(0.5)

        # All attempts exhausted
        print(f"❌ Exit FAILED after 4 attempts — {symbol}")
        _ip_blocked_exit = "not allowed" in str(_last_order_err).lower() if '_last_order_err' in dir() else False

        if _ip_blocked_exit:
            global _ip_blocked, _ip_alert_sent
            _ip_blocked = True
            if not _ip_alert_sent:
                _ip_alert_sent = True
                send_message(
                    f"🚨 EXIT FAILED — IP BLOCKED\n"
                    f"📌 {symbol} qty={exit_qty}\n"
                    f"🔧 Fix: developers.kite.trade\n"
                    f"   → My Apps → Your App → IP Whitelist\n"
                    f"   → DELETE ALL entries → Save\n"
                    f"⚠️ EXIT MANUALLY ON KITE NOW!\n"
                    f"⚠️ No more alerts until fixed (spam prevented)"
                )
                # Auto-retry whitelist
                try:
                    print("🔄 Auto-whitelisting IP...", flush=True)
                    _wl = update_kite_ip_whitelist()
                    if _wl:
                        _ip_blocked    = False
                        _ip_alert_sent = False
                        send_message("✅ IP auto-whitelisted — trading resumed")
                except Exception as _wl_e:
                    print(f"⚠️ Auto whitelist failed: {_wl_e}", flush=True)
            else:
                print(f"🔕 IP still blocked — exit alert already sent", flush=True)
        else:
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
    Also enforces the daily trade limit as a final safety net before the trade starts.
    """
    global nifty_active, crude_active
    global nifty_trade_active, crude_trade_active, banknifty_trade_active, finnifty_trade_active, sensex_trade_active
    global nifty_position, crude_position, banknifty_position, finnifty_position, sensex_position
    global global_trade_active
    global last_executed_signal_nifty, last_executed_signal_crude
    global last_executed_signal_banknifty, last_executed_signal_finnifty, last_executed_signal_sensex

    # ── Final trade limit safety check before starting ───────────────────────
    limit_map = {
        "NIFTY":     (nifty_trade_count,     MAX_NIFTY_TRADES_PER_DAY),
        "BANKNIFTY": (banknifty_trade_count,  MAX_BANKNIFTY_TRADES_PER_DAY),
        "FINNIFTY":  (finnifty_trade_count,   MAX_FINNIFTY_TRADES_PER_DAY),
        "SENSEX":    (sensex_trade_count,     MAX_SENSEX_TRADES_PER_DAY),
        "CRUDE":     (crude_trade_count,      MAX_CRUDE_TRADES_PER_DAY),
    }
    _count, _max = limit_map.get(instrument, (0, 99))
    if _count >= _max:
        print(f"🔒 {instrument} run_trade_wrapper: trade limit {_count}/{_max} already hit — aborting", flush=True)
        with lock:
            if instrument == "NIFTY":       nifty_trade_active = False
            elif instrument == "BANKNIFTY": banknifty_trade_active = False
            elif instrument == "FINNIFTY":  finnifty_trade_active = False
            elif instrument == "SENSEX":    sensex_trade_active = False
            else:                           crude_trade_active = False
            global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
        return

    try:
        manage_trade(symbol, price, lot, exchange, instrument, signal, probability, market_type,
                     gen_id=gen_id)

    finally:
        with lock:
            if instrument == "NIFTY":
                pos_dict = nifty_position
            elif instrument == "BANKNIFTY":
                pos_dict = banknifty_position
            elif instrument == "FINNIFTY":
                pos_dict = finnifty_position
            elif instrument == "SENSEX":
                pos_dict = sensex_position
            else:
                pos_dict = crude_position

            # Only clear state when this trade's symbol is still the active one.
            if pos_dict.get("symbol") == symbol:
                # ── Record exited symbol for same-strike guard ────────────────
                # Only block same strike after LOSS exits (HT flip, SL, max-loss)
                # NOT after profit exits (spike reversal, profit lock) — allow re-entry
                if pnl < 0:
                    _last_exited_symbol[instrument] = symbol
                    print(f"🚫 {instrument} {symbol} blocked (loss exit) — same strike won't re-enter today", flush=True)

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
                elif instrument == "FINNIFTY":
                    finnifty_trade_active = False
                    last_executed_signal_finnifty = None
                    finnifty_loop._carryover_done = None
                    finnifty_loop._sig_alerted    = None
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
            global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active

            
            
def analyze_performance():


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
    global _daily_target_exited, _profit_protection_floor
    global _whipsaw_pause_until, _flip_timestamps
    global _loss_streak, _win_streak, _blocked_strikes

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

            # Low balance guard — only SENSEX when equity < ₹1,500
            try:
                _eq_bal = float(kite.margins().get("equity", {}).get("available", {}).get("live_balance", 0) or 0)
                if _eq_bal > 0 and _eq_bal < LOW_BALANCE_THRESHOLD:
                    _lb_key = f"_lb_logged_NIFTY"
                    if not getattr(nifty_loop, _lb_key, False) or time.time() - getattr(nifty_loop, _lb_key+"_t", 0) > 300:
                        setattr(nifty_loop, _lb_key, True)
                        setattr(nifty_loop, _lb_key+"_t", time.time())
                        print(f"💸 Balance ₹{_eq_bal:.0f} < ₹{LOW_BALANCE_THRESHOLD} — NIFTY skipped (SENSEX only)", flush=True)
                    time.sleep(60)
                    continue
            except Exception:
                pass

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

            # ── Before 9:15 AM: market not yet open ──────────────────────────
            if now_dt.hour == 9 and now_dt.minute < 20:
                print("⏳ NIFTY: waiting until 9:20 AM — first candle still forming...", flush=True)
                time.sleep(30)
                continue

            # ── After 3:20 PM: block all NEW entries (force-close time) ─────────
            # Force close fires at 3:20 PM. Don't allow a fresh/carry-over entry
            # in the 3:20–3:30 PM window — it would get immediately force-closed.
            if now_dt.hour > 15 or (now_dt.hour == 15 and now_dt.minute >= 20):
                print("🛑 NIFTY past 3:20 PM — no new entries, sleeping until tomorrow 9:00 AM")
                time.sleep(60)   # check every minute, loop will skip until morning
                continue

            # Reset daily stats at start of new trading day
            reset_daily_pnl()

            # Loss streak cooldown — pause then RESET (same fix as CRUDE).
            if _loss_streak["NIFTY"] >= 3:
                print("⚠️ Loss streak >= 3 — pausing NIFTY 15 min then resetting streak", flush=True)
                send_message("❌ NIFTY: 3 consecutive losses — pausing 15 min")
                time.sleep(900)
                _loss_streak["NIFTY"] = 0
                print("♻️ NIFTY loss streak reset — resuming trading", flush=True)
                continue

            # ══════════════════════════════════════════════════════════════
            # 🔒  HARD SAME-DIRECTION GUARD
            # Only skip if the open position MATCHES the current HalfTrend direction.
            # If opposite position is open, fall through so Layer 1 can flip it.
            with lock:
                _trade_active  = nifty_trade_active or nifty_position["active"]
                _active_signal = nifty_position.get("signal")
            if _trade_active:
                _curr_trend = int(cached_nifty_ht.iloc[-2]["trend"]) if cached_nifty_ht is not None else -1
                _curr_sig   = "CALL" if _curr_trend == 0 else "PUT"
                if _active_signal == _curr_sig:
                    print(f"⏭️ NIFTY HARD GUARD: trade active ({_active_signal}) matches HT ({_curr_sig}) — waiting", flush=True)
                    time.sleep(10)
                    continue
                elif _active_signal is None:
                    # Trade just finished — run_trade_wrapper.finally may still be running
                    # Wait briefly for it to complete and clear nifty_trade_active
                    print(f"⏭️ NIFTY HARD GUARD: nifty_trade_active=True but signal=None — finishing cleanup, waiting 2s", flush=True)
                    time.sleep(2)
                    continue
                print(f"⚠️ NIFTY: open {_active_signal} but HT={_curr_sig} — running flip check", flush=True)

            # Refresh data cache every 30 seconds — 5-minute bars for faster arrow detection
            if time.time() - last_fetch_nifty > 30 or cached_nifty_df is None:
                cached_nifty_df = get_cached_data(config.NIFTY_TOKEN, "15minute", HT_LOOKBACK_CANDLES)
                if cached_nifty_df is not None and len(cached_nifty_df) >= 120:
                    cached_nifty_ht = halftrend_tv(cached_nifty_df, amplitude=HT_AMPLITUDE, channel_deviation=2)
                last_fetch_nifty = time.time()

            if cached_nifty_df is None or len(cached_nifty_df) < 120 or cached_nifty_ht is None:
                print(f"⚠️ NIFTY: insufficient data — df={len(cached_nifty_df) if cached_nifty_df is not None else 'None'} bars, ht={'ok' if cached_nifty_ht is not None else 'None'}", flush=True)
                time.sleep(10)
                continue

            ht_df = cached_nifty_ht
            current_trend = int(ht_df.iloc[-2]["trend"])
            print(f"🧠 NIFTY trend={'CALL(bullish)' if current_trend == 0 else 'PUT(bearish)'}  bars={len(ht_df)}  time={datetime.now(IST).strftime('%H:%M:%S')}", flush=True)

            # ── Signal Detection (fresh arrow + carry-over) ───────────────────
            signal, arrow_idx, is_fresh = get_last_active_signal(ht_df)

            arrow_level = None
            if signal is not None and arrow_idx is not None:
                arrow_bar  = ht_df.iloc[arrow_idx]
                arrow_level = arrow_bar["atrLow"] if signal == "CALL" else arrow_bar["atrHigh"]
                bars_ago   = len(ht_df) - arrow_idx - 2
                # Block carry-over signals from previous days
                _arrow_date = pd.to_datetime(arrow_bar["date"]).date() if "date" in arrow_bar else None
                _today_date = datetime.now(IST).date()
                if not is_fresh and _arrow_date and _arrow_date < _today_date:
                    print(f"🚫 NIFTY carry-over blocked — signal from {_arrow_date} (previous day)", flush=True)
                    time.sleep(10)
                    continue


                # Block carry-over signals from previous days
                _arrow_date = pd.to_datetime(arrow_bar["date"]).date() if "date" in arrow_bar else None
                _today_date = datetime.now(IST).date()
                if not is_fresh and _arrow_date and _arrow_date < _today_date:
                    print(f"🚫 NIFTY carry-over blocked — signal from {_arrow_date} (previous day)", flush=True)
                    time.sleep(10)
                    continue

                if is_fresh:
                    tag = "🟢 FRESH" if signal == "CALL" else "🔴 FRESH"
                    print(f"{tag} NIFTY {signal} @ {arrow_level:.2f}  HT={arrow_bar['ht']:.2f}", flush=True)
                else:
                    tag = "🟢 CARRY-OVER" if signal == "CALL" else "🔴 CARRY-OVER"
                    print(f"{tag} NIFTY {signal} — {bars_ago} bars ({bars_ago*15} min ago) @ {arrow_level:.2f}", flush=True)

            if signal is None:
                status = "NO_ARROW_NIFTY"
                if last_status != status or time.time() - last_weak_log_time > 60:
                    trend_name = "BULLISH" if int(ht_df.iloc[-2]["trend"]) == 0 else "BEARISH"
                    print(f"⏸️ NIFTY: trend={trend_name} but no valid arrow in last 120 bars — waiting", flush=True)
                    last_status = status
                    last_weak_log_time = time.time()
                time.sleep(10)
                continue

            # ── Daily trade limit check ───────────────────────────────────────
            print(f"📊 NIFTY trades today: {nifty_trade_count}/{MAX_NIFTY_TRADES_PER_DAY}", flush=True)
            if nifty_trade_count >= MAX_NIFTY_TRADES_PER_DAY:
                _lim_key = f"NIFTY_limit_{datetime.now(IST).strftime('%Y-%m-%d')}"
                if getattr(nifty_loop, "_limit_alerted", None) != _lim_key:
                    nifty_loop._limit_alerted = _lim_key
                    print(f"🔒 NIFTY: {nifty_trade_count}/{MAX_NIFTY_TRADES_PER_DAY} trades done for today", flush=True)
                    send_message(f"🔒 NIFTY: {MAX_NIFTY_TRADES_PER_DAY} trades done for today. Resuming tomorrow.")
                time.sleep(60)
                continue

            _just_flipped_nifty = False   # reset each iteration; set True right after flip exit

            # ── Carry-over: enter only once per day ───────────────────────────
            today_str     = datetime.now(IST).strftime("%Y-%m-%d")
            carryover_key = f"NIFTY_{signal}_{today_str}"
            if not is_fresh:
                if getattr(nifty_loop, "_carryover_done", None) == carryover_key:
                    print(f"⏭️ NIFTY carry-over already entered today ({carryover_key}) — skipping", flush=True)
                    time.sleep(10)
                    continue
            else:
                # Fresh arrow fired — reset carryover so it can re-enter if needed
                nifty_loop._carryover_done = None

            # ══════════════════════════════════════════════════════════════
            # 📐  HTF FILTER  (30-min direction must agree with 15-min signal)
            # ══════════════════════════════════════════════════════════════
            _htf_ok, _htf_reason = check_htf_filter(signal, config.NIFTY_TOKEN)
            if not _htf_ok:
                print(f"📐 NIFTY HTF block — {_htf_reason}", flush=True)
                _hkey = f"NIFTY_htf_{signal}_{datetime.now(IST).strftime('%Y-%m-%d_%H')}"
                if getattr(nifty_loop, "_htf_alerted", None) != _hkey:
                    send_message(f"📐 NIFTY HTF BLOCK\n{_htf_reason}")
                    nifty_loop._htf_alerted = _hkey
                time.sleep(30)
                continue
            print(f"   {_htf_reason}", flush=True)

            # ══════════════════════════════════════════════════════════════
            # 🔒  LAYER 1 — Kite flip detection (BEFORE filters)
            # Exit any opposite position first — exits must never be blocked
            # ══════════════════════════════════════════════════════════════
            _kite_pos_cache.pop("NIFTY", None)
            kite_pos = get_open_kite_position("NIFTY")
            if kite_pos:
                kite_sig = "CALL" if kite_pos["symbol"].endswith("CE") else "PUT"
                if kite_sig == signal:
                    with lock:
                        if not nifty_position["active"]:
                            nifty_position.update({"symbol": kite_pos["symbol"], "qty": kite_pos["qty"],
                                                   "exchange": kite_pos["exchange"], "signal": kite_sig, "active": True})
                            nifty_trade_active = True
                    time.sleep(10)
                    continue
                else:
                    print(f"🔁 NIFTY FLIP (Kite): {kite_sig} → {signal}", flush=True)
                    exit_ok = exit_position(kite_pos["symbol"], kite_pos["qty"], kite_pos["exchange"])
                    if exit_ok:
                        send_message(f"🔁 NIFTY flip exit\nClosed: {kite_sig} ({kite_pos['symbol']})\nNew signal: {signal}")
                        _kite_pos_cache.pop("NIFTY", None)
                    else:
                        print("⚠️ NIFTY flip exit failed — retrying", flush=True)
                        time.sleep(5)
                        continue
                    with lock:
                        nifty_position.update({"symbol": None, "qty": 0, "exchange": None, "signal": None, "active": False})
                        nifty_trade_active = False
                        global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
                        last_executed_signal_nifty = None
                    _just_flipped_nifty = True
                    record_flip_and_check_whipsaw("NIFTY")
                    # Increment flip counter — next Claude call gets fresh evaluation
                    _claude_flip_counter["NIFTY"] = _claude_flip_counter.get("NIFTY", 0) + 1
                    for _k in list(_claude_filter_cache.keys()):
                        if _k.startswith("NIFTY_"):
                            _claude_filter_cache.pop(_k, None)
                    time.sleep(3)
            else:
                with lock:
                    if nifty_position["active"]:
                        nifty_position.update({"symbol": None, "qty": 0, "exchange": None, "signal": None, "active": False})
                        nifty_trade_active = False

            # ══════════════════════════════════════════════════════════════
            # 📊  STRATEGY FILTERS  (entry only — flip handled above)
            # ══════════════════════════════════════════════════════════════
            _filter_ok, _filter_reason = apply_entry_filters(
                signal, "NIFTY", cached_nifty_df, config.NIFTY_TOKEN,
                is_flip_reentry=_just_flipped_nifty,
                ht_df=cached_nifty_ht,
                hull_band_pct=get_hull_signal(cached_nifty_df)[3] or 0)

            if not _filter_ok:
                print(f"🚫 NIFTY entry blocked — {_filter_reason}", flush=True)
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
            # 🔒  ONE-ORDER-AT-A-TIME GUARD (layers 2 & 3)
            if is_fresh and signal == last_executed_signal_nifty:
                print(f"⏭️ NIFTY Layer3: fresh {signal} == last_executed={last_executed_signal_nifty} — skipping", flush=True)
                time.sleep(10)
                continue

            # ══════════════════════════════════════════════════════════════
            # 🚀  ENTRY — all guards passed, place the order
            # ══════════════════════════════════════════════════════════════
            with lock:
                if nifty_trade_active:
                    already_active = True
                else:
                    already_active = False
                    nifty_trade_active = True
                    global_trade_active = True

            if already_active:
                print(f"⏭️ NIFTY: nifty_trade_active=True — skipping", flush=True)
                time.sleep(10)
                continue

            # ── Profit-lock exit cooldown (15 min) ────────────────────────────
            _pl_exit_ts = _profit_lock_exit_time.get("NIFTY", 0)
            _pl_wait = 300 - (time.time() - _pl_exit_ts)  # 5 min cooldown after profit lock
            if _pl_wait > 0:
                print(f"⏳ NIFTY profit-lock cooldown — {int(_pl_wait)}s remaining before next entry", flush=True)
                with lock:
                    nifty_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
                time.sleep(min(_pl_wait, 60))
                continue

            print(f"🚀 NIFTY all guards passed — calling find_option({signal})", flush=True)
            symbol, price, lot, exchange = find_option(signal, "NIFTY")

            if not symbol or price is None:
                print(f"❌ NIFTY find_option returned None — no valid option found for {signal}", flush=True)
                with lock:
                    nifty_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
                time.sleep(10)
                continue

            # ── Same-strike guard: block immediate re-entry of just-exited symbol ──
            # Only blocks carry-over (is_fresh=False). Fresh arrows always override.
            # Same-strike block — never re-enter same strike that was exited today
            if symbol == _last_exited_symbol.get("NIFTY") and not is_fresh:
                print(f"🚫 NIFTY same-strike blocked for today: {symbol} — find_option will pick different strike", flush=True)
                with lock:
                    nifty_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
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
                        "qty":      get_quantity(lot, exchange, "NIFTY"),
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

                # Always mark carryover_key so any future carry-over of the
                # same direction today is blocked — even if this was a FRESH entry.
                nifty_loop._carryover_done = carryover_key

                if not is_fresh:
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
                    global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active

        except Exception as e:
            err_str = str(e)
            print("❌ NIFTY LOOP ERROR:", err_str, flush=True)
            if any(x in err_str.lower() for x in ["token", "403", "unauthorized", "invalid api key"]):
                _tk = f"NIFTY_auth_{datetime.now(IST).strftime('%Y-%m-%d_%H')}"
                if getattr(nifty_loop, "_token_alerted", None) != _tk:
                    nifty_loop._token_alerted = _tk
                    send_message(f"❌ NIFTY: Kite auth error — token expired\nError: {err_str[:150]}\nAction: Redeploy or whitelist IP")
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
    global _daily_target_exited, _profit_protection_floor
    global _whipsaw_pause_until, _flip_timestamps
    global _loss_streak, _win_streak, _blocked_strikes

    _crude_weekend_msg_sent = [False]

    while True:
        try:
            # ── Instrument kill-switch ────────────────────────────────────────
            if not ENABLE_CRUDE:
                global _crude_disabled_logged
                if not _crude_disabled_logged:
                    print("⛔ CRUDE trading disabled (ENABLE_CRUDE=False)", flush=True)
                    _crude_disabled_logged = True
                time.sleep(60)
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
            if _loss_streak["CRUDE"] >= 3:
                print("⚠️ Loss streak >= 3 — pausing CRUDE 15 min then resetting streak", flush=True)
                send_message("❌ CRUDE: 3 consecutive losses — pausing 15 min")
                time.sleep(900)   # 15-minute cooldown
                _loss_streak["CRUDE"] = 0
                print("♻️ CRUDE loss streak reset — resuming trading", flush=True)
                continue

            # ── HARD SAME-DIRECTION GUARD ─────────────────────────────────────
            with lock:
                _trade_active  = crude_trade_active or crude_position["active"]
                _active_signal = crude_position.get("signal")
            if _trade_active:
                _curr_trend = int(cached_crude_ht.iloc[-2]["trend"]) if cached_crude_ht is not None else -1
                _curr_sig   = "CALL" if _curr_trend == 0 else "PUT"
                if _active_signal == _curr_sig:
                    time.sleep(10)
                    continue
                print(f"⚠️ CRUDE: open {_active_signal} but HT={_curr_sig} — running flip check", flush=True)

            # Refresh data cache every 20 seconds
            if time.time() - last_fetch_crude > 20 or cached_crude_15m is None:
                cached_crude_15m = get_cached_data(CRUDE_TOKEN, "15minute", 600)
                # Recompute HalfTrend only when data refreshes
                if cached_crude_15m is not None and len(cached_crude_15m) >= 50:
                    cached_crude_ht = halftrend_tv(cached_crude_15m, amplitude=HT_AMPLITUDE, channel_deviation=2)
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
                    # Block carry-over signals from previous days
                    _arrow_date = pd.to_datetime(arrow_bar["date"]).date() if "date" in arrow_bar else None
                    _today_date = datetime.now(IST).date()
                    if not is_fresh and _arrow_date and _arrow_date < _today_date:
                        print(f"🚫 CRUDE carry-over blocked — signal from {_arrow_date} (previous day)", flush=True)
                        time.sleep(10)
                        continue

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
                _lim_key = f"CRUDE_limit_{datetime.now(IST).strftime('%Y-%m-%d')}"
                if getattr(crude_loop, "_limit_alerted", None) != _lim_key:
                    crude_loop._limit_alerted = _lim_key
                    print(f"🔒 CRUDE: {crude_trade_count}/{MAX_CRUDE_TRADES_PER_DAY} trades done for today", flush=True)
                    send_message(f"🔒 CRUDE: {MAX_CRUDE_TRADES_PER_DAY} trades done for today. Resuming tomorrow.")
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
            # 📐  HTF FILTER  (30-min direction must agree with 15-min signal)
            # ══════════════════════════════════════════════════════════════
            _htf_ok, _htf_reason = check_htf_filter(signal, CRUDE_TOKEN)
            if not _htf_ok:
                print(f"📐 CRUDE HTF block — {_htf_reason}", flush=True)
                _hkey = f"CRUDE_htf_{signal}_{datetime.now(IST).strftime('%Y-%m-%d_%H')}"
                if getattr(crude_loop, "_htf_alerted", None) != _hkey:
                    send_message(f"📐 CRUDE HTF BLOCK\n{_htf_reason}")
                    crude_loop._htf_alerted = _hkey
                time.sleep(30)
                continue
            print(f"   {_htf_reason}", flush=True)

            # ══════════════════════════════════════════════════════════════
            # 🔒  LAYER 1 — Kite flip detection (BEFORE filters)
            # ══════════════════════════════════════════════════════════════
            _kite_pos_cache.pop("CRUDE", None)
            kite_pos = get_open_kite_position("CRUDE")
            if kite_pos:
                kite_sig = "CALL" if kite_pos["symbol"].endswith("CE") else "PUT"
                if kite_sig == signal:
                    with lock:
                        if not crude_position["active"]:
                            crude_position.update({"symbol": kite_pos["symbol"], "qty": kite_pos["qty"],
                                                   "exchange": kite_pos["exchange"], "signal": kite_sig, "active": True})
                            crude_trade_active = True
                    time.sleep(10)
                    continue
                else:
                    print(f"🔁 CRUDE FLIP (Kite): {kite_sig} → {signal}", flush=True)
                    exit_ok = exit_position(kite_pos["symbol"], kite_pos["qty"], kite_pos["exchange"])
                    if exit_ok:
                        send_message(f"🔁 CRUDE flip exit\nClosed: {kite_sig} ({kite_pos['symbol']})\nNew signal: {signal}")
                        _kite_pos_cache.pop("CRUDE", None)
                    else:
                        print("⚠️ CRUDE flip exit failed — retrying", flush=True)
                        time.sleep(5)
                        continue
                    with lock:
                        crude_position.update({"symbol": None, "qty": 0, "exchange": None, "signal": None, "active": False})
                        crude_trade_active = False
                        global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
                        last_executed_signal_crude = None
                    crude_loop._carryover_done = None
                    crude_loop._sig_alerted = None
                    _just_flipped_crude = True
                    time.sleep(3)
            else:
                with lock:
                    if crude_position["active"]:
                        crude_position.update({"symbol": None, "qty": 0, "exchange": None, "signal": None, "active": False})
                        crude_trade_active = False

            # ══════════════════════════════════════════════════════════════
            # 📊  STRATEGY FILTERS (entry only — flip handled above)
            # ══════════════════════════════════════════════════════════════
            _just_flipped_crude = getattr(crude_loop, "_just_flipped", False)
            crude_loop._just_flipped = False
            _filter_ok, _filter_reason = apply_entry_filters(
                signal, "CRUDE", cached_crude_15m, CRUDE_TOKEN,
                is_flip_reentry=_just_flipped_crude,
                ht_df=cached_crude_ht,
                hull_band_pct=get_hull_signal(cached_crude_15m)[3] or 0)

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
            # 🔒  ONE-ORDER-AT-A-TIME GUARD (layers 2 & 3)
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
                        global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
                        last_executed_signal_crude = None
                    crude_loop._carryover_done = None
                    crude_loop._sig_alerted = None
                    _just_flipped_crude = True   # ← skip Hull check on re-entry
                    time.sleep(3)

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
                    global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
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
                            "qty":      get_quantity(lot, exchange, "CRUDE"),
                            "exchange": exchange,
                            "signal":   signal,
                            "active":   True,
                        })
                        last_executed_signal_crude = signal
                        # NOTE: legacy shared globals (last_running_signal,
                        # current_symbol, current_qty, current_exchange) are no
                        # longer written here — see nifty_loop comment above.

                    _kite_pos_cache.pop("CRUDE", None)   # invalidate position cache

                    # Always mark carryover_done so carry-over re-entry is blocked
                    crude_loop._carryover_done = carryover_key

                    if not is_fresh:
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
                        global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
            else:
                with lock:
                    crude_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active

        except Exception as e:
            err_str = str(e)
            print("❌ CRUDE LOOP ERROR:", err_str, flush=True)
            if any(x in err_str.lower() for x in ["token", "403", "unauthorized", "invalid api key"]):
                _tk = f"CRUDE_auth_{datetime.now(IST).strftime('%Y-%m-%d_%H')}"
                if getattr(crude_loop, "_token_alerted", None) != _tk:
                    crude_loop._token_alerted = _tk
                    send_message(f"❌ CRUDE: Kite auth error — token expired\nError: {err_str[:150]}\nAction: Redeploy or whitelist IP")
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
    global _daily_target_exited, _profit_protection_floor
    global _whipsaw_pause_until, _flip_timestamps
    global _loss_streak, _win_streak, _blocked_strikes

    _bn_weekend_msg_sent = [False]
    _bn_wakeup_msg_sent  = [False]

    while True:
        try:
            # ── Instrument kill-switch ────────────────────────────────────────
            if not ENABLE_BANKNIFTY:
                global _banknifty_disabled_logged
                if not _banknifty_disabled_logged:
                    print("⛔ BANKNIFTY trading disabled (ENABLE_BANKNIFTY=False)", flush=True)
                    _banknifty_disabled_logged = True
                time.sleep(60)
                continue

            # Low balance guard
            try:
                _eq_bal = float(kite.margins().get("equity", {}).get("available", {}).get("live_balance", 0) or 0)
                if _eq_bal > 0 and _eq_bal < LOW_BALANCE_THRESHOLD:
                    print(f"Low balance BANKNIFTY skipped", flush=True)
                    time.sleep(60)
                    continue
            except Exception:
                pass

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

            # ── Before 9:30 AM: wait for market to stabilise ─────────────────
            if now_dt.hour == 9 and now_dt.minute < 20:
                print("⏳ BANKNIFTY: waiting until 9:20 AM — first candle still forming...", flush=True)
                time.sleep(30)
                continue

            # ── After 3:20 PM: block all NEW entries (force-close time) ─────────
            if now_dt.hour > 15 or (now_dt.hour == 15 and now_dt.minute >= 20):
                print("🛑 BANKNIFTY past 3:20 PM — no new entries, sleeping until tomorrow 9:00 AM")
                time.sleep(60)
                continue

            # Reset daily stats at start of new trading day
            reset_daily_pnl()

            # Loss streak cooldown
            if _loss_streak["BANKNIFTY"] >= 3:
                print("⚠️ Loss streak >= 3 — pausing BANKNIFTY 15 min then resetting streak", flush=True)
                send_message("❌ BANKNIFTY: 3 consecutive losses — pausing 15 min")
                time.sleep(900)
                _loss_streak["BANKNIFTY"] = 0
                print("♻️ BANKNIFTY loss streak reset — resuming trading", flush=True)
                continue

            # ── HARD SAME-DIRECTION GUARD ─────────────────────────────────────
            with lock:
                _trade_active  = banknifty_trade_active or banknifty_position["active"]
                _active_signal = banknifty_position.get("signal")
            if _trade_active:
                _curr_trend = int(cached_banknifty_ht.iloc[-2]["trend"]) if cached_banknifty_ht is not None else -1
                _curr_sig   = "CALL" if _curr_trend == 0 else "PUT"
                if _active_signal == _curr_sig:
                    time.sleep(10)
                    continue
                print(f"⚠️ BANKNIFTY: open {_active_signal} but HT={_curr_sig} — running flip check", flush=True)

            # Refresh data cache every 30 seconds — 5-minute bars for faster arrow detection
            if time.time() - last_fetch_banknifty > 30 or cached_banknifty_df is None:
                cached_banknifty_df = get_cached_data(BANKNIFTY_TOKEN, "15minute", HT_LOOKBACK_CANDLES)
                if cached_banknifty_df is not None and len(cached_banknifty_df) >= 120:
                    cached_banknifty_ht = halftrend_tv(cached_banknifty_df, amplitude=HT_AMPLITUDE, channel_deviation=2)
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
                    # Block carry-over signals from previous days
                    _arrow_date = pd.to_datetime(arrow_bar["date"]).date() if "date" in arrow_bar else None
                    _today_date = datetime.now(IST).date()
                    if not is_fresh and _arrow_date and _arrow_date < _today_date:
                        print(f"🚫 BANKNIFTY carry-over blocked — signal from {_arrow_date} (previous day)", flush=True)
                        time.sleep(10)
                        continue

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
                _lim_key = f"BANKNIFTY_limit_{datetime.now(IST).strftime('%Y-%m-%d')}"
                if getattr(banknifty_loop, "_limit_alerted", None) != _lim_key:
                    banknifty_loop._limit_alerted = _lim_key
                    print(f"🔒 BANKNIFTY: {banknifty_trade_count}/{MAX_BANKNIFTY_TRADES_PER_DAY} trades done for today", flush=True)
                    send_message(f"🔒 BANKNIFTY: {MAX_BANKNIFTY_TRADES_PER_DAY} trades done for today. Resuming tomorrow.")
                time.sleep(60)
                continue

            # ── Signal detected — log only, no Telegram ──────────────────────
            if signal is not None and arrow_idx is not None:
                arrow_bar = ht_df.iloc[arrow_idx]
                _level = arrow_bar["atrLow"] if signal == "CALL" else arrow_bar["atrHigh"]
                _bars_ago = len(ht_df) - arrow_idx - 2
                # Block carry-over signals from previous days
                _arrow_date = pd.to_datetime(arrow_bar["date"]).date() if "date" in arrow_bar else None
                _today_date = datetime.now(IST).date()
                if not is_fresh and _arrow_date and _arrow_date < _today_date:
                    print(f"🚫 BANKNIFTY carry-over blocked — signal from {_arrow_date} (previous day)", flush=True)
                    time.sleep(10)
                    continue

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
            # 📐  HTF FILTER  (30-min direction must agree with 15-min signal)
            # ══════════════════════════════════════════════════════════════
            _htf_ok, _htf_reason = check_htf_filter(signal, BANKNIFTY_TOKEN)
            if not _htf_ok:
                print(f"📐 BANKNIFTY HTF block — {_htf_reason}", flush=True)
                _hkey = f"BANKNIFTY_htf_{signal}_{datetime.now(IST).strftime('%Y-%m-%d_%H')}"
                if getattr(banknifty_loop, "_htf_alerted", None) != _hkey:
                    send_message(f"📐 BANKNIFTY HTF BLOCK\n{_htf_reason}")
                    banknifty_loop._htf_alerted = _hkey
                time.sleep(30)
                continue
            print(f"   {_htf_reason}", flush=True)

            # ══════════════════════════════════════════════════════════════
            # 🔒  LAYER 1 — Kite flip detection (BEFORE filters)
            # ══════════════════════════════════════════════════════════════
            _kite_pos_cache.pop("BANKNIFTY", None)
            kite_pos = get_open_kite_position("BANKNIFTY")
            if kite_pos:
                kite_sig = "CALL" if kite_pos["symbol"].endswith("CE") else "PUT"
                if kite_sig == signal:
                    with lock:
                        if not banknifty_position["active"]:
                            banknifty_position.update({"symbol": kite_pos["symbol"], "qty": kite_pos["qty"],
                                                       "exchange": kite_pos["exchange"], "signal": kite_sig, "active": True})
                            banknifty_trade_active = True
                    time.sleep(10)
                    continue
                else:
                    print(f"🔁 BANKNIFTY FLIP (Kite): {kite_sig} → {signal}", flush=True)
                    exit_ok = exit_position(kite_pos["symbol"], kite_pos["qty"], kite_pos["exchange"])
                    if exit_ok:
                        send_message(f"🔁 BANKNIFTY flip exit\nClosed: {kite_sig} ({kite_pos['symbol']})\nNew signal: {signal}")
                        _kite_pos_cache.pop("BANKNIFTY", None)
                    else:
                        print("⚠️ BANKNIFTY flip exit failed — retrying", flush=True)
                        time.sleep(5)
                        continue
                    with lock:
                        banknifty_position.update({"symbol": None, "qty": 0, "exchange": None, "signal": None, "active": False})
                        banknifty_trade_active = False
                        global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
                        last_executed_signal_banknifty = None
                    banknifty_loop._carryover_done = None
                    banknifty_loop._sig_alerted    = None
                    banknifty_loop._just_flipped   = True
                    time.sleep(3)
            else:
                with lock:
                    if banknifty_position["active"]:
                        banknifty_position.update({"symbol": None, "qty": 0, "exchange": None, "signal": None, "active": False})
                        banknifty_trade_active = False

            # ══════════════════════════════════════════════════════════════
            # 📊  STRATEGY FILTERS (entry only — flip handled above)
            # ══════════════════════════════════════════════════════════════
            _just_flipped_bn = getattr(banknifty_loop, "_just_flipped", False)
            banknifty_loop._just_flipped = False
            _filter_ok, _filter_reason = apply_entry_filters(
                signal, "BANKNIFTY", cached_banknifty_df, BANKNIFTY_TOKEN,
                is_flip_reentry=_just_flipped_bn,
                ht_df=cached_banknifty_ht,
                hull_band_pct=get_hull_signal(cached_banknifty_df)[3] or 0)

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
            # 🔒  ONE-ORDER-AT-A-TIME GUARD (layers 2 & 3)
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
                        global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
                        last_executed_signal_banknifty = None
                    banknifty_loop._carryover_done = None
                    banknifty_loop._sig_alerted    = None
                    banknifty_loop._just_flipped   = True   # ← skip Hull on re-entry
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

            # ── Profit-lock exit cooldown (15 min) ────────────────────────────
            _pl_exit_ts = _profit_lock_exit_time.get("BANKNIFTY", 0)
            _pl_wait = 300 - (time.time() - _pl_exit_ts)  # 5 min cooldown after profit lock
            if _pl_wait > 0:
                print(f"⏳ BANKNIFTY profit-lock cooldown — {int(_pl_wait)}s remaining before next entry", flush=True)
                with lock:
                    banknifty_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
                time.sleep(min(_pl_wait, 60))
                continue

            print(f"🧠 BANKNIFTY entering: {signal}")
            symbol, price, lot, exchange = find_option(signal, "BANKNIFTY")

            if not symbol or price is None:
                with lock:
                    banknifty_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
                time.sleep(10)
                continue

            # ── Same-strike guard ─────────────────────────────────────────────
            if symbol == _last_exited_symbol.get("BANKNIFTY") and not is_fresh:
                print(f"🚫 BANKNIFTY same-strike block: {symbol} was just exited — waiting for new arrow")
                with lock:
                    banknifty_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
                time.sleep(30)
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

                # Always mark carryover_done so carry-over re-entry is blocked
                banknifty_loop._carryover_done = carryover_key

                if not is_fresh:
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
                    global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active

        except Exception as e:
            err_str = str(e)
            print("❌ BANKNIFTY LOOP ERROR:", err_str, flush=True)
            if any(x in err_str.lower() for x in ["token", "403", "unauthorized", "invalid api key"]):
                _tk = f"BN_auth_{datetime.now(IST).strftime('%Y-%m-%d_%H')}"
                if getattr(banknifty_loop, "_token_alerted", None) != _tk:
                    banknifty_loop._token_alerted = _tk
                    send_message(f"❌ BANKNIFTY: Kite auth error — token expired\nError: {err_str[:150]}\nAction: Redeploy or whitelist IP")
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

# =====================================================
# 📊 FINNIFTY LOOP
# =====================================================

def finnifty_loop():
    global last_executed_signal_finnifty, global_trade_active
    global last_status, last_weak_log_time
    global last_fetch_finnifty, cached_finnifty_df, cached_finnifty_ht
    global finnifty_trade_active, finnifty_position
    global win_streak, loss_streak
    global _daily_target_exited, _profit_protection_floor
    global _whipsaw_pause_until, _flip_timestamps
    global _loss_streak, _win_streak, _blocked_strikes

    _fn_weekend_msg_sent = [False]
    _fn_wakeup_msg_sent  = [False]

    while True:
        try:
            # ── Instrument kill-switch ────────────────────────────────────────
            if not ENABLE_FINNIFTY:
                global _finnifty_disabled_logged
                if not _finnifty_disabled_logged:
                    print("⛔ FINNIFTY trading disabled (ENABLE_FINNIFTY=False)", flush=True)
                    _finnifty_disabled_logged = True
                time.sleep(60)
                continue

            # Low balance guard
            try:
                _eq_bal = float(kite.margins().get("equity", {}).get("available", {}).get("live_balance", 0) or 0)
                if _eq_bal > 0 and _eq_bal < LOW_BALANCE_THRESHOLD:
                    print(f"Low balance FINNIFTY skipped", flush=True)
                    time.sleep(60)
                    continue
            except Exception:
                pass

            now_dt = datetime.now(IST)

            # ── Weekend: sleep and do nothing ────────────────────────────────
            if now_dt.weekday() >= 5:
                if not _fn_weekend_msg_sent[0]:
                    send_message("😴 FINNIFTY: Weekend — bot sleeping until Monday 8:55 AM IST")
                    _fn_weekend_msg_sent[0] = True
                    _fn_wakeup_msg_sent[0]  = False
                time.sleep(300)
                continue

            # ── Pre-market: before 9:00 AM on weekdays ───────────────────────
            if now_dt.hour < 9:
                _fn_weekend_msg_sent[0] = False
                if not _fn_wakeup_msg_sent[0] and now_dt.weekday() == 0:
                    send_message("🌅 FINNIFTY: Monday — bot active, waiting for 9:15 AM market open")
                    _fn_wakeup_msg_sent[0] = True
                time.sleep(60)
                continue

            # ── Before 9:30 AM: wait for market to stabilise ─────────────────
            if now_dt.hour == 9 and now_dt.minute < 20:
                print("⏳ FINNIFTY: waiting until 9:20 AM — first candle still forming...", flush=True)
                time.sleep(30)
                continue

            # ── After 3:20 PM: block all NEW entries (force-close time) ─────────
            if now_dt.hour > 15 or (now_dt.hour == 15 and now_dt.minute >= 20):
                print("🛑 FINNIFTY past 3:20 PM — no new entries, sleeping until tomorrow 9:00 AM")
                time.sleep(60)
                continue

            # Reset daily stats at start of new trading day
            reset_daily_pnl()

            # Loss streak cooldown
            if _loss_streak["FINNIFTY"] >= 3:
                print("⚠️ Loss streak >= 3 — pausing FINNIFTY 15 min then resetting streak", flush=True)
                send_message("❌ FINNIFTY: 3 consecutive losses — pausing 15 min")
                time.sleep(900)
                _loss_streak["FINNIFTY"] = 0
                print("♻️ FINNIFTY loss streak reset — resuming trading", flush=True)
                continue

            # ── HARD SAME-DIRECTION GUARD ─────────────────────────────────────
            with lock:
                _trade_active  = finnifty_trade_active or finnifty_position["active"]
                _active_signal = finnifty_position.get("signal")
            if _trade_active:
                _curr_trend = int(cached_finnifty_ht.iloc[-2]["trend"]) if cached_finnifty_ht is not None else -1
                _curr_sig   = "CALL" if _curr_trend == 0 else "PUT"
                if _active_signal == _curr_sig:
                    time.sleep(10)
                    continue
                print(f"⚠️ BANKNIFTY: open {_active_signal} but HT={_curr_sig} — running flip check", flush=True)

            # Refresh data cache every 30 seconds — 5-minute bars for faster arrow detection
            if time.time() - last_fetch_finnifty > 30 or cached_finnifty_df is None:
                cached_finnifty_df = get_cached_data(FINNIFTY_TOKEN, "15minute", HT_LOOKBACK_CANDLES)
                if cached_finnifty_df is not None and len(cached_finnifty_df) >= 120:
                    cached_finnifty_ht = halftrend_tv(cached_finnifty_df, amplitude=HT_AMPLITUDE, channel_deviation=2)
                last_fetch_finnifty = time.time()

            if cached_finnifty_df is None or len(cached_finnifty_df) < 120 or cached_finnifty_ht is None:
                time.sleep(10)
                continue

            ht_df = cached_finnifty_ht
            current_trend = int(ht_df.iloc[-2]["trend"])
            print("🧠 FINNIFTY Trend:", "CALL" if current_trend == 0 else "PUT")

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
                    # Block carry-over signals from previous days
                    _arrow_date = pd.to_datetime(arrow_bar["date"]).date() if "date" in arrow_bar else None
                    _today_date = datetime.now(IST).date()
                    if not is_fresh and _arrow_date and _arrow_date < _today_date:
                        print(f"🚫 FINNIFTY carry-over blocked — signal from {_arrow_date} (previous day)", flush=True)
                        time.sleep(10)
                        continue

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
            if banknifty_trade_count >= MAX_FINNIFTY_TRADES_PER_DAY:
                _lim_key = f"BANKNIFTY_limit_{datetime.now(IST).strftime('%Y-%m-%d')}"
                if getattr(banknifty_loop, "_limit_alerted", None) != _lim_key:
                    finnifty_loop._limit_alerted = _lim_key
                    print(f"🔒 BANKNIFTY: {banknifty_trade_count}/{MAX_FINNIFTY_TRADES_PER_DAY} trades done for today", flush=True)
                    send_message(f"🔒 BANKNIFTY: {MAX_FINNIFTY_TRADES_PER_DAY} trades done for today. Resuming tomorrow.")
                time.sleep(60)
                continue

            # ── Signal detected — log only, no Telegram ──────────────────────
            if signal is not None and arrow_idx is not None:
                arrow_bar = ht_df.iloc[arrow_idx]
                _level = arrow_bar["atrLow"] if signal == "CALL" else arrow_bar["atrHigh"]
                _bars_ago = len(ht_df) - arrow_idx - 2
                # Block carry-over signals from previous days
                _arrow_date = pd.to_datetime(arrow_bar["date"]).date() if "date" in arrow_bar else None
                _today_date = datetime.now(IST).date()
                if not is_fresh and _arrow_date and _arrow_date < _today_date:
                    print(f"🚫 FINNIFTY carry-over blocked — signal from {_arrow_date} (previous day)", flush=True)
                    time.sleep(10)
                    continue

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
            # 📐  HTF FILTER  (30-min direction must agree with 15-min signal)
            # ══════════════════════════════════════════════════════════════
            _htf_ok, _htf_reason = check_htf_filter(signal, FINNIFTY_TOKEN)
            if not _htf_ok:
                print(f"📐 BANKNIFTY HTF block — {_htf_reason}", flush=True)
                _hkey = f"BANKNIFTY_htf_{signal}_{datetime.now(IST).strftime('%Y-%m-%d_%H')}"
                if getattr(banknifty_loop, "_htf_alerted", None) != _hkey:
                    send_message(f"📐 BANKNIFTY HTF BLOCK\n{_htf_reason}")
                    banknifty_loop._htf_alerted = _hkey
                time.sleep(30)
                continue
            print(f"   {_htf_reason}", flush=True)

            # ══════════════════════════════════════════════════════════════
            # 🔒  LAYER 1 — Kite flip detection (BEFORE filters)
            # ══════════════════════════════════════════════════════════════
            _kite_pos_cache.pop("FINNIFTY", None)
            kite_pos = get_open_kite_position("FINNIFTY")
            if kite_pos:
                kite_sig = "CALL" if kite_pos["symbol"].endswith("CE") else "PUT"
                if kite_sig == signal:
                    with lock:
                        if not finnifty_position["active"]:
                            finnifty_position.update({"symbol": kite_pos["symbol"], "qty": kite_pos["qty"],
                                                       "exchange": kite_pos["exchange"], "signal": kite_sig, "active": True})
                            finnifty_trade_active = True
                    time.sleep(10)
                    continue
                else:
                    print(f"🔁 FINNIFTY FLIP (Kite): {kite_sig} → {signal}", flush=True)
                    exit_ok = exit_position(kite_pos["symbol"], kite_pos["qty"], kite_pos["exchange"])
                    if exit_ok:
                        send_message(f"🔁 FINNIFTY flip exit\nClosed: {kite_sig} ({kite_pos['symbol']})\nNew signal: {signal}")
                        _kite_pos_cache.pop("FINNIFTY", None)
                    else:
                        print("⚠️ FINNIFTY flip exit failed — retrying", flush=True)
                        time.sleep(5)
                        continue
                    with lock:
                        finnifty_position.update({"symbol": None, "qty": 0, "exchange": None, "signal": None, "active": False})
                        finnifty_trade_active = False
                        global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
                        last_executed_signal_finnifty = None
                    finnifty_loop._carryover_done = None
                    finnifty_loop._sig_alerted    = None
                    finnifty_loop._just_flipped   = True
                    record_flip_and_check_whipsaw("FINNIFTY")
                    time.sleep(3)
            else:
                with lock:
                    if finnifty_position["active"]:
                        finnifty_position.update({"symbol": None, "qty": 0, "exchange": None, "signal": None, "active": False})
                        finnifty_trade_active = False

            # ══════════════════════════════════════════════════════════════
            # 📊  STRATEGY FILTERS (entry only — flip handled above)
            # ══════════════════════════════════════════════════════════════
            _just_flipped_bn = getattr(banknifty_loop, "_just_flipped", False)
            finnifty_loop._just_flipped = False
            _filter_ok, _filter_reason = apply_entry_filters(
                signal, "FINNIFTY", cached_finnifty_df, FINNIFTY_TOKEN,
                is_flip_reentry=_just_flipped_bn,
                ht_df=cached_finnifty_ht,
                hull_band_pct=get_hull_signal(cached_finnifty_df)[3] or 0)

            if not _filter_ok:
                print(f"🚫 FINNIFTY entry blocked — {_filter_reason}", flush=True)
                _fkey = f"FINNIFTY_f_{datetime.now(IST).strftime('%Y-%m-%d_%H%M')[:13]}_{_filter_reason[:30]}"
                if getattr(banknifty_loop, "_filter_alerted", None) != _fkey:
                    try:
                        send_message(f"🚫 FINNIFTY ORDER BLOCKED\n{_filter_reason}")
                        finnifty_loop._filter_alerted = _fkey
                    except Exception as _e:
                        print(f"⚠️ Filter alert send failed: {_e}", flush=True)
                time.sleep(30)
                continue
            print(f"   {_filter_reason}", flush=True)

            # ══════════════════════════════════════════════════════════════
            # 🔒  ONE-ORDER-AT-A-TIME GUARD (layers 2 & 3)
            if kite_pos:
                kite_sig = "CALL" if kite_pos["symbol"].endswith("CE") else "PUT"

                if kite_sig == signal:
                    with lock:
                        if not finnifty_position["active"]:
                            finnifty_position.update({
                                "symbol":   kite_pos["symbol"],
                                "qty":      kite_pos["qty"],
                                "exchange": kite_pos["exchange"],
                                "signal":   kite_sig,
                                "active":   True,
                            })
                            finnifty_trade_active = True
                    time.sleep(10)
                    continue

                else:
                    # Open position in opposite direction → flip
                    print(f"🔁 FINNIFTY FLIP (Kite): {kite_sig} → {signal}")
                    exit_ok = exit_position(kite_pos["symbol"], kite_pos["qty"],
                                            kite_pos["exchange"])
                    if exit_ok:
                        send_message(
                            f"🔁 FINNIFTY flip exit\n"
                            f"Closed: {kite_sig} ({kite_pos['symbol']})\n"
                            f"New signal: {signal}"
                        )
                        _kite_pos_cache.pop("FINNIFTY", None)
                    else:
                        print("⚠️ FINNIFTY flip exit failed — retrying next tick")
                        time.sleep(5)
                        continue

                    with lock:
                        finnifty_position.update({"symbol": None, "qty": 0,
                                                   "exchange": None, "signal": None,
                                                   "active": False})
                        finnifty_trade_active = False
                        global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
                        last_executed_signal_finnifty = None
                    finnifty_loop._carryover_done = None
                    finnifty_loop._sig_alerted    = None
                    finnifty_loop._just_flipped   = True   # ← skip Hull on re-entry
                    time.sleep(3)

            else:
                # No live Kite position — sync in-memory if drifted
                with lock:
                    if finnifty_position["active"]:
                        print("⚠️ FINNIFTY in-memory says active but Kite shows no position — resetting")
                        finnifty_position.update({"symbol": None, "qty": 0,
                                                   "exchange": None, "signal": None,
                                                   "active": False})
                        finnifty_trade_active = False

            # Layer 2 — in-memory flag (fast path)
            with lock:
                pos_active = finnifty_position["active"]
                pos_signal = finnifty_position["signal"]

            if pos_active and pos_signal == signal:
                time.sleep(10)
                continue

            # Layer 3 — duplicate prevention for same fresh signal
            if is_fresh and signal == last_executed_signal_finnifty:
                time.sleep(10)
                continue

            # ══════════════════════════════════════════════════════════════
            # 🚀  ENTRY — all guards passed, place the order
            # ══════════════════════════════════════════════════════════════
            with lock:
                if finnifty_trade_active:
                    already_active = True
                else:
                    already_active = False
                    finnifty_trade_active = True
                    global_trade_active = True

            if already_active:
                time.sleep(10)
                continue

            # ── Profit-lock exit cooldown (15 min) ────────────────────────────
            _pl_exit_ts = _profit_lock_exit_time.get("FINNIFTY", 0)
            _pl_wait = 300 - (time.time() - _pl_exit_ts)  # 5 min cooldown after profit lock
            if _pl_wait > 0:
                print(f"⏳ FINNIFTY profit-lock cooldown — {int(_pl_wait)}s remaining before next entry", flush=True)
                with lock:
                    finnifty_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
                time.sleep(min(_pl_wait, 60))
                continue

            print(f"🧠 FINNIFTY entering: {signal}")
            symbol, price, lot, exchange = find_option(signal, "FINNIFTY")

            if not symbol or price is None:
                with lock:
                    finnifty_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
                time.sleep(10)
                continue

            # ── Same-strike guard ─────────────────────────────────────────────
            if symbol == _last_exited_symbol.get("FINNIFTY") and not is_fresh:
                print(f"🚫 FINNIFTY same-strike block: {symbol} was just exited — waiting for new arrow")
                with lock:
                    finnifty_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
                time.sleep(30)
                continue

            filled_price = place_order(symbol, lot, exchange, "FINNIFTY")

            if filled_price:
                with lock:
                    _finnifty_trade_gen[0] += 1
                    _bn_gen_id = _finnifty_trade_gen[0]
                    finnifty_position.update({
                        "symbol":   symbol,
                        "qty":      get_quantity(lot, exchange, "FINNIFTY"),
                        "exchange": exchange,
                        "signal":   signal,
                        "active":   True,
                    })
                    last_executed_signal_finnifty = signal

                _kite_pos_cache.pop("FINNIFTY", None)

                # Always mark carryover_done so carry-over re-entry is blocked
                finnifty_loop._carryover_done = carryover_key

                if not is_fresh:
                    send_message(
                        f"♻️ BANKNIFTY carry-over entry\n"
                        f"Signal: {signal} (trend continuing)\n"
                        f"{symbol} @ ₹{filled_price}"
                    )
                else:
                    send_message(f"🆕 BANKNIFTY {signal} entry\n{symbol} @ ₹{filled_price}")

                threading.Thread(
                    target=run_trade_wrapper,
                    args=(symbol, filled_price, lot, exchange, "FINNIFTY", signal, 0, "TREND"),
                    kwargs={"gen_id": _bn_gen_id},
                    daemon=True
                ).start()
                print(f"🎯 FINNIFTY Trade: {symbol} @ ₹{filled_price}  lots={lot}  gen={_bn_gen_id}")
            else:
                with lock:
                    finnifty_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active

        except Exception as e:
            err_str = str(e)
            print("❌ FINNIFTY LOOP ERROR:", err_str, flush=True)
            if any(x in err_str.lower() for x in ["token", "403", "unauthorized", "invalid api key"]):
                _tk = f"BN_auth_{datetime.now(IST).strftime('%Y-%m-%d_%H')}"
                if getattr(banknifty_loop, "_token_alerted", None) != _tk:
                    finnifty_loop._token_alerted = _tk
                    send_message(f"❌ FINNIFTY: Kite auth error — token expired\nError: {err_str[:150]}\nAction: Redeploy or whitelist IP")
            try:
                if get_open_kite_position("FINNIFTY") is None:
                    with lock:
                        finnifty_trade_active = False
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
    global _daily_target_exited, _profit_protection_floor
    global _whipsaw_pause_until, _flip_timestamps
    global _loss_streak, _win_streak, _blocked_strikes

    _sx_weekend_msg_sent = [False]
    _sx_wakeup_msg_sent  = [False]

    while True:
        try:
            # ── Instrument kill-switch ────────────────────────────────────────
            if not ENABLE_SENSEX:
                global _sensex_disabled_logged
                if not _sensex_disabled_logged:
                    print("⛔ SENSEX trading disabled (ENABLE_SENSEX=False)", flush=True)
                    _sensex_disabled_logged = True
                time.sleep(60)
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

            # ── Before 9:30 AM: wait for market to stabilise ─────────────────
            if now_dt.hour == 9 and now_dt.minute < 20:
                print("⏳ SENSEX: waiting until 9:20 AM — first candle still forming...", flush=True)
                time.sleep(30)
                continue

            # ── After 3:20 PM: block all NEW entries (force-close time) ─────────
            if now_dt.hour > 15 or (now_dt.hour == 15 and now_dt.minute >= 20):
                print("🛑 SENSEX past 3:20 PM — no new entries, sleeping until tomorrow 9:00 AM")
                time.sleep(60)
                continue

            # Reset daily stats at start of new trading day
            reset_daily_pnl()

            # Loss streak cooldown
            if _loss_streak["SENSEX"] >= 3:
                print("⚠️ Loss streak >= 3 — pausing SENSEX 15 min then resetting streak", flush=True)
                send_message("❌ SENSEX: 3 consecutive losses — pausing 15 min")
                time.sleep(900)
                _loss_streak["SENSEX"] = 0
                print("♻️ SENSEX loss streak reset — resuming trading", flush=True)
                continue

            # ── HARD SAME-DIRECTION GUARD ─────────────────────────────────────
            # Only skip if the open position MATCHES the current signal.
            # If an opposite position is open, we must NOT skip — the flip
            # exit logic at Layer 1 (below) needs to run to close it.
            with lock:
                _trade_active  = sensex_trade_active or sensex_position["active"]
                _active_signal = sensex_position.get("signal")

            if _trade_active:
                # Quick check: does open trade match current HalfTrend trend?
                _curr_trend = int(cached_sensex_ht.iloc[-2]["trend"]) if cached_sensex_ht is not None else -1
                _curr_sig   = "CALL" if _curr_trend == 0 else "PUT"
                if _active_signal == _curr_sig:
                    # Same direction — safe to skip
                    time.sleep(10)
                    continue
                # Opposite direction — fall through so Layer 1 can flip it
                print(f"⚠️ SENSEX: open {_active_signal} trade but HT says {_curr_sig} — running flip check", flush=True)

            # Refresh data cache every 30 seconds — 5-minute bars for faster arrow detection
            if time.time() - last_fetch_sensex > 30 or cached_sensex_df is None:
                cached_sensex_df = get_cached_data(SENSEX_TOKEN, "15minute", HT_LOOKBACK_CANDLES)
                if cached_sensex_df is not None and len(cached_sensex_df) >= 120:
                    cached_sensex_ht = halftrend_tv(cached_sensex_df, amplitude=HT_AMPLITUDE, channel_deviation=2)
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
                    # Block carry-over signals from previous days
                    _arrow_date = pd.to_datetime(arrow_bar["date"]).date() if "date" in arrow_bar else None
                    _today_date = datetime.now(IST).date()
                    if not is_fresh and _arrow_date and _arrow_date < _today_date:
                        print(f"🚫 SENSEX carry-over blocked — signal from {_arrow_date} (previous day)", flush=True)
                        time.sleep(10)
                        continue

                    tag = "🟢 CARRY-OVER" if signal == "CALL" else "🔴 CARRY-OVER"
                    print(f"{tag} SENSEX {signal} — {bars_ago} bars ({bars_ago*5} min) ago @ {arrow_level:.2f}", flush=True)

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
                _lim_key = f"SENSEX_limit_{datetime.now(IST).strftime('%Y-%m-%d')}"
                if getattr(sensex_loop, "_limit_alerted", None) != _lim_key:
                    sensex_loop._limit_alerted = _lim_key
                    print(f"🔒 SENSEX: {sensex_trade_count}/{MAX_SENSEX_TRADES_PER_DAY} trades done for today", flush=True)
                    send_message(f"🔒 SENSEX: {MAX_SENSEX_TRADES_PER_DAY} trades done for today. Resuming tomorrow.")
                time.sleep(60)
                continue

            # ── Signal detected — log only, no Telegram ──────────────────────
            if signal is not None and arrow_idx is not None:
                arrow_bar = ht_df.iloc[arrow_idx]
                _level = arrow_bar["atrLow"] if signal == "CALL" else arrow_bar["atrHigh"]
                _bars_ago = len(ht_df) - arrow_idx - 2
                # Block carry-over signals from previous days
                _arrow_date = pd.to_datetime(arrow_bar["date"]).date() if "date" in arrow_bar else None
                _today_date = datetime.now(IST).date()
                if not is_fresh and _arrow_date and _arrow_date < _today_date:
                    print(f"🚫 SENSEX carry-over blocked — signal from {_arrow_date} (previous day)", flush=True)
                    time.sleep(10)
                    continue

                _freshness = "FRESH" if is_fresh else f"CARRY-OVER ({_bars_ago * 15} min ago)"
                print(f"🔔 SENSEX {signal} {_freshness} @ ₹{_level:.2f}", flush=True)

            # ── Carry-over: enter only once per day ───────────────────────────
            today_str = datetime.now(IST).strftime("%Y-%m-%d")
            carryover_key = f"SENSEX_{signal}_{today_str}"
            if not is_fresh:
                if getattr(sensex_loop, "_carryover_done", None) == carryover_key:
                    time.sleep(10)
                    continue
            else:
                # Fresh arrow — reset carryover so re-entry is allowed
                sensex_loop._carryover_done = None

            # ══════════════════════════════════════════════════════════════
            # 📐  HTF FILTER  (30-min direction must agree with 15-min signal)
            # ══════════════════════════════════════════════════════════════
            _htf_ok, _htf_reason = check_htf_filter(signal, SENSEX_TOKEN)
            if not _htf_ok:
                print(f"📐 SENSEX HTF block — {_htf_reason}", flush=True)
                _hkey = f"SENSEX_htf_{signal}_{datetime.now(IST).strftime('%Y-%m-%d_%H')}"
                if getattr(sensex_loop, "_htf_alerted", None) != _hkey:
                    send_message(f"📐 SENSEX HTF BLOCK\n{_htf_reason}")
                    sensex_loop._htf_alerted = _hkey
                time.sleep(30)
                continue
            print(f"   {_htf_reason}", flush=True)

            # ══════════════════════════════════════════════════════════════
            # 🔒  LAYER 1 — Kite flip detection (MUST run before filters)
            # Exit any opposite position BEFORE applying entry filters.
            # Filters only block NEW entries — they must never block exits.
            # ══════════════════════════════════════════════════════════════
            _kite_pos_cache.pop("SENSEX", None)
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
                    # Open position in opposite direction → flip exit
                    print(f"🔁 SENSEX FLIP (Kite): {kite_sig} → {signal}", flush=True)
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
                        print("⚠️ SENSEX flip exit failed — retrying next tick", flush=True)
                        time.sleep(5)
                        continue

                    with lock:
                        sensex_position.update({"symbol": None, "qty": 0,
                                                "exchange": None, "signal": None,
                                                "active": False})
                        sensex_trade_active = False
                        global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
                        last_executed_signal_sensex = None
                    sensex_loop._carryover_done = None
                    sensex_loop._sig_alerted    = None
                    sensex_loop._just_flipped   = True
                    record_flip_and_check_whipsaw("SENSEX")
                    # Increment flip counter — next Claude call gets fresh evaluation
                    _claude_flip_counter["SENSEX"] = _claude_flip_counter.get("SENSEX", 0) + 1
                    for _k in list(_claude_filter_cache.keys()):
                        if _k.startswith("SENSEX_"):
                            _claude_filter_cache.pop(_k, None)
                    time.sleep(3)

            # ══════════════════════════════════════════════════════════════
            # 📊  STRATEGY FILTERS (entry only — flip already handled above)
            # ══════════════════════════════════════════════════════════════
            _just_flipped_sx = getattr(sensex_loop, "_just_flipped", False)
            sensex_loop._just_flipped = False
            _filter_ok, _filter_reason = apply_entry_filters(
                signal, "SENSEX", cached_sensex_df, SENSEX_TOKEN,
                is_flip_reentry=_just_flipped_sx,
                ht_df=cached_sensex_ht,
                hull_band_pct=get_hull_signal(cached_sensex_df)[3] or 0)

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
            # 🔒  ONE-ORDER-AT-A-TIME GUARD (layers 2 & 3)

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

            # ── Profit-lock exit cooldown (15 min) ────────────────────────────
            _pl_exit_ts = _profit_lock_exit_time.get("SENSEX", 0)
            _pl_wait = 300 - (time.time() - _pl_exit_ts)  # 5 min cooldown after profit lock
            if _pl_wait > 0:
                print(f"⏳ SENSEX profit-lock cooldown — {int(_pl_wait)}s remaining before next entry", flush=True)
                with lock:
                    sensex_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
                time.sleep(min(_pl_wait, 60))
                continue

            print(f"🧠 SENSEX entering: {signal}")
            symbol, price, lot, exchange = find_option(signal, "SENSEX")

            if not symbol or price is None:
                with lock:
                    sensex_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
                time.sleep(10)
                continue

            # ── Same-strike guard ─────────────────────────────────────────────
            # Same-strike block — never re-enter same strike that was exited today
            if symbol == _last_exited_symbol.get("SENSEX") and not is_fresh:
                print(f"🚫 SENSEX same-strike blocked for today: {symbol}", flush=True)
                with lock:
                    sensex_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
                time.sleep(30)
                continue

            filled_price = place_order(symbol, lot, exchange, "SENSEX")

            # ── Double-check trade limit (race condition guard) ───────────────
            # sensex_trade_count may have incremented while find_option was running
            if sensex_trade_count >= MAX_SENSEX_TRADES_PER_DAY and not filled_price:
                print(f"🔒 SENSEX trade limit reached during order — aborting", flush=True)
                with lock:
                    sensex_trade_active = False
                    global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active
                time.sleep(10)
                continue

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

                # Always mark carryover_done — blocks carry-over re-entry even after fresh entry
                sensex_loop._carryover_done = carryover_key
                if not is_fresh:
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
                    global_trade_active = nifty_trade_active or banknifty_trade_active or finnifty_trade_active or sensex_trade_active or crude_trade_active

        except Exception as e:
            err_str = str(e)
            print("❌ SENSEX LOOP ERROR:", err_str, flush=True)
            if any(x in err_str.lower() for x in ["token", "403", "unauthorized", "invalid api key"]):
                _tk = f"SX_auth_{datetime.now(IST).strftime('%Y-%m-%d_%H')}"
                if getattr(sensex_loop, "_token_alerted", None) != _tk:
                    sensex_loop._token_alerted = _tk
                    send_message(f"❌ SENSEX: Kite auth error — token expired\nError: {err_str[:150]}\nAction: Redeploy or whitelist IP")
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
    """Returns total shares for given lots based on instrument lot size.
    Lot sizes configurable via Railway Variables:
    NIFTY_LOT_SIZE, BANKNIFTY_LOT_SIZE, FINNIFTY_LOT_SIZE, SENSEX_LOT_SIZE, CRUDE_LOT_SIZE
    """
    if exchange == "MCX":              return lots * CRUDE_LOT_SIZE
    if instrument == "BANKNIFTY":      return lots * BANKNIFTY_LOT_SIZE
    if instrument == "FINNIFTY":       return lots * FINNIFTY_LOT_SIZE
    if instrument == "SENSEX":         return lots * SENSEX_LOT_SIZE
    if instrument == "NIFTY":          return lots * NIFTY_LOT_SIZE
    if exchange in ("NFO", "BFO"):     return lots * NIFTY_LOT_SIZE   # default NFO/BFO
    return lots
    
def get_balance(instrument):
    """
    Returns available balance from Kite.
    Uses correct margin segment per instrument:
    - NFO (NIFTY/BANKNIFTY/FINNIFTY) → equity segment
    - BFO (SENSEX) → equity segment (BSE F&O uses equity margin)
    - MCX (CRUDE) → commodity segment
    """
    try:
        margin = kite.margins()

        # Select correct segment
        if instrument == "CRUDE":
            seg = margin.get("commodity", {}).get("available", {})
        else:
            seg = margin.get("equity", {}).get("available", {})

        # Try multiple field names Kite uses
        cash         = float(seg.get("cash", 0) or seg.get("adhoc_margin", 0) or 0)
        live_balance = float(seg.get("live_balance", 0) or seg.get("opening_balance", 0) or 0)
        collateral   = float(seg.get("collateral", 0) or 0)

        # Use best available: cash or live_balance (whichever is higher)
        balance = max(cash, live_balance, collateral)

        if balance <= 0:
            # Last fallback — try net available
            net_avail = float(seg.get("net", 0) or 0)
            balance = net_avail
            print(f"⚠️ get_balance: zero from normal fields [{instrument}] — trying net={net_avail}")

        print(f"💳 Balance [{instrument}]: cash=₹{cash:.0f} live=₹{live_balance:.0f} → using ₹{balance:.0f}", flush=True)
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
      3. SL is set at 45% of option premium (i.e. exit if premium drops 45%).
      4. risk_per_lot = SL_points * lot_size
      5. lots = floor(risk_amount / risk_per_lot)
      6. Hard cap: total trade value (premium * lot_size * lots) <= MAX_CAPITAL_PCT of balance.
      7. Streak and drawdown adjustments applied last.

    Nifty lot size = 75 (as of 2024 revision — update if SEBI changes it again).
    Crude lot size = 100 bbls.
    """
    global win_streak, loss_streak
    global _daily_target_exited, _profit_protection_floor
    global _whipsaw_pause_until, _flip_timestamps
    global _loss_streak, _win_streak, _blocked_strikes
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
        lot_size = NIFTY_LOT_SIZE
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

    _streak = _loss_streak.get(instrument, loss_streak) if instrument else loss_streak
    if _streak >= 3:
        lots = 1
        print(f"🛑 Loss streak {_streak} → forced to 1 lot")
    elif _streak >= 2:
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

    # ── 10. Daily profit risk cap — protect locked-in gains ──────────────
    # If we've already made profit today, cap the max loss on THIS trade at
    # ₹800 so we never give back more than ₹800 of today's gains in one hit.
    # Max loss per trade = price * SL_PCT * lot_size * lots = risk_per_lot * lots
    # → lots_allowed = floor(MAX_TRADE_RISK / risk_per_lot), min 1
    MAX_TRADE_RISK = 800   # ₹ — max allowed loss on a single trade
    if daily_pnl >= 2000 and risk_per_lot > 0:
        lots_by_risk_cap = max(1, int(MAX_TRADE_RISK / risk_per_lot))
        if lots_by_risk_cap < lots:
            print(f"🛡️ Daily profit risk cap: daily_pnl=₹{daily_pnl:.0f} → "
                  f"max risk ₹{MAX_TRADE_RISK} → capping {lots}→{lots_by_risk_cap} lots "
                  f"(max loss ₹{lots_by_risk_cap * risk_per_lot:.0f})", flush=True)
            lots = lots_by_risk_cap

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
    global _daily_target_exited, _profit_protection_floor
    global _whipsaw_pause_until, _flip_timestamps
    global _loss_streak, _win_streak, _blocked_strikes
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
        global nifty_daily_pnl, banknifty_daily_pnl, finnifty_daily_pnl, sensex_daily_pnl, crude_daily_pnl
        global nifty_trade_count, banknifty_trade_count, finnifty_trade_count, sensex_trade_count, crude_trade_count
        global nifty_daily_wins, nifty_daily_losses
        global banknifty_daily_wins, banknifty_daily_losses
        global finnifty_daily_wins, finnifty_daily_losses
        global sensex_daily_wins, sensex_daily_losses
        global crude_daily_wins, crude_daily_losses
        global _last_no_signal_alert_nifty, _last_no_signal_alert_banknifty
        global _last_no_signal_alert_sensex, _last_no_signal_alert_crude
        # NOTE: swing_daily_pnl is NOT reset here — swing trades carry across days.
        # It resets only when we explicitly call swing reset (end of month / manual).

        nifty_daily_pnl = 0
        banknifty_daily_pnl = 0
        finnifty_daily_pnl = 0
        sensex_daily_pnl = 0
        crude_daily_pnl = 0
        nifty_trade_count = 0
        banknifty_trade_count = 0
        finnifty_trade_count = 0
        sensex_trade_count = 0
        crude_trade_count = 0
        nifty_daily_wins = 0
        nifty_daily_losses = 0
        banknifty_daily_wins = 0
        banknifty_daily_losses = 0
        finnifty_daily_wins = 0
        finnifty_daily_losses = 0
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

        # Reset first candle range cache for new day
        global _first_candle_cache, _first_candle_alert_sent, _fc_breakout_done
        _first_candle_cache.clear()
        _first_candle_alert_sent.clear()
        _fc_breakout_done.clear()
        print("📊 First candle range cache reset for new day", flush=True)

        # ── Reset progressive profit lock for new day ─────────────────────
        global _peak_daily_pnl, _profit_lock_floor, _profit_lock_tier
        _peak_daily_pnl    = 0.0
        _profit_lock_floor = 0.0
        _profit_lock_tier  = 0
        print("🔓 Profit lock reset for new trading day", flush=True)

        # ── Reset daily profit target flag for new day ────────────────────
        global _daily_target_exited, _profit_protection_floor
        _daily_target_exited     = False
        _profit_protection_floor = 0.0
        daily_profit_target_monitor._protection_triggered = False
        print("🎯 Daily profit target reset for new trading day", flush=True)

        # ── Reset same-strike guard for new day ───────────────────────────
        global _loss_streak, _win_streak
        for _inst in _loss_streak:
            _loss_streak[_inst] = 0
            _win_streak[_inst]  = 0
        _last_exited_symbol.clear()
        for _inst in _blocked_strikes:
            _blocked_strikes[_inst].clear()
        for _inst in _flip_timestamps:
            _flip_timestamps[_inst].clear()
        _whipsaw_pause_until.clear()
        print("🔓 Blocked strikes + whipsaw state cleared for new trading day", flush=True)

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
    Combined end-of-day report sent at 11:32 PM covering all instruments.
    Uses Kite positions API as primary source (same as individual EOD reports)
    so it survives Railway restarts and always matches Kite dashboard.
    """
    global report_sent_today
    report_sent_today = True

    today = datetime.now(IST).strftime("%d %b %Y")

    try:
        # ── Fetch P&L for every instrument from Kite (ground truth) ──────────
        n_pnl,  n_wins,  n_losses,  n_count  = _best_day_pnl(
            "NIFTY",     nifty_daily_pnl,     nifty_daily_wins,     nifty_daily_losses,     nifty_trade_count)
        bn_pnl, bn_wins, bn_losses, bn_count = _best_day_pnl(
            "BANKNIFTY", banknifty_daily_pnl, banknifty_daily_wins, banknifty_daily_losses, banknifty_trade_count)
        fn_pnl, fn_wins, fn_losses, fn_count = _best_day_pnl(
            "FINNIFTY",  finnifty_daily_pnl,  finnifty_daily_wins,  finnifty_daily_losses,  finnifty_trade_count)
        sx_pnl, sx_wins, sx_losses, sx_count = _best_day_pnl(
            "SENSEX",    sensex_daily_pnl,    sensex_daily_wins,    sensex_daily_losses,    sensex_trade_count)
        c_pnl,  c_wins,  c_losses,  c_count  = _best_day_pnl(
            "CRUDE",     crude_daily_pnl,     crude_daily_wins,     crude_daily_losses,     crude_trade_count)

        total_pnl    = n_pnl + bn_pnl + fn_pnl + sx_pnl + c_pnl
        total_trades = n_count + bn_count + fn_count + sx_count + c_count
        total_wins   = n_wins  + bn_wins  + fn_wins  + sx_wins  + c_wins
        total_losses = n_losses + bn_losses + fn_losses + sx_losses + c_losses
        win_rate     = (total_wins / total_trades * 100) if total_trades > 0 else 0

        def _section(label, pnl, wins, losses, count):
            if count == 0:
                return f"\n{label}: No trades today"
            wr = (wins / count * 100) if count > 0 else 0
            sign = "✅" if pnl >= 0 else "❌"
            return (
                f"\n{sign} {label}\n"
                f"   P&L: ₹{pnl:,.0f}  |  {count} trades  |  WR: {wr:.0f}%"
            )

        report = (
            f"📅 DAILY TRADING REPORT — {today}\n"
            f"{'='*30}\n"
            f"🏦 Combined Net P&L : ₹{total_pnl:,.0f}\n"
            f"📊 Total Trades     : {total_trades}  "
            f"(✅ {total_wins} wins  ❌ {total_losses} losses)\n"
            f"🎯 Overall Win Rate : {win_rate:.1f}%\n"
            f"{'='*30}"
            + _section("NIFTY",     n_pnl,  n_wins,  n_losses,  n_count)
            + _section("BANKNIFTY", bn_pnl, bn_wins, bn_losses, bn_count)
            + _section("FINNIFTY",  fn_pnl, fn_wins, fn_losses, fn_count)
            + _section("SENSEX",    sx_pnl, sx_wins, sx_losses, sx_count)
            + _section("CRUDE OIL", c_pnl,  c_wins,  c_losses,  c_count) +
            f"\n{'='*30}\n"
            f"⏰ Report time: {datetime.now(IST).strftime('%H:%M:%S IST')}"
        )

        send_message(report)
        print("📊 Daily report sent", flush=True)

    except Exception as e:
        print(f"Report error: {e}", flush=True)
        send_message(f"❌ Daily report error: {e}")


def _kite_day_pnl(instrument):
    """
    Fetch today's realized P&L directly from Kite's positions API.
    This is the ground truth — survives Railway restarts and always matches
    what the user sees in Kite dashboard.

    Returns (pnl, wins, losses, trade_count).
    Wins/losses counted per distinct option symbol traded.
    """
    exchange_map = {
        "NIFTY":     "NFO",
        "BANKNIFTY": "NFO",
        "FINNIFTY":  "NFO",
        "SENSEX":    "BFO",
        "CRUDE":     "MCX",
    }
    prefix_map = {
        "NIFTY":     "NIFTY",
        "BANKNIFTY": "BANKNIFTY",
        "FINNIFTY":  "FINNIFTY",
        "SENSEX":    "SENSEX",
        "CRUDE":     "CRUDEOIL",   # MCX Crude Oil symbol starts with CRUDEOIL
    }

    try:
        positions    = kite.positions()
        day_pos      = positions.get("day", [])
        target_exch  = exchange_map.get(instrument.upper(), "NFO")
        sym_prefix   = prefix_map.get(instrument.upper())

        filtered = []
        for p in day_pos:
            if p.get("exchange") != target_exch:
                continue
            sym = p.get("tradingsymbol", "")
            # Distinguish NFO instruments (NIFTY vs BANKNIFTY vs FINNIFTY)
            if instrument.upper() == "NIFTY":
                if sym.startswith("BANKNIFTY") or sym.startswith("FINNIFTY"):
                    continue
            if instrument.upper() == "BANKNIFTY":
                if not sym.startswith("BANKNIFTY"):
                    continue
            if instrument.upper() == "FINNIFTY":
                if not sym.startswith("FINNIFTY"):
                    continue
            if sym_prefix and not sym.startswith(sym_prefix):
                continue
            filtered.append(p)

        if not filtered:
            return 0.0, 0, 0, 0

        # Group by tradingsymbol — partial bookings create multiple entries
        from collections import defaultdict
        sym_pnl = defaultdict(float)
        for p in filtered:
            sym_pnl[p.get("tradingsymbol", "")] += float(p.get("pnl", 0))

        pnl    = sum(sym_pnl.values())
        wins   = sum(1 for v in sym_pnl.values() if v > 0)
        losses = sum(1 for v in sym_pnl.values() if v <= 0)
        count  = len(sym_pnl)   # unique symbols = unique trades

        # Also count open position if not already in day positions
        net_pos = positions.get("net", [])
        for p in net_pos:
            if p.get("exchange") != target_exch:
                continue
            sym = p.get("tradingsymbol", "")
            if instrument.upper() == "NIFTY":
                if sym.startswith("BANKNIFTY") or sym.startswith("FINNIFTY"):
                    continue
            if instrument.upper() == "BANKNIFTY" and not sym.startswith("BANKNIFTY"):
                continue
            if instrument.upper() == "FINNIFTY" and not sym.startswith("FINNIFTY"):
                continue
            if sym_prefix and not sym.startswith(sym_prefix):
                continue
            if p.get("quantity", 0) > 0 and sym not in sym_pnl:
                count += 1   # open trade not yet in day positions

        return pnl, wins, losses, count

    except Exception as e:
        print(f"⚠️ _kite_day_pnl({instrument}): {e}", flush=True)
        return 0.0, 0, 0, 0


def _read_today_csv(instrument):
    """
    Fallback: read today's closed trades for the given instrument from trade_log.csv.
    Used only when Kite API is unavailable.
    Returns (pnl, wins, losses, trade_count).
    """
    today_str = datetime.now(IST).strftime("%Y-%m-%d")
    try:
        if not os.path.exists(TRADE_LOG_FILE):
            return 0, 0, 0, 0
        df = pd.read_csv(TRADE_LOG_FILE)
        if df.empty or "time" not in df.columns:
            return 0, 0, 0, 0
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        # Localise to IST so date comparison is always correct regardless of server TZ
        df["time"] = df["time"].dt.tz_localize("UTC", ambiguous="NaT", nonexistent="NaT").dt.tz_convert(IST)
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


def _best_day_pnl(instrument, mem_pnl, mem_wins, mem_losses, mem_count):
    """
    Priority order for EOD P&L data:
      1. Kite positions API  — ground truth, survives restarts
      2. CSV trade log       — fallback if Kite unavailable
      3. In-memory counters  — last resort (lost on restart)
    Returns (pnl, wins, losses, count).
    """
    kite_pnl, kite_wins, kite_losses, kite_count = _kite_day_pnl(instrument)
    if kite_count > 0:
        return kite_pnl, kite_wins, kite_losses, kite_count

    csv_pnl, csv_wins, csv_losses, csv_count = _read_today_csv(instrument)
    if csv_count > 0:
        return csv_pnl, csv_wins, csv_losses, csv_count

    # In-memory fallback
    return mem_pnl, mem_wins, mem_losses, mem_count


def send_nifty_eod_report():
    """Sent at 3:31 PM. Uses Kite positions API as primary source (restart-safe)."""
    pnl, wins, losses, count = _best_day_pnl(
        "NIFTY", nifty_daily_pnl, nifty_daily_wins, nifty_daily_losses, nifty_trade_count
    )
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
    """Sent at 11:31 PM. Uses Kite positions API as primary source (restart-safe)."""
    pnl, wins, losses, count = _best_day_pnl(
        "CRUDE", crude_daily_pnl, crude_daily_wins, crude_daily_losses, crude_trade_count
    )

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


# ═══════════════════════════════════════════════════════════════════════════════
# 🔍  SCREENER.IN INTEGRATION  — Auto stock selection via saved screen
# ═══════════════════════════════════════════════════════════════════════════════
# Screen query to save on screener.in:
#   Sales growth 3Years > 20
#   AND Profit growth 3Years > 20
#   AND Return on capital employed > 18
#   AND Return on equity > 18
#   AND Debt to equity < 0.5
#   AND Promoter holding > 50
#   AND Market capitalization > 500
#   AND Current price > DMA 200
#   AND Down from 52w high > 25
#
# Setup steps (Google / Gmail login — session cookie method):
#   1. Go to https://www.screener.in/screens/new/
#   2. Paste the query above → Run → Save with a name
#   3. Note the screen ID from URL (e.g. screener.in/screens/123456/ → ID=123456)
#   4. Open screener.in in Chrome → F12 → Application → Cookies → www.screener.in
#      Copy the value of the cookie named  "sessionid"
#   5. Add to config.py:
#        USE_SCREENER             = True
#        SCREENER_SESSION_COOKIE  = "abc123xyz..."   ← paste sessionid value here
#        SCREENER_SCREEN_ID       = "123456"
#
#   The sessionid cookie lasts weeks/months. When it expires the bot sends a
#   Telegram alert — just copy a fresh one from your browser and update config.py.
# ─────────────────────────────────────────────────────────────────────────────

import re as _re
try:
    import requests as _requests
    _REQUESTS_AVAILABLE = True
except ImportError:
    _REQUESTS_AVAILABLE = False
    print("⚠️ 'requests' library not found — Screener.in integration disabled. Run: pip install requests")


def _screener_build_session():
    """
    Build a requests.Session pre-loaded with the user's Screener.in sessionid cookie.
    No login flow needed — works with Google / Gmail OAuth accounts.
    Raises RuntimeError if session cookie is not configured or appears invalid.
    """
    if not _REQUESTS_AVAILABLE:
        raise RuntimeError("requests library not installed")
    if not SCREENER_SESSION_COOKIE:
        raise RuntimeError(
            "SCREENER_SESSION_COOKIE not set in config.py. "
            "Open screener.in → F12 → Application → Cookies → copy 'sessionid' value."
        )

    session = _requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0.0.0 Safari/537.36",
    })
    # Inject the browser session cookie directly — no login POST needed
    session.cookies.set("sessionid", SCREENER_SESSION_COOKIE, domain="www.screener.in")

    # Quick validation: hit the dashboard and check we're actually logged in
    check = session.get("https://www.screener.in/", timeout=15)
    # If cookie is expired/invalid, Screener redirects to /login/
    if "/login/" in check.url:
        raise RuntimeError(
            "Screener.in session cookie has expired. "
            "Open screener.in in your browser (log in via Google), then:\n"
            "F12 → Application → Cookies → www.screener.in → copy 'sessionid' value → update SCREENER_SESSION_COOKIE in config.py"
        )

    print("✅ Screener.in session cookie valid", flush=True)
    return session


def _screener_parse_nse_symbols(csv_text):
    """
    Parse NSE stock symbols from Screener.in CSV export.
    Screener.in exports include an 'NSE' or 'NSE Code' column.
    Returns list of uppercase NSE symbols, skipping blank / BSE-only rows.
    """
    import csv, io
    symbols = []

    try:
        reader = csv.DictReader(io.StringIO(csv_text.strip()))
        if reader.fieldnames is None:
            print("⚠️ Screener CSV has no headers", flush=True)
            return []

        # Normalise header names for robust matching
        headers = [h.strip() for h in reader.fieldnames]
        # Candidates in priority order
        nse_col = None
        for candidate in ("NSE Code", "NSE", "NSE Symbol", "Ticker", "Symbol"):
            for h in headers:
                if h.strip().upper() == candidate.upper():
                    nse_col = h
                    break
            if nse_col:
                break

        if not nse_col:
            print(f"⚠️ Screener CSV: could not find NSE symbol column. Headers: {headers}", flush=True)
            # Last resort: try first non-numeric column that looks like a symbol
            for h in headers:
                if h.strip().upper() not in ("S.NO.", "S.NO", "CMP", "PRICE", "P/E", "NAME"):
                    nse_col = h
                    break

        if not nse_col:
            return []

        for row in reader:
            sym = row.get(nse_col, "").strip().upper()
            # Skip blanks, dashes, or purely numeric entries
            if sym and sym != "-" and not sym.isdigit():
                symbols.append(sym)

    except Exception as e:
        print(f"⚠️ Screener CSV parse error: {e}", flush=True)

    return symbols


def _get_fo_symbols():
    """
    Return a set of NSE stock symbols that have options traded on NFO.
    Queries the live Kite instruments list once per call (cached per refresh cycle).
    """
    try:
        nfo = kite.instruments("NFO")
        # Stock options have instrument_type "CE" or "PE" and a name that is the stock symbol
        fo_set = set()
        for inst in nfo:
            if inst.get("instrument_type") in ("CE", "PE"):
                name = inst.get("name", "").strip().upper()
                if name:
                    fo_set.add(name)
        return fo_set
    except Exception as e:
        print(f"⚠️ F&O symbol fetch error: {e}", flush=True)
        return set()


def refresh_screener_daily(force=False):
    """
    Fetch today's Screener.in screen results and populate:
      _screener_stocks_today    — all NSE symbols passing the screen
      _screener_fo_stocks_today — subset that are F&O eligible (have NFO options)

    Called automatically at 8:55 AM IST every weekday.
    Set force=True to trigger a manual refresh (e.g. from Telegram command).
    Skips if already refreshed today (unless force=True).
    """
    global _screener_session, _screener_stocks_today, _screener_fo_stocks_today, _screener_refresh_date

    if not USE_SCREENER:
        return
    if not SCREENER_SCREEN_ID:
        print("⚠️ SCREENER_SCREEN_ID not set in config.py — skipping Screener refresh", flush=True)
        return

    today = datetime.now(IST).date()
    if not force and _screener_refresh_date == today:
        return  # already refreshed today

    print(f"🔍 Fetching Screener.in screen {SCREENER_SCREEN_ID}...", flush=True)
    try:
        # Build session from stored cookie if not yet created
        if _screener_session is None:
            _screener_session = _screener_build_session()

        export_url = f"https://www.screener.in/screens/{SCREENER_SCREEN_ID}/?export=1"
        resp = _screener_session.get(export_url, timeout=20)

        # Cookie expired mid-session — rebuild once and retry
        if "/login/" in resp.url or resp.status_code in (401, 403):
            print("🔄 Screener cookie expired — rebuilding session", flush=True)
            _screener_session = _screener_build_session()
            resp = _screener_session.get(export_url, timeout=20)

        resp.raise_for_status()

        symbols = _screener_parse_nse_symbols(resp.text)
        if not symbols:
            print("⚠️ Screener returned 0 symbols — check screen ID or login", flush=True)
            return

        # Cross-reference with Kite NFO for F&O eligibility
        fo_set = _get_fo_symbols()
        fo_symbols = [s for s in symbols if s in fo_set]

        _screener_stocks_today    = symbols
        _screener_fo_stocks_today = fo_symbols
        _screener_refresh_date    = today

        print(
            f"✅ Screener refresh done: {len(symbols)} stocks, "
            f"{len(fo_symbols)} F&O eligible: {fo_symbols[:10]}",
            flush=True
        )
        send_message(
            f"🔍 SCREENER REFRESH — {today.strftime('%d %b %Y')}\n"
            f"📋 Screen ID : {SCREENER_SCREEN_ID}\n"
            f"📈 Stocks    : {len(symbols)} qualify the filter\n"
            f"🎯 F&O stocks: {len(fo_symbols)}\n"
            f"   {', '.join(fo_symbols) if fo_symbols else 'None'}\n"
            f"\n"
            f"🛒 Swing universe  : {', '.join(symbols[:15])}{'…' if len(symbols)>15 else ''}\n"
            f"📊 F&O options list: {', '.join(fo_symbols[:15])}{'…' if len(fo_symbols)>15 else ''}"
        )

    except Exception as e:
        print(f"❌ Screener refresh failed: {e}", flush=True)
        send_message(
            f"❌ SCREENER REFRESH FAILED\n"
            f"Error: {e}\n"
            f"\n"
            f"If cookie expired:\n"
            f"1. Open screener.in in Chrome (log in via Google)\n"
            f"2. F12 → Application → Cookies → www.screener.in\n"
            f"3. Copy 'sessionid' value\n"
            f"4. Update SCREENER_SESSION_COOKIE in config.py & redeploy\n"
            f"\n"
            f"Falling back to stocks.txt / stock_options.txt today"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 📈  SWING TRADE MODULE  — NSE Equity CNC, Daily HalfTrend
# ═══════════════════════════════════════════════════════════════════════════════

def load_swing_stocks():
    """
    Read stock symbols from stocks.txt.
    Format: one NSE symbol per line, e.g.:
        RELIANCE
        TCS
        # commented lines are ignored
    Returns a list of uppercase symbols.
    """
    if not os.path.exists(SWING_STOCKS_FILE):
        print(f"⚠️ {SWING_STOCKS_FILE} not found — create it with one NSE symbol per line")
        return []
    with open(SWING_STOCKS_FILE) as f:
        stocks = [
            line.strip().upper()
            for line in f
            if line.strip() and not line.strip().startswith("#")
        ]
    return stocks


def get_stock_token(symbol):
    """Return the NSE instrument token for an equity symbol (cached)."""
    global _swing_token_cache
    if symbol in _swing_token_cache:
        return _swing_token_cache[symbol]
    try:
        instruments = kite.instruments("NSE")
        for inst in instruments:
            if inst["tradingsymbol"] == symbol and inst["instrument_type"] == "EQ":
                _swing_token_cache[symbol] = inst["instrument_token"]
                return inst["instrument_token"]
        print(f"⚠️ Token not found for {symbol}")
    except Exception as e:
        print(f"❌ get_stock_token({symbol}): {e}")
    return None


def get_swing_daily_data(symbol, token):
    """
    Fetch 200 daily candles for a stock.
    Cached for 4 hours so we don't hammer the Kite API on every loop tick.
    Returns a DataFrame or None.
    """
    global _swing_data_cache
    now_ts = time.time()
    if symbol in _swing_data_cache:
        cached_ts, cached_df = _swing_data_cache[symbol]
        if now_ts - cached_ts < 14400:   # 4-hour TTL
            return cached_df
    try:
        to_date   = datetime.now(IST).replace(tzinfo=None)
        from_date = to_date - timedelta(days=300)   # 300 days → ~200 trading days
        data = kite.historical_data(token, from_date, to_date, "day")
        if not data:
            return None
        df = pd.DataFrame(data)
        df["date"] = pd.to_datetime(df["date"])
        df = df.rename(columns={"date": "time"})
        df = df.sort_values("time").reset_index(drop=True)
        _swing_data_cache[symbol] = (now_ts, df)
        return df
    except Exception as e:
        print(f"❌ get_swing_daily_data({symbol}): {e}")
        return None


def place_swing_buy_order(symbol, qty):
    """
    Place a CNC BUY order on NSE for swing entry.
    Returns filled price or None.
    """
    try:
        ltp = safe_ltp(f"NSE:{symbol}")
        if ltp is None or ltp <= 0:
            print(f"❌ swing buy: invalid LTP for {symbol}")
            return None

        price = round(ltp * 1.002, 2)   # 0.2% above LTP for fast fill

        # Balance check
        balance = get_balance("NIFTY")   # equity segment
        total_cost = price * qty
        if balance < total_cost * 1.02:
            msg = f"🚫 Swing: Insufficient balance for {symbol}\nNeed ₹{total_cost:,.0f}, have ₹{balance:,.0f}"
            print(msg)
            send_message(msg)
            return None

        order_id = kite.place_order(
            variety="regular",
            exchange="NSE",
            tradingsymbol=symbol,
            transaction_type="BUY",
            quantity=qty,
            order_type="LIMIT",
            price=price,
            product="CNC"
        )
        print(f"📥 Swing BUY placed: {symbol}  qty={qty}  price={price}  order={order_id}")

        # Wait for fill (up to 15s, 5 attempts)
        filled_price = None
        for attempt in range(5):
            time.sleep(3)
            try:
                orders = kite.orders()
                for o in orders:
                    if o["order_id"] == order_id:
                        if o["status"] == "COMPLETE":
                            filled_price = o["average_price"]
                            break
                        elif o["status"] in ["CANCELLED", "REJECTED"]:
                            print(f"❌ Swing BUY {symbol} rejected/cancelled")
                            return None
            except Exception as e:
                print(f"⚠️ Swing order poll error: {e}")
            if filled_price:
                break
            # Nudge price slightly higher
            try:
                new_price = round(price * 1.001, 2)
                kite.modify_order(variety="regular", order_id=order_id, price=new_price)
                price = new_price
            except Exception:
                pass

        if not filled_price:
            try:
                kite.cancel_order(variety="regular", order_id=order_id)
            except Exception:
                pass
            print(f"❌ Swing BUY {symbol} not filled — cancelled")
            return None

        print(f"✅ Swing BUY filled: {symbol} @ ₹{filled_price}  qty={qty}")
        return filled_price

    except Exception as e:
        print(f"❌ place_swing_buy_order({symbol}): {e}")
        return None


def place_swing_sell_order(symbol, qty, reason="EXIT"):
    """
    Place a CNC SELL order on NSE to exit a swing position.
    Returns filled price or None.
    """
    try:
        ltp = safe_ltp(f"NSE:{symbol}")
        if ltp is None or ltp <= 0:
            # Fallback — use market order if LTP unavailable
            print(f"⚠️ Swing sell: LTP unavailable for {symbol} — using MARKET order")
            order_id = kite.place_order(
                variety="regular", exchange="NSE", tradingsymbol=symbol,
                transaction_type="SELL", quantity=qty,
                order_type="MARKET", product="CNC"
            )
        else:
            price = round(ltp * 0.998, 2)   # 0.2% below LTP for fast fill
            order_id = kite.place_order(
                variety="regular", exchange="NSE", tradingsymbol=symbol,
                transaction_type="SELL", quantity=qty,
                order_type="LIMIT", price=price, product="CNC"
            )
            print(f"📤 Swing SELL placed: {symbol}  qty={qty}  price={price}  reason={reason}")

        # Wait for fill (up to 30s, 6 attempts)
        filled_price = None
        for attempt in range(6):
            time.sleep(5)
            try:
                orders = kite.orders()
                for o in orders:
                    if o["order_id"] == order_id:
                        if o["status"] == "COMPLETE":
                            filled_price = o["average_price"]
                            break
                        elif o["status"] in ["CANCELLED", "REJECTED"]:
                            print(f"❌ Swing SELL {symbol} rejected/cancelled")
                            return None
            except Exception as e:
                print(f"⚠️ Swing sell poll error: {e}")
            if filled_price:
                break
            # Nudge price slightly lower to fill faster
            try:
                new_price = round(ltp * (0.998 - attempt * 0.003), 2) if ltp else None
                if new_price and new_price > 0:
                    kite.modify_order(variety="regular", order_id=order_id, price=new_price)
            except Exception:
                pass

        if not filled_price:
            # Last resort: convert to MARKET
            try:
                kite.modify_order(variety="regular", order_id=order_id,
                                  order_type="MARKET", price=0)
                time.sleep(3)
                orders = kite.orders()
                for o in orders:
                    if o["order_id"] == order_id and o["status"] == "COMPLETE":
                        filled_price = o["average_price"]
            except Exception as e:
                print(f"⚠️ Market conversion failed: {e}")

        if filled_price:
            print(f"✅ Swing SELL filled: {symbol} @ ₹{filled_price}  qty={qty}")
        else:
            print(f"❌ Swing SELL {symbol} not filled after retries")
        return filled_price

    except Exception as e:
        print(f"❌ place_swing_sell_order({symbol}): {e}")
        return None


def manage_swing_position(symbol, entry_price, qty, sl_price, target_price):
    """
    Monitor an open swing position in its own thread.
    Exit on:  (1) SL hit   (2) Target hit   (3) HalfTrend daily flip to PUT
    Pauses monitoring outside market hours (resumes next morning).
    """
    global swing_daily_pnl, swing_daily_wins, swing_daily_losses, swing_trade_count

    token = get_stock_token(symbol)
    print(f"👁️ Monitoring swing: {symbol}  entry={entry_price:.2f}  SL={sl_price:.2f}  target={target_price:.2f}")

    while True:
        try:
            now_ist = datetime.now(IST)

            # ── Outside market hours — sleep and wait ─────────────────────────
            in_market_hours = (
                (now_ist.weekday() < 5) and
                (
                    (now_ist.hour == 9 and now_ist.minute >= 30) or
                    (9 < now_ist.hour < 15) or
                    (now_ist.hour == 15 and now_ist.minute <= 25)
                )
            )
            if not in_market_hours:
                time.sleep(60)
                continue

            # ── Get current LTP ───────────────────────────────────────────────
            ltp = safe_ltp(f"NSE:{symbol}")
            if ltp is None or ltp <= 0:
                time.sleep(30)
                continue

            pnl_per_share = ltp - entry_price
            pnl_total     = pnl_per_share * qty
            pnl_pct       = (pnl_per_share / entry_price) * 100

            exit_reason = None

            # ── (1) Hard SL ───────────────────────────────────────────────────
            if ltp <= sl_price:
                exit_reason = f"🛑 SL HIT ({pnl_pct:.1f}%)"

            # ── (2) Profit Target ─────────────────────────────────────────────
            elif ltp >= target_price:
                exit_reason = f"🎯 TARGET HIT (+{pnl_pct:.1f}%)"

            # ── (3) HalfTrend daily flip to PUT ───────────────────────────────
            else:
                if token:
                    df_daily = get_swing_daily_data(symbol, token)
                    if df_daily is not None and len(df_daily) >= 50:
                        ht = halftrend_tv(df_daily, amplitude=2, channel_deviation=2)
                        if ht is not None and len(ht) >= 2:
                            # iloc[-2] = last closed daily candle (anti-repaint)
                            trend = int(ht.iloc[-2]["trend"])
                            if trend == 1:   # 1 = bearish/PUT
                                exit_reason = f"🔴 HALFTREND FLIP (daily PUT signal)"

            # ── Exit ─────────────────────────────────────────────────────────
            if exit_reason:
                print(f"📤 Swing exit triggered: {symbol}  reason={exit_reason}  LTP={ltp:.2f}")
                filled_exit = place_swing_sell_order(symbol, qty, reason=exit_reason)

                if filled_exit:
                    actual_pnl = (filled_exit - entry_price) * qty
                    pnl_pct_f  = ((filled_exit - entry_price) / entry_price) * 100
                    emoji      = "✅" if actual_pnl > 0 else "❌"

                    with swing_positions_lock:
                        swing_positions.pop(symbol, None)

                    swing_daily_pnl    += actual_pnl
                    swing_trade_count  += 1
                    if actual_pnl > 0:
                        swing_daily_wins   += 1
                    else:
                        swing_daily_losses += 1

                    log_trade_full(symbol, entry_price, filled_exit, actual_pnl,
                                   f"SWING:{symbol}", "CALL", 0)

                    send_message(
                        f"{emoji} SWING TRADE CLOSED — {symbol}\n"
                        f"{'='*28}\n"
                        f"📌 Reason  : {exit_reason}\n"
                        f"💰 P&L     : ₹{actual_pnl:+,.0f}  ({pnl_pct_f:+.1f}%)\n"
                        f"📊 Entry   : ₹{entry_price:.2f}  |  Exit: ₹{filled_exit:.2f}\n"
                        f"📦 Qty     : {qty} shares\n"
                        f"📅 Swing P&L today: ₹{swing_daily_pnl:+,.0f}"
                    )
                    print(f"✅ Swing {symbol} closed — P&L ₹{actual_pnl:+,.0f}")
                else:
                    send_message(
                        f"⚠️ SWING EXIT FAILED: {symbol}\n"
                        f"Reason: {exit_reason}\n"
                        f"❗ Manual exit required — check Kite app"
                    )
                return   # exit thread

            # ── Periodic status log ───────────────────────────────────────────
            print(f"📊 Swing {symbol}: LTP={ltp:.2f}  P&L={pnl_pct:+.1f}%  "
                  f"SL={sl_price:.2f}  Target={target_price:.2f}")

        except Exception as e:
            print(f"❌ manage_swing_position({symbol}): {e}")

        time.sleep(120)   # check every 2 minutes


def swing_loop():
    """
    Master swing loop.
    - Reads stocks.txt every cycle
    - Enters new positions when HalfTrend daily gives a CALL signal
    - manage_swing_position() threads handle exits
    - Runs 24/7; only scans for entries during market hours
    """
    print("🚀 Swing loop started")
    _last_scan_log = 0

    while True:
        try:
            if not ENABLE_SWING:
                time.sleep(60)
                continue

            now_ist = datetime.now(IST)

            # ── Only scan for NEW entries during market hours ─────────────────
            in_market_hours = (
                (now_ist.weekday() < 5) and
                (
                    (now_ist.hour == 9 and now_ist.minute >= 30) or
                    (9 < now_ist.hour < 15) or
                    (now_ist.hour == 15 and now_ist.minute <= 20)
                )
            )
            if not in_market_hours:
                time.sleep(120)
                continue

            # Use Screener results if enabled and populated; else fall back to stocks.txt
            if USE_SCREENER and _screener_stocks_today:
                stocks = _screener_stocks_today
            else:
                stocks = load_swing_stocks()
            if not stocks:
                time.sleep(300)
                continue

            with swing_positions_lock:
                current_positions = len(swing_positions)

            if current_positions >= MAX_SWING_POSITIONS:
                if time.time() - _last_scan_log > 600:
                    print(f"📊 Swing: max positions ({MAX_SWING_POSITIONS}) reached — not scanning for new entries")
                    _last_scan_log = time.time()
                time.sleep(120)
                continue

            # ── Scan each stock ───────────────────────────────────────────────
            for symbol in stocks:
                with swing_positions_lock:
                    if symbol in swing_positions:
                        continue   # already have an open position

                    if len(swing_positions) >= MAX_SWING_POSITIONS:
                        break

                token = get_stock_token(symbol)
                if token is None:
                    continue

                df_daily = get_swing_daily_data(symbol, token)
                if df_daily is None or len(df_daily) < 50:
                    print(f"⚠️ Swing {symbol}: insufficient daily data")
                    continue

                ht = halftrend_tv(df_daily, amplitude=2, channel_deviation=2)
                if ht is None or len(ht) < 2:
                    continue

                # Use last CLOSED daily candle (iloc[-2]) — anti-repaint rule
                last_closed = ht.iloc[-2]
                trend       = int(last_closed["trend"])   # 0=bullish 1=bearish

                # Entry condition: HalfTrend is bullish (CALL) AND a buy arrow
                # appeared on the last closed candle or recent candles
                signal, arrow_idx, is_fresh = get_last_active_signal(ht)

                if signal != "CALL":
                    print(f"   {symbol}: no CALL signal (trend={'BULL' if trend==0 else 'BEAR'})")
                    continue

                # ── Check LTP and calculate position ─────────────────────────
                ltp = safe_ltp(f"NSE:{symbol}")
                if ltp is None or ltp <= 0:
                    print(f"⚠️ Swing {symbol}: invalid LTP")
                    continue

                qty = max(1, int(SWING_CAPITAL_PER_STOCK / ltp))
                sl_price     = round(ltp * (1 - SWING_SL_PCT),     2)
                target_price = round(ltp * (1 + SWING_TARGET_PCT),  2)

                bars_ago = len(ht) - arrow_idx - 2 if arrow_idx is not None else "?"
                freshness = "FRESH" if is_fresh else f"CARRY-OVER ({bars_ago} days ago)"
                print(f"🔔 Swing entry signal: {symbol}  LTP={ltp:.2f}  signal={freshness}")
                print(f"   Qty={qty}  SL={sl_price:.2f}  Target={target_price:.2f}"
                      f"  Deploy=₹{ltp*qty:,.0f}")

                # ── Place CNC buy order ───────────────────────────────────────
                filled_price = place_swing_buy_order(symbol, qty)
                if not filled_price:
                    print(f"⚠️ Swing {symbol}: buy order not filled")
                    continue

                # Recalculate SL/target from actual fill price
                sl_price     = round(filled_price * (1 - SWING_SL_PCT),    2)
                target_price = round(filled_price * (1 + SWING_TARGET_PCT), 2)

                with swing_positions_lock:
                    swing_positions[symbol] = {
                        "entry":      filled_price,
                        "qty":        qty,
                        "sl":         sl_price,
                        "target":     target_price,
                        "signal":     "CALL",
                        "entry_time": datetime.now(IST),
                    }

                send_message(
                    f"🆕 SWING ENTRY — {symbol}\n"
                    f"{'='*28}\n"
                    f"📈 Signal  : {freshness}\n"
                    f"💰 Entry   : ₹{filled_price:.2f}  |  Qty: {qty} shares\n"
                    f"🛑 SL      : ₹{sl_price:.2f}  (-{SWING_SL_PCT*100:.0f}%)\n"
                    f"🎯 Target  : ₹{target_price:.2f}  (+{SWING_TARGET_PCT*100:.0f}%)\n"
                    f"💼 Deployed: ₹{filled_price*qty:,.0f}"
                )

                # Start monitor thread for this position
                threading.Thread(
                    target=manage_swing_position,
                    args=(symbol, filled_price, qty, sl_price, target_price),
                    daemon=True,
                    name=f"Swing-{symbol}"
                ).start()
                print(f"🎯 Swing {symbol} entered @ ₹{filled_price}  SL={sl_price}  Target={target_price}")

                time.sleep(2)   # small gap between orders

        except Exception as e:
            print(f"❌ swing_loop error: {e}")

        time.sleep(300)   # scan every 5 minutes


# ═══════════════════════════════════════════════════════════════════════════════
# 📊  STOCK OPTIONS MODULE  — NFO CE/PE, Daily HalfTrend, MIS Intraday
# ═══════════════════════════════════════════════════════════════════════════════

def load_stock_options_list():
    """
    Read stock symbols from stock_options.txt.
    Format: one NSE symbol per line (e.g. RELIANCE, TCS).
    Lines starting with # are ignored.
    """
    if not os.path.exists(STOCK_OPTIONS_FILE):
        print(f"⚠️ {STOCK_OPTIONS_FILE} not found — create it with one NSE F&O stock per line")
        return []
    with open(STOCK_OPTIONS_FILE) as f:
        return [
            line.strip().upper()
            for line in f
            if line.strip() and not line.strip().startswith("#")
        ]


def find_stock_option(signal, stock_symbol):
    """
    Find the best CE or PE option for a stock on NFO.

    Logic:
      1. Get underlying LTP from NSE:{stock_symbol}
      2. Load NFO instruments filtered by stock name
      3. Nearest expiry, compute ATM strike and step
      4. Try ATM → 1 OTM; pick first option within capital budget
      5. Return (tradingsymbol, premium, actual_qty, "NFO", lot_size)

    Returns (None, None, None, None, None) on failure.
    """
    try:
        opt_type = "CE" if signal == "CALL" else "PE"

        # ── Underlying LTP ────────────────────────────────────────────────────
        ltp = safe_ltp(f"NSE:{stock_symbol}")
        if ltp is None or ltp <= 0:
            print(f"⚠️ find_stock_option: no LTP for {stock_symbol}")
            return None, None, None, None, None

        # ── Load NFO option chain for this stock ──────────────────────────────
        nfo_instruments = get_instruments_cached("NFO")
        today = datetime.now(IST).date()

        opts = [
            i for i in nfo_instruments
            if i["name"] == stock_symbol
            and i["instrument_type"] == opt_type
            and i["expiry"] >= today
        ]

        if not opts:
            print(f"⚠️ find_stock_option: no {opt_type} options found for {stock_symbol}")
            return None, None, None, None, None

        # Nearest expiry
        expiry = sorted(set(i["expiry"] for i in opts))[0]
        opts_exp = [i for i in opts if i["expiry"] == expiry]

        # ── Compute strike step ───────────────────────────────────────────────
        strikes_sorted = sorted(set(int(i["strike"]) for i in opts_exp))
        if len(strikes_sorted) >= 2:
            diffs = [strikes_sorted[j+1] - strikes_sorted[j]
                     for j in range(len(strikes_sorted)-1)]
            step = min(diffs)
        else:
            step = 50   # fallback

        # ── ATM strike ────────────────────────────────────────────────────────
        atm = round(ltp / step) * step

        # Candidate strikes: ATM, then 1 OTM
        if signal == "CALL":
            candidates_strikes = [atm, atm + step]
        else:
            candidates_strikes = [atm, atm - step]

        # ── Lot size from first matching instrument ───────────────────────────
        lot_size = opts_exp[0].get("lot_size", 1) if opts_exp else 1
        if lot_size <= 0:
            lot_size = 1

        # ── Find best affordable option ───────────────────────────────────────
        for target_strike in candidates_strikes:
            matching = [i for i in opts_exp if int(i["strike"]) == target_strike]
            if not matching:
                continue

            inst  = matching[0]
            sym   = f"NFO:{inst['tradingsymbol']}"
            price = safe_ltp(sym)

            if price is None or price <= 0:
                continue

            # How many lots can we buy within capital budget?
            cost_per_lot = price * lot_size
            lots = max(1, int(STOCK_OPTIONS_CAPITAL / cost_per_lot))
            actual_qty = lots * lot_size

            total_cost = price * actual_qty
            if total_cost > STOCK_OPTIONS_CAPITAL * 1.5:
                # Even 1 lot is too expensive — skip
                print(f"   {inst['tradingsymbol']}: 1 lot = ₹{cost_per_lot:,.0f} exceeds capital budget")
                continue

            print(f"✅ Stock option found: {inst['tradingsymbol']}  "
                  f"strike={target_strike}  premium={price:.2f}  "
                  f"lots={lots}  qty={actual_qty}  lot_size={lot_size}")
            return inst["tradingsymbol"], price, actual_qty, "NFO", lot_size

        print(f"⚠️ find_stock_option({stock_symbol}): no affordable option within ATM/1-OTM")
        return None, None, None, None, None

    except Exception as e:
        print(f"❌ find_stock_option({stock_symbol}, {signal}): {e}")
        return None, None, None, None, None


def place_stock_option_order(option_symbol, actual_qty):
    """
    Place a MIS BUY order for a stock option on NFO.
    actual_qty is already in shares (lots × lot_size), bypasses get_quantity().
    Returns filled price or None.
    """
    try:
        full_sym = f"NFO:{option_symbol}"
        ltp = safe_ltp(full_sym)
        if ltp is None or ltp <= 0:
            print(f"❌ place_stock_option_order: invalid LTP for {option_symbol}")
            return None

        price = round(ltp * 1.003, 2)   # 0.3% buffer for fast fill

        # Balance check
        balance = get_balance("NIFTY")   # equity segment
        total_cost = price * actual_qty
        if balance < total_cost * 1.02:
            msg = (f"🚫 Insufficient balance for {option_symbol}\n"
                   f"Need ₹{total_cost:,.0f}, have ₹{balance:,.0f}")
            print(msg)
            send_message(msg)
            return None

        order_id = kite.place_order(
            variety="regular",
            exchange="NFO",
            tradingsymbol=option_symbol,
            transaction_type="BUY",
            quantity=actual_qty,
            order_type="LIMIT",
            price=price,
            product="MIS"
        )
        print(f"📥 Stock option BUY: {option_symbol}  qty={actual_qty}  price={price}")

        filled_price = None
        for attempt in range(5):
            time.sleep(2)
            try:
                for o in kite.orders():
                    if o["order_id"] == order_id:
                        if o["status"] == "COMPLETE":
                            filled_price = o["average_price"]
                            break
                        elif o["status"] in ["CANCELLED", "REJECTED"]:
                            print(f"❌ Stock option BUY {option_symbol} rejected")
                            return None
            except Exception as e:
                print(f"⚠️ Order poll: {e}")
            if filled_price:
                break
            try:
                kite.modify_order(variety="regular", order_id=order_id,
                                  price=round(price * 1.001, 2))
            except Exception:
                pass

        if not filled_price:
            try:
                kite.cancel_order(variety="regular", order_id=order_id)
            except Exception:
                pass
            print(f"❌ Stock option BUY {option_symbol} not filled — cancelled")
            return None

        print(f"✅ Stock option filled: {option_symbol} @ ₹{filled_price}  qty={actual_qty}")
        return filled_price

    except Exception as e:
        print(f"❌ place_stock_option_order({option_symbol}): {e}")
        return None


def sell_stock_option(option_symbol, actual_qty, reason="EXIT"):
    """
    Place a MIS SELL order to exit a stock option position.
    Returns filled price or None.
    """
    try:
        full_sym = f"NFO:{option_symbol}"
        ltp = safe_ltp(full_sym)

        if ltp is None or ltp <= 0:
            # Market order fallback
            order_id = kite.place_order(
                variety="regular", exchange="NFO", tradingsymbol=option_symbol,
                transaction_type="SELL", quantity=actual_qty,
                order_type="MARKET", product="MIS"
            )
        else:
            price = round(ltp * 0.997, 2)
            order_id = kite.place_order(
                variety="regular", exchange="NFO", tradingsymbol=option_symbol,
                transaction_type="SELL", quantity=actual_qty,
                order_type="LIMIT", price=price, product="MIS"
            )
            print(f"📤 Stock option SELL: {option_symbol}  qty={actual_qty}  price={price}  ({reason})")

        filled_price = None
        for attempt in range(6):
            time.sleep(3)
            try:
                for o in kite.orders():
                    if o["order_id"] == order_id:
                        if o["status"] == "COMPLETE":
                            filled_price = o["average_price"]
                            break
                        elif o["status"] in ["CANCELLED", "REJECTED"]:
                            return None
            except Exception as e:
                print(f"⚠️ Sell poll: {e}")
            if filled_price:
                break
            try:
                nudge = round(ltp * (0.995 - attempt * 0.003), 2) if ltp else None
                if nudge and nudge > 0:
                    kite.modify_order(variety="regular", order_id=order_id, price=nudge)
            except Exception:
                pass

        if not filled_price:
            try:
                kite.modify_order(variety="regular", order_id=order_id,
                                  order_type="MARKET", price=0)
                time.sleep(3)
                for o in kite.orders():
                    if o["order_id"] == order_id and o["status"] == "COMPLETE":
                        filled_price = o["average_price"]
            except Exception as e:
                print(f"⚠️ Market fallback: {e}")

        return filled_price

    except Exception as e:
        print(f"❌ sell_stock_option({option_symbol}): {e}")
        return None


def manage_stock_option_position(stock_symbol, option_symbol, entry_price,
                                  actual_qty, signal):
    """
    Monitor a stock option MIS position in its own thread.

    Exit triggers:
      (1) SL hit:       ltp <= entry * (1 - STOCK_OPTIONS_SL_PCT)
      (2) Target hit:   ltp >= entry * (1 + STOCK_OPTIONS_TARGET_PCT)
      (3) Force close:  time >= 3:15 PM IST  (before MIS auto square-off)
    """
    global stock_options_daily_pnl, stock_options_daily_wins
    global stock_options_daily_losses, stock_options_trade_count

    sl_price     = round(entry_price * (1 - STOCK_OPTIONS_SL_PCT),     2)
    target_price = round(entry_price * (1 + STOCK_OPTIONS_TARGET_PCT),  2)

    print(f"👁️ Monitoring stock option: {option_symbol}  "
          f"entry={entry_price:.2f}  SL={sl_price:.2f}  target={target_price:.2f}")

    while True:
        try:
            now_ist = datetime.now(IST)

            # ── Force close at 3:15 PM ────────────────────────────────────────
            force_close_time = (
                now_ist.hour > STOCK_OPT_FORCE_CLOSE_HOUR or
                (now_ist.hour == STOCK_OPT_FORCE_CLOSE_HOUR and
                 now_ist.minute >= STOCK_OPT_FORCE_CLOSE_MIN)
            )

            # ── Get current LTP ───────────────────────────────────────────────
            ltp = safe_ltp(f"NFO:{option_symbol}")
            if ltp is None or ltp <= 0:
                if force_close_time:
                    # LTP unavailable at close time — still try to sell
                    ltp = entry_price   # use entry as fallback for P&L calc
                else:
                    time.sleep(15)
                    continue

            pnl = (ltp - entry_price) * actual_qty
            pnl_pct = ((ltp - entry_price) / entry_price) * 100

            exit_reason = None

            if force_close_time:
                exit_reason = f"⏰ FORCE CLOSE 3:15 PM ({pnl_pct:+.1f}%)"
            elif ltp <= sl_price:
                exit_reason = f"🛑 SL HIT ({pnl_pct:.1f}%)"
            elif ltp >= target_price:
                exit_reason = f"🎯 TARGET HIT (+{pnl_pct:.1f}%)"

            # ── Execute exit ──────────────────────────────────────────────────
            if exit_reason:
                print(f"📤 Stock option exit: {option_symbol}  {exit_reason}  LTP={ltp:.2f}")
                filled_exit = sell_stock_option(option_symbol, actual_qty, reason=exit_reason)

                actual_pnl = (filled_exit - entry_price) * actual_qty if filled_exit else pnl
                exit_price = filled_exit or ltp
                emoji = "✅" if actual_pnl > 0 else "❌"

                with stock_options_positions_lock:
                    stock_options_positions.pop(stock_symbol, None)

                stock_options_daily_pnl    += actual_pnl
                stock_options_trade_count  += 1
                if actual_pnl > 0:
                    stock_options_daily_wins   += 1
                else:
                    stock_options_daily_losses += 1

                log_trade_full(option_symbol, entry_price, exit_price, actual_pnl,
                               f"STOCKOPT:{stock_symbol}", signal, 0)

                send_message(
                    f"{emoji} STOCK OPTION CLOSED — {stock_symbol} {signal}\n"
                    f"{'='*28}\n"
                    f"📌 Option  : {option_symbol}\n"
                    f"📌 Reason  : {exit_reason}\n"
                    f"💰 P&L     : ₹{actual_pnl:+,.0f}  ({(actual_pnl/(entry_price*actual_qty))*100:+.1f}%)\n"
                    f"📊 Entry   : ₹{entry_price:.2f}  |  Exit: ₹{exit_price:.2f}\n"
                    f"📦 Qty     : {actual_qty}\n"
                    f"📅 StockOpt P&L today: ₹{stock_options_daily_pnl:+,.0f}"
                )

                if not filled_exit:
                    send_message(f"⚠️ STOCK OPTION EXIT FAILED: {option_symbol}\n"
                                 f"❗ Check Kite — MIS auto square-off will handle it")
                return   # exit thread

            print(f"📊 StockOpt {option_symbol}: LTP={ltp:.2f}  P&L={pnl_pct:+.1f}%  "
                  f"SL={sl_price:.2f}  Tgt={target_price:.2f}")

        except Exception as e:
            print(f"❌ manage_stock_option_position({option_symbol}): {e}")

        time.sleep(60)   # check every 1 minute


def stock_options_loop():
    """
    Main stock options loop.
    - Reads stock_options.txt every morning
    - At market open: checks last closed daily candle HalfTrend → CALL → buy CE, PUT → buy PE
    - One entry per stock per day (daily signal doesn't change intraday)
    - manage_stock_option_position() thread handles SL / target / 3:15 PM force-close
    """
    _entered_today  = set()   # symbols already entered today
    _last_reset_day = [None]
    print("🚀 Stock options loop started")

    while True:
        try:
            if not ENABLE_STOCK_OPTIONS:
                time.sleep(60)
                continue

            now_ist = datetime.now(IST)

            # ── Reset daily entry guard at midnight ───────────────────────────
            if _last_reset_day[0] != now_ist.date():
                _entered_today.clear()
                _last_reset_day[0] = now_ist.date()
                print("🔄 Stock options: daily entry guard reset")

            # ── Only trade during market hours ────────────────────────────────
            in_window = (
                now_ist.weekday() < 5 and
                (
                    (now_ist.hour == 9 and now_ist.minute >= 30) or
                    (9 < now_ist.hour < STOCK_OPT_FORCE_CLOSE_HOUR) or
                    (now_ist.hour == STOCK_OPT_FORCE_CLOSE_HOUR and
                     now_ist.minute < STOCK_OPT_FORCE_CLOSE_MIN)
                )
            )
            if not in_window:
                time.sleep(60)
                continue

            # Use Screener F&O list if enabled and populated; else fall back to stock_options.txt
            if USE_SCREENER and _screener_fo_stocks_today:
                stocks = _screener_fo_stocks_today
            else:
                stocks = load_stock_options_list()
            if not stocks:
                time.sleep(300)
                continue

            with stock_options_positions_lock:
                open_count = len(stock_options_positions)

            for stock_symbol in stocks:
                # ── Guards ────────────────────────────────────────────────────
                if stock_symbol in _entered_today:
                    continue

                with stock_options_positions_lock:
                    if stock_symbol in stock_options_positions:
                        continue
                    if len(stock_options_positions) >= MAX_STOCK_OPTIONS_POSITIONS:
                        break

                # ── Daily HalfTrend signal ────────────────────────────────────
                token = get_stock_token(stock_symbol)
                if token is None:
                    continue

                df_daily = get_swing_daily_data(stock_symbol, token)
                if df_daily is None or len(df_daily) < 50:
                    print(f"⚠️ StockOpt {stock_symbol}: insufficient daily data")
                    continue

                ht = halftrend_tv(df_daily, amplitude=2, channel_deviation=2)
                if ht is None or len(ht) < 2:
                    continue

                # Last CLOSED daily candle — anti-repaint rule
                trend = int(ht.iloc[-2]["trend"])   # 0=CALL 1=PUT
                signal = "CALL" if trend == 0 else "PUT"

                # Check for an active signal (fresh arrow on last closed candle)
                sig, arrow_idx, is_fresh = get_last_active_signal(ht)
                if sig is None:
                    print(f"   StockOpt {stock_symbol}: no active signal — skipping")
                    continue

                signal = sig   # use signal from get_last_active_signal
                bars_ago = (len(ht) - arrow_idx - 2) if arrow_idx is not None else "?"
                freshness = "FRESH" if is_fresh else f"CARRY-OVER ({bars_ago} days ago)"
                print(f"🔔 StockOpt {stock_symbol}: {signal} signal ({freshness})")

                # ── Find option ───────────────────────────────────────────────
                opt_symbol, opt_price, actual_qty, exchange, lot_size = \
                    find_stock_option(signal, stock_symbol)

                if not opt_symbol or actual_qty is None:
                    _noopt_key = f"STOCKOPT_{stock_symbol}_noopt_{now_ist.strftime('%Y-%m-%d')}"
                    if getattr(stock_options_loop, "_noopt_alerted", None) != _noopt_key:
                        send_message(
                            f"⚠️ STOCK OPTION: No suitable {signal} option for {stock_symbol}\n"
                            f"Check option chain — may be illiquid or strike out of range"
                        )
                        stock_options_loop._noopt_alerted = _noopt_key
                    continue

                # ── Place order ───────────────────────────────────────────────
                filled_price = place_stock_option_order(opt_symbol, actual_qty)
                if not filled_price:
                    continue

                sl_price     = round(filled_price * (1 - STOCK_OPTIONS_SL_PCT),    2)
                target_price = round(filled_price * (1 + STOCK_OPTIONS_TARGET_PCT), 2)

                with stock_options_positions_lock:
                    stock_options_positions[stock_symbol] = {
                        "option_symbol": opt_symbol,
                        "entry":         filled_price,
                        "qty":           actual_qty,
                        "sl":            sl_price,
                        "target":        target_price,
                        "signal":        signal,
                        "exchange":      "NFO",
                    }

                _entered_today.add(stock_symbol)

                send_message(
                    f"🆕 STOCK OPTION ENTRY — {stock_symbol} {signal}\n"
                    f"{'='*28}\n"
                    f"📌 Option   : {opt_symbol}\n"
                    f"📈 Signal   : {freshness}\n"
                    f"💰 Entry    : ₹{filled_price:.2f}  |  Qty: {actual_qty}\n"
                    f"🛑 SL       : ₹{sl_price:.2f}  (-{STOCK_OPTIONS_SL_PCT*100:.0f}%)\n"
                    f"🎯 Target   : ₹{target_price:.2f}  (+{STOCK_OPTIONS_TARGET_PCT*100:.0f}%)\n"
                    f"⏰ Force close: 3:15 PM IST\n"
                    f"💼 Deployed : ₹{filled_price*actual_qty:,.0f}"
                )

                threading.Thread(
                    target=manage_stock_option_position,
                    args=(stock_symbol, opt_symbol, filled_price, actual_qty, signal),
                    daemon=True,
                    name=f"StockOpt-{stock_symbol}"
                ).start()
                print(f"🎯 StockOpt {stock_symbol}: {opt_symbol} @ ₹{filled_price}")
                time.sleep(2)

        except Exception as e:
            print(f"❌ stock_options_loop error: {e}")

        time.sleep(120)   # scan every 2 minutes


def send_stock_options_eod_report():
    """Send stock options summary at 3:35 PM. Reads from CSV for restart safety."""
    try:
        df = pd.read_csv(TRADE_LOG_FILE)
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        df = df[df["time"].dt.strftime("%Y-%m-%d") == today_str]
        df = df[df["instrument"].str.startswith("STOCKOPT:", na=False)]
        csv_pnl    = float(df["pnl"].sum())   if not df.empty else 0
        csv_wins   = int((df["pnl"] > 0).sum()) if not df.empty else 0
        csv_losses = int((df["pnl"] <= 0).sum()) if not df.empty else 0
        csv_count  = len(df)
    except Exception:
        csv_pnl, csv_wins, csv_losses, csv_count = 0, 0, 0, 0

    pnl    = max(csv_pnl,    stock_options_daily_pnl,    key=abs) if csv_count or stock_options_trade_count else 0
    wins   = max(csv_wins,   stock_options_daily_wins)
    losses = max(csv_losses, stock_options_daily_losses)
    count  = max(csv_count,  stock_options_trade_count)

    with stock_options_positions_lock:
        open_names = ", ".join(stock_options_positions.keys()) or "None"

    total = wins + losses
    wr = (wins / total * 100) if total > 0 else 0

    send_message(
        f"📊 STOCK OPTIONS SUMMARY\n"
        f"{'='*28}\n"
        f"💰 P&L today   : ₹{pnl:+,.0f}\n"
        f"📈 Trades      : {total}  (✅ {wins}W  ❌ {losses}L)  WR={wr:.0f}%\n"
        f"📦 Still open  : {open_names}\n"
        f"⏰ Report time : 3:35 PM IST"
    )


def send_swing_eod_report():
    """Send swing trade summary at 3:34 PM. Reads from CSV for restart safety."""
    csv_pnl, csv_wins, csv_losses, csv_count = _read_today_csv_swing()

    pnl    = max(csv_pnl,    swing_daily_pnl,    key=abs) if csv_count or swing_trade_count else swing_daily_pnl
    wins   = max(csv_wins,   swing_daily_wins)
    losses = max(csv_losses, swing_daily_losses)
    count  = max(csv_count,  swing_trade_count)

    with swing_positions_lock:
        open_count = len(swing_positions)
        open_names = ", ".join(swing_positions.keys()) if swing_positions else "None"

    total = wins + losses
    wr = (wins / total * 100) if total > 0 else 0

    send_message(
        f"📈 SWING TRADE SUMMARY\n"
        f"{'='*28}\n"
        f"💰 Closed P&L : ₹{pnl:+,.0f}\n"
        f"📊 Closed      : {total}  (✅ {wins}W  ❌ {losses}L)  WR={wr:.0f}%\n"
        f"📦 Open now    : {open_count} position(s) — {open_names}\n"
        f"⏰ Report time : 3:34 PM IST"
    )


def _read_today_csv_swing():
    """Read today's SWING trades from CSV for EOD report."""
    try:
        if not os.path.exists(TRADE_LOG_FILE):
            return 0, 0, 0, 0
        df = pd.read_csv(TRADE_LOG_FILE)
        df["time"] = pd.to_datetime(df["time"], errors="coerce")
        today_str = datetime.now(IST).strftime("%Y-%m-%d")
        df = df[df["time"].dt.strftime("%Y-%m-%d") == today_str]
        df = df[df["instrument"].str.startswith("SWING:", na=False)]
        if df.empty:
            return 0, 0, 0, 0
        pnl    = float(df["pnl"].sum())
        wins   = int((df["pnl"] > 0).sum())
        losses = int((df["pnl"] <= 0).sum())
        count  = len(df)
        return pnl, wins, losses, count
    except Exception as e:
        print(f"⚠️ _read_today_csv_swing: {e}")
        return 0, 0, 0, 0


def send_sensex_eod_report():
    """Sent at 3:33 PM. Uses Kite positions API as primary source (restart-safe)."""
    pnl, wins, losses, count = _best_day_pnl(
        "SENSEX", sensex_daily_pnl, sensex_daily_wins, sensex_daily_losses, sensex_trade_count
    )

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
    """Sent at 3:32 PM. Uses Kite positions API as primary source (restart-safe)."""
    pnl, wins, losses, count = _best_day_pnl(
        "BANKNIFTY", banknifty_daily_pnl, banknifty_daily_wins, banknifty_daily_losses, banknifty_trade_count
    )

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
    # 🔍 API TEST — halt if token invalid
    # -----------------------------
    _api_ok = False
    for _api_attempt in range(3):
        try:
            print(f"🔍 Testing Kite API (attempt {_api_attempt+1}/3)...", flush=True)
            test = kite.ltp("NSE:NIFTY 50")
            print("✅ Kite API working:", test, flush=True)
            _api_ok = True
            break
        except Exception as _api_err:
            print(f"❌ Kite API FAILED (attempt {_api_attempt+1}/3): {_api_err}", flush=True)
            if _api_attempt < 2:
                print("   Retrying login...", flush=True)
                try:
                    zerodha_auto_login()
                except Exception:
                    pass
                time.sleep(5)

    if not _api_ok:
        _err_msg = (
            f"❌ KITE API INVALID — BOT HALTED\n"
            f"api_key or access_token is invalid.\n"
            f"Tried 3 times — all failed.\n\n"
            f"Fix steps:\n"
            f"1. Check KITE_API_KEY in Railway vars\n"
            f"2. Check KITE_PASSWORD is correct\n"
            f"3. Check KITE_TOTP_SECRET is correct\n"
            f"4. Whitelist IP at developers.kite.trade\n"
            f"5. Redeploy after fixing"
        )
        print(_err_msg, flush=True)
        try:
            send_message(_err_msg)
        except Exception:
            pass
        # Stop — no point starting loops with invalid token
        import sys
        sys.exit(1)

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

    # ── Restore today's trade counters (survives mid-day redeploy) ───────────
    restore_daily_state()

    # ── Mark today as already reset so loops don't wipe restored counters ────
    from datetime import date as _date
    last_reset_date = _date.today()
    print(f"📅 last_reset_date set to {last_reset_date} — restored counters protected", flush=True)

    # ── Daily report scheduler ──────────────────────────────────────────────
    _nifty_eod_sent       = [False]   # mutable so inner function can write
    _banknifty_eod_sent   = [False]
    _sensex_eod_sent      = [False]
    _swing_eod_sent       = [False]
    _stockopt_eod_sent    = [False]
    _crude_eod_sent       = [False]
    _full_report_sent     = [False]
    _screener_refresh_sent = [False]    # daily 8:55 AM Screener fetch flag

    def daily_report_scheduler():
        """
        Sends end-of-day reports at precise times:
          8:55 AM IST  → Screener.in screen fetch (stock universe refresh)
          3:31 PM IST  → Nifty session closed report
          3:32 PM IST  → BankNifty session closed report
          3:33 PM IST  → SENSEX session closed report
          3:34 PM IST  → Swing trade summary (open positions + today's closed)
         11:31 PM IST  → Crude session closed report  (after 11:25 PM force-close)
         11:32 PM IST  → Combined NIFTY + BANKNIFTY + SENSEX + CRUDE daily report
        Resets sent-flags at midnight.
        """
        while True:
            try:
                now = datetime.now(IST)

                # Reset flags at midnight
                if now.hour == 0 and now.minute < 2:
                    _nifty_eod_sent[0]       = False
                    _banknifty_eod_sent[0]   = False
                    _sensex_eod_sent[0]      = False
                    daily_report_scheduler._finnifty_eod_sent = False
                    _swing_eod_sent[0]       = False
                    _stockopt_eod_sent[0]    = False
                    _crude_eod_sent[0]       = False
                    _full_report_sent[0]     = False
                    _screener_refresh_sent[0]= False

                # 8:55 AM weekdays — Screener.in stock universe refresh (only when enabled)
                if (USE_SCREENER
                        and now.hour == 8 and now.minute == 55
                        and now.weekday() < 5
                        and not _screener_refresh_sent[0]):
                    threading.Thread(
                        target=refresh_screener_daily,
                        daemon=True,
                        name="ScreenerRefresh"
                    ).start()
                    _screener_refresh_sent[0] = True

                # ── EOD Reports — weekdays only (Mon=0 to Fri=4) ──────────────
                _is_weekday = now.weekday() < 5

                # 3:31 PM — Nifty EOD report
                if _is_weekday and now.hour == 15 and now.minute == 31 and not _nifty_eod_sent[0]:
                    send_nifty_eod_report()
                    _nifty_eod_sent[0] = True

                # 3:32 PM — BankNifty EOD report
                if _is_weekday and now.hour == 15 and now.minute == 32 and not _banknifty_eod_sent[0]:
                    send_banknifty_eod_report()
                    _banknifty_eod_sent[0] = True

                # 3:32 PM (30s later) — FINNIFTY EOD report
                if _is_weekday and now.hour == 15 and now.minute == 32 and now.second >= 30 and not getattr(daily_report_scheduler, "_finnifty_eod_sent", False):
                    try:
                        fn_pnl_r, fn_w, fn_l, fn_c = _kite_day_pnl("FINNIFTY")
                        emoji = "✅" if fn_pnl_r >= 0 else "❌"
                        _today_str = datetime.now(IST).strftime("%d %b %Y")
                        _wr = f"{fn_w/max(fn_c,1)*100:.0f}%"
                        send_message(
                            f"{emoji} FINNIFTY SESSION CLOSED\n"
                            f"📅 {_today_str}\n"
                            f"💰 Net P&L    : Rs.{fn_pnl_r:,.0f}\n"
                            f"📊 Trades     : {fn_c}  ({fn_w} wins  {fn_l} losses)\n"
                            f"🎯 Win Rate   : {_wr}"
                        )
                    except Exception as _fe:
                        print(f"⚠️ FINNIFTY EOD report error: {_fe}", flush=True)
                    daily_report_scheduler._finnifty_eod_sent = True

                # 3:33 PM — SENSEX EOD report (same session as NIFTY/BANKNIFTY)
                if _is_weekday and now.hour == 15 and now.minute == 33 and not _sensex_eod_sent[0]:
                    send_sensex_eod_report()
                    _sensex_eod_sent[0] = True

                # 3:34 PM — Swing trade summary
                if _is_weekday and now.hour == 15 and now.minute == 34 and not _swing_eod_sent[0]:
                    send_swing_eod_report()
                    _swing_eod_sent[0] = True

                # 3:35 PM — Stock options summary
                if _is_weekday and now.hour == 15 and now.minute == 35 and not _stockopt_eod_sent[0]:
                    send_stock_options_eod_report()
                    _stockopt_eod_sent[0] = True

                # 11:31 PM — Crude EOD report (after force-close at 11:25 PM, all trades done)
                if _is_weekday and now.hour == 23 and now.minute == 31 and not _crude_eod_sent[0]:
                    send_crude_eod_report()
                    _crude_eod_sent[0] = True

                # 11:32 PM — Combined daily report (all instruments together)
                if _is_weekday and now.hour == 23 and now.minute == 32 and not _full_report_sent[0]:
                    send_daily_report()
                    _full_report_sent[0] = True

            except Exception as e:
                print(f"❌ Report scheduler error: {e}")

            time.sleep(30)   # check every 30 seconds

    threading.Thread(target=daily_report_scheduler, daemon=True, name="ReportScheduler").start()
    threading.Thread(target=daily_profit_target_monitor, daemon=True, name="ProfitTargetMonitor").start()

    _claude_filter_cache.clear()
    _claude_flip_counter.clear()
    print("🔍 Signal filter cache cleared on startup", flush=True)

    threading.Thread(target=nifty_loop, daemon=True).start()
    print(f"✅ NIFTY loop started (token={config.NIFTY_TOKEN})", flush=True)

    if BANKNIFTY_TOKEN:
        threading.Thread(target=banknifty_loop, daemon=True).start()
        print(f"✅ BANKNIFTY loop started (token={BANKNIFTY_TOKEN})", flush=True)
    else:
        print("❌ BANKNIFTY LOOP SKIPPED — BANKNIFTY_TOKEN missing from config.py", flush=True)

    if FINNIFTY_TOKEN:
        threading.Thread(target=finnifty_loop, daemon=True).start()
        print(f"✅ FINNIFTY loop started (token={FINNIFTY_TOKEN})", flush=True)
    else:
        print("❌ FINNIFTY LOOP SKIPPED — FINNIFTY_TOKEN missing from config.py", flush=True)

    if SENSEX_TOKEN:
        threading.Thread(target=sensex_loop, daemon=True).start()
        print(f"✅ SENSEX loop started (token={SENSEX_TOKEN})", flush=True)
    else:
        print("❌ SENSEX LOOP SKIPPED — SENSEX_TOKEN missing from config.py", flush=True)
        send_message("⚠️ SENSEX loop NOT started — SENSEX_TOKEN missing from config.py")

    if ENABLE_SWING:
        threading.Thread(target=swing_loop, daemon=True, name="SwingLoop").start()
    else:
        print("⚠️ SWING LOOP SKIPPED (ENABLE_SWING=False)")

    if ENABLE_STOCK_OPTIONS:
        threading.Thread(target=stock_options_loop, daemon=True, name="StockOptionsLoop").start()
    else:
        print("⚠️ STOCK OPTIONS LOOP SKIPPED (ENABLE_STOCK_OPTIONS=False)")

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
        fn_status       = f"✅ Token: {FINNIFTY_TOKEN}"  if FINNIFTY_TOKEN  else "⚠️ DISABLED (token not found)"
        crude_status    = f"✅ Token: {CRUDE_TOKEN}"    if CRUDE_TOKEN    else "⚠️ DISABLED (token not found)"
        nifty_status    = f"✅ Token: {config.NIFTY_TOKEN}"
        bn_status       = f"✅ Token: {BANKNIFTY_TOKEN}" if BANKNIFTY_TOKEN else "⚠️ DISABLED (token not found)"
        sx_status       = f"✅ Token: {SENSEX_TOKEN}"    if SENSEX_TOKEN    else "⚠️ DISABLED (token not found)"
        send_message(
            f"🚀 HALFTREND BOT STARTED\n"
            f"{'='*28}\n"
            f"📌 NIFTY     : {nifty_status}\n"
            f"   Hours     : 9:20 AM – 3:20 PM IST | Lot: 65 qty\n"
            f"\n"
            f"🏦 BANKNIFTY : {bn_status}\n"
            f"   Hours     : 9:20 AM – 3:20 PM IST | Lot: 30 qty\n"
            f"\n"
            f"📈 FINNIFTY  : {fn_status}\n"
            f"   Hours     : 9:20 AM – 3:20 PM IST | Lot: 60 qty\n"
            f"\n"
            f"📊 SENSEX    : {sx_status}\n"
            f"   Hours     : 9:20 AM – 3:20 PM IST | Lot: 20 qty\n"
            f"\n"
            f"🛢️ CRUDE     : {crude_status}\n"
            f"   Hours     : 3:30 PM – 11:25 PM IST | Lot: 100 qty\n"
            f"\n"
            f"⚙️ Signal : HalfTrend + Hull Suite (5-min candles)\n"
            f"⚙️ SL     : 45% single-tier (enable USE_STOP_LOSS=True)\n"
            f"⚙️ Target : HalfTrend flip exit | Profit lock from ₹1,000\n"
            f"⚙️ Flip   : Immediate exit + re-entry on arrow reversal\n"
            f"\n"
            f"📅 Reports: NIFTY@3:31 | BankNifty@3:32 | FINNIFTY@3:32:30 | SENSEX@3:33 | Crude@11:31"
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
            # Also retry at 8:10 AM if the 8:00 AM attempt failed (_access_token_refreshed_today
            # stores True only on success, so 8:10 AM retry fires automatically if needed).
            _in_refresh_window = (
                now.weekday() < 5
                and not _access_token_refreshed_today[0]
                and (
                    (now.hour == 8 and now.minute < 5)          # primary: 8:00–8:04
                    or (now.hour == 8 and 10 <= now.minute < 15) # retry-1: 8:10–8:14
                    or (now.hour == 8 and 20 <= now.minute < 25) # retry-2: 8:20–8:24
                )
            )
            if _in_refresh_window:
                _attempt_label = (
                    "primary (8:00 AM)"   if now.minute < 5  else
                    "retry-1 (8:10 AM)"   if now.minute < 15 else
                    "retry-2 (8:20 AM)"
                )
                print(f"🔑 Daily access token refresh — {_attempt_label}...", flush=True)
                _refresh_error = [None]

                # Update IP whitelist before login (Railway may assign new IP after restart)
                threading.Thread(target=update_kite_ip_whitelist, daemon=True).start()

                # zerodha_auto_login() already retries 3× internally with backoff
                try:
                    new_token = zerodha_auto_login()
                except Exception as _ex:
                    new_token = None
                    _refresh_error[0] = str(_ex)

                ist_str = now.strftime("%d %b %Y %H:%M IST")
                if new_token:
                    _access_token_refreshed_today[0] = True  # mark success → stop retrying
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
                    # Only spam Telegram on the last retry window, not every attempt
                    is_last_retry = (now.hour == 8 and 20 <= now.minute < 25)
                    print(f"❌ Token refresh failed [{_attempt_label}]: {err_detail}", flush=True)
                    if is_last_retry:
                        try:
                            send_message(
                                f"❌ KITE TOKEN REFRESH FAILED (all retries exhausted)\n"
                                f"{'='*28}\n"
                                f"🕐 Time  : {ist_str}\n"
                                f"❗ Error : {err_detail[:300]}\n"
                                f"⚠️ Please refresh token manually via Kite!\n"
                                f"💡 Tip: Check Railway logs for HTTP status / body details"
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


    # end of __main__