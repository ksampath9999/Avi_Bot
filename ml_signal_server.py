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
# MODEL CONFIG
# -----------------------------
MODEL_PATH = "ml_model.pkl"
MODEL_URL = "https://drive.google.com/uc?export=download&id=1VHtGkihPhZys4cWtTHzHPdPwVhK_2LXc""   # <-- paste your Google Drive direct link


# -----------------------------
# DOWNLOAD MODEL
# -----------------------------
def download_model():
    if not os.path.exists(MODEL_PATH):
        print("⬇️ Downloading model...")

        try:
            response = requests.get(MODEL_URL, stream=True)

            # 🚨 VALIDATION
            content_type = response.headers.get("Content-Type", "")

            if "text/html" in content_type:
                raise Exception("❌ Got HTML instead of model file")

            with open(MODEL_PATH, "wb") as f:
                for chunk in response.iter_content(1024):
                    if chunk:
                        f.write(chunk)

            print("✅ Model downloaded")

        except Exception as e:
            print("❌ Download failed:", e)


# -----------------------------
# LOAD MODEL
# -----------------------------
model = None
MODEL_LOADED = False

try:
    if os.path.exists(MODEL_PATH):
        model = joblib.load(MODEL_PATH)
        MODEL_LOADED = True
        print("✅ Model loaded successfully")
    else:
        print("⚠️ Model file not found — ML disabled")

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
        data = kite.historical_data(
            config.NIFTY_TOKEN,
            now - datetime.timedelta(days=2),
            now,
            "5minute"
        )

        df = pd.DataFrame(data)

        print("Data length:", len(df))

        # -----------------------------
        # FALLBACK IF VERY LOW DATA
        # -----------------------------
        if len(df) < 10:
            print("⚠️ Low data → fallback")

            last = df.iloc[-1]

            if last["close"] > last["open"]:
                return jsonify({"signal": "CALL", "reason": "Fallback low data"})
            else:
                return jsonify({"signal": "PUT", "reason": "Fallback low data"})

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
                return jsonify({"signal": "CALL", "reason": "Fallback dropna"})
            else:
                return jsonify({"signal": "PUT", "reason": "Fallback dropna"})

        last = df.iloc[-1]
        prev = df.iloc[-2]

        # -----------------------------
        # ML PREDICTION
        # -----------------------------
        if MODEL_LOADED and model is not None:

            try:
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

            except Exception as e:
                print("ML prediction error:", e)

        # -----------------------------
        # FINAL FALLBACK (BREAKOUT)
        # -----------------------------
        print("⚠️ Using breakout fallback")

        if last["close"] > prev["high"]:
            return jsonify({"signal": "CALL", "reason": "Breakout fallback"})

        elif last["close"] < prev["low"]:
            return jsonify({"signal": "PUT", "reason": "Breakout fallback"})

        else:
            # LAST SAFETY (NEVER STAY HOLD TOO LONG)
            if last["close"] > last["open"]:
                return jsonify({"signal": "CALL", "reason": "Candle fallback"})
            else:
                return jsonify({"signal": "PUT", "reason": "Candle fallback"})

    except Exception as e:
        print("❌ ERROR:", e)

        return jsonify({
            "signal": "HOLD",
            "reason": str(e)
        })


# -----------------------------
# RUN SERVER (RENDER READY)
# -----------------------------
if __name__ == "__main__":
    port = 10000
    app.run(host="0.0.0.0", port=port)