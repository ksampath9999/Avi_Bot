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
last_reset_date = None

MAX_DRAWDOWN = -3000   # adjust based on capital
win_streak = 0
loss_streak = 0
peak_pnl = 0

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
        send_message("🚫 Max trades reached")
        return False

    if daily_pnl <= config.MAX_DAILY_LOSS:
        send_message("🚫 Max daily loss hit — trading stopped")
        return False

    if daily_pnl >= config.DAILY_TARGET:
        send_message("🎯 Target achieved — trading stopped")
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
        
def is_liquid_option(symbol, exchange):

    try:
        full_symbol = f"{exchange}:{symbol}"

        data = kite.ltp(full_symbol)[full_symbol]

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
        
def score_option(symbol, exchange):

    try:
        full_symbol = f"{exchange}:{symbol}"
        data = kite.ltp(full_symbol)[full_symbol]

        price = data.get("last_price", 0)

        # basic filters
        if price <= 0:
            return 0

        if price < 10 or price > 500:
            return 0

        # fake scoring (since LTP API doesn't give volume/OI directly)
        # we simulate using price stability
        score = 100 / (abs(price - 100) + 1)

        return score

    except:
        return 0
        
def is_good_spread(symbol, exchange):

    try:
        full_symbol = f"{exchange}:{symbol}"

        data = kite.quote([full_symbol])[full_symbol]

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
        
def is_sideways_market(token):

    try:
        now = datetime.datetime.now()

        df = pd.DataFrame(kite.historical_data(
            token,
            now - datetime.timedelta(minutes=60),
            now,
            "5minute"
        ))

        if len(df) < 10:
            return True  # treat as sideways

        # VWAP
        df["vwap"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()

        last = df.iloc[-1]

        # ATR (volatility)
        df["range"] = df["high"] - df["low"]
        atr = df["range"].rolling(10).mean().iloc[-1]

        vwap_distance = abs(last["close"] - last["vwap"])

        print(f"VWAP distance: {vwap_distance} | ATR: {atr}")

        # -----------------------------
        # SIDEWAYS CONDITIONS
        # -----------------------------
        if vwap_distance < 10:
            return True

        if atr < 15:
            return True

        return False

    except Exception as e:
        print("Sideways check error:", e)
        return True

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
        lot_size = 65
    else:
        exchange = "MCX"
        name = "CRUDEOIL"
        lot = config.CRUDE_LOT
        step = 100
        token = config.CRUDE_TOKEN
        token_symbol = f"{exchange}:CRUDEOIL"
        lot_size = 100

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
    # MODE (SELF LEARNING)
    # -----------------------------
    saved_mode = load_best_settings(instrument)
    ai_mode = get_strike_mode(token)
    mode = saved_mode if saved_mode else ai_mode

    print(f"{instrument} LTP: {ltp} | Mode: {mode}")

    # -----------------------------
    # CAPITAL MANAGEMENT
    # -----------------------------
    balance = get_balance() or 10000
    max_trade_value = balance * 0.20

    # -----------------------------
    # STRIKE PRIORITY
    # -----------------------------
    if signal == "CALL":
        strikes = [atm, atm + step, atm - step]   # ATM → OTM → ITM
    else:
        strikes = [atm, atm - step, atm + step]

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

    candidates = []

    # -----------------------------
    # MAIN LOOP (SMART SELECTION)
    # -----------------------------
    for strike in strikes:

        print(f"🔍 Trying strike: {strike}")

        for i in opts:

            if i["expiry"] != expiry or i["instrument_type"] != opt_type:
                continue

            try:
                s = int(i["strike"])
            except:
                continue

            # CRUDE: only 100-step strikes
            if instrument == "CRUDE" and s % 100 != 0:
                continue

            if s != strike:
                continue

            symbol = f"{exchange}:{i['tradingsymbol']}"

            try:
                price = kite.ltp(symbol)[symbol]["last_price"]

                # 🔥 LIQUIDITY FILTER
                if not is_liquid_option(i["tradingsymbol"], exchange):
                    continue

                # 🔥 SPREAD FILTER
                if not is_good_spread(i["tradingsymbol"], exchange):
                    print(f"❌ Bad spread: {i['tradingsymbol']}")
                    continue

            except:
                continue

            trade_value = price * lot_size

            # 💰 CAPITAL CHECK
            if trade_value > max_trade_value:
                continue

            # 📊 SCORE (closer to ₹100 premium preferred)
            score = 100 / (abs(price - 100) + 1)

            candidates.append({
                "symbol": i["tradingsymbol"],
                "price": price,
                "score": score
            })

    # -----------------------------
    # BEST SELECTION
    # -----------------------------
    if candidates:

        best = sorted(candidates, key=lambda x: x["score"], reverse=True)[0]

        print(f"🏆 Best Selected: {best['symbol']} @ {best['price']}")

        lot = calculate_lots(best["price"], exchange, instrument)

        return best["symbol"], best["price"], lot, exchange

    # -----------------------------
    # FALLBACK (NEAREST STRIKE)
    # -----------------------------
    print("⚠️ Fallback mode")

    best = None
    best_price = None
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

        diff = abs(s - atm)

        if diff < min_diff:

            symbol = f"{exchange}:{i['tradingsymbol']}"

            try:
                price = kite.ltp(symbol)[symbol]["last_price"]

                if not is_liquid_option(i["tradingsymbol"], exchange):
                    continue

                if not is_good_spread(i["tradingsymbol"], exchange):
                    continue

            except:
                continue

            trade_value = price * lot_size

            if trade_value <= max_trade_value:
                min_diff = diff
                best = i["tradingsymbol"]
                best_price = price

    if best:
        print(f"✅ Fallback selected: {best} @ {best_price}")

        lot = calculate_lots(best["price"], exchange, instrument)

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
    global win_streak, loss_streak
    global peak_pnl

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

            # Break-even
            if profit > entry * 0.05:
                trailing_sl = max(trailing_sl, entry)

            # Lock profits
            if profit > entry * 0.10:
                trailing_sl = max(trailing_sl, highest_price * 0.92)

            if profit > entry * 0.15:
                trailing_sl = max(trailing_sl, highest_price * 0.95)

            # -----------------------------
            # EXIT: TRAILING SL
            # -----------------------------
            if ltp <= trailing_sl:

                pnl = (ltp - entry) * qty
                daily_pnl += pnl

                # 🔥 ADAPTIVE RISK UPDATE
                if pnl > 0:
                    win_streak += 1
                    loss_streak = 0
                else:
                    loss_streak += 1
                    win_streak = 0
                    last_loss_time = time.time()

                # 📉 SLIPPAGE
                slippage = abs(entry - ltp)

                # 📉 DRAWDOWN UPDATE
                if daily_pnl > peak_pnl:
                    peak_pnl = daily_pnl

                drawdown = daily_pnl - peak_pnl

                send_message(
                    f"🛑 EXIT (TRAIL SL)\n"
                    f"{symbol}\n"
                    f"Exit: {ltp}\n"
                    f"PnL: ₹{round(pnl,2)}\n"
                    f"Slippage: ₹{round(slippage,2)}\n"
                    f"WinStreak: {win_streak} | LossStreak: {loss_streak}\n"
                    f"Drawdown: ₹{round(drawdown,2)}"
                )

                log_trade(symbol, entry, ltp, pnl, instrument)
                break

            # -----------------------------
            # EXIT: TARGET HIT
            # -----------------------------
            if ltp >= target:

                pnl = (ltp - entry) * qty
                daily_pnl += pnl

                win_streak += 1
                loss_streak = 0

                slippage = abs(entry - ltp)

                if daily_pnl > peak_pnl:
                    peak_pnl = daily_pnl

                drawdown = daily_pnl - peak_pnl

                send_message(
                    f"🎯 TARGET HIT\n"
                    f"{symbol}\n"
                    f"Exit: {ltp}\n"
                    f"PnL: ₹{round(pnl,2)}\n"
                    f"Slippage: ₹{round(slippage,2)}\n"
                    f"WinStreak: {win_streak}\n"
                    f"Drawdown: ₹{round(drawdown,2)}"
                )

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
        reset_daily_pnl()
        now = datetime.datetime.now(IST)
        
        if not equity_safe():
            time.sleep(60)
            continue

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

        # 🔥 TREND CHECK (UPDATED)
        strong_trend = is_strong_trend_day(config.NIFTY_TOKEN)

        if not strong_trend:
            print("⚠️ Weak NIFTY trend → trading with 1 lot")

        signal = get_final_signal()

        print("📊 NIFTY SIGNAL:", signal)

        if signal == "HOLD":
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
            
        # 🚨 NEWS FILTER
        if is_news_volatility(config.NIFTY_TOKEN):
            print("🚫 News volatility detected — skipping trade")
            time.sleep(20)
            continue
            
        # 🔥 ENTRY CONFIRMATION
        if not confirm_entry(config.NIFTY_TOKEN, signal):
            print("❌ Entry not confirmed")
            time.sleep(10)
            continue
            
        # 🔥 REVERSAL TRAP FILTER
        if is_reversal_trap(config.NIFTY_TOKEN, signal):
            print("🚫 Reversal trap detected — skipping trade")
            time.sleep(10)
            continue    
        

        symbol, price, lot, exchange = find_option(signal, "NIFTY")

        # 🔥 REDUCE LOT IN WEAK TREND
        if not strong_trend and lot:
            lot = 1

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
        reset_daily_pnl()
        now = datetime.datetime.now(IST)
        
        if not equity_safe():
            time.sleep(60)
            continue

        # Market hours
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

        # 🔥 TREND CHECK
        strong_trend = is_strong_trend_day(config.CRUDE_TOKEN)

        if not strong_trend:
            print("⚠️ Weak CRUDE trend → trading with 1 lot")

        signal = get_crude_signal()

        print("🛢️ CRUDE SIGNAL:", signal)

        if signal == "HOLD":
            time.sleep(10)
            continue

        # ❌ Duplicate signal block (fast check)
        if signal == last_signal_crude:
            print("⚠️ Duplicate CRUDE signal skipped")
            time.sleep(10)
            continue

        # ⏱ Cooldown block (fast check)
        if time.time() - last_trade_time_crude < SIGNAL_COOLDOWN:
            print("⏱ CRUDE cooldown active")
            time.sleep(10)
            continue
            
        if is_news_volatility(config.CRUDE_TOKEN):
            print("🚫 CRUDE news volatility — skipping")
            time.sleep(20)
            continue

        # 🔥 ENTRY CONFIRMATION (medium cost)
        if not confirm_entry(config.CRUDE_TOKEN, signal):
            print("❌ Entry not confirmed")
            time.sleep(10)
            continue

        # 🚨 REVERSAL TRAP (expensive check)
        if is_reversal_trap(config.CRUDE_TOKEN, signal):
            print("🚫 CRUDE reversal trap — skipping")
            time.sleep(10)
            continue

        symbol, price, lot, exchange = find_option(signal, "CRUDE")

        # 🔥 REDUCE LOT IN WEAK TREND
        if not strong_trend and lot:
            lot = 1

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
        
def calculate_lots(price, exchange, instrument):

    global win_streak, loss_streak

    balance = get_balance() or 10000
    risk_amount = balance * config.RISK_PER_TRADE

    if exchange == "NFO":
        lot_size = 65
        token = config.NIFTY_TOKEN
    else:
        lot_size = 100
        token = config.CRUDE_TOKEN

    sl_points = price * 0.10
    risk_per_lot = sl_points * lot_size

    if risk_per_lot == 0:
        return 1

    lots = int(risk_amount / risk_per_lot)

    # -----------------------------
    # 🔥 ADAPTIVE RISK LOGIC
    # -----------------------------

    # 🚀 Increase after wins
    if win_streak >= 2:
        print("🚀 Winning streak → increasing lot")
        lots = int(lots * 1.5)

    # 🛑 Reduce after losses
    if loss_streak >= 2:
        print("⚠️ Losing streak → reducing lot")
        lots = max(1, int(lots * 0.5))

    # -----------------------------
    # 🔥 TREND BOOSTER (already added)
    # -----------------------------
    if is_strong_trend_day(token):
        lots = int(lots * 1.5)

    # Safety
    lots = max(1, lots)
    lots = min(lots, config.MAX_LOTS)

    return lots

def is_strong_trend_day(token):

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

        # VWAP
        df["vwap"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()

        last = df.iloc[-1]

        # ATR
        df["range"] = df["high"] - df["low"]
        atr = df["range"].rolling(14).mean().iloc[-1]

        vwap_distance = abs(last["close"] - last["vwap"])

        print(f"🔥 Trend Check → VWAP Dist: {vwap_distance}, ATR: {atr}")

        # -----------------------------
        # STRONG TREND CONDITIONS
        # -----------------------------
        if vwap_distance > 20 and atr > 25:
            return True

        return False

    except Exception as e:
        print("Trend detection error:", e)
        return False
        
def is_reversal_trap(token, signal):

    try:
        now = datetime.datetime.now()

        df = pd.DataFrame(kite.historical_data(
            token,
            now - datetime.timedelta(minutes=30),
            now,
            "5minute"
        ))

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
        now = datetime.datetime.now()

        df = pd.DataFrame(kite.historical_data(
            token,
            now - datetime.timedelta(minutes=60),
            now,
            "5minute"
        ))

        if len(df) < 10:
            return False

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

    today = datetime.date.today()

    if last_reset_date != today:
        print("🔄 Resetting daily stats")

        daily_pnl = 0
        trade_count = 0
        last_reset_date = today
        
def equity_safe():

    global daily_pnl, peak_pnl

    drawdown = daily_pnl - peak_pnl

    if drawdown <= MAX_DRAWDOWN:
        print("🚫 Max drawdown reached — stopping trading")
        send_message("🚫 Equity protection triggered — trading stopped")
        return False

    return True

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