import requests
import time
import datetime
import pytz
import pandas as pd
import threading
from kiteconnect import KiteConnect
import config
from telegram_bot import send_message

kite = KiteConnect(api_key=config.API_KEY)
kite.set_access_token(config.ACCESS_TOKEN)

IST = pytz.timezone("Asia/Kolkata")
SIGNAL_URL = "https://avi-bot-1.onrender.com/signal"

# -----------------------------
# STATES
# -----------------------------
nifty_active = False
crude_active = False

# -----------------------------
# BASIC
# -----------------------------
def get_ltp(symbol):
    return kite.ltp(symbol)[symbol]["last_price"]

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
# NIFTY STRATEGIES
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
        return "CALL" if df.iloc[-1]["close"] > df.iloc[-1]["vwap"] else "PUT"
    except:
        return "HOLD"

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
        ltp = kite.ltp("NSE:NIFTY 50")["NSE:NIFTY 50"]["last_price"]
        return "CALL" if ltp > pivot else "PUT"
    except:
        return "HOLD"

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

def get_final_signal():
    for fn in [ml_signal, breakout_signal, vwap_signal, pivot_signal, momentum_signal]:
        sig = fn()
        if sig != "HOLD":
            return sig
    return "HOLD"

# -----------------------------
# PRO CRUDE STRATEGY
# -----------------------------
def get_crude_signal():

    try:
        now = datetime.datetime.now()

        df = pd.DataFrame(kite.historical_data(
            config.CRUDE_TOKEN,
            now - datetime.timedelta(hours=2),
            now,
            "5minute"
        ))

        if len(df) < 20:
            return "HOLD"

        df["vwap"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()
        df["vol_ma"] = df["volume"].rolling(10).mean()

        last = df.iloc[-1]
        prev = df.iloc[-2]

        body = abs(last["close"] - last["open"])
        rng = last["high"] - last["low"]

        strong = body > rng * 0.5
        small = body < rng * 0.3

        vol_spike = last["volume"] > last["vol_ma"]

        above_vwap = last["close"] > last["vwap"]
        below_vwap = last["close"] < last["vwap"]

        breakout_up = last["close"] > prev["high"]
        breakout_down = last["close"] < prev["low"]

        if small:
            return "HOLD"

        if breakout_up and above_vwap and vol_spike and strong:
            print("CRUDE CALL")
            return "CALL"

        if breakout_down and below_vwap and vol_spike and strong:
            print("CRUDE PUT")
            return "PUT"

        return "HOLD"

    except Exception as e:
        print("Crude error:", e)
        return "HOLD"

# -----------------------------
# OPTION SELECTOR
# -----------------------------
def find_option(signal, instrument):

    if instrument == "NIFTY":
        exchange = "NFO"
        name = "NIFTY"
        lot = config.NIFTY_LOT
        pmin, pmax = config.MIN_PREMIUM, config.MAX_PREMIUM
    else:
        exchange = "MCX"
        name = "CRUDEOIL"
        lot = config.CRUDE_LOT
        pmin, pmax = 50, 300

    instruments = kite.instruments(exchange)

    opts = [
        i for i in instruments
        if name in i["name"] and i["instrument_type"] in ["CE", "PE"]
    ]

    today = datetime.datetime.now().date()
    expiry = sorted(set(i["expiry"] for i in opts if i["expiry"] >= today))[0]

    opt_type = "CE" if signal == "CALL" else "PE"

    best, best_price = None, None

    for i in opts:

        if i["expiry"] != expiry or i["instrument_type"] != opt_type:
            continue

        symbol = f"{exchange}:{i['tradingsymbol']}"

        try:
            price = kite.ltp(symbol)[symbol]["last_price"]
        except:
            continue

        if pmin <= price <= pmax:
            if best_price is None or abs(price - 100) < abs(best_price - 100):
                best = i["tradingsymbol"]
                best_price = price

    return best, best_price, lot, exchange

# -----------------------------
# ORDER
# -----------------------------
def place_order(symbol, qty, exchange):
    try:
        kite.place_order(
            variety="regular",
            exchange=exchange,
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
def manage_trade(symbol, entry, qty, exchange, instrument):

    sl = entry * (0.80 if instrument == "CRUDE" else 0.90)
    target = entry * (1.30 if instrument == "CRUDE" else 1.18)

    send_message(f"🚀 {instrument} TRADE\n{symbol} @ {entry}")

    while True:
        try:
            ltp = kite.ltp(f"{exchange}:{symbol}")[f"{exchange}:{symbol}"]["last_price"]

            if ltp >= target:
                send_message(f"🎯 {instrument} TARGET")
                break

            if ltp <= sl:
                send_message(f"🛑 {instrument} SL")
                break

            time.sleep(5)

        except:
            break

# -----------------------------
# THREADS
# -----------------------------
def nifty_loop():
    global nifty_active

    while True:
        now = datetime.datetime.now(IST)

        if not (9 <= now.hour < 15):
            time.sleep(60)
            continue

        if nifty_active:
            time.sleep(5)
            continue

        signal = get_final_signal()

        if signal == "HOLD":
            time.sleep(10)
            continue

        symbol, price, lot, exchange = find_option(signal, "NIFTY")

        if symbol and place_order(symbol, lot, exchange):
            nifty_active = True
            manage_trade(symbol, price, lot, exchange, "NIFTY")
            nifty_active = False


def crude_loop():
    global crude_active

    while True:
        now = datetime.datetime.now(IST)

        if not (9 <= now.hour < 23):
            time.sleep(60)
            continue

        if crude_active:
            time.sleep(5)
            continue

        signal = get_crude_signal()

        if signal == "HOLD":
            time.sleep(10)
            continue

        symbol, price, lot, exchange = find_option(signal, "CRUDE")

        if symbol and place_order(symbol, lot, exchange):
            crude_active = True
            manage_trade(symbol, price, lot, exchange, "CRUDE")
            crude_active = False

# -----------------------------
# MAIN
# -----------------------------
if __name__ == "__main__":

    send_message("🚀 BOT STARTED (FINAL PRO MODE)")

    threading.Thread(target=nifty_loop).start()
    threading.Thread(target=crude_loop).start()