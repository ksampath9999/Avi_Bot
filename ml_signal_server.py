from flask import Flask, jsonify
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from kiteconnect import KiteConnect
import joblib
import config

app = Flask(__name__)

kite = KiteConnect(api_key=config.API_KEY)
kite.set_access_token(config.ACCESS_TOKEN)

# -----------------------------
# LOAD MODEL
# -----------------------------
try:
    model = joblib.load("model.pkl")
    print("✅ Model loaded")
except:
    model = None
    print("⚠️ Model not found")


# -----------------------------
# FETCH DATA (MULTI TF)
# -----------------------------
def get_data(interval):

    now = datetime.now()
    start = now - timedelta(days=5)

    data = kite.historical_data(
        256265,
        start,
        now,
        interval
    )

    df = pd.DataFrame(data)

    return df if not df.empty else None


# -----------------------------
# INDICATORS
# -----------------------------
def add_indicators(df):

    df["ema20"] = df["close"].ewm(span=20).mean()
    df["ema50"] = df["close"].ewm(span=50).mean()

    df["returns"] = df["close"].pct_change()
    df["std"] = df["close"].rolling(5).std()

    # VWAP
    df["cum_vol"] = df["volume"].cumsum()
    df["cum_vol_price"] = (df["close"] * df["volume"]).cumsum()
    df["vwap"] = df["cum_vol_price"] / df["cum_vol"]

    return df


# -----------------------------
# SIDEWAYS FILTER
# -----------------------------
def is_sideways(df):
    r = df.tail(20)
    return (r["high"].max() - r["low"].min()) < 30


# -----------------------------
# VOLUME SPIKE
# -----------------------------
def volume_spike(df):
    avg = df["volume"].rolling(20).mean().iloc[-1]
    return df.iloc[-1]["volume"] > 1.5 * avg


# -----------------------------
# BREAKOUT
# -----------------------------
def breakout(df):
    high = df["high"].rolling(20).max().iloc[-2]
    low = df["low"].rolling(20).min().iloc[-2]

    last = df.iloc[-1]["close"]

    return last > high, last < low


# -----------------------------
# TREND (15 MIN)
# -----------------------------
def get_trend(df):
    last = df.iloc[-1]
    if last["ema20"] > last["ema50"] and last["close"] > last["vwap"]:
        return "UP"
    elif last["ema20"] < last["ema50"] and last["close"] < last["vwap"]:
        return "DOWN"
    return "SIDE"


# -----------------------------
# SIGNAL ENGINE
# -----------------------------
def generate_signal():

    df5 = get_data("5minute")
    df15 = get_data("15minute")

    if df5 is None or df15 is None:
        return {"signal": "HOLD", "reason": "No data"}

    df5 = add_indicators(df5).dropna()
    df15 = add_indicators(df15).dropna()

    if len(df5) < 50 or len(df15) < 50:
        return {"signal": "HOLD", "reason": "Insufficient data"}

    # -----------------------------
    # FILTERS
    # -----------------------------
    if is_sideways(df5):
        return {"signal": "HOLD", "reason": "Sideways"}

    trend = get_trend(df15)

    if trend == "SIDE":
        return {"signal": "HOLD", "reason": "Weak trend"}

    if not volume_spike(df5):
        return {"signal": "HOLD", "reason": "No volume"}

    breakout_up, breakout_down = breakout(df5)

    # -----------------------------
    # ML
    # -----------------------------
    last = df5.iloc[-1]

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
        confidence = 0.5

    if confidence < 0.65:
        return {"signal": "HOLD", "reason": "Low confidence"}

    # -----------------------------
    # FINAL DECISION
    # -----------------------------
    score = 0

    if trend == "UP": score += 1
    if trend == "DOWN": score += 1
    if breakout_up or breakout_down: score += 1
    if volume_spike(df5): score += 1
    if confidence > 0.7: score += 1

    trade_quality = "A+" if score >= 4 else "A" if score >= 3 else "B"

    if breakout_up and trend == "UP" and pred == 1:
        return {
            "signal": "CALL",
            "confidence": round(confidence, 2),
            "quality": trade_quality
        }

    if breakout_down and trend == "DOWN" and pred == 0:
        return {
            "signal": "PUT",
            "confidence": round(confidence, 2),
            "quality": trade_quality
        }

    return {"signal": "HOLD", "reason": "Mismatch"}
    

# -----------------------------
# API
# -----------------------------
@app.route("/signal", methods=["GET"])
def signal():
    return jsonify(generate_signal())


# -----------------------------
# RUN
# -----------------------------
if __name__ == "__main__":
    print("🚀 PRO ML SERVER RUNNING")
    app.run(port=5001)