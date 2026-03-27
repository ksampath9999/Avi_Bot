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

kite = KiteConnect(api_key=config.API_KEY)
kite.set_access_token(config.ACCESS_TOKEN)

IST = pytz.timezone("Asia/Kolkata")
SIGNAL_URL = "https://avi-bot-1.onrender.com/signal"
last_analysis_time = 0

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

last_signal_nifty = None
last_signal_crude = None

last_trade_time_nifty = 0
last_trade_time_crude = 0

SIGNAL_COOLDOWN = 300  # 5 minutes

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

        vol_spike = last["volume"] > last["vol_ma"] * 1.2

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
        step = 50
        token = config.NIFTY_TOKEN
        token_symbol = "NSE:NIFTY 50"
    else:
        exchange = "MCX"
        name = "CRUDEOIL"
        lot = config.CRUDE_LOT
        step = 100
        token = config.CRUDE_TOKEN
        token_symbol = f"{exchange}:CRUDEOIL"

    # -----------------------------
    # GET LTP
    # -----------------------------
    try:
        ltp = kite.ltp(token_symbol)[token_symbol]["last_price"]
    except:
        print("❌ LTP fetch failed")
        return None, None, None, None

    atm = round(ltp / step) * step

    # -----------------------------
    # MODE SELECTION (FIXED)
    # -----------------------------
    saved_mode = load_best_settings(instrument)
    ai_mode = get_strike_mode(token)

    mode = saved_mode if saved_mode else ai_mode

    # -----------------------------
    # STRIKE LOGIC
    # -----------------------------
    if signal == "CALL":
        if mode == "ATM":
            strike = atm
        elif mode == "ITM":
            strike = atm - step
        elif mode == "OTM":
            strike = atm + step
        else:
            strike = atm

    elif signal == "PUT":
        if mode == "ATM":
            strike = atm
        elif mode == "ITM":
            strike = atm + step
        elif mode == "OTM":
            strike = atm - step
        else:
            strike = atm

    print(f"{instrument} LTP: {ltp} | Mode: {mode} | Strike: {strike}")

    # -----------------------------
    # FETCH OPTIONS
    # -----------------------------
    instruments = kite.instruments(exchange)
    today = datetime.datetime.now().date()

    opts = [
        i for i in instruments
        if (
            (instrument == "CRUDE" and "CRUDEOIL" in i["tradingsymbol"]) or
            (instrument == "NIFTY" and name in i["name"])
        )
        and i["instrument_type"] in ["CE", "PE"]
        and i["expiry"] >= today
    ]

    if not opts:
        return None, None, None, None

    expiry = sorted(set(i["expiry"] for i in opts))[0]
    opt_type = "CE" if signal == "CALL" else "PE"

    best = None
    best_price = None

    # -----------------------------
    # EXACT STRIKE MATCH
    # -----------------------------
    for i in opts:

        if i["expiry"] != expiry or i["instrument_type"] != opt_type:
            continue

        try:
            s = int(i["strike"])
        except:
            continue

        # ✅ CRUDE 100 STRIKE FILTER
        if instrument == "CRUDE" and s % 100 != 0:
            continue

        if s != strike:
            continue

        symbol = f"{exchange}:{i['tradingsymbol']}"

        try:
            price = kite.ltp(symbol)[symbol]["last_price"]
        except:
            continue

        best = i["tradingsymbol"]
        best_price = price
        break

    # -----------------------------
    # FALLBACK (NEAREST STRIKE)
    # -----------------------------
    if not best:

        min_diff = float("inf")

        for i in opts:

            if i["expiry"] != expiry or i["instrument_type"] != opt_type:
                continue

            try:
                s = int(i["strike"])
            except:
                continue

            if instrument == "CRUDE" and s % 100 != 0:
                continue

            diff = abs(s - strike)

            if diff < min_diff:
                symbol = f"{exchange}:{i['tradingsymbol']}"
                try:
                    price = kite.ltp(symbol)[symbol]["last_price"]
                except:
                    continue

                min_diff = diff
                best = i["tradingsymbol"]
                best_price = price

    if best and best_price:
        print(f"✅ Selected: {best} @ {best_price}")
        return best, best_price, lot, exchange

    print("❌ No strike found")
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
            quantity = get_quantity(qty, exchange),
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
                log_trade(symbol, entry, ltp, pnl, instrument)
                break

            # Optional hard target
            if ltp >= target:
                pnl = (ltp - entry) * qty
                daily_pnl += pnl
                send_message(f"🎯 TARGET HIT ₹{round(pnl,2)}")
                log_trade(symbol, entry, ltp, pnl, instrument)
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
    global last_signal_nifty, last_trade_time_nifty

    while True:
        now = datetime.datetime.now(IST)

        # Market hours
        if not (9 <= now.hour < 15):
            time.sleep(60)
            continue

        # Active trade running
        if nifty_active:
            time.sleep(5)
            continue

        # Risk control
        if not can_trade():
            time.sleep(30)
            continue

        # Market filters
        if not is_market_trending(config.NIFTY_TOKEN):
            time.sleep(20)
            continue

        if not is_strong_trend_day(config.NIFTY_TOKEN):
            time.sleep(20)
            continue

        signal = get_final_signal()

        print("📊 NIFTY SIGNAL:", signal)

        if signal == "HOLD":
            time.sleep(10)
            continue

        # 🔥 ENTRY CONFIRMATION
        token = config.NIFTY_TOKEN  # or CRUDE_TOKEN

        if not confirm_entry(token, signal):
            print("❌ Entry not confirmed")
            time.sleep(10)
            continue

        # ❌ Duplicate signal block
        if signal == last_signal_nifty:
            print("⚠️ Duplicate NIFTY signal skipped")
            time.sleep(10)
            continue

        # ⏱ Cooldown block
        if time.time() - last_trade_time_nifty < SIGNAL_COOLDOWN:
            print("⏱ NIFTY cooldown active")
            time.sleep(10)
            continue

        symbol, price, _, exchange = find_option(signal, "NIFTY")

        lot = calculate_lots(price, exchange)

        success = False

        if symbol and price:
            success = place_order(symbol, lot, exchange)

        if success:
            last_signal_nifty = signal
            last_trade_time_nifty = time.time()

            nifty_active = True
            manage_trade(symbol, price, lot, exchange, "NIFTY")
            nifty_active = False

def crude_loop():
    global crude_active
    global last_signal_crude, last_trade_time_crude

    while True:
        now = datetime.datetime.now(IST)

        # Market hours (MCX extended)
        if not (9 <= now.hour < 23):
            time.sleep(60)
            continue

        # Active trade running
        if crude_active:
            time.sleep(5)
            continue

        # Risk control
        if not can_trade():
            time.sleep(30)
            continue

        # Market filters
        if not is_market_trending(config.CRUDE_TOKEN):
            time.sleep(20)
            continue

        if not is_strong_trend_day(config.CRUDE_TOKEN):
            time.sleep(20)
            continue

        signal = get_crude_signal()

        print("🛢️ CRUDE SIGNAL:", signal)

        if signal == "HOLD":
            time.sleep(10)
            continue

        # 🔥 ENTRY CONFIRMATION
        token = config.CRUDE_TOKEN

        if not confirm_entry(token, signal):
            print("❌ Entry not confirmed")
            time.sleep(10)
            continue

        # ❌ Duplicate signal block
        if signal == last_signal_crude:
            print("⚠️ Duplicate CRUDE signal skipped")
            time.sleep(10)
            continue

        # ⏱ Cooldown block
        if time.time() - last_trade_time_crude < SIGNAL_COOLDOWN:
            print("⏱ CRUDE cooldown active")
            time.sleep(10)
            continue

        symbol, price, _, exchange = find_option(signal, "CRUDE")

        lot = calculate_lots(price, exchange)

        success = False

        if symbol and price:
            success = place_order(symbol, lot, exchange)

        if success:
            last_signal_crude = signal
            last_trade_time_crude = time.time()

            crude_active = True
            manage_trade(symbol, price, lot, exchange, "CRUDE")
            crude_active = False
        
def get_strike_mode(token):

    try:
        now = datetime.datetime.now()

        df = pd.DataFrame(kite.historical_data(
            token,
            now - datetime.timedelta(hours=2),
            now,
            "5minute"
        ))

        if len(df) < 20:
            return "ATM"

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

def log_trade(symbol, entry, exit_price, pnl, instrument):

    file = "nifty_trade_log.csv" if instrument == "NIFTY" else "crude_trade_log.csv"

    file_exists = os.path.isfile(file)

    with open(file, "a", newline="") as f:
        writer = csv.writer(f)

        if not file_exists:
            writer.writerow(["time", "symbol", "entry", "exit", "pnl"])

        writer.writerow([
            datetime.datetime.now(),
            symbol,
            entry,
            exit_price,
            pnl
        ])
        
def analyze_performance(instrument):

    try:
        file = "nifty_trade_log.csv" if instrument == "NIFTY" else "crude_trade_log.csv"

        df = pd.read_csv(file)

        if len(df) < 10:
            return

        win_rate = len(df[df["pnl"] > 0]) / len(df)
        avg_pnl = df["pnl"].mean()

        print(f"{instrument} → WinRate: {win_rate}, AvgPnL: {avg_pnl}")

        if win_rate < 0.4:
            mode = "ITM"
        elif win_rate > 0.6:
            mode = "OTM"
        else:
            mode = "ATM"

        save_best_settings(mode, win_rate, avg_pnl, instrument)

    except Exception as e:
        print(f"{instrument} analyze error:", e)
        
def save_best_settings(mode, win_rate, avg_pnl, instrument):

    file = "nifty_settings.json" if instrument == "NIFTY" else "crude_settings.json"

    data = {
        "date": str(datetime.date.today()),
        "mode": mode,
        "win_rate": win_rate,
        "avg_pnl": avg_pnl
    }

    with open(file, "w") as f:
        json.dump(data, f)

    print(f"💾 Saved {instrument} settings:", data)
    
def load_best_settings(instrument):

    file = "nifty_settings.json" if instrument == "NIFTY" else "crude_settings.json"

    try:
        with open(file, "r") as f:
            data = json.load(f)

        print(f"📂 Loaded {instrument} settings:", data)

        return data.get("mode", "ATM")

    except:
        return "ATM"
        
def performance_loop():
    while True:
        analyze_performance("NIFTY")
        analyze_performance("CRUDE")
        time.sleep(1800)
        
def confirm_entry(token, signal):

    try:
        now = datetime.datetime.now()

        df = pd.DataFrame(kite.historical_data(
            token,
            now - datetime.timedelta(minutes=20),
            now,
            "5minute"
        ))

        if len(df) < 5:
            return False

        df["vwap"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()
        df["vol_ma"] = df["volume"].rolling(5).mean()

        last = df.iloc[-1]
        prev = df.iloc[-2]

        # -----------------------------
        # CALL CONFIRMATION
        # -----------------------------
        if signal == "CALL":
            if (
                last["close"] > last["vwap"] and
                last["close"] > prev["high"] and
                last["volume"] > last["vol_ma"] * 1.2
            ):
                print("✅ CALL confirmed")
                return True

        # -----------------------------
        # PUT CONFIRMATION
        # -----------------------------
        if signal == "PUT":
            if (
                last["close"] < last["vwap"] and
                last["close"] < prev["low"] and
                last["volume"] > last["vol_ma"] * 1.2
            ):
                print("✅ PUT confirmed")
                return True

        return False

    except Exception as e:
        print("Entry confirm error:", e)
        return False
        
def get_quantity(lots, exchange):
    if exchange == "NFO":   # NIFTY
        return lots * 65
    elif exchange == "MCX": # CRUDE
        return lots * 100
    return lots
    
def get_balance():
    try:
        margin = kite.margins()
        return margin["equity"]["available"]["cash"]
    except:
        return 0
        
def calculate_lots(price, exchange):

    balance = get_balance()

    if balance == 0:
        return 1

    risk_amount = balance * config.RISK_PER_TRADE

    # Lot sizes
    if exchange == "NFO":
        lot_size = 65
    elif exchange == "MCX":
        lot_size = 100
    else:
        lot_size = 1

    # Estimated SL (10%)
    sl_points = price * 0.10

    risk_per_lot = sl_points * lot_size

    if risk_per_lot == 0:
        return 1

    lots = int(risk_amount / risk_per_lot)

    # Safety limits
    lots = max(1, lots)
    lots = min(lots, config.MAX_LOTS)

    return lots

# -----------------------------
# MAIN
# -----------------------------
if __name__ == "__main__":

    send_message("🚀 BOT STARTED")

    # ✅ START LEARNING ENGINE
    threading.Thread(target=performance_loop, daemon=True).start()

    # ✅ START TRADING LOOPS
    threading.Thread(target=nifty_loop).start()
    threading.Thread(target=crude_loop).start()