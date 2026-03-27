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

# -----------------------------
# PNL TRACKING
# -----------------------------
daily_pnl = 0
total_trades = 0
wins = 0
losses = 0


# -----------------------------
# GET SIGNAL (ML + FALLBACK)
# -----------------------------
def get_signal():

    try:
        data = requests.get(SIGNAL_URL, timeout=5).json()
        print("ML Signal:", data)

        signal = data.get("signal", "HOLD")
        quality = data.get("quality", "B")

    except:
        print("ML server error → fallback")
        signal = "HOLD"
        quality = "B"

    # -----------------------------
    # FALLBACK LOGIC
    # -----------------------------
    if signal == "HOLD":

        print("⚠️ Using fallback strategy")

        try:
            candles = kite.historical_data(
                config.NIFTY_TOKEN,
                datetime.datetime.now() - datetime.timedelta(minutes=20),
                datetime.datetime.now(),
                "5minute"
            )

            df = pd.DataFrame(candles)

            if len(df) >= 2:

                last = df.iloc[-1]
                prev = df.iloc[-2]

                # Breakout strategy
                if last["close"] > prev["high"]:
                    print("Fallback → CALL")
                    return "CALL", "B"

                elif last["close"] < prev["low"]:
                    print("Fallback → PUT")
                    return "PUT", "B"

                else:
                    return "HOLD", "B"

            else:
                return "HOLD", "B"

        except Exception as e:
            print("Fallback error:", e)
            return "HOLD", "B"

    return signal, quality


# -----------------------------
# LTP
# -----------------------------
def get_ltp(symbol):
    return kite.ltp(symbol)[symbol]["last_price"]


# -----------------------------
# SINGLE LOT
# -----------------------------
def calculate_qty(price):
    return config.LOT_SIZE


# -----------------------------
# LIGHT TREND FILTER
# -----------------------------
def is_trending():

    try:
        now = datetime.datetime.now()

        data = kite.historical_data(
            config.NIFTY_TOKEN,
            now - datetime.timedelta(minutes=60),
            now,
            "5minute"
        )

        df = pd.DataFrame(data)

        if len(df) < 10:
            return True

        vwap = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()
        last = df.iloc[-1]

        return abs(last["close"] - vwap.iloc[-1]) > 10

    except:
        return True


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

    except Exception as e:
        print("Option error:", e)
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

    sl = entry * 0.88
    target = entry * 1.20

    total_trades += 1

    send_message(f"🚀 TRADE\n{symbol} @ {entry}")

    while True:
        try:
            ltp = get_ltp(f"NFO:{symbol}")
            pnl = (ltp - entry) * qty

            print(f"{symbol} → {ltp} PnL: {pnl}")

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

    global trade_active

    send_message("🚀 BOT STARTED (ML + FALLBACK MODE)")

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

        # Trend filter (light)
        if not is_trending():
            print("Weak trend")
            time.sleep(30)
            continue

        # -----------------------------
        # SIGNAL
        # -----------------------------
        signal, quality = get_signal()

        if signal == "HOLD":
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
            manage_trade(symbol, price, qty)

        time.sleep(60)


# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    run_bot()