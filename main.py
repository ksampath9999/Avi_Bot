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
daily_loss = 0
extra_risk_cut = False

# -----------------------------
# PNL TRACKING
# -----------------------------
daily_pnl = 0
total_trades = 0
wins = 0
losses = 0


# -----------------------------
# EXPIRY CHECK
# -----------------------------
def is_expiry_day():
    return datetime.datetime.now(IST).weekday() == 1


# -----------------------------
# TREND FILTER
# -----------------------------
def is_trending_day():
    try:
        now = datetime.datetime.now()

        data = kite.historical_data(
            config.NIFTY_TOKEN,
            now - datetime.timedelta(hours=3),
            now,
            "5minute"
        )

        df = pd.DataFrame(data)

        if len(df) < 20:
            return False

        vwap = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()
        last = df.iloc[-1]

        df["ema20"] = df["close"].ewm(span=20).mean()
        df["ema50"] = df["close"].ewm(span=50).mean()

        recent_range = df["high"].iloc[-1] - df["low"].iloc[-1]
        avg_range = (df["high"] - df["low"]).rolling(10).mean().iloc[-1]

        vwap_distance = abs(last["close"] - vwap.iloc[-1])
        trend_strength = abs(df["ema20"].iloc[-1] - df["ema50"].iloc[-1])

        return vwap_distance > 15 and recent_range > avg_range and trend_strength > 5

    except Exception as e:
        print("Trend error:", e)
        return False


# -----------------------------
# SIGNAL
# -----------------------------
def get_signal():
    try:
        return requests.get(SIGNAL_URL, timeout=10).json()
    except:
        return {"signal": "HOLD"}


# -----------------------------
# LTP
# -----------------------------
def get_ltp(symbol):
    return kite.ltp(symbol)[symbol]["last_price"]


# -----------------------------
# SINGLE LOT
# -----------------------------
def calculate_qty(price):
    return config.LOT_SIZE  # 65


# -----------------------------
# SCALPING FILTER
# -----------------------------
def smart_scalping_filter(signal):
    try:
        now = datetime.datetime.now()

        data = kite.historical_data(
            config.NIFTY_TOKEN,
            now - datetime.timedelta(minutes=30),
            now,
            "5minute"
        )

        df = pd.DataFrame(data)

        if len(df) < 5:
            return False

        last = df.iloc[-1]

        vwap = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()

        orb_high = df["high"].iloc[:3].max()
        orb_low = df["low"].iloc[:3].min()

        if (df["high"].max() - df["low"].min()) < 20:
            return False

        if signal == "CALL" and last["close"] < vwap.iloc[-1]:
            return False

        if signal == "PUT" and last["close"] > vwap.iloc[-1]:
            return False

        if signal == "CALL" and last["close"] <= orb_high:
            return False

        if signal == "PUT" and last["close"] >= orb_low:
            return False

        return True

    except:
        return False


# -----------------------------
# EXPIRY FETCH
# -----------------------------
def get_weekly_expiry(instruments):
    today = datetime.datetime.now().date()
    expiries = sorted(set(i["expiry"] for i in instruments))
    for exp in expiries:
        if exp >= today:
            return exp
    return None


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

        expiry = get_weekly_expiry(opts)
        options = [i for i in opts if i["expiry"] == expiry]

        if signal == "CALL":
            strikes = [atm, atm + 50, atm + 100]
            opt_type = "CE"
        else:
            strikes = [atm, atm - 50, atm - 100]
            opt_type = "PE"

        best_symbol = None
        best_price = None

        for inst in options:

            if inst["instrument_type"] != opt_type:
                continue

            if inst["strike"] not in strikes:
                continue

            symbol = f"NFO:{inst['tradingsymbol']}"

            try:
                price = kite.ltp(symbol)[symbol]["last_price"]
            except:
                continue

            if is_expiry_day():
                if price < 60 or price > 100:
                    continue
            else:
                if price < config.MIN_PREMIUM or price > config.MAX_PREMIUM:
                    continue

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
        return kite.place_order(
            variety="regular",
            exchange="NFO",
            tradingsymbol=symbol,
            transaction_type="BUY",
            quantity=qty,
            order_type="MARKET",
            product="MIS"
        )
    except Exception as e:
        send_message(f"❌ Order error: {e}")
        return None


# -----------------------------
# TRADE MANAGEMENT
# -----------------------------
def manage_trade(symbol, entry, qty, mode):

    global trade_active, daily_loss
    global daily_pnl, total_trades, wins, losses

    if mode == "SCALP":
        sl = entry * 0.85
        target = entry * 1.25
        sleep = 3
    else:
        sl = entry * (1 - config.STOP_LOSS)
        target = entry * (1 + config.TARGET)
        sleep = 5

    send_message(f"🚀 {mode} TRADE\n{symbol} @ {entry}")

    total_trades += 1

    while True:
        try:
            ltp = get_ltp(f"NFO:{symbol}")
            pnl = (ltp - entry) * qty

            if ltp >= target:
                send_message(f"🎯 TARGET HIT ₹{round(pnl,2)}")
                daily_pnl += pnl
                wins += 1
                break

            if ltp <= sl:
                send_message(f"🛑 SL HIT ₹{round(pnl,2)}")
                daily_pnl -= abs(pnl)
                losses += 1
                daily_loss += abs(pnl)
                break

            time.sleep(sleep)

        except:
            break

    trade_active = False


# -----------------------------
# DAILY REPORT
# -----------------------------
def send_daily_report():

    try:
        win_rate = (wins / total_trades * 100) if total_trades > 0 else 0

        msg = f"""
📊 DAILY REPORT

Trades: {total_trades}
Wins: {wins}
Losses: {losses}

Win Rate: {round(win_rate,2)}%

PnL: ₹{round(daily_pnl,2)}
"""

        send_message(msg)

    except Exception as e:
        print("Report error:", e)


# -----------------------------
# MAIN LOOP
# -----------------------------
def run_bot():

    global trade_active, extra_risk_cut

    send_message("🚀 BOT STARTED (WITH PNL TRACKING)")

    while True:

        now = datetime.datetime.now(IST)

        # SEND REPORT AFTER MARKET
        if now.hour >= 15:
            send_daily_report()
            time.sleep(600)
            continue

        if now.hour < 9:
            time.sleep(300)
            continue

        # TREND FILTER
        if not is_trending_day():
            time.sleep(300)
            continue

        extra_risk_cut = False

        if is_expiry_day():

            if now.hour >= 15:
                time.sleep(300)
                continue

            if (now.hour == 13 and now.minute >= 30) or now.hour == 14:
                extra_risk_cut = True

        # MODE
        if (now.hour == 9 and now.minute >= 15) or (now.hour == 10 and now.minute < 30):
            mode = "SCALP"
        else:
            mode = "NORMAL"

        if trade_active:
            time.sleep(30)
            continue

        signal_data = get_signal()

        signal = signal_data.get("signal", "HOLD")
        quality = signal_data.get("quality", "B")

        if mode == "SCALP":
            allowed = ["A", "A+"] if is_expiry_day() else ["A", "A+", "B"]

            if not smart_scalping_filter(signal):
                time.sleep(120)
                continue
        else:
            allowed = ["A", "A+"]

        if signal == "HOLD" or quality not in allowed:
            time.sleep(120)
            continue

        symbol, price = find_option(signal)

        if not symbol:
            send_message("❌ No valid option found")
            time.sleep(120)
            continue

        qty = calculate_qty(price)

        order = place_order(symbol, qty)

        if order:
            trade_active = True
            manage_trade(symbol, price, qty, mode)

        time.sleep(120)


# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    run_bot()