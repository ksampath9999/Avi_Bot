import requests
import time
import datetime
import pytz
from kiteconnect import KiteConnect
import config
from telegram_bot import send_message

# -----------------------------
# INIT
# -----------------------------
kite = KiteConnect(api_key=config.API_KEY)
kite.set_access_token(config.ACCESS_TOKEN)

IST = pytz.timezone("Asia/Kolkata")

# -----------------------------
# CONFIG
# -----------------------------
CAPITAL = 100000
RISK_PER_TRADE = config.RISK_PER_TRADE
MAX_DAILY_LOSS = config.MAX_DAILY_LOSS
MAX_TRADES = config.MAX_TRADES

SIGNAL_URL = "https://avi-bot-1.onrender.com/signal"  # 🔴 your live server

trades_today = 0
daily_loss = 0
trade_active = False


# -----------------------------
# GET SIGNAL
# -----------------------------
def get_signal():
    try:
        res = requests.get(SIGNAL_URL, timeout=10)

        if res.status_code != 200:
            print("Bad response:", res.text)
            return {"signal": "HOLD"}

        data = res.json()
        print("Server Response:", data)
        return data

    except Exception as e:
        print("Signal error:", e)
        return {"signal": "HOLD"}


# -----------------------------
# GET PRICE
# -----------------------------
def get_ltp(symbol):
    return kite.ltp(symbol)[symbol]["last_price"]


# -----------------------------
# POSITION SIZE
# -----------------------------
def calculate_qty(price):
    risk_amount = CAPITAL * RISK_PER_TRADE
    qty = int(risk_amount / price)
    return max(config.LOT_SIZE, qty)


# -----------------------------
# FIND ATM OPTION
# -----------------------------
def find_option(signal):

    try:
        nifty_price = kite.ltp("NSE:NIFTY 50")["NSE:NIFTY 50"]["last_price"]

        strike = round(nifty_price / 50) * 50

        instruments = kite.instruments("NFO")

        for inst in instruments:

            sym = inst["tradingsymbol"]

            if "NIFTY" not in sym:
                continue

            if str(strike) not in sym:
                continue

            if signal == "CALL" and not sym.endswith("CE"):
                continue

            if signal == "PUT" and not sym.endswith("PE"):
                continue

            try:
                price = get_ltp(f"NFO:{sym}")
            except:
                continue

            if price is None:
                continue

            if config.MIN_PREMIUM <= price <= config.MAX_PREMIUM:
                print(f"Selected: {sym} @ {price}")
                return sym, price

        return None, None

    except Exception as e:
        print("Option error:", e)
        return None, None


# -----------------------------
# PLACE ORDER
# -----------------------------
def place_order(symbol, qty):

    try:
        order = kite.place_order(
            variety=kite.VARIETY_REGULAR,
            exchange="NFO",
            tradingsymbol=symbol,
            transaction_type="BUY",
            quantity=qty,
            order_type="MARKET",
            product="MIS"
        )
        return order

    except Exception as e:
        print("Order error:", e)
        send_message(f"❌ Order error: {e}")
        return None


# -----------------------------
# TRADE MANAGEMENT
# -----------------------------
def manage_trade(symbol, entry, qty):

    global daily_loss, trade_active

    sl = entry * (1 - config.STOP_LOSS)
    target = entry * (1 + config.TARGET)
    trail_sl = sl

    send_message(f"""
📊 TRADE STARTED
{symbol}
Entry: {entry}
SL: {round(sl,2)}
Target: {round(target,2)}
Qty: {qty}
""")

    while True:
        try:
            ltp = get_ltp(f"NFO:{symbol}")
            pnl = (ltp - entry) * qty

            print(f"{symbol} | LTP: {ltp} | PnL: {pnl}")

            if ltp >= target:
                send_message(f"🎯 TARGET HIT\n{symbol}\nPnL: ₹{round(pnl,2)}")
                break

            if ltp <= trail_sl:
                send_message(f"🛑 SL HIT\n{symbol}\nPnL: ₹{round(pnl,2)}")
                daily_loss += abs(pnl)
                break

            if ltp > entry * 1.2:
                trail_sl = max(trail_sl, ltp - 10)

            time.sleep(5)

        except Exception as e:
            print("Trade error:", e)
            break

    trade_active = False


# -----------------------------
# DAILY REPORT
# -----------------------------
def daily_report():
    send_message(f"""
📊 DAILY REPORT
Trades: {trades_today}
Loss: ₹{daily_loss}
""")


# -----------------------------
# MAIN LOOP (AUTO MODE)
# -----------------------------
def run_bot():

    global trades_today, daily_loss, trade_active

    send_message("🚀 BOT SERVICE STARTED (AUTO MODE)")

    while True:

        now = datetime.datetime.now(IST)

        start = now.replace(hour=9, minute=0, second=0, microsecond=0)
        end = now.replace(hour=15, minute=30, second=0, microsecond=0)

        # BEFORE MARKET
        if now < start:
            print("⏳ Waiting for market open...")
            time.sleep(300)
            continue

        # AFTER MARKET
        if now > end:
            print("🌙 Market closed. Resetting...")

            trades_today = 0
            daily_loss = 0
            trade_active = False

            time.sleep(600)
            continue

        # MARKET OPEN MESSAGE
        if now.hour == 9 and now.minute < 5:
            send_message("🟢 Market Open - Bot Active")

        # RISK CONTROL
        if daily_loss >= MAX_DAILY_LOSS:
            send_message("🛑 DAILY LOSS LIMIT HIT")
            time.sleep(600)
            continue

        if trades_today >= MAX_TRADES:
            send_message("📉 MAX TRADES DONE")
            time.sleep(600)
            continue

        if trade_active:
            time.sleep(60)
            continue

        signal_data = get_signal()

        signal = signal_data.get("signal", "HOLD")
        quality = signal_data.get("quality", "B")
        confidence = signal_data.get("confidence", 0)

        print(f"Signal: {signal} | Quality: {quality} | Conf: {confidence}")

        if signal == "HOLD" or quality not in ["A", "A+"]:
            time.sleep(300)
            continue

        symbol, price = find_option(signal)

        if not symbol:
            send_message("❌ No valid option found")
            time.sleep(300)
            continue

        qty = calculate_qty(price)

        send_message(f"""
🎯 {quality} TRADE
Symbol: {symbol}
Price: {price}
Qty: {qty}
Confidence: {confidence}
""")

        order = place_order(symbol, qty)

        if order:
            trade_active = True
            trades_today += 1
            manage_trade(symbol, price, qty)

        time.sleep(300)


# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    run_bot()