from flask import Flask, jsonify
import pandas as pd
import datetime
import os
import requests
import joblib
from kiteconnect import KiteConnect
import config

app = Flask(__name__)

# -----------------------------
# INIT KITE
# -----------------------------
kite = KiteConnect(api_key=config.API_KEY)
kite.set_access_token(config.ACCESS_TOKEN)

# -----------------------------
# GOOGLE DRIVE MODEL DOWNLOAD
# -----------------------------
MODEL_PATH = "ml_model.pkl"
MODEL_URL = "https://drive.google.com/uc?export=download&id=1VHtGkihPhZys4cWtTHzHPdPwVhK_2LXc"


def download_model():
    if not os.path.exists(MODEL_PATH):
        print("⬇️ Downloading model...")

        try:
            response = requests.get(MODEL_URL)

            with open(MODEL_PATH, "wb") as f:
                f.write(response.content)

            print("✅ Model downloaded")

        except Exception as e:
            print("❌ Download failed:", e)


# -----------------------------
# LOAD MODEL
# -----------------------------
MODEL_LOADED = False

try:
    download_model()
    model = joblib.load(MODEL_PATH)
    MODEL_LOADED = True
    print("✅ Model loaded successfully")

except Exception as e:
    print("❌ Model load failed:", e)


# -----------------------------
# SIGNAL API
# -----------------------------
@app.route("/signal")
def get_signal():

    try:
        now = datetime.datetime.now()

        # Fetch recent data (robust)
        data = kite.historical_data(
            config.NIFTY_TOKEN,
            now - datetime.timedelta(hours=2),
            now,
            "5minute"
        )

        df = pd.DataFrame(data)

        print("Data length:", len(df))

        if len(df) < 20:
            return jsonify({
                "signal": "HOLD",
                "reason": "Insufficient data"
            })

        # -----------------------------
        # SIMPLE FEATURES (ROBUST)
        # -----------------------------
        df["returns"] = df["close"].pct_change()
        df["ema"] = df["close"].ewm(span=10).mean()

        df = df.dropna()

        if len(df) < 10:
            return jsonify({
                "signal": "HOLD",
                "reason": "Feature drop empty"
            })

        last = df.iloc[-1]

        # -----------------------------
        # ML PREDICTION
        # -----------------------------
        if MODEL_LOADED:

            features = [[
                last["close"],
                last["ema"],
                last["returns"]
            ]]

            prediction = model.predict(features)[0]

            signal = "CALL" if prediction == 1 else "PUT"

            return jsonify({
                "signal": signal,
                "reason": "ML prediction"
            })

        # -----------------------------
        # FALLBACK (IMPORTANT)
        # -----------------------------
        else:

            print("⚠️ Using fallback (no model)")

            prev = df.iloc[-2]

            if last["close"] > prev["high"]:
                return jsonify({
                    "signal": "CALL",
                    "reason": "Fallback breakout"
                })

            elif last["close"] < prev["low"]:
                return jsonify({
                    "signal": "PUT",
                    "reason": "Fallback breakout"
                })

            else:
                return jsonify({
                    "signal": "HOLD",
                    "reason": "No clear move"
                })

    except Exception as e:
        print("❌ ERROR:", e)

        return jsonify({
            "signal": "HOLD",
            "reason": str(e)
        })


# -----------------------------
# RUN SERVER
# -----------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port)