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

# -----------------------------
# PNL TRACKING
# -----------------------------
daily_pnl = 0
total_trades = 0
wins = 0
losses = 0


# -----------------------------
# LTP
# -----------------------------
def get_ltp(symbol):
    return kite.ltp(symbol)[symbol]["last_price"]


# -----------------------------
# FIXED LOT
# -----------------------------
def calculate_qty(price):
    return config.LOT_SIZE   # 65


# -----------------------------
# STRATEGY 1: ML SIGNAL
# -----------------------------
def ml_signal():
    try:
        data = requests.get(SIGNAL_URL, timeout=5).json()
        signal = data.get("signal", "HOLD")

        if signal in ["CALL", "PUT"]:
            print("ML Signal:", signal)
            return signal

    except:
        pass

    return "HOLD"


# -----------------------------
# STRATEGY 2: BREAKOUT
# -----------------------------
def breakout_signal():
    try:
        candles = kite.historical_data(
            config.NIFTY_TOKEN,
            datetime.datetime.now() - datetime.timedelta(minutes=30),
            datetime.datetime.now(),
            "5minute"
        )

        df = pd.DataFrame(candles)

        if len(df) < 3:
            return "HOLD"

        last = df.iloc[-1]
        prev = df.iloc[-2]

        if last["close"] > prev["high"]:
            print("Breakout → CALL")
            return "CALL"

        elif last["close"] < prev["low"]:
            print("Breakout → PUT")
            return "PUT"

        return "HOLD"

    except:
        return "HOLD"


# -----------------------------
# STRATEGY 3: MOMENTUM
# -----------------------------
def momentum_signal():
    try:
        candles = kite.historical_data(
            config.NIFTY_TOKEN,
            datetime.datetime.now() - datetime.timedelta(minutes=15),
            datetime.datetime.now(),
            "5minute"
        )

        df = pd.DataFrame(candles)

        if len(df) < 2:
            return "HOLD"

        last = df.iloc[-1]

        body = abs(last["close"] - last["open"])
        range_candle = last["high"] - last["low"]

        # strong candle condition
        if body > (range_candle * 0.6):

            if last["close"] > last["open"]:
                print("Momentum → CALL")
                return "CALL"
            else:
                print("Momentum → PUT")
                return "PUT"

        return "HOLD"

    except:
        return "HOLD"


# -----------------------------
# FINAL SIGNAL DECISION
# -----------------------------
def get_final_signal():

    # Priority 1: ML
    signal = ml_signal()
    if signal != "HOLD":
        return signal

    # Priority 2: Breakout
    signal = breakout_signal()
    if signal != "HOLD":
        return signal

    # Priority 3: Momentum
    signal = momentum_signal()
    return signal


# -----------------------------
# OPTION SELECTION
# -----------------------------
def find_option(signal):

    try:
        nifty = kite.ltp("NSE:NIFTY 50")["NSE:NIFTY 50"]["last_price"]
        atm = round(nifty / 50) * 50

        instruments = kite.instruments("NFO")

        opts = [
            i for i in instruments
            if i["name"] == "NIFTY" and i["instrument_type"] in ["CE", "PE"]
        ]

        today = datetime.datetime.now().date()
        expiries = sorted(set(i["expiry"] for i in opts))
        expiry = next((e for e in expiries if e >= today), None)

        options = [i for i in opts if i["expiry"] == expiry]

        opt_type = "CE" if signal == "CALL" else "PE"

        best_symbol = None
        best_price = None

        for inst in options:

            if inst["instrument_type"] != opt_type:
                continue

            symbol = f"NFO:{inst['tradingsymbol']}"

            try:
                price = kite.ltp(symbol)[symbol]["last_price"]
            except:
                continue

            if 30 <= price <= 150:
                if best_price is None or abs(price - 80) < abs(best_price - 80):
                    best_symbol = inst["tradingsymbol"]
                    best_price = price

        return best_symbol, best_price

    except:
        return None, None


# -----------------------------
# ORDER
# -----------------------------
def place_order(symbol, qty):

    try:
        order = kite.place_order(
            variety="regular",
            exchange="NFO",
            tradingsymbol=symbol,
            transaction_type="BUY",
            quantity=qty,
            order_type="MARKET",
            product="MIS"
        )

        send_message(f"✅ Order: {symbol}")
        return order

    except Exception as e:
        send_message(f"❌ Order error: {e}")
        return None


# -----------------------------
# TRADE MANAGEMENT
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
# REPORT
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

    global trade_active, last_trade_time

    send_message("🚀 BOT STARTED (MULTI-STRATEGY MODE)")

    while True:

        now = datetime.datetime.now(IST)

        if now.hour < 9:
            time.sleep(60)
            continue

        if now.hour >= 15:
            send_report()
            time.sleep(600)
            continue

        if trade_active:
            time.sleep(10)
            continue

        # COOLDOWN (2 min)
        if last_trade_time:
            diff = (datetime.datetime.now() - last_trade_time).seconds
            if diff < 120:
                print("Cooldown active")
                time.sleep(30)
                continue

        # -----------------------------
        # GET FINAL SIGNAL
        # -----------------------------
        signal = get_final_signal()

        if signal == "HOLD":
            print("No signal")
            time.sleep(30)
            continue

        # -----------------------------
        # OPTION
        # -----------------------------
        symbol, price = find_option(signal)

        if not symbol:
            print("No option found")
            time.sleep(30)
            continue

        qty = calculate_qty(price)

        # -----------------------------
        # ORDER
        # -----------------------------
        order = place_order(symbol, qty)

        if order:
            trade_active = True
            last_trade_time = datetime.datetime.now()
            manage_trade(symbol, price, qty)

        time.sleep(60)


# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    run_bot()