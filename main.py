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



bot_started = False
lock = threading.Lock()
IST = pytz.timezone("Asia/Kolkata")
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

data_cache = {}
CACHE_TTL = 15  # seconds

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
LTP_TTL = 2  # seconds

quote_cache = {}
QUOTE_TTL = 2

CRUDE_TOKEN = None
NIFTY_FUT_TOKEN = None

ml_cache = {"time": 0, "data": None}
ML_CACHE_TTL = 2  # seconds


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
            return {"min_conf": 55, "lot_mult": 0.7}

        elif session == "AFTERNOON":
            return {"min_conf": 60, "lot_mult": 1}

    else:  # CRUDE

        if session == "MORNING":
            return {"min_conf": 55, "lot_mult": 1}

        elif session == "MIDDAY":
            return {"min_conf": 55, "lot_mult": 0.7}

        elif session == "EVENING_TREND":
            return {"min_conf": 50, "lot_mult": 1.5}

        elif session == "VOLATILE_SESSION":
            return {"min_conf": 65, "lot_mult": 1}

    return None


def safe_ltp(symbol):
    global ltp_cache

    now = time.time()

    if symbol in ltp_cache:
        ts, price = ltp_cache[symbol]
        if now - ts < LTP_TTL:
            return price

    for _ in range(2):
        try:
            price = kite.ltp(symbol)[symbol]["last_price"]
            ltp_cache[symbol] = (now, price)
            return price
        except:
            time.sleep(0.5)

    return None

# -----------------------------
# MARKET FILTERS
# -----------------------------
def is_market_trending(token, df=None):

    try:
        # ✅ Use passed data (FAST)
        if df is None:
            df = get_cached_data(token, "5minute", 20)

        if df is None or len(df) < 10:
            return False

        last = df.iloc[-1]

        # Example logic (keep your existing if different)
        vwap = df["close"].mean()
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

    # 🛑 Portfolio protection FIRST
    if not portfolio_safe():
        return False

    # 🚫 Risk OFF
    if risk_off:
        return False

    # 🛑 Bad day stop
    if daily_pnl < config.MAX_DAILY_LOSS:
        return False

    # 🎯 Profit lock
    if daily_pnl > config.DAILY_TARGET:
        return False

    # 🚫 Max trades
    if trade_count >= config.MAX_TRADES:
        return False

    # ⏳ Cooldown after loss
    if last_loss_time and time.time() - last_loss_time < config.COOLDOWN_AFTER_LOSS:
        return False

    # 🚫 Losing streak control
    if loss_streak >= 3:
        time.sleep(120)
        return False

    return True



# -----------------------------
# NIFTY STRATEGIES
# -----------------------------



def pivot_signal(token):
    try:
        now = datetime.datetime.now()
        df = get_cached_data(token, "5minute", 30)
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
        now = datetime.datetime.now()
        df = get_cached_data(token, "5minute", 30)
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
        now = datetime.datetime.now()

        df = get_cached_data(token, "5minute", 30)

        if len(df) < 20:
            return "HOLD"
            
        df = df.copy()

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
    except:
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
        df = get_cached_data(token, "5minute", 15)

    try:
        full_symbol = f"{exchange}:{symbol}"

        price = safe_ltp(full_symbol)
        if price is None or price < 10 or price > 500:
            return 0

        # -----------------------------
        # 🎯 PRICE OPTIMIZATION
        # -----------------------------
        score = 100 / (abs(price - 100) + 1)

        # -----------------------------
        # 📈 MOMENTUM BOOST
        # -----------------------------
        now = datetime.datetime.now()

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


# -----------------------------
# OPTION SELECTOR
# -----------------------------
def find_option(signal, instrument):

    symbol = None
    price = None
    lot = None
    exchange = None

    # -----------------------------
    # CONFIG
    # -----------------------------
    if instrument == "NIFTY":
        exchange = "NFO"
        name = "NIFTY"
        step = 50
        token = config.NIFTY_TOKEN
        token_symbol = "NSE:NIFTY 50"
        lot_size = 65
    else:
        exchange = "MCX"
        name = "CRUDEOIL"
        step = 100
        token = CRUDE_TOKEN
        token_symbol = f"{exchange}:CRUDEOIL"
        lot_size = 100

    # -----------------------------
    # FETCH DATA (SAFE)
    # -----------------------------
    df = get_cached_data(token, "5minute", 15)
    if df is None or df.empty:
        print("❌ No data for option scoring")
        return None, None, None, None

    # -----------------------------
    # LTP
    # -----------------------------
    ltp = safe_ltp(token_symbol)
    if ltp is None:
        print("❌ LTP fetch failed")
        return None, None, None, None

    atm = round(ltp / step) * step

    # -----------------------------
    # MODE
    # -----------------------------
    saved_mode = load_best_settings(instrument)
    ai_mode = get_strike_mode(token)
    mode = saved_mode if saved_mode else ai_mode

    print(f"{instrument} LTP: {ltp} | Mode: {mode}")

    # -----------------------------
    # CAPITAL
    # -----------------------------
    balance = get_balance() or 10000
    max_trade_value = balance * 0.20

    # -----------------------------
    # STRIKES
    # -----------------------------
    if signal == "CALL":
        strikes = [atm, atm + step, atm - step]
    else:
        strikes = [atm, atm - step, atm + step]

    instruments = get_instruments_cached(exchange)
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
        print("❌ No instruments found")
        return None, None, None, None

    expiry = sorted(set(i["expiry"] for i in opts))[0]
    opt_type = "CE" if signal == "CALL" else "PE"

    candidates = []

    # -----------------------------
    # MAIN SELECTION
    # -----------------------------
    for strike in strikes:

        for i in opts:

            if i["expiry"] != expiry or i["instrument_type"] != opt_type:
                continue

            try:
                s = int(i["strike"])
            except:
                continue

            if instrument == "CRUDE" and s % 100 != 0:
                continue

            if s != strike:
                continue

            sym = f"{exchange}:{i['tradingsymbol']}"
            p = safe_ltp(sym)

            if p is None:
                continue

            if not is_liquid_option(i["tradingsymbol"], exchange):
                continue

            trade_value = p * lot_size
            if trade_value > max_trade_value * 3:
                continue

            score = score_option(i["tradingsymbol"], exchange, token, signal, df)

            candidates.append({
                "symbol": i["tradingsymbol"],
                "price": p,
                "score": score
            })

    # -----------------------------
    # BEST PICK
    # -----------------------------
    if candidates:
        best = sorted(candidates, key=lambda x: x["score"], reverse=True)[0]

        print(f"🏆 Selected: {best['symbol']} @ {best['price']}")

        lot = calculate_lots(best["price"], exchange, instrument, strong_trend=False)

        return best["symbol"], best["price"], lot, exchange

    print("🚫 No candidates — fallback")

    # -----------------------------
    # FALLBACK
    # -----------------------------
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

        diff = abs(s - atm)

        if diff < min_diff:

            sym = f"{exchange}:{i['tradingsymbol']}"
            p = safe_ltp(sym)

            if p is None:
                continue

            if not is_liquid_option(i["tradingsymbol"], exchange):
                continue

            trade_value = p * lot_size
            if trade_value <= max_trade_value * 3:
                min_diff = diff
                best = i["tradingsymbol"]
                best_price = p

    if best:
        print(f"✅ Fallback selected: {best} @ {best_price}")

        lot = calculate_lots(best_price, exchange, instrument, strong_trend=False)

        return best, best_price, lot, exchange

    print("❌ No valid option found")

    return None, None, None, None

# -----------------------------
# ORDER
# -----------------------------

def place_order(symbol, qty, exchange, instrument):
    
    print(f"🚀 PLACE ORDER CALLED: {symbol}, lot: {qty}, exchange: {exchange}")

    #if not is_good_spread(symbol, exchange):
    #    print("🚫 Spread too high — skipping")
    #    return None

    try:
        full_symbol = f"{exchange}:{symbol}"

        ltp = safe_ltp(full_symbol)
        if ltp is None:
            return None

        expected_price = ltp
        
        # 🔥 SMART LIMIT PRICE (ADD HERE)

        spread_buffer = 0.003 if exchange == "NFO" else 0.005
        price = round(ltp * (1 + spread_buffer), 1)
        
        print("➡️ Sending order to Zerodha...")

        order_id = kite.place_order(
            variety="regular",
            exchange=exchange,
            tradingsymbol=symbol,
            transaction_type="BUY",
            quantity=get_quantity(qty, exchange),
            order_type="LIMIT",
            price=price,
            product = "MIS" if exchange == "NFO" else "NRML"
        )

        send_message(f"📥 Order placed: {symbol} @ {price}")

        filled_price = None

        for _ in range(3):
            time.sleep(1)

            orders = kite.orders()

            for o in orders:
                if o["order_id"] == order_id and o["status"] == "COMPLETE":
                    filled_price = o["average_price"]
                    break

            if filled_price:
                break

            # small adjustment
            price = round(price * 1.002, 1)

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
        
        # 🚫 SLIPPAGE PROTECTION (ADD HERE)
        if abs(slippage) > expected_price * 0.02:
            send_message(f"⚠️ High slippage — continue with caution\n{symbol}")

        return filled_price

    except Exception as e:
        print("❌ ORDER ERROR FULL:", str(e))
        send_message(f"❌ Order error: {e}")
        return None

# -----------------------------
# TRADE MGMT
# -----------------------------
def manage_trade(symbol, entry, qty, exchange, instrument):

    global daily_pnl, trade_count, last_loss_time
    global win_streak, loss_streak
    global portfolio_pnl, peak_portfolio, risk_off
    global max_drawdown

    with lock:
        trade_count += 1

    full_symbol = f"{exchange}:{symbol}"
    actual_qty = get_quantity(qty, exchange)
    remaining_qty = actual_qty

    # -----------------------------
    # 🔥 SL CALCULATION (FIXED)
    # -----------------------------
    df = get_cached_data(
        config.NIFTY_TOKEN if instrument == "NIFTY" else CRUDE_TOKEN,
        "5minute",
        30
    )

    if df is not None and len(df) > 10:
        df["range"] = df["high"] - df["low"]
        atr = df["range"].rolling(10).mean().iloc[-1]

        if instrument == "NIFTY":
            sl = max(entry - (atr * 0.8), entry * 0.88)

        elif instrument == "CRUDE":
            sl = max(entry - (atr * 1.2), entry * 0.82)

        else:
            sl = max(entry - atr, entry * 0.85)

    else:
        sl = entry * 0.90 if instrument == "NIFTY" else entry * 0.85

    # -----------------------------
    # 🎯 DYNAMIC RR
    # -----------------------------
    risk = entry - sl

    strong_trend = is_strong_trend_day(
        config.NIFTY_TOKEN if instrument == "NIFTY" else CRUDE_TOKEN
    )

    rr = 2
    if instrument == "NIFTY":
        rr = 3 if strong_trend else 2
    elif instrument == "CRUDE":
        rr = 3.5 if strong_trend else 2.5

    target = entry + risk * rr

    trailing_sl = sl
    highest_price = entry

    entry_time = time.time()
    partial_booked = False

    send_message(f"🚀 {instrument} TRADE\n{symbol} @ {entry}")

    while True:
        try:
            ltp = safe_ltp(full_symbol)
            if ltp is None:
                time.sleep(1)
                continue

            profit = ltp - entry

            # -----------------------------
            # 🔥 QUICK SL (ADJUSTED)
            # -----------------------------
            if (instrument == "NIFTY" and profit < -entry * 0.05) or \
               (instrument == "CRUDE" and profit < -entry * 0.08):

                pnl = (ltp - entry) * remaining_qty

                with lock:
                    daily_pnl += pnl
                    portfolio_pnl += pnl

                    # 🚀 ADD THIS (PEAK TRACKING)
                    if portfolio_pnl > peak_portfolio:
                        peak_portfolio = portfolio_pnl
                        
                    # 📉 TRACK MAX DRAWDOWN (ADD THIS)
                    drawdown = peak_portfolio - portfolio_pnl
                    if drawdown > max_drawdown:
                        max_drawdown = drawdown

                send_message(f"🚫 Quick SL\n{symbol}")
                log_trade(symbol, entry, ltp, pnl, instrument)

                # ✅ UPDATE STREAKS (CORRECT PLACE)
                if pnl > 0:
                    win_streak += 1
                    loss_streak = 0
                else:
                    loss_streak += 1
                    win_streak = 0
                    last_loss_time = time.time()

                break
                

            # -----------------------------
            # ⏱ TIME EXIT
            # -----------------------------
            if time.time() - entry_time > 900:
                pnl = (ltp - entry) * remaining_qty

                with lock:
                    daily_pnl += pnl
                    portfolio_pnl += pnl
                    
                    # 🚀 ADD THIS (PEAK TRACKING)
                    if portfolio_pnl > peak_portfolio:
                        peak_portfolio = portfolio_pnl
                        
                    # 📉 TRACK MAX DRAWDOWN (ADD THIS)
                    drawdown = peak_portfolio - portfolio_pnl
                    if drawdown > max_drawdown:
                        max_drawdown = drawdown

                send_message(f"⏱ TIME EXIT\n{symbol}")
                log_trade(symbol, entry, ltp, pnl, instrument)

                # ✅ UPDATE STREAKS (CORRECT PLACE)
                if pnl > 0:
                    win_streak += 1
                    loss_streak = 0
                else:
                    loss_streak += 1
                    win_streak = 0
                    last_loss_time = time.time()

                break

            # -----------------------------
            # 📈 TRACK HIGH
            # -----------------------------
            if ltp > highest_price:
                highest_price = ltp

            # -----------------------------
            # 💰 PARTIAL BOOKING
            # -----------------------------
            if not partial_booked and remaining_qty == actual_qty:

                if (instrument == "NIFTY" and profit > entry * 0.08) or \
                   (instrument == "CRUDE" and profit > entry * 0.12):

                    partial_qty = actual_qty // 2

                    if partial_qty > 0:
                        pnl = (ltp - entry) * partial_qty

                        with lock:
                            daily_pnl += pnl
                            portfolio_pnl += pnl
                            
                            # 🚀 ADD THIS (PEAK TRACKING)
                            if portfolio_pnl > peak_portfolio:
                                peak_portfolio = portfolio_pnl
                                
                                
                            # 📉 TRACK MAX DRAWDOWN (ADD THIS)
                            drawdown = peak_portfolio - portfolio_pnl
                            if drawdown > max_drawdown:
                                max_drawdown = drawdown

                        remaining_qty -= partial_qty
                        partial_booked = True

                        send_message(f"💰 PARTIAL EXIT\n{symbol}")
                        log_trade(symbol, entry, ltp, pnl, instrument + "_PARTIAL")

                        
                        
            # ⚡ REVERSAL EXIT (VERY POWERFUL)

            if instrument == "NIFTY" and profit > 0:
                if ltp < highest_price * 0.98:
                    pnl = (ltp - entry) * remaining_qty

                    with lock:
                        daily_pnl += pnl
                        portfolio_pnl += pnl
                        
                        # 🚀 ADD THIS (PEAK TRACKING)
                        if portfolio_pnl > peak_portfolio:
                            peak_portfolio = portfolio_pnl
                            
                        # 📉 TRACK MAX DRAWDOWN (ADD THIS)
                        drawdown = peak_portfolio - portfolio_pnl
                        if drawdown > max_drawdown:
                            max_drawdown = drawdown

                    send_message(f"⚡ Momentum exit\n{symbol}")
                    log_trade(symbol, entry, ltp, pnl, instrument)

                    # ✅ UPDATE STREAKS (CORRECT PLACE)
                    if pnl > 0:
                        win_streak += 1
                        loss_streak = 0
                    else:
                        loss_streak += 1
                        win_streak = 0
                        last_loss_time = time.time()

                    break

            if instrument == "CRUDE" and profit > 0:
                if ltp < highest_price * 0.97:

                    pnl = (ltp - entry) * remaining_qty

                    with lock:
                        daily_pnl += pnl
                        portfolio_pnl += pnl
                        
                        
                        # 🚀 ADD THIS (PEAK TRACKING)
                        if portfolio_pnl > peak_portfolio:
                            peak_portfolio = portfolio_pnl
                            
                        # 📉 TRACK MAX DRAWDOWN (ADD THIS)
                        drawdown = peak_portfolio - portfolio_pnl
                        if drawdown > max_drawdown:
                            max_drawdown = drawdown

                    send_message(f"⚡ Momentum exit\n{symbol}")
                    log_trade(symbol, entry, ltp, pnl, instrument)

                    # ✅ UPDATE STREAKS (CORRECT PLACE)
                    if pnl > 0:
                        win_streak += 1
                        loss_streak = 0
                    else:
                        loss_streak += 1
                        win_streak = 0
                        last_loss_time = time.time()

                    break

            # -----------------------------
            # ⚡ TRAILING SL
            # -----------------------------
            trail_pct = 0.96 if instrument == "NIFTY" else 0.97

            if profit > entry * 0.05:
                trailing_sl = max(trailing_sl, entry * 1.02)

            if ltp > entry:
                trailing_sl = max(trailing_sl, ltp * trail_pct)

            # -----------------------------
            # 🛑 SL EXIT
            # -----------------------------
            if ltp <= trailing_sl:

                pnl = (ltp - entry) * remaining_qty

                with lock:
                    daily_pnl += pnl
                    portfolio_pnl += pnl
                    
                    
                    # 🚀 ADD THIS (PEAK TRACKING)
                    if portfolio_pnl > peak_portfolio:
                        peak_portfolio = portfolio_pnl
                        
                    # 📉 TRACK MAX DRAWDOWN (ADD THIS)
                    drawdown = peak_portfolio - portfolio_pnl
                    if drawdown > max_drawdown:
                        max_drawdown = drawdown

                send_message(f"🛑 EXIT\n{symbol}")
                log_trade(symbol, entry, ltp, pnl, instrument)

                # ✅ UPDATE STREAKS (CORRECT PLACE)
                if pnl > 0:
                    win_streak += 1
                    loss_streak = 0
                else:
                    loss_streak += 1
                    win_streak = 0
                    last_loss_time = time.time()

                break

            # -----------------------------
            # 🎯 TARGET HIT
            # -----------------------------
            if ltp >= target:

                if remaining_qty > 1:
                    half = remaining_qty // 2

                    pnl = (ltp - entry) * half

                    with lock:
                        daily_pnl += pnl
                        portfolio_pnl += pnl
                        
                        
                         # 🚀 ADD THIS (PEAK TRACKING)
                        if portfolio_pnl > peak_portfolio:
                            peak_portfolio = portfolio_pnl
                            
                        # 📉 TRACK MAX DRAWDOWN (ADD THIS)
                        drawdown = peak_portfolio - portfolio_pnl
                        if drawdown > max_drawdown:
                            max_drawdown = drawdown

                    remaining_qty -= half
                    send_message(f"💰 Runner booked\n{symbol}")
                    continue

                else:
                    pnl = (ltp - entry) * remaining_qty

                    with lock:
                        daily_pnl += pnl
                        portfolio_pnl += pnl
                        
                        
                        # 🚀 ADD THIS (PEAK TRACKING)
                        if portfolio_pnl > peak_portfolio:
                            peak_portfolio = portfolio_pnl
                            
                        # 📉 TRACK MAX DRAWDOWN (ADD THIS)
                        drawdown = peak_portfolio - portfolio_pnl
                        if drawdown > max_drawdown:
                            max_drawdown = drawdown

                    send_message(f"🎯 FINAL EXIT\n{symbol}")
                    log_trade(symbol, entry, ltp, pnl, instrument)

                    # ✅ UPDATE STREAKS (CORRECT PLACE)
                    if pnl > 0:
                        win_streak += 1
                        loss_streak = 0
                    else:
                        loss_streak += 1
                        win_streak = 0
                        last_loss_time = time.time()

                    break

            time.sleep(1)

        except Exception as e:
            print("Trade error:", e)
            break
            
            
            
def run_trade_wrapper(symbol, price, lot, exchange, instrument):
    global nifty_active, crude_active

    # ✅ SET ACTIVE FLAG
    if instrument == "NIFTY":
        nifty_active = True
    else:
        crude_active = True

    try:
        manage_trade(symbol, price, lot, exchange, instrument)

    finally:
        # ✅ RESET ONLY AFTER TRADE COMPLETES
        if instrument == "NIFTY":
            nifty_active = False
        else:
            crude_active = False
 
 
# -----------------------------
# THREADS
# -----------------------------
def nifty_loop():
    global nifty_active, last_signal_nifty, last_trade_time_nifty
    global last_analysis_time, report_sent_today

    print("🔥 NIFTY LOOP STARTED")

    if time.time() - last_analysis_time > 1800:
        adjust_strategy()
        last_analysis_time = time.time()

    if last_reset_date != datetime.date.today():
        reset_daily_pnl()

    while True:
        now = datetime.datetime.now()

        # 🚨 HARD STOP
        if portfolio_pnl < HARD_STOP_LOSS:
            print("🚨 HARD STOP ACTIVATED — NIFTY")
            send_message("🚨 HARD STOP — Trading stopped (NIFTY)")
            break

        # -----------------------------
        # FETCH DATA
        # -----------------------------
        df = get_cached_data(config.NIFTY_TOKEN, "5minute", 20)

        if df is None or df.empty:
            print("❌ EMPTY DATA — SKIPPING NIFTY")
            time.sleep(5)
            continue

        if len(df) < 5:
            print("⚠️ Not enough candles")
            time.sleep(5)
            continue

        last = df.iloc[-1]
        prev = df.iloc[-2]

        # -----------------------------
        # TIME FILTERS
        # -----------------------------
        if 12 <= now.hour < 13:
            time.sleep(60)
            continue

        if now.hour == 9 and now.minute < 20:
            time.sleep(60)
            continue

        if now.hour == 15 and now.minute > 25:
            time.sleep(60)
            continue

        if not (9 <= now.hour < 15 or (now.hour == 15 and now.minute <= 30)):
            time.sleep(60)
            continue

        if now.hour == 15 and now.minute >= 30 and not report_sent_today:
            send_daily_report()
            report_sent_today = True

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

        # -----------------------------
        # TREND
        # -----------------------------
        trend = is_market_trending(config.NIFTY_TOKEN)
        strong_trend = is_strong_trend_day(config.NIFTY_TOKEN)

        # -----------------------------
        # SIGNAL (FIXED POSITION)
        # -----------------------------
        signal, ml_conf = multi_strategy_signal(config.NIFTY_TOKEN, "NIFTY")

        if ml_conf < 50:
            print("⚠️ Weak ML — skipping")
            print("ML CONF:", ml_conf)
            continue
       

        if signal == "HOLD":
            print("Nifty Signal:", signal)
            time.sleep(5)
            continue

        # -----------------------------
        # SCORING
        # -----------------------------
        body = abs(last["close"] - last["open"])
        rng = last["high"] - last["low"]
        trade_score = 0

        move = abs(last["close"] - prev["close"])
        if move > last["close"] * 0.003:
            trade_score += 20

        if rng > 0 and body > rng * 0.5:
            trade_score += 20

        vol_ma = df["volume"].rolling(5).mean().iloc[-1]
        if last["volume"] > vol_ma:
            trade_score += 20

        if trend:
            trade_score += 20

        

        print(f"🎯 Score: {trade_score}")

        if trade_score < 10:
            print("Trade Score:", trade_score)
            continue

        # -----------------------------
        # SESSION
        # -----------------------------
        session_cfg = get_session_config("NIFTY")
        if not session_cfg:
            time.sleep(60)
            continue

        confidence = get_trade_confidence(
            config.NIFTY_TOKEN, signal, df, strong_trend
        )
        confidence += 5
        
        if NIFTY_FUT_TOKEN:
            fut_df = get_cached_data(NIFTY_FUT_TOKEN, "5minute", 20)

            if fut_df is not None and len(fut_df) > 5:
                fut_last = fut_df.iloc[-1]
                fut_prev = fut_df.iloc[-2]

                if signal == "CALL" and fut_last["close"] < fut_prev["close"]:
                    confidence -= 5

                if signal == "PUT" and fut_last["close"] > fut_prev["close"]:
                    confidence -= 5

        # ✅ FUTURE VOLUME BOOST (FIXED POSITION)
        if NIFTY_FUT_TOKEN and 'fut_df' in locals() and fut_df is not None:
            try:
                fut_vol_ma = fut_df["volume"].rolling(5).mean().iloc[-1]
                if fut_last["volume"] > fut_vol_ma:
                    confidence += 5
            except:
                pass

        if confidence < session_cfg["min_conf"]:
            print("Confidence:", confidence, "Required:", session_cfg["min_conf"])
            continue

        print(f"Confidence: {confidence}")

        # -----------------------------
        # FAST FILTERS
        # -----------------------------
        if signal == last_signal_nifty:
            continue

        if time.time() - last_trade_time_nifty < SIGNAL_COOLDOWN:
            continue

        if not confirm_entry(config.NIFTY_TOKEN, signal, df):
            print("Confirm:", confirm_entry(config.NIFTY_TOKEN, signal, df))
            continue


        # -----------------------------
        # OPTION SELECTION
        # -----------------------------
        symbol, price, lot, exchange = find_option(signal, "NIFTY")

        if not symbol or not price or lot is None:
            print("Option:", symbol, price, lot)
            continue

        # -----------------------------
        # LOT SIZE
        # -----------------------------
        if not strong_trend:
            lot = 1

        if strong_trend:
            if confidence > 90:
                lot = int(lot * 1.8)
            elif confidence > 80:
                lot = int(lot * 1.5)

        lot = int(lot * session_cfg["lot_mult"])
        lot = min(lot, config.MAX_LOTS)
        lot = max(1, lot)
        
        # 🚀 Confidence-based lot boost
        if ml_conf > 90:
            lot = int(lot * 2)
        elif ml_conf > 80:
            lot = int(lot * 1.5)
       



        # -----------------------------
        # ENTRY TIMING
        # -----------------------------
        ltp1 = safe_ltp(f"{exchange}:{symbol}")
        time.sleep(0.3)
        ltp2 = safe_ltp(f"{exchange}:{symbol}")

        if ltp1 is None or ltp2 is None:
            continue

        if signal == "CALL" and ltp2 < ltp1:
            continue

        if signal == "PUT" and ltp2 > ltp1:
            continue

        # -----------------------------
        # ORDER
        # -----------------------------
        print(f"🚀 Placing NIFTY order: {symbol}, lot: {lot}")

        filled_price = place_order(symbol, lot, exchange, "NIFTY")

        if filled_price:
            last_signal_nifty = signal
            last_trade_time_nifty = time.time()

            threading.Thread(
                target=run_trade_wrapper,
                args=(symbol, filled_price, lot, exchange, "NIFTY"),
                daemon=True
            ).start()

        time.sleep(1)
        
        print(f"""
            DEBUG:
            Signal: {signal}
            ML: {ml_conf}
            Score: {trade_score}
            Confidence: {confidence}
            """)

def crude_loop():
    global crude_active, last_signal_crude, last_trade_time_crude
    global last_analysis_time, report_sent_today

    print("🔥 CRUDE LOOP STARTED")

    if time.time() - last_analysis_time > 1800:
        adjust_strategy()
        last_analysis_time = time.time()

    if last_reset_date != datetime.date.today():
        reset_daily_pnl()

    while True:
        now = datetime.datetime.now(IST)
        print("💓 CRUDE LOOP RUNNING")

        # 🚨 HARD STOP
        if portfolio_pnl < HARD_STOP_LOSS:
            print("🚨 HARD STOP ACTIVATED — CRUDE")
            send_message("🚨 HARD STOP — Trading stopped (CRUDE)")
            break

        # -----------------------------
        # DATA FETCH
        # -----------------------------
        df = get_cached_data(CRUDE_TOKEN, "5minute", 20)

        if df is None or df.empty:
            print("❌ EMPTY DATA — SKIPPING")
            time.sleep(5)
            continue

        if len(df) < 5:
            print("⚠️ Not enough candles")
            time.sleep(5)
            continue

        last = df.iloc[-1]
        prev = df.iloc[-2]

        # -----------------------------
        # TIME FILTERS (FIXED)
        # -----------------------------
        if 12 <= now.hour < 13:
            time.sleep(60)
            continue

        if now.hour == 9 and now.minute < 20:
            time.sleep(60)
            continue

        # ✅ CRUDE FULL SESSION
        if not (9 <= now.hour <= 23):
            time.sleep(60)
            continue

        # 📊 Daily report
        if now.hour == 23 and not report_sent_today:
            send_daily_report()
            report_sent_today = True

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

        # -----------------------------
        # TREND (OPTIMIZED)
        # -----------------------------
        trend = is_market_trending(CRUDE_TOKEN, df)
        strong_trend = is_strong_trend_day(CRUDE_TOKEN, df)

        # -----------------------------
        # SCORING
        # -----------------------------
        body = abs(last["close"] - last["open"])
        rng = last["high"] - last["low"]
        trade_score = 0

        move = abs(last["close"] - prev["close"])
        if move > last["close"] * 0.003:
            trade_score += 20

        if rng > 0 and body > rng * 0.5:
            trade_score += 20

        vol_ma = df["volume"].rolling(5).mean().iloc[-1]
        if last["volume"] > vol_ma:
            trade_score += 20

        if trend:
            trade_score += 20

        

        # -----------------------------
        # SIGNAL
        # -----------------------------
        signal, ml_conf = multi_strategy_signal(CRUDE_TOKEN, "CRUDE")

        if ml_conf < 50:
            print("ML CONF:", ml_conf)
            continue
        
        crude_sig = get_crude_signal(CRUDE_TOKEN)

        print(f"🎯 Signal: {signal}, Score: {trade_score}")

        if signal == "HOLD":
            print("Crude Signal:", signal)
            time.sleep(5)
            continue

        # -----------------------------
        # RELAXED SCORE
        # -----------------------------
        if trade_score < 10:
            print("Trade Score:", trade_score)
            continue

        # -----------------------------
        # SESSION CONFIG
        # -----------------------------
        session_cfg = get_session_config("CRUDE")
        if not session_cfg:
            time.sleep(60)
            continue

        confidence = get_trade_confidence(CRUDE_TOKEN, signal, df, strong_trend)
        confidence += 5

        # ✅ SIGNAL BOOST
        if crude_sig == signal:
            confidence += 10

        # ✅ SMALL BOOST (NO ML fallback)
        confidence += 3

        if crude_sig != "HOLD" and crude_sig != signal:
            print("⚠️ Signal conflict — allowing")


        # ✅ RELAXED CONFIDENCE
        if confidence < session_cfg["min_conf"]:
            print("Confidence:", confidence, "Required:", session_cfg["min_conf"])
            
            time.sleep(10)
            continue

        print(f"Confidence: {confidence}")

        # -----------------------------
        # FAST FILTERS
        # -----------------------------
        if signal == last_signal_crude:
            continue

        if time.time() - last_trade_time_crude < SIGNAL_COOLDOWN:
            continue

        # ✅ SOFT CONFIRMATION (FIXED)
        if not confirm_entry(CRUDE_TOKEN, signal, df):
            print("Confirm:", confirm_entry(CRUDE_TOKEN, signal, df))


        # -----------------------------
        # OPTION SELECTION
        # -----------------------------
        symbol, price, lot, exchange = find_option(signal, "CRUDE")

        print(f"Selected Option: {symbol}, Price: {price}, Lot: {lot}")

        if not symbol or not price or lot is None:
            print("Option:", symbol, price, lot)
            continue

        # -----------------------------
        # LOT SIZE
        # -----------------------------
        if not strong_trend:
            lot = 1

        if strong_trend:
            if confidence > 90:
                lot = int(lot * 1.8)
            elif confidence > 80:
                lot = int(lot * 1.5)

        lot = int(lot * session_cfg["lot_mult"])
        lot = min(lot, config.MAX_LOTS)
        lot = max(1, lot)
        
        # 🚀 Confidence-based lot boost
        if ml_conf > 90:
            lot = int(lot * 2)
        elif ml_conf > 80:
            lot = int(lot * 1.5)
        

        if strong_trend and trade_score > 70:
            lot = min(int(lot * 1.2), config.MAX_LOTS)

        # -----------------------------
        # ENTRY TIMING
        # -----------------------------
        ltp1 = safe_ltp(f"{exchange}:{symbol}")
        time.sleep(0.3)
        ltp2 = safe_ltp(f"{exchange}:{symbol}")

        if ltp1 is None or ltp2 is None:
            continue

        if signal == "CALL" and ltp2 < ltp1:
            continue

        if signal == "PUT" and ltp2 > ltp1:
            print("🚫 Weak PUT momentum")
            continue

        # -----------------------------
        # ORDER
        # -----------------------------
        print(f"🚀 Placing order: {symbol}, lot: {lot}")

        filled_price = place_order(symbol, lot, exchange, "CRUDE")

        if filled_price:
            last_signal_crude = signal
            last_trade_time_crude = time.time()

            threading.Thread(
                target=run_trade_wrapper,
                args=(symbol, filled_price, lot, exchange, "CRUDE"),
                daemon=True
            ).start()

        time.sleep(1)
        
        print(f"""
            DEBUG:
            Signal: {signal}
            ML: {ml_conf}
            Score: {trade_score}
            Confidence: {confidence}
            """)

        
def get_strike_mode(token):

    try:
        now = datetime.datetime.now()

        df = get_cached_data(token, "5minute", 30)

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
            df = get_cached_data(token, "5minute", 20)

        if df is None or len(df) < 10:
            return False
            
            
        df = df.copy()
        df["vwap"] = (df["close"] * df["volume"]).cumsum() / df["volume"].cumsum()

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
        
def calculate_lots(price, exchange, instrument, strong_trend=False):

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
        token = CRUDE_TOKEN

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
    if strong_trend and win_streak >= 1:
        lots = int(lots * 1.5)

    # Safety
    lots = max(1, lots)
    lots = min(lots, config.MAX_LOTS)

    return lots

def is_strong_trend_day(token, df=None):

    try:
        if df is None:
            df = get_cached_data(token, "5minute", 20)

        if df is None or len(df) < 10:
            return False

        move = abs(df.iloc[-1]["close"] - df.iloc[0]["close"])

        return move > df.iloc[-1]["close"] * 0.01

    except:
        return False
        
def is_reversal_trap(token, signal):

    try:
        now = datetime.datetime.now()

        df = get_cached_data(token, "5minute", 30)
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
        now = datetime.datetime.now()

        df = get_cached_data(token, "5minute", 30)
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
    global trade_alert_sent
    global report_sent_today, max_drawdown
    global portfolio_pnl, peak_portfolio   # ✅ CORRECT VARIABLES

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

        # ✅ FIXED VARIABLES
        portfolio_pnl = 0
        peak_portfolio = 0

        win_streak = 0
        loss_streak = 0
        report_sent_today = False
        max_drawdown = 0

        last_reset_date = today
        
 
def get_trade_confidence(token, signal, df=None, strong_trend=False):

    try:
        if df is None:
            df = get_cached_data(token, "5minute", 30)

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
        now = datetime.datetime.now()

        df = get_cached_data(token, "5minute", 30)
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

        df = get_cached_data(token, "5minute", 30)
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
        
def breakout_signal(token):
    try:
        now = datetime.datetime.now()

        df = get_cached_data(token, "5minute", 30)
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
        
def pullback_signal(token):

    try:
        now = datetime.datetime.now()

        df = get_cached_data(token, "5minute", 30)
        if df is None or len(df) < 10:
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
        

        
def multi_strategy_signal(token, instrument):

    signals = []
    ml_conf = 50

    signals.append(vwap_signal(token))
    signals.append(breakout_signal(token))
    signals.append(pullback_signal(token))

    try:
        data = get_ml_cached()

        if data:
            ml_signal = data.get("signal", "HOLD")
            ml_conf = data.get("confidence", 50)

            if ml_conf > 60:
                signals.append(ml_signal)

    except:
        pass

    call_count = signals.count("CALL")
    put_count = signals.count("PUT")

    if call_count >= 2:
        return "CALL", ml_conf

    if put_count >= 2:
        return "PUT", ml_conf

    return "HOLD", ml_conf
    
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
        now = datetime.datetime.now()

        df = get_cached_data(token, "5minute", 30)
        if df is None or len(df) < 10:
            return True

        day_range = df["high"].max() - df["low"].min()

        # Tune for instruments
        if day_range < 60:   # NIFTY
            return True

        return False

    except:
        return True
        
        
def get_cached_data(token, interval, duration):

    global data_cache

    key = f"{token}_{interval}"

    now_ts = time.time()

    if key in data_cache:
        ts, df = data_cache[key]
        if now_ts - ts < CACHE_TTL:
            return df

    try:
        now = datetime.datetime.now()
        from_time = now - datetime.timedelta(days=2)

        data = kite.historical_data(
            token,
            from_time,
            now,
            interval
        )

        df = pd.DataFrame(data)

        data_cache[key] = (now_ts, df)

        return df

    except Exception as e:
        print("❌ Data fetch error:", e)
        return None



        
def backtest(token, instrument, days=5):

    print(f"📊 Running backtest for {instrument}")

    now = datetime.datetime.now()

    df = pd.DataFrame(kite.historical_data(
        token,
        now - datetime.timedelta(days=days),
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
        sl = entry * 0.90
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
    
def send_daily_report():

    global portfolio_pnl, max_drawdown

    file = "trade_log.json"

    if not os.path.exists(file):
        send_message("📊 No trades today")
        return

    with open(file, "r") as f:
        data = json.load(f)

    today = datetime.date.today()

    wins = 0
    losses = 0
    total_trades = 0

    for t in data:
        trade_date = datetime.datetime.fromisoformat(t["time"]).date()

        if trade_date == today:

            # Ignore partial logs if needed
            if "_PARTIAL" in t["instrument"]:
                continue

            total_trades += 1

            if t["pnl"] > 0:
                wins += 1
            else:
                losses += 1

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

    import time
    import os
    import atexit
    import threading

    # -----------------------------
    # 🎯 TOKEN INITIALIZATION (FIXED)
    # -----------------------------
    NIFTY_TOKEN = 256265  # Index (always safe)

    CRUDE_TOKEN = get_latest_fut_token("CRUDEOIL", "MCX")
    NIFTY_FUT_TOKEN = get_nifty_fut_token()

    if CRUDE_TOKEN is None:
        print("🚨 CRUDE DISABLED — TOKEN NOT FOUND")

    if NIFTY_FUT_TOKEN is None:
        print("⚠️ NIFTY FUT TOKEN NOT FOUND")

    # -----------------------------
    # 🔍 API TEST
    # -----------------------------
    try:
        print("🔍 Testing Kite API...")
        test = kite.ltp("NSE:NIFTY 50")
        print("✅ Kite API working:", test)
    except Exception as e:
        print("❌ Kite API FAILED:", e)

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
    # 🚀 START ML SERVER
    # -----------------------------
    threading.Thread(target=run_ml_server, daemon=True).start()
    print("✅ ML thread started")

    time.sleep(2)

    # -----------------------------
    # 🚀 START TRADING LOOPS
    # -----------------------------
    threading.Thread(target=nifty_loop, daemon=True).start()

    # ✅ Start CRUDE only if token valid
    if CRUDE_TOKEN:
        threading.Thread(target=crude_loop, daemon=True).start()
    else:
        print("⚠️ CRUDE LOOP SKIPPED")

    print("🚀 Trading engine started")

    # -----------------------------
    # 📢 START MESSAGE
    # -----------------------------
    time.sleep(2)
    try:
        send_message("🚀 Bot started successfully")
    except:
        pass

    # -----------------------------
    # 🔁 DAILY TOKEN REFRESH (PRO)
    # -----------------------------
    def refresh_tokens():
        global CRUDE_TOKEN, NIFTY_FUT_TOKEN

        while True:
            now = datetime.datetime.now()

            if now.hour == 9 and now.minute < 5:
                print("🔄 Refreshing tokens...")

                CRUDE_TOKEN = get_latest_fut_token("CRUDEOIL", "MCX")
                NIFTY_FUT_TOKEN = get_nifty_fut_token()

                print("✅ Tokens refreshed")

            time.sleep(60)

    threading.Thread(target=refresh_tokens, daemon=True).start()

    # -----------------------------
    # 🔁 KEEP ALIVE
    # -----------------------------
    while True:
        time.sleep(60)