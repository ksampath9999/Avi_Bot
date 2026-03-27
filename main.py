import requests
import time
import datetime
import pytz
import pandas as pd
from kiteconnect import KiteConnect
import config
from telegram_bot import send_message

# -----------------------------
# INIT
# -----------------------------
kite = KiteConnect(api_key=config.API_KEY)
kite.set_access_token(config.ACCESS_TOKEN)

IST = pytz.timezone("Asia/Kolkata")
SIGNAL_URL = "https://avi-bot-1.onrender.com/signal"

trade_active = False
last_trade_time = None
report_sent = False

# -----------------------------
# PNL TRACKING
# -----------------------------
daily_pnl = 0
total_trades = 0
wins = 0
losses = 0


# -----------------------------
# BASIC
# -----------------------------
def get_ltp(symbol):
    return kite.ltp(symbol)[symbol]["last_price"]

def calculate_qty(price):
    return config.LOT_SIZE


# -----------------------------
# ML SIGNAL
# -----------------------------
def ml_signal():
    try:
        data = requests.get(SIGNAL_URL, timeout=5).json()
        sig = data.get("signal", "HOLD")
        if sig in ["CALL", "PUT"]:
            return sig
    except:
        pass
    return "HOLD"


# -----------------------------
# BREAKOUT
# -----------------------------
def breakout_signal():
    try:
        now = datetime.datetime.now()
        df = pd.DataFrame(kite.historical_data(
            config.NIFTY_TOKEN,
            now - datetime.timedelta(minutes=30),
            now,
            "5minute"
        ))

        if len(df) < 3:
            return "HOLD"

        if df.iloc[-1]["close"] > df.iloc[-2]["high"]:
            return "CALL"
        if df.iloc[-1]["close"] < df.iloc[-2]["low"]:
            return "PUT"

        return "HOLD"
    except:
        return "HOLD"


# -----------------------------
# VWAP
# -----------------------------
def vwap_signal():
    try:
        now = datetime.datetime.now()
        df = pd.DataFrame(kite.historical_data(
            config.NIFTY_TOKEN,
            now - datetime.timedelta(hours=2),
            now,
            "5minute"
        ))

        if len(df) < 10:
            return "HOLD"

        df["vwap"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()

        if df.iloc[-1]["close"] > df.iloc[-1]["vwap"]:
            return "CALL"
        else:
            return "PUT"

    except:
        return "HOLD"


# -----------------------------
# PIVOT
# -----------------------------
def pivot_signal():
    try:
        now = datetime.datetime.now()
        df = pd.DataFrame(kite.historical_data(
            config.NIFTY_TOKEN,
            now - datetime.timedelta(days=3),
            now,
            "day"
        ))

        prev = df.iloc[-2]

        pivot = (prev["high"] + prev["low"] + prev["close"]) / 3
        r1 = 2 * pivot - prev["low"]
        s1 = 2 * pivot - prev["high"]

        ltp = kite.ltp("NSE:NIFTY 50")["NSE:NIFTY 50"]["last_price"]

        if ltp > r1:
            return "CALL"
        if ltp < s1:
            return "PUT"
        if ltp > pivot:
            return "CALL"
        return "PUT"

    except:
        return "HOLD"


# -----------------------------
# MOMENTUM
# -----------------------------
def momentum_signal():
    try:
        now = datetime.datetime.now()
        df = pd.DataFrame(kite.historical_data(
            config.NIFTY_TOKEN,
            now - datetime.timedelta(minutes=15),
            now,
            "5minute"
        ))

        last = df.iloc[-1]
        body = abs(last["close"] - last["open"])
        rng = last["high"] - last["low"]

        if body > rng * 0.6:
            return "CALL" if last["close"] > last["open"] else "PUT"

        return "HOLD"

    except:
        return "HOLD"


# -----------------------------
# FINAL SIGNAL
# -----------------------------
def get_final_signal():

    for fn in [ml_signal, breakout_signal, vwap_signal, pivot_signal, momentum_signal]:
        sig = fn()
        if sig != "HOLD":
            print("Signal from", fn.__name__, ":", sig)
            return sig

    return "HOLD"


# -----------------------------
# OPTION SELECT
# -----------------------------
def find_option(signal):

    nifty = kite.ltp("NSE:NIFTY 50")["NSE:NIFTY 50"]["last_price"]
    atm = round(nifty / 50) * 50

    instruments = kite.instruments("NFO")

    opts = [i for i in instruments if i["name"] == "NIFTY"]

    expiry = sorted(set(i["expiry"] for i in opts if i["expiry"] >= datetime.datetime.now().date()))[0]

    opt_type = "CE" if signal == "CALL" else "PE"

    best = None
    best_price = None

    for i in opts:

        if i["expiry"] != expiry or i["instrument_type"] != opt_type:
            continue

        symbol = f"NFO:{i['tradingsymbol']}"

        try:
            price = kite.ltp(symbol)[symbol]["last_price"]
        except:
            continue

        if 30 <= price <= 150:
            if best_price is None or abs(price - 80) < abs(best_price - 80):
                best = i["tradingsymbol"]
                best_price = price

    return best, best_price


# -----------------------------
# ORDER
# -----------------------------
def place_order(symbol, qty):
    try:
        kite.place_order(
            variety="regular",
            exchange="NFO",
            tradingsymbol=symbol,
            transaction_type="BUY",
            quantity=qty,
            order_type="MARKET",
            product="MIS"
        )
        send_message(f"✅ Order: {symbol}")
        return True
    except Exception as e:
        send_message(f"❌ Order error: {e}")
        return False


# -----------------------------
# TRADE MGMT
# -----------------------------
def manage_trade(symbol, entry, qty):

    global trade_active, daily_pnl, total_trades, wins, losses

    sl = entry * 0.90
    target = entry * 1.18

    total_trades += 1

    send_message(f"🚀 TRADE\n{symbol} @ {entry}")

    while True:
        try:
            ltp = get_ltp(f"NFO:{symbol}")
            pnl = (ltp - entry) * qty

            if ltp >= target:
                daily_pnl += pnl
                wins += 1
                send_message(f"🎯 TARGET ₹{round(pnl,2)}")
                break

            if ltp <= sl:
                daily_pnl -= abs(pnl)
                losses += 1
                send_message(f"🛑 SL ₹{round(pnl,2)}")
                break

            time.sleep(5)

        except:
            break

    trade_active = False


# -----------------------------
# REPORT (ONLY ONCE)
# -----------------------------
def send_report():
    win_rate = (wins / total_trades * 100) if total_trades else 0

    msg = f"""
📊 DAILY REPORT

Trades: {total_trades}
Wins: {wins}
Losses: {losses}

Win Rate: {round(win_rate,2)}%
PnL: ₹{round(daily_pnl,2)}
"""
    send_message(msg)


# -----------------------------
# MAIN LOOP
# -----------------------------
def run_bot():

    global trade_active, last_trade_time, report_sent

    send_message("🚀 BOT STARTED (ULTIMATE PRO MODE)")

    while True:

        now = datetime.datetime.now(IST)

        # ONLY ONE REPORT AFTER 3:30
        if now.hour >= 15 and not report_sent:
            send_report()
            report_sent = True

        if now.hour < 9:
            time.sleep(60)
            continue

        if now.hour >= 15:
            time.sleep(300)
            continue

        if trade_active:
            time.sleep(10)
            continue

        if last_trade_time:
            if (datetime.datetime.now() - last_trade_time).seconds < 120:
                time.sleep(20)
                continue

        signal = get_final_signal()

        if signal == "HOLD":
            time.sleep(30)
            continue

        symbol, price = find_option(signal)

        if not symbol:
            time.sleep(30)
            continue

        qty = calculate_qty(price)

        if place_order(symbol, qty):
            trade_active = True
            last_trade_time = datetime.datetime.now()
            manage_trade(symbol, price, qty)

        time.sleep(30)


if __name__ == "__main__":
    run_bot()