from flask import Flask, jsonify
import pandas as pd
from datetime import datetime, timedelta
from kiteconnect import KiteConnect
import joblib
import config
import os

app = Flask(__name__)

# -----------------------------
# ZERODHA CONNECTION
# -----------------------------
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
    print("⚠️ Model not found (fallback mode)")


# -----------------------------
# FETCH DATA
# -----------------------------
def get_data(interval="5minute"):

    try:
        now = datetime.now()
        start = now - timedelta(days=5)

        data = kite.historical_data(
            instrument_token=256265,
            from_date=start,
            to_date=now,
            interval=interval
        )

        df = pd.DataFrame(data)

        if df.empty:
            return None

        return df

    except Exception as e:
        print("Data error:", e)
        return None


# -----------------------------
# ADD INDICATORS
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
    recent = df.tail(20)
    return (recent["high"].max() - recent["low"].min()) < 30


# -----------------------------
# VOLUME SPIKE
# -----------------------------
def volume_spike(df):
    avg_vol = df["volume"].rolling(20).mean().iloc[-1]
    return df.iloc[-1]["volume"] > 1.5 * avg_vol


# -----------------------------
# BREAKOUT CHECK
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
# MAIN SIGNAL FUNCTION
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
    # SIDEWAYS FILTER
    # -----------------------------
    if is_sideways(df5):
        return {"signal": "HOLD", "reason": "Sideways"}

    # -----------------------------
    # TREND
    # -----------------------------
    trend = get_trend(df15)

    if trend == "SIDE":
        return {"signal": "HOLD", "reason": "Weak trend"}

    # -----------------------------
    # VOLUME
    # -----------------------------
    if not volume_spike(df5):
        return {"signal": "HOLD", "reason": "Low volume"}

    # -----------------------------
    # BREAKOUT
    # -----------------------------
    breakout_up, breakout_down = breakout(df5)

    if not breakout_up and not breakout_down:
        return {"signal": "HOLD", "reason": "No breakout"}

    last = df5.iloc[-1]

    # -----------------------------
    # ML PREDICTION
    # -----------------------------
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

    # -----------------------------
    # CONFIDENCE FILTER
    # -----------------------------
    if confidence < 0.65:
        return {"signal": "HOLD", "reason": "Low confidence"}

    # -----------------------------
    # QUALITY SCORE
    # -----------------------------
    score = 0

    if trend != "SIDE": score += 1
    if breakout_up or breakout_down: score += 1
    if volume_spike(df5): score += 1
    if confidence > 0.7: score += 1

    quality = "A+" if score >= 4 else "A" if score >= 3 else "B"

    # -----------------------------
    # FINAL SIGNAL
    # -----------------------------
    if breakout_up and trend == "UP" and pred == 1:
        return {
            "signal": "CALL",
            "confidence": round(confidence, 2),
            "quality": quality
        }

    elif breakout_down and trend == "DOWN" and pred == 0:
        return {
            "signal": "PUT",
            "confidence": round(confidence, 2),
            "quality": quality
        }

    else:
        return {"signal": "HOLD", "reason": "Mismatch"}


# -----------------------------
# API
# -----------------------------
@app.route("/")
def home():
    return "ML Server Running"


@app.route("/signal")
def signal():
    return jsonify(generate_signal())


# -----------------------------
# RUN (RENDER FIX)
# -----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    print(f"🚀 Server running on port {port}")
    app.run(host="0.0.0.0", port=port)