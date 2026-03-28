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

lock = threading.Lock()

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

SIGNAL_COOLDOWN = 120  
alert_sent = False
last_analysis_time = 0

portfolio_pnl = 0
peak_portfolio = 0
risk_off = False

report_sent_today = False
max_drawdown = 0

trade_alert_sent = {
    "max_trades": False,
    "max_loss": False,
    "target_hit": False
}



def get_session_config(instrument):

    session = get_market_session(instrument)

    if instrument == "NIFTY":

        if session == "MORNING":
            return {"min_conf": 50, "lot_mult": 1.2}

        elif session == "MIDDAY":
            return {"min_conf": 70, "lot_mult": 0.5}

        elif session == "AFTERNOON":
            return {"min_conf": 60, "lot_mult": 1}

    else:  # CRUDE

        if session == "MORNING":
            return {"min_conf": 55, "lot_mult": 1}

        elif session == "MIDDAY":
            return {"min_conf": 75, "lot_mult": 0.5}

        elif session == "EVENING_TREND":
            return {"min_conf": 50, "lot_mult": 1.5}

        elif session == "VOLATILE_SESSION":
            return {"min_conf": 65, "lot_mult": 1}

    return None


def safe_ltp(symbol):
    for _ in range(3):
        try:
            return kite.ltp(symbol)[symbol]["last_price"]
        except:
            time.sleep(1)
    return None

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

# -----------------------------
# RISK CONTROL
# -----------------------------
def can_trade():
    global daily_pnl, trade_count, last_loss_time, trade_alert_sent

    # -----------------------------
    # MAX TRADES
    # -----------------------------
    if trade_count >= config.MAX_TRADES:
        if not trade_alert_sent["max_trades"]:
            send_message("🚫 Max trades reached")
            trade_alert_sent["max_trades"] = True
        return False

    # -----------------------------
    # MAX LOSS
    # -----------------------------
    if daily_pnl <= config.MAX_DAILY_LOSS:
        if not trade_alert_sent["max_loss"]:
            send_message("🚫 Max daily loss hit — trading stopped")
            trade_alert_sent["max_loss"] = True
        return False

    # -----------------------------
    # TARGET HIT
    # -----------------------------
    if daily_pnl >= config.DAILY_TARGET:
        if not trade_alert_sent["target_hit"]:
            send_message("🎯 Target achieved — trading stopped")
            trade_alert_sent["target_hit"] = True
        return False

    # -----------------------------
    # COOLDOWN
    # -----------------------------
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
        ltp = safe_ltp("NSE:NIFTY 50")
        if ltp is None:
            return "HOLD"

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

        data = kite.quote([full_symbol])[full_symbol]

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

        # ✅ SAFE LTP (no crash)
        price = safe_ltp(full_symbol)

        if price is None:
            return 0

        # -----------------------------
        # BASIC FILTERS
        # -----------------------------
        if price <= 0:
            return 0

        if price < 10 or price > 500:
            return 0

        # -----------------------------
        # SCORING LOGIC
        # -----------------------------
        # 🎯 Prefer near ₹100 premium
        price_score = 100 / (abs(price - 100) + 1)

        # 🧠 Add slight boost for mid-range options
        if 40 <= price <= 150:
            price_score *= 1.2

        return price_score

    except Exception as e:
        print(f"Score error {symbol}: {e}")
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
        ltp = safe_ltp(token_symbol)
        if ltp is None:
            return None, None, None, None
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
                price = safe_ltp(symbol)

                if price is None:
                    print(f"❌ LTP failed: {symbol}")
                    continue   # inside loop

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
            if trade_value > max_trade_value * 1.2:
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

        symbol = best["symbol"]
        price = best["price"]

        print(f"🏆 Selected: {symbol} @ {price}")

        lot = calculate_lots(price, exchange, instrument)

        return symbol, price, lot, exchange

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
                price = safe_ltp(symbol)

                if price is None:
                    print(f"❌ LTP failed: {symbol}")
                    continue   # inside loop

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

        lot = calculate_lots(best_price, exchange, instrument)

        return best, best_price, lot, exchange

    print("❌ No strike found")
    return None, None, None, None
# -----------------------------
# ORDER
# -----------------------------
def place_order(symbol, qty, exchange):

    try:
        full_symbol = f"{exchange}:{symbol}"

        ltp = safe_ltp(full_symbol)
        if ltp is None:
           return None
        expected_price = ltp
        price = round(ltp * 1.01, 1)

        order_id = kite.place_order(
            variety="regular",
            exchange=exchange,
            tradingsymbol=symbol,
            transaction_type="BUY",
            quantity=get_quantity(qty, exchange),
            order_type="LIMIT",
            price=price,
            product="MIS"
        )

        send_message(f"📥 Order placed: {symbol} @ {price}")

        filled_price = None

        for i in range(3):
            time.sleep(3)

            orders = kite.orders()

            for o in orders:
                if o["order_id"] == order_id and o["status"] == "COMPLETE":
                    filled_price = o["average_price"]
                    break

            if filled_price:
                break

            price = round(price * 1.01, 1)

            kite.modify_order(
                variety="regular",
                order_id=order_id,
                price=price
            )

        if not filled_price:
            kite.cancel_order(variety="regular", order_id=order_id)
            send_message(f"❌ Order cancelled: {symbol}")
            return None

        slippage = round(filled_price - expected_price, 2)

        send_message(
            f"✅ FILLED\n{symbol}\nExpected: {expected_price}\nFilled: {filled_price}\nSlippage: {slippage}"
        )

        return filled_price

    except Exception as e:
        send_message(f"❌ Order error: {e}")
        return None

# -----------------------------
# TRADE MGMT
# -----------------------------
def manage_trade(symbol, entry, qty, exchange, instrument):

    global daily_pnl, trade_count, last_loss_time
    global win_streak, loss_streak, peak_pnl
    global portfolio_pnl, peak_portfolio, risk_off
    global max_drawdown
    
    with lock:
        trade_count += 1

    full_symbol = f"{exchange}:{symbol}"

    sl = entry * 0.90
    target = entry * 1.20

    trailing_sl = sl
    highest_price = entry

    send_message(f"🚀 {instrument} TRADE\n{symbol} @ {entry}")

    while True:
        try:
            ltp = safe_ltp(full_symbol)
            if ltp is None:
                continue

            if ltp > highest_price:
                highest_price = ltp

            profit = ltp - entry

            # -----------------------------
            # TRAILING LOGIC
            # -----------------------------
            if profit > entry * 0.05:
                trailing_sl = max(trailing_sl, entry)

            if profit > entry * 0.10:
                trailing_sl = max(trailing_sl, highest_price * 0.92)

            if profit > entry * 0.15:
                trailing_sl = max(trailing_sl, highest_price * 0.95)

            actual_qty = get_quantity(qty, exchange)

            # -----------------------------
            # EXIT: TRAIL SL
            # -----------------------------
            if ltp <= trailing_sl:

                with lock:
                    pnl = (ltp - entry) * actual_qty
                    daily_pnl += pnl
                    portfolio_pnl += pnl   # 🔥 NEW

                    # Portfolio peak tracking
                    if portfolio_pnl > peak_portfolio:
                        peak_portfolio = portfolio_pnl

                # Win/loss tracking
                with lock:
                    if pnl > 0:
                        win_streak += 1
                        loss_streak = 0
                    else:
                        loss_streak += 1
                        win_streak = 0
                        last_loss_time = time.time()

                # Daily drawdown
                with lock:
                    if daily_pnl > peak_pnl:
                        peak_pnl = daily_pnl

                drawdown = daily_pnl - peak_pnl

                # Portfolio drawdown
                with lock:
                    portfolio_dd = portfolio_pnl - peak_portfolio

                print(f"📉 Portfolio DD: {portfolio_dd}")

                # 🔥 RISK-OFF TRIGGER
                if portfolio_dd <= config.MAX_DRAWDOWN:
                    print("🚫 Portfolio drawdown hit — activating risk off")
                    risk_off = True

                send_message(
                    f"🛑 EXIT\n{symbol}\nPnL: ₹{round(pnl,2)}\nDD: ₹{round(drawdown,2)}\nPortfolio DD: ₹{round(portfolio_dd,2)}"
                )

                log_trade(symbol, entry, ltp, pnl, instrument)
                break

            # -----------------------------
            # TARGET
            # -----------------------------
            if ltp >= target:

                with lock:
                    pnl = (ltp - entry) * actual_qty
                    daily_pnl += pnl
                    portfolio_pnl += pnl   # 🔥 NEW

                    # Portfolio peak tracking
                    if portfolio_pnl > peak_portfolio:
                        peak_portfolio = portfolio_pnl

                with lock:
                    win_streak += 1
                    loss_streak = 0
                    
                with lock:
                    portfolio_dd = portfolio_pnl - peak_portfolio

                print(f"📈 Portfolio PnL: {portfolio_pnl}")

                send_message(
                    f"🎯 TARGET HIT ₹{round(pnl,2)}\nPortfolio: ₹{round(portfolio_pnl,2)}"
                )

                log_trade(symbol, entry, ltp, pnl, instrument)
                
                dd = portfolio_pnl - peak_portfolio
                if dd < max_drawdown:
                    max_drawdown = dd
                
                break
                

            time.sleep(5)
            
            

            

        except Exception as e:
            print("Trade error:", e)
            break
# -----------------------------
# THREADS
# -----------------------------
def nifty_loop():
    global nifty_active, last_signal_nifty, last_trade_time_nifty
    global last_analysis_time
    global report_sent_today

    

    if time.time() - last_analysis_time > 1800:  # every 30 mins
        adjust_strategy()
        last_analysis_time = time.time()
        
    if last_reset_date != datetime.date.today():
        reset_daily_pnl()

    while True:
        
        now = datetime.datetime.now(IST)
        
        # Send report after 3:30 PM
        if now.hour == 15 and now.minute >= 30 and not report_sent_today:
            send_daily_report()
            report_sent_today = True

        # -----------------------------
        # MARKET TIME
        # -----------------------------
        if not (9 <= now.hour < 15 or (now.hour == 15 and now.minute <= 30)):
            time.sleep(60)
            continue

        # Avoid late trades
        if now.hour == 15 and now.minute > 25:
            print("⚠️ Avoiding late NIFTY trades")
            time.sleep(60)
            continue

        if nifty_active:
            time.sleep(5)
            continue

        if not can_trade():
            time.sleep(30)
            continue
        if risk_off:
            print("🛑 Risk OFF active")
            time.sleep(60)
            continue
            
        if not portfolio_safe():
            print("🛑 Portfolio risk active — no trades")
            time.sleep(60)
            continue

        # -----------------------------
        # MARKET FILTERS
        # -----------------------------
        if not is_market_trending(config.NIFTY_TOKEN):
            time.sleep(20)
            continue

        if is_sideways_market(config.NIFTY_TOKEN) and not is_strong_trend_day(config.NIFTY_TOKEN) :
            print("⚠️ Sideways market — skipping")
            time.sleep(20)
            continue

        strong_trend = is_strong_trend_day(config.NIFTY_TOKEN)

        signal = multi_strategy_signal(config.NIFTY_TOKEN, "NIFTY")
        print("📊 NIFTY SIGNAL:", signal)

        if signal == "HOLD":
            time.sleep(10)
            continue
            
        # -----------------------------
        # SESSION LOGIC
        # -----------------------------
        session_cfg = get_session_config("NIFTY")

        if not session_cfg:
            time.sleep(60)
            continue

        print(f"🕒 NIFTY Session: {get_market_session('NIFTY')}")

        # -----------------------------
        # HIGH PROBABILITY FILTER
        # -----------------------------
        confidence = get_trade_confidence(config.NIFTY_TOKEN, signal)

        if confidence < session_cfg["min_conf"] - 20:
            print("❌ Low confidence (session)")
            time.sleep(10)
            continue
            
        if get_market_session("NIFTY") == "MIDDAY":
            if is_sideways_market(config.NIFTY_TOKEN):
                print("⚠️ Midday sideways — skipping")
                time.sleep(20)
                continue

        # -----------------------------
        # FAST FILTERS
        # -----------------------------
        if signal == last_signal_nifty:
            time.sleep(10)
            continue

        if time.time() - last_trade_time_nifty < SIGNAL_COOLDOWN:
            time.sleep(10)
            continue

        # -----------------------------
        # HEAVY FILTERS
        # -----------------------------
        if is_news_volatility(config.NIFTY_TOKEN):
            time.sleep(20)
            continue

        if not confirm_entry(config.NIFTY_TOKEN, signal):
            time.sleep(10)
            continue
            
        if is_false_breakout(config.NIFTY_TOKEN, signal):
            print("🚫 False breakout — skipping")
            time.sleep(10)
            continue

        if is_reversal_trap(config.NIFTY_TOKEN, signal):
            time.sleep(10)
            continue

        # -----------------------------
        # FIND OPTION
        # -----------------------------
        symbol, price, lot, exchange = find_option(signal, "NIFTY")

        if not symbol or not price or lot is None:
            print("❌ Invalid option — skipping")
            time.sleep(10)
            continue

        # Reduce size in weak trend
        if not strong_trend:
            lot = 1

        # High confidence boost
        if confidence > 80:
            lot *= 2
            
        lot = int(lot * session_cfg["lot_mult"])
        lot = max(1, lot)

        # -----------------------------
        # PLACE ORDER
        # -----------------------------
        filled_price = place_order(symbol, lot, exchange)

        if filled_price:
            last_signal_nifty = signal
            last_trade_time_nifty = time.time()

            nifty_active = True
            manage_trade(symbol, filled_price, lot, exchange, "NIFTY")
            nifty_active = False

def crude_loop():
    global crude_active, last_signal_crude, last_trade_time_crude
    global last_analysis_time
    global report_sent_today

    if time.time() - last_analysis_time > 1800:  # every 30 mins
        adjust_strategy()
        last_analysis_time = time.time()
        
    if last_reset_date != datetime.date.today():
        reset_daily_pnl()

    while True:
        
        now = datetime.datetime.now(IST)

        
        # Send report at 11 PM
        if now.hour == 23 and not report_sent_today:
            send_daily_report()
            report_sent_today = True

        # -----------------------------
        # MARKET TIME (MCX)
        # -----------------------------
        if not (9 <= now.hour < 23):
            time.sleep(60)
            continue

        if crude_active:
            time.sleep(5)
            continue

        if not can_trade():
            time.sleep(30)
            continue
            
        if risk_off:
            print("🛑 Risk OFF active")
            time.sleep(60)
            continue
            
        if not portfolio_safe():
            print("🛑 Portfolio risk active — no trades")
            time.sleep(60)
            continue

        # -----------------------------
        # MARKET FILTERS
        # -----------------------------
        if not is_market_trending(config.CRUDE_TOKEN):
            time.sleep(20)
            continue

        if is_sideways_market(config.CRUDE_TOKEN) and not is_strong_trend_day(config.CRUDE_TOKEN):
            print("⚠️ CRUDE sideways — skipping")
            time.sleep(20)
            continue

        strong_trend = is_strong_trend_day(config.CRUDE_TOKEN)

        signal = multi_strategy_signal(config.CRUDE_TOKEN, "CRUDE")
        print("🛢️ CRUDE SIGNAL:", signal)

        if signal == "HOLD":
            time.sleep(10)
            continue
            
        # -----------------------------
        # SESSION LOGIC
        # -----------------------------
        session_cfg = get_session_config("CRUDE")

        if not session_cfg:
            time.sleep(60)
            continue

        print(f"🕒 CRUDE Session: {get_market_session('CRUDE')}")

        # -----------------------------
        # HIGH PROBABILITY FILTER
        # -----------------------------
        confidence = get_trade_confidence(config.CRUDE_TOKEN, signal)

        if confidence < session_cfg["min_conf"] - 20:
            print("❌ Low confidence (session)")
            time.sleep(10)
            continue
            
        if get_market_session("CRUDE") == "MIDDAY":
            if is_sideways_market(config.CRUDE_TOKEN):
                print("⚠️ CRUDE midday sideways — skipping")
                time.sleep(20)
                continue

        # -----------------------------
        # FAST FILTERS
        # -----------------------------
        if signal == last_signal_crude:
            time.sleep(10)
            continue

        if time.time() - last_trade_time_crude < SIGNAL_COOLDOWN:
            time.sleep(10)
            continue

        # -----------------------------
        # HEAVY FILTERS
        # -----------------------------
        if is_news_volatility(config.CRUDE_TOKEN):
            time.sleep(20)
            continue
            
        

        if not confirm_entry(config.CRUDE_TOKEN, signal):
            time.sleep(10)
            continue
            
        if is_false_breakout(config.CRUDE_TOKEN, signal):
            print("🚫 False breakout — skipping")
            time.sleep(10)
            continue

        if is_reversal_trap(config.CRUDE_TOKEN, signal):
            time.sleep(10)
            continue

        # -----------------------------
        # FIND OPTION
        # -----------------------------
        symbol, price, lot, exchange = find_option(signal, "CRUDE")

        if not symbol or not price or lot is None:
            print("❌ Invalid option — skipping")
            time.sleep(10)
            continue

        # Reduce size in weak trend
        if not strong_trend:
            lot = 1

        # High confidence boost
        if confidence > 80:
            lot *= 2
            
        lot = int(lot * session_cfg["lot_mult"])
        lot = max(1, lot)

        # -----------------------------
        # PLACE ORDER
        # -----------------------------
        filled_price = place_order(symbol, lot, exchange)

        if filled_price:
            last_signal_crude = signal
            last_trade_time_crude = time.time()

            crude_active = True
            manage_trade(symbol, filled_price, lot, exchange, "CRUDE")
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

    trade = {
        "time": str(datetime.datetime.now()),
        "symbol": symbol,
        "entry": entry,
        "exit": exit_price,
        "pnl": pnl,
        "instrument": instrument,
        "session": get_market_session(instrument)
    }

    file = "trade_log.json"

    if not os.path.exists(file):
        with open(file, "w") as f:
            json.dump([], f)

    with open(file, "r") as f:
        data = json.load(f)

    data.append(trade)

    with open(file, "w") as f:
        json.dump(data, f, indent=4)
        
        
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
        return margin["equity"]["available"]["cash"] or 10000
    except:
        return 0
        
def calculate_lots(price, exchange, instrument):

    global win_streak, loss_streak

    balance = get_balance() or 10000
    risk_amount = balance * config.RISK_PER_TRADE
    
    if instrument == "CRUDE":
        return 1

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
            "15minute"
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
    global peak_pnl, win_streak, loss_streak
    global trade_alert_sent
    global report_sent_today, max_drawdown
    

    today = datetime.date.today()

    if last_reset_date != today:
        print("🔄 Resetting daily stats")
        
        trade_alert_sent = {
            "max_trades": False,
            "max_loss": False,
            "target_hit": False
        }

        daily_pnl = 0
        trade_count = 0
        peak_pnl = 0
        win_streak = 0
        loss_streak = 0
        report_sent_today = False
        max_drawdown = 0

        last_reset_date = today
        
def equity_safe():

    global daily_pnl, peak_pnl

    drawdown = daily_pnl - peak_pnl

    if drawdown <= MAX_DRAWDOWN:
        print("🚫 Max drawdown reached — stopping trading")
        send_message("🚫 Equity protection triggered — trading stopped")
        return False

    return True
    
def get_trade_confidence(token, signal):

    try:
        now = datetime.datetime.now()

        df = pd.DataFrame(kite.historical_data(
            token,
            now - datetime.timedelta(minutes=30),
            now,
            "5minute"
        ))

        if len(df) < 10:
            return 0

        df["vwap"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()
        df["vol_ma"] = df["volume"].rolling(5).mean()

        last = df.iloc[-1]
        prev = df.iloc[-2]

        score = 0

        # -----------------------------
        # 1. VWAP CONFIRMATION
        # -----------------------------
        if signal == "CALL" and last["close"] > last["vwap"]:
            score += 25
        elif signal == "PUT" and last["close"] < last["vwap"]:
            score += 25

        # -----------------------------
        # 2. BREAKOUT CONFIRMATION
        # -----------------------------
        if signal == "CALL" and last["close"] > prev["high"]:
            score += 25
        elif signal == "PUT" and last["close"] < prev["low"]:
            score += 25

        # -----------------------------
        # 3. VOLUME SPIKE
        # -----------------------------
        if last["volume"] > last["vol_ma"] * 1.2:
            score += 20

        # -----------------------------
        # 4. STRONG CANDLE
        # -----------------------------
        body = abs(last["close"] - last["open"])
        rng = last["high"] - last["low"]

        if rng > 0 and body > rng * 0.6:
            score += 15

        # -----------------------------
        # 5. TREND DAY BONUS
        # -----------------------------
        if is_strong_trend_day(token):
            score += 15

        print(f"🔥 Confidence Score: {score}")

        return score

    except Exception as e:
        print("Confidence error:", e)
        return 0
        
def is_false_breakout(token, signal):

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

    now = datetime.datetime.now(IST)

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
            

def vwap_signal(token):

    try:
        now = datetime.datetime.now()

        df = pd.DataFrame(kite.historical_data(
            token,
            now - datetime.timedelta(minutes=60),
            now,
            "5minute"
        ))

        if len(df) < 10:
            return "HOLD"

        df["vwap"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()

        last = df.iloc[-1]

        if last["close"] > last["vwap"]:
            return "CALL"
        elif last["close"] < last["vwap"]:
            return "PUT"

        return "HOLD"

    except:
        return "HOLD"
        
def breakout_signal(token):

    try:
        now = datetime.datetime.now()

        df = pd.DataFrame(kite.historical_data(
            token,
            now - datetime.timedelta(minutes=30),
            now,
            "5minute"
        ))

        if len(df) < 5:
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
        
def pullback_signal(token):

    try:
        now = datetime.datetime.now()

        df = pd.DataFrame(kite.historical_data(
            token,
            now - datetime.timedelta(minutes=60),
            now,
            "5minute"
        ))

        if len(df) < 20:
            return "HOLD"

        df["ema"] = df["close"].ewm(span=9).mean()

        last = df.iloc[-1]

        if last["close"] > last["ema"]:
            return "CALL"
        elif last["close"] < last["ema"]:
            return "PUT"

        return "HOLD"

    except:
        return "HOLD"
        
def multi_strategy_signal(token, instrument):

    signals = []

    vwap = vwap_signal(token)
    breakout = breakout_signal(token)
    pullback = pullback_signal(token)

    signals.extend([vwap, breakout, pullback])

    # Optional ML
    try:
        ml = ml_signal()
        signals.append(ml)
    except:
        pass

    print(f"📊 {instrument} Strategy Signals:", signals)

    # -----------------------------
    # 🔥 WEIGHTED SCORING (#4)
    # -----------------------------
    score = 0

    for s in signals:
        if s == "CALL":
            score += 1
        elif s == "PUT":
            score -= 1

    # -----------------------------
    # 🔥 SESSION INTEGRATION (BONUS)
    # -----------------------------
    session = get_market_session(instrument)

    # Midday → strict filtering
    if session == "MIDDAY":
        if abs(score) < 2:
            print("⚠️ Midday weak signal — skipping")
            return "HOLD"

    # Evening crude → allow aggressive
    if instrument == "CRUDE" and session == "EVENING_TREND":
        if score >= 1:
            return "CALL"
        elif score <= -1:
            return "PUT"

    # -----------------------------
    # FINAL DECISION
    # -----------------------------
    if score >= 2:
        return "CALL"
    elif score <= -2:
        return "PUT"

    return "HOLD"
    
    
def analyze_performance():

    file = "trade_log.json"

    if not os.path.exists(file):
        return

    with open(file, "r") as f:
        data = json.load(f)

    if len(data) < 10:
        return

    results = {
        "NIFTY": {"win": 0, "loss": 0},
        "CRUDE": {"win": 0, "loss": 0}
    }

    for t in data[-50:]:  # last 50 trades

        inst = t["instrument"]

        if t["pnl"] > 0:
            results[inst]["win"] += 1
        else:
            results[inst]["loss"] += 1

    print("📊 Performance:", results)

    return results
    
def adjust_strategy():

    results = analyze_performance()

    if not results:
        return

    for inst in ["NIFTY", "CRUDE"]:

        wins = results[inst]["win"]
        losses = results[inst]["loss"]

        total = wins + losses

        if total == 0:
            continue

        win_rate = wins / total

        print(f"{inst} Win Rate: {round(win_rate,2)}")

        # -----------------------------
        # ADJUST CONFIDENCE
        # -----------------------------
        if win_rate < 0.4:
            config.MIN_CONFIDENCE += 5
            print(f"🔒 Increasing confidence for {inst}")

        elif win_rate > 0.6:
            config.MIN_CONFIDENCE -= 5
            print(f"🚀 Relaxing confidence for {inst}")

        # safety limits
        config.MIN_CONFIDENCE = max(50, min(80, config.MIN_CONFIDENCE))
        
        
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
    # DRAWDOWN CONTROL
    # -----------------------------
    drawdown = portfolio_pnl - peak_portfolio

    if drawdown <= config.MAX_DRAWDOWN:
        print("🚫 Max drawdown hit")

        if config.RISK_OFF_AFTER_LOSS:
            risk_off = True

        return False

    return True
    
def send_daily_report():

    global daily_pnl, trade_count, win_streak, loss_streak
    global portfolio_pnl, max_drawdown

    total_trades = trade_count
    wins = win_streak
    losses = loss_streak

    win_rate = 0
    if total_trades > 0:
        win_rate = (wins / total_trades) * 100

    report = f"""
📊 DAILY REPORT

💰 Total PnL: ₹{round(portfolio_pnl,2)}
📈 Trades: {total_trades}
✅ Wins: {wins}
❌ Losses: {losses}
🎯 Win Rate: {round(win_rate,2)}%

📉 Max Drawdown: ₹{round(max_drawdown,2)}
"""

    send_message(report)

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