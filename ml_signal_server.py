from flask import Flask, jsonify

# ✅ ADD THIS BLOCK HERE
import logging
log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

import pandas as pd
import datetime
import os
import requests
import joblib
from kiteconnect import KiteConnect
import config
import time
import gdown


last_fetch_time = 0
cached_df = None

app = Flask(__name__)

# -----------------------------
# INIT KITE
# -----------------------------
kite = KiteConnect(api_key=config.API_KEY)
kite.set_access_token(config.ACCESS_TOKEN)

# -----------------------------
# MODEL CONFIG
# -----------------------------
#MODEL_PATH = "ml_model.pkl"
#MODEL_URL = "https://drive.google.com/uc?export=download&id=1VHtGkihPhZys4cWtTHzHPdPwVhK_2LXc"   # <-- paste your Google Drive direct link

MODEL_PATH = "ml_model.pkl"
MODEL_FILE_ID = "1VHtGkihPhZys4cWtTHzHPdPwVhK_2LXc"

    
def get_data():
    global last_fetch_time, cached_df

    if cached_df is not None and time.time() - last_fetch_time < 10:
        return cached_df

    now = datetime.datetime.now()

    data = kite.historical_data(
        config.NIFTY_TOKEN,
        now - datetime.timedelta(days=2),
        now,
        "5minute"
    )

    cached_df = pd.DataFrame(data)
    last_fetch_time = time.time()

    return cached_df

# -----------------------------
# DOWNLOAD MODEL
# -----------------------------
def download_model():
    if os.path.exists(MODEL_PATH):
        print("✅ Model already exists")
        return True

    try:
        url = f"https://drive.google.com/uc?id={MODEL_FILE_ID}"
        print("⬇️ Downloading model via gdown...")
        gdown.download(url, MODEL_PATH, quiet=False)

        if not os.path.exists(MODEL_PATH):
            print("❌ Download failed — file missing")
            return False

        print("✅ Model downloaded successfully")
        return True

    except Exception as e:
        print("❌ gdown error:", e)
        return False


# -----------------------------
# LOAD MODEL
# -----------------------------
model = None
MODEL_LOADED = False

try:
    # 🔥 RETRY DOWNLOAD (PRO TIP ADDED HERE)
    success = False

    model = None
    MODEL_LOADED = False

    try:
        if download_model():
            model = joblib.load(MODEL_PATH)
            MODEL_LOADED = True
            print("✅ Model loaded successfully")

            # 🔍 DEBUG CHECK (ADD HERE)
            print("🔍 Model type:", type(model))

        else:
            print("⚠️ Model download failed")

    except Exception as e:
        print("❌ Model load failed:", e)

    if not success:
        print("⚠️ Model download failed after retries")

    if os.path.exists(MODEL_PATH):
        model = joblib.load(MODEL_PATH)
        MODEL_LOADED = True
        print("✅ Model loaded successfully")
    else:
        print("⚠️ Model file missing")

except Exception as e:
    print("❌ Model load failed:", e)


# -----------------------------
# SIGNAL API
# -----------------------------
@app.route("/signal")
def get_signal():

    try:
        now = datetime.datetime.now()

        # ✅ FIXED: 2 DAYS DATA (VERY IMPORTANT)

        df = get_data()

        if df is None or df.empty:
            print("❌ No data available")
            return jsonify({
                "signal": "HOLD",
                "confidence": 50,
                "reason": "No data"
            })

        print("Data length:", len(df))

        # -----------------------------
        # FALLBACK IF VERY LOW DATA
        # -----------------------------
        if len(df) < 10:
            print("⚠️ Low data → fallback")

            last = df.iloc[-1]

            if last["close"] > last["open"]:
                return jsonify({
                    "signal": "CALL",
                    "confidence": 60,
                    "reason": "Fallback low data"
                })
            else:
                return jsonify({
                    "signal": "PUT",
                    "confidence": 60,
                    "reason": "Fallback low data"
                })


        df = df.copy()
        # -----------------------------
        # FEATURES (SIMPLE & ROBUST)
        # -----------------------------
        df["returns"] = df["close"].pct_change()
        df["ema"] = df["close"].ewm(span=10).mean()

        df = df.dropna()

        if len(df) < 10:
            print("⚠️ After dropna → fallback")

            last = df.iloc[-1]

            if last["close"] > last["open"]:
                return jsonify({
                    "signal": "CALL",
                    "confidence": 60,
                    "reason": "Fallback low data"
                })
            else:
                return jsonify({
                    "signal": "PUT",
                    "confidence": 60,
                    "reason": "Fallback low data"
                })

        last = df.iloc[-1]
        prev = df.iloc[-2]

        # 🚫 LOW VOLATILITY FILTER (ADD HERE)
        rng = last["high"] - last["low"]

        if rng < last["close"] * 0.0005:
            print("⚠️ Low volatility market")

            return jsonify({
                "signal": "HOLD",
                "confidence": 50,
                "reason": "Low volatility"
            })

        # -----------------------------
        # ML PREDICTION
        # -----------------------------
        if MODEL_LOADED and model is not None:

            try:
                features = [[
                    last["close"],
                    last["ema"],
                    last["returns"],
                    last["high"] - last["low"],      # volatility
                    last["close"] - prev["close"]    # momentum
                ]]

                proba = model.predict_proba(features)[0]

                call_prob = proba[1]
                put_prob = proba[0]

                if call_prob > put_prob:
                    signal = "CALL"
                    confidence = call_prob * 100
                else:
                    signal = "PUT"
                    confidence = put_prob * 100

                # 🚫 LOW CONFIDENCE FILTER (ADD HERE)
                # 🚀 COMBINED SMART FILTER
                if confidence < 52:
                    print(f"⚠️ Weak ML: {confidence}")
                    return jsonify({
                        "signal": signal,   # ✅ DO NOT FORCE HOLD
                        "confidence": round(confidence, 2),
                        "reason": "Weak ML but allowed"
                    })

                    print(f"⚠️ Low confidence: {confidence}")
                    return jsonify({
                        "signal": "HOLD",
                        "confidence": round(confidence, 2),
                        "reason": "Low ML confidence"
                    })
                    
                print(f"🤖 ML Output → {signal} | Confidence: {confidence}")    

                return jsonify({
                    "signal": signal,
                    "confidence": round(confidence, 2),
                    "reason": "ML prediction"
                })

            except Exception as e:
                print("ML prediction error:", e)

        # -----------------------------
        # FINAL FALLBACK (BREAKOUT)
        # -----------------------------
        print("⚠️ Using breakout fallback")

        if last["close"] > prev["high"]:
            return jsonify({
                    "signal": "CALL",
                    "confidence": 60,
                    "reason": "Fallback low data"
                })

        elif last["close"] < prev["low"]:
            return jsonify({
                    "signal": "PUT",
                    "confidence": 60,
                    "reason": "Fallback low data"
                })

        else:
            # LAST SAFETY (NEVER STAY HOLD TOO LONG)
            if last["close"] > last["open"]:
                return jsonify({
                    "signal": "CALL",
                    "confidence": 60,
                    "reason": "Fallback low data"
                })
            else:
                return jsonify({
                    "signal": "PUT",
                    "confidence": 60,
                    "reason": "Fallback low data"
                })

    except Exception as e:
        print("❌ ERROR:", e)

        return jsonify({
            "signal": "HOLD",
            "reason": str(e)
        })
        
        
@app.route("/health")
def health():
    return {
        "status": "ok",
        "model_loaded": MODEL_LOADED
    }


# -----------------------------
# RUN SERVER (RENDER READY)
# -----------------------------
if __name__ == "__main__":
    port = 10000
    app.run(host="0.0.0.0", port=port)