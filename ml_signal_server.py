from flask import Flask, jsonify
import pandas as pd
from datetime import datetime, timedelta
from kiteconnect import KiteConnect
import joblib
import config
import os

app = Flask(__name__)

kite = KiteConnect(api_key=config.API_KEY)
kite.set_access_token(config.ACCESS_TOKEN)

# -----------------------------
# LOAD MODEL
# -----------------------------
try:
    model = joblib.load("model.pkl")
except:
    model = None

# -----------------------------
# FETCH DATA
# -----------------------------
def get_data(interval):
    try:
        now = datetime.now()
        start = now - timedelta(days=10)  # more history

        data = kite.historical_data(
            256265,
            start,
            now,
            interval
        )

        df = pd.DataFrame(data)
        return df if not df.empty else None

    except:
        return None

# -----------------------------
# INDICATORS
# -----------------------------
def add_indicators(df):
    df["ema20"] = df["close"].ewm(span=20).mean()
    df["ema50"] = df["close"].ewm(span=50).mean()
    df["returns"] = df["close"].pct_change()
    df["std"] = df["close"].rolling(5).std()

    df["cum_vol"] = df["volume"].cumsum()
    df["cum_vol_price"] = (df["close"] * df["volume"]).cumsum()
    df["vwap"] = df["cum_vol_price"] / df["cum_vol"]

    return df

# -----------------------------
# SIDEWAYS
# -----------------------------
def is_sideways(df):
    r = df.tail(20)
    return (r["high"].max() - r["low"].min()) < 20

# -----------------------------
# VOLUME (RELAXED)
# -----------------------------
def volume_spike(df):
    avg = df["volume"].rolling(20).mean().iloc[-1]
    return df.iloc[-1]["volume"] > 1.2 * avg

# -----------------------------
# BREAKOUT (RELAXED)
# -----------------------------
def breakout(df):
    high = df["high"].rolling(10).max().iloc[-2]
    low = df["low"].rolling(10).min().iloc[-2]

    last = df.iloc[-1]["close"]

    return last > high, last < low

# -----------------------------
# TREND
# -----------------------------
def get_trend(df):
    last = df.iloc[-1]

    if last["ema20"] > last["ema50"]:
        return "UP"
    elif last["ema20"] < last["ema50"]:
        return "DOWN"
    return "SIDE"

# -----------------------------
# SIGNAL
# -----------------------------
def generate_signal():

    df5 = get_data("5minute")
    df15 = get_data("15minute")

    if df5 is None or df15 is None:
        return {"signal": "HOLD", "reason": "No data"}

    df5 = add_indicators(df5).dropna()
    df15 = add_indicators(df15).dropna()

    if len(df5) < 30 or len(df15) < 30:
        return {"signal": "HOLD", "reason": "Insufficient data"}

    if is_sideways(df5):
        return {"signal": "HOLD", "reason": "Sideways"}

    trend = get_trend(df15)

    if trend == "SIDE":
        return {"signal": "HOLD", "reason": "Weak trend"}

    if not volume_spike(df5):
        return {"signal": "HOLD", "reason": "Low volume"}

    breakout_up, breakout_down = breakout(df5)

    last = df5.iloc[-1]

    # ML
    if model:
        X = [[
            last["returns"],
            last["std"],
            last["close"] - last["vwap"]
        ]]
        pred = model.predict(X)[0]
        prob = model.predict_proba(X)[0]
        confidence = max(prob)
    else:
        pred = 1 if last["close"] > last["vwap"] else 0
        confidence = 0.6

    if confidence < 0.55:
        return {"signal": "HOLD", "reason": "Low confidence"}

    # scoring
    score = 0
    if breakout_up or breakout_down: score += 1
    if volume_spike(df5): score += 1
    if confidence > 0.65: score += 1

    quality = "A+" if score >= 3 else "A" if score >= 2 else "B"

    if breakout_up and trend == "UP" and pred == 1:
        return {"signal": "CALL", "confidence": round(confidence,2), "quality": quality}

    if breakout_down and trend == "DOWN" and pred == 0:
        return {"signal": "PUT", "confidence": round(confidence,2), "quality": quality}

    return {"signal": "HOLD", "reason": "Mismatch"}

# -----------------------------
# ROUTES
# -----------------------------
@app.route("/")
def home():
    return "ML Server Running"

@app.route("/signal")
def signal():
    return jsonify(generate_signal())

# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)