import requests
import time
import datetime
from kiteconnect import KiteConnect
import config
from telegram_bot import send_message

kite = KiteConnect(api_key=config.API_KEY)
kite.set_access_token(config.ACCESS_TOKEN)


# -----------------------------
# CONFIG
# -----------------------------
CAPITAL = 100000
RISK_PER_TRADE = 0.02   # 2%
MAX_DAILY_LOSS = 3000
MAX_TRADES = 5

trades_today = 0
daily_loss = 0
trade_active = False


# -----------------------------
# GET SIGNAL
# -----------------------------
def get_signal():
    try:
        res = requests.get("http://127.0.0.1:5001/signal")
        return res.json()
    except:
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
    return max(qty, 50)  # minimum lot


# -----------------------------
# FIND OPTION
# -----------------------------
def find_option(signal):
    instruments = kite.instruments("NFO")

    for inst in instruments:
        sym = inst["tradingsymbol"]

        if "NIFTY" in sym:

            if signal == "CALL" and sym.endswith("CE"):
                price = get_ltp(f"NFO:{sym}")
                if 50 <= price <= 120:
                    return sym, price

            elif signal == "PUT" and sym.endswith("PE"):
                price = get_ltp(f"NFO:{sym}")
                if 50 <= price <= 120:
                    return sym, price

    return None, None


# -----------------------------
# PLACE ORDER
# -----------------------------
def place_order(symbol, qty):
    return kite.place_order(
        variety=kite.VARIETY_REGULAR,
        exchange="NFO",
        tradingsymbol=symbol,
        transaction_type="BUY",
        quantity=qty,
        order_type="MARKET",
        product="MIS"
    )


# -----------------------------
# MANAGE TRADE
# -----------------------------
def manage_trade(symbol, entry, qty):

    global daily_loss, trade_active

    sl = entry * 0.7
    target = entry * 1.5
    trail = sl

    send_message(f"""
📊 TRADE START
{symbol}
Entry: {entry}
SL: {round(sl,2)}
Target: {round(target,2)}
Qty: {qty}
""")

    while True:

        ltp = get_ltp(f"NFO:{symbol}")
        pnl = (ltp - entry) * qty

        print(f"LTP: {ltp} | PnL: {pnl}")

        # TARGET
        if ltp >= target:
            send_message(f"🎯 TARGET HIT: ₹{round(pnl,2)}")
            break

        # STOP LOSS
        if ltp <= trail:
            send_message(f"🛑 SL HIT: ₹{round(pnl,2)}")
            daily_loss += abs(pnl)
            break

        # TRAILING
        if ltp > entry * 1.2:
            trail = max(trail, ltp - 10)

        time.sleep(5)

    trade_active = False


# -----------------------------
# DAILY REPORT
# -----------------------------
def daily_report():
    send_message(f"""
📊 DAY SUMMARY
Trades: {trades_today}
Loss: ₹{daily_loss}
""")


# -----------------------------
# MAIN LOOP
# -----------------------------
def run_bot():

    global trades_today, daily_loss, trade_active

    send_message("🚀 BOT STARTED")

    while True:

        now = datetime.datetime.now()

        # MARKET TIME
        if now.hour < 9 or now.hour > 15:
            time.sleep(300)
            continue

        # STOP IF LOSS LIMIT HIT
        if daily_loss >= MAX_DAILY_LOSS:
            send_message("🛑 DAILY LOSS LIMIT HIT")
            break

        # MAX TRADES
        if trades_today >= MAX_TRADES:
            send_message("📉 MAX TRADES DONE")
            break

        # SKIP IF TRADE RUNNING
        if trade_active:
            time.sleep(60)
            continue

        signal_data = get_signal()

        signal = signal_data.get("signal", "HOLD")
        quality = signal_data.get("quality", "B")

        print("Signal:", signal, "| Quality:", quality)

        # ONLY TAKE GOOD TRADES
        if signal == "HOLD" or quality not in ["A", "A+"]:
            time.sleep(300)
            continue

        symbol, price = find_option(signal)

        if not symbol:
            send_message("❌ No option found")
            time.sleep(300)
            continue

        qty = calculate_qty(price)

        send_message(f"🎯 {quality} TRADE\n{symbol} @ {price}")

        place_order(symbol, qty)

        trade_active = True
        trades_today += 1

        manage_trade(symbol, price, qty)

    daily_report()


# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    run_bot()