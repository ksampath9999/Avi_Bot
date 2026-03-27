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
# RISK VARIABLES
# -----------------------------
daily_pnl = 0
trade_count = 0
last_loss_time = None

# -----------------------------
# MARKET FILTERS
# -----------------------------
def is_market_trending(token):
    try:
        now = datetime.datetime.now()
        df = pd.DataFrame(kite.historical_data(
            token,
            now - datetime.timedelta(hours=2),
            now,
            "5minute"
        ))

        if len(df) < 20:
            return False

        df["tr"] = df["high"] - df["low"]
        df["atr"] = df["tr"].rolling(14).mean()

        recent_range = df["high"].max() - df["low"].min()
        atr = df["atr"].iloc[-1]

        if atr < 5:
            return False
        if recent_range < atr * 3:
            return False

        return True
    except:
        return False


def is_strong_trend_day(token):
    try:
        now = datetime.datetime.now()
        df = pd.DataFrame(kite.historical_data(
            token,
            now - datetime.timedelta(hours=3),
            now,
            "5minute"
        ))

        if len(df) < 20:
            return False

        df["vwap"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()
        last = df.iloc[-1]

        vwap_distance = abs(last["close"] - last["vwap"])
        day_range = df["high"].max() - df["low"].min()

        df["tr"] = df["high"] - df["low"]
        df["atr"] = df["tr"].rolling(14).mean()
        atr = df["atr"].iloc[-1]

        if vwap_distance > 15 and day_range > atr * 4:
            return True

        return False
    except:
        return False

# -----------------------------
# RISK CONTROL
# -----------------------------
def can_trade():
    global daily_pnl, trade_count, last_loss_time

    if trade_count >= config.MAX_TRADES:
        print("Max trades reached")
        return False

    if daily_pnl <= config.MAX_DAILY_LOSS:
        print("Max loss hit")
        return False

    if daily_pnl >= config.DAILY_TARGET:
        print("Target achieved")
        return False

    if last_loss_time:
        if time.time() - last_loss_time < config.COOLDOWN_AFTER_LOSS:
            print("Cooldown active")
            return False

    return True

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

        if small:
            return "HOLD"

        vol_spike = last["volume"] > last["vol_ma"]

        above_vwap = last["close"] > last["vwap"]
        below_vwap = last["close"] < last["vwap"]

        breakout_up = last["close"] > prev["high"]
        breakout_down = last["close"] < prev["low"]

        if breakout_up and above_vwap and vol_spike and strong:
            return "CALL"

        if breakout_down and below_vwap and vol_spike and strong:
            return "PUT"

        return "HOLD"

    except:
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
        pmin, pmax = 10, 1000   # wider safe range

    instruments = kite.instruments(exchange)

    opts = []

    for i in instruments:
        if instrument == "CRUDE":
            if "CRUDEOIL" in i["tradingsymbol"] and i["instrument_type"] in ["CE", "PE"]:
                opts.append(i)
        else:
            if name in i["name"] and i["instrument_type"] in ["CE", "PE"]:
                opts.append(i)

    if not opts:
        print("❌ No options found")
        return None, None, None, None

    today = datetime.datetime.now().date()

    expiries = sorted(set(i["expiry"] for i in opts if i["expiry"] >= today))
    if not expiries:
        print("❌ No valid expiry")
        return None, None, None, None

    expiry = expiries[0]

    opt_type = "CE" if signal == "CALL" else "PE"

    best = None
    best_price = None

    for i in opts:

        # -----------------------------
        # FILTER EXPIRY + TYPE
        # -----------------------------
        if i["expiry"] != expiry or i["instrument_type"] != opt_type:
            continue

        # -----------------------------
        # ✅ CRUDE STRIKE FILTER (100 ONLY)
        # -----------------------------
        if instrument == "CRUDE":
            try:
                if int(i["strike"]) % 100 != 0:
                    continue
            except:
                continue

        symbol = f"{exchange}:{i['tradingsymbol']}"

        try:
            price = kite.ltp(symbol)[symbol]["last_price"]
        except:
            continue

        print(f"Checking {i['tradingsymbol']} → {price}")

        if pmin <= price <= pmax:

            # choose premium near ₹100 (best liquidity)
            if best_price is None or abs(price - 100) < abs(best_price - 100):
                best = i["tradingsymbol"]
                best_price = price

    if best and best_price:
        print(f"✅ Selected: {best} @ {best_price}")
        return best, best_price, lot, exchange

    print("❌ No valid option in range")
    return None, None, None, None
# -----------------------------
# ORDER
# -----------------------------
def place_order(symbol, qty, exchange):

    try:
        full_symbol = f"{exchange}:{symbol}"

        # -----------------------------
        # GET LTP
        # -----------------------------
        ltp = kite.ltp(full_symbol)[full_symbol]["last_price"]

        expected_price = ltp

        # Initial price (tight entry)
        price = round(ltp * 1.01, 1)

        order_id = kite.place_order(
            variety="regular",
            exchange=exchange,
            tradingsymbol=symbol,
            transaction_type="BUY",
            quantity=qty,
            order_type="LIMIT",
            price=price,
            product="MIS"
        )

        send_message(f"📥 Order placed: {symbol} @ {price}")

        # -----------------------------
        # EXECUTION LOOP
        # -----------------------------
        retries = 3
        filled_price = None

        for i in range(retries):

            time.sleep(3)

            orders = kite.orders()

            for o in orders:
                if o["order_id"] == order_id:

                    # -----------------------------
                    # FILLED
                    # -----------------------------
                    if o["status"] == "COMPLETE":
                        filled_price = o["average_price"]
                        break

            if filled_price:
                break

            # -----------------------------
            # MODIFY PRICE (STEP UP)
            # -----------------------------
            price = round(price * 1.01, 1)

            kite.modify_order(
                variety="regular",
                order_id=order_id,
                price=price
            )

            send_message(f"🔁 Retry {i+1} → {price}")

        # -----------------------------
        # FINAL CHECK
        # -----------------------------
        if not filled_price:

            # Cancel order
            kite.cancel_order(
                variety="regular",
                order_id=order_id
            )

            send_message(f"❌ Order cancelled (not filled): {symbol}")
            return False

        # -----------------------------
        # SLIPPAGE CALCULATION
        # -----------------------------
        slippage = round(filled_price - expected_price, 2)

        send_message(
            f"""✅ ORDER FILLED

            {symbol}
            Expected: ₹{expected_price}
            Filled: ₹{filled_price}
            Slippage: ₹{slippage}
            """
        )

        return True

    except Exception as e:
        send_message(f"❌ Order error: {e}")
        return False

# -----------------------------
# TRADE MGMT
# -----------------------------
def manage_trade(symbol, entry, qty, exchange, instrument):

    global daily_pnl, trade_count, last_loss_time

    trade_count += 1

    full_symbol = f"{exchange}:{symbol}"

    # -----------------------------
    # INITIAL LEVELS
    # -----------------------------
    sl = entry * 0.90
    target = entry * 1.20

    trailing_sl = sl
    highest_price = entry

    send_message(f"🚀 {instrument} TRADE\n{symbol} @ {entry}")

    while True:
        try:
            ltp = kite.ltp(full_symbol)[full_symbol]["last_price"]

            # -----------------------------
            # TRACK HIGHEST PRICE
            # -----------------------------
            if ltp > highest_price:
                highest_price = ltp

            # -----------------------------
            # TRAILING LOGIC
            # -----------------------------
            profit = ltp - entry

            # Move to break-even
            if profit > entry * 0.05:
                trailing_sl = max(trailing_sl, entry)

            # Lock profits
            if profit > entry * 0.10:
                trailing_sl = max(trailing_sl, highest_price * 0.92)

            if profit > entry * 0.15:
                trailing_sl = max(trailing_sl, highest_price * 0.95)

            # -----------------------------
            # EXIT CONDITIONS
            # -----------------------------
            if ltp <= trailing_sl:
                pnl = (ltp - entry) * qty
                daily_pnl += pnl

                if pnl < 0:
                    last_loss_time = time.time()

                send_message(f"🛑 EXIT (TRAIL SL)\nPnL: ₹{round(pnl,2)}")
                break

            # Optional hard target
            if ltp >= target:
                pnl = (ltp - entry) * qty
                daily_pnl += pnl
                send_message(f"🎯 TARGET HIT ₹{round(pnl,2)}")
                break

            time.sleep(5)

        except Exception as e:
            print("Trade error:", e)
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

        if not can_trade():
            time.sleep(30)
            continue

        if not is_market_trending(config.NIFTY_TOKEN):
            time.sleep(20)
            continue

        if not is_strong_trend_day(config.NIFTY_TOKEN):
            time.sleep(20)
            continue

        signal = get_final_signal()

        if signal == "HOLD":
            time.sleep(10)
            continue

        symbol, price, lot, exchange = find_option(signal, "NIFTY")

        if symbol and price:
            success = place_order(symbol, lot, exchange)

        if success:
            nifty_active = True  # or crude_active
            manage_trade(symbol, price, lot, exchange, "CRUDE")
            crude_active = False
            nifty_active = True
            manage_trade(symbol, price, lot, exchange, "NIFTY")
            nifty_active = False
        print("CRUDE SIGNAL:", signal)

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

        if not can_trade():
            time.sleep(30)
            continue

        if not is_market_trending(config.CRUDE_TOKEN):
            time.sleep(20)
            continue

        if not is_strong_trend_day(config.CRUDE_TOKEN):
            time.sleep(20)
            continue

        signal = get_crude_signal()

        if signal == "HOLD":
            time.sleep(10)
            continue

        symbol, price, lot, exchange = find_option(signal, "CRUDE")

        if symbol and price:
            success = place_order(symbol, lot, exchange)

        if success:
            nifty_active = True  # or crude_active
            manage_trade(symbol, price, lot, exchange, "CRUDE")
            crude_active = False
            crude_active = True
            manage_trade(symbol, price, lot, exchange, "CRUDE")
            crude_active = False
        print("CRUDE SIGNAL:", signal)

# -----------------------------
# MAIN
# -----------------------------
if __name__ == "__main__":

    send_message("🚀 BOT STARTED (ELITE + RISK MODE)")

    threading.Thread(target=nifty_loop).start()
    threading.Thread(target=crude_loop).start()