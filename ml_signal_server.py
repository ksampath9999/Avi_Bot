"""
═══════════════════════════════════════════════════════════════════════════════
  ML Signal Server  —  Self-Training NIFTY Direction Model
  No pre-trained model file needed. Trains itself at startup using
  Kite historical data and retrains daily at 9:00 AM IST.

  Deploy as a second Railway service alongside main.py.
  main.py calls GET /signal before each NIFTY entry.

  Endpoints:
    GET /signal  → {"signal":"CALL|PUT|HOLD","confidence":xx,"reason":"..."}
    GET /health  → {"status":"ok","model_loaded":true,"accuracy":xx}
    GET /retrain → manually trigger a retrain

  Requirements:
    pip install flask kiteconnect pandas numpy scikit-learn pytz
═══════════════════════════════════════════════════════════════════════════════
"""

import logging
logging.getLogger("werkzeug").setLevel(logging.ERROR)

import os
import time
import datetime
import threading
import pytz

import numpy as np
import pandas as pd
from flask import Flask, jsonify

from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import accuracy_score
from sklearn.preprocessing import StandardScaler

from kiteconnect import KiteConnect
import config

# ─────────────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────────────
TRAIN_DAYS       = 90       # days of 15-min history to train on
RETRAIN_HOUR_IST = 9        # retrain every day at 9:00 AM IST
MIN_CONFIDENCE   = 60       # below this % → return HOLD
N_ESTIMATORS     = 200      # Random Forest trees
MAX_DEPTH        = 6        # max tree depth (prevents overfitting)
IST              = pytz.timezone("Asia/Kolkata")

# ─────────────────────────────────────────────────────────────────────────────
# KITE  — token is pushed by main.py via POST /set_token after daily login.
#          No ACCESS_TOKEN needed here at startup.
# ─────────────────────────────────────────────────────────────────────────────
kite          = KiteConnect(api_key=config.API_KEY)
TOKEN_READY   = False   # becomes True once main.py sends the token

app = Flask(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# GLOBAL MODEL STATE
# ─────────────────────────────────────────────────────────────────────────────
model           = None
scaler          = None
MODEL_LOADED    = False
MODEL_ACCURACY  = 0.0
LAST_TRAIN_TIME = None
_train_lock     = threading.Lock()

# Live data cache (refreshed every 10s)
_live_cache     = {"df": None, "ts": 0}


# ─────────────────────────────────────────────────────────────────────────────
# FEATURE ENGINEERING
# ─────────────────────────────────────────────────────────────────────────────
def compute_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute technical features from OHLCV data.
    All features look backward only — no lookahead bias.
    """
    d = df.copy()

    # ── Returns ──────────────────────────────────────────────────────────────
    d["ret_1"]  = d["close"].pct_change(1)
    d["ret_3"]  = d["close"].pct_change(3)
    d["ret_5"]  = d["close"].pct_change(5)
    d["ret_10"] = d["close"].pct_change(10)

    # ── EMA crossover ────────────────────────────────────────────────────────
    ema9  = d["close"].ewm(span=9,  adjust=False).mean()
    ema21 = d["close"].ewm(span=21, adjust=False).mean()
    d["ema_ratio"] = (ema9 / ema21) - 1          # positive = fast above slow

    # ── RSI (14) ─────────────────────────────────────────────────────────────
    delta = d["close"].diff()
    gain  = delta.clip(lower=0).ewm(alpha=1/14, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1/14, adjust=False).mean()
    d["rsi"] = 100 - (100 / (1 + gain / loss.replace(0, np.nan)))
    d["rsi_norm"] = (d["rsi"] - 50) / 50          # centre around 0

    # ── Volatility / ATR ─────────────────────────────────────────────────────
    prev_close     = d["close"].shift(1)
    tr             = pd.concat([
        d["high"] - d["low"],
        (d["high"] - prev_close).abs(),
        (d["low"]  - prev_close).abs()
    ], axis=1).max(axis=1)
    d["atr_ratio"] = tr.ewm(alpha=1/14, adjust=False).mean() / d["close"]

    # ── Candle anatomy ────────────────────────────────────────────────────────
    candle_range      = (d["high"] - d["low"]).replace(0, np.nan)
    d["hl_ratio"]     = candle_range / d["close"]
    d["body_ratio"]   = (d["close"] - d["open"]).abs() / candle_range
    d["upper_shadow"] = (d["high"] - d[["open","close"]].max(axis=1)) / candle_range
    d["lower_shadow"] = (d[["open","close"]].min(axis=1) - d["low"])  / candle_range

    # ── Volume ratio (volume vs 20-bar avg) ──────────────────────────────────
    if "volume" in d.columns and d["volume"].sum() > 0:
        d["vol_ratio"] = d["volume"] / d["volume"].rolling(20).mean().replace(0, np.nan)
    else:
        d["vol_ratio"] = 1.0

    # ── Rolling std (short-term volatility regime) ────────────────────────────
    d["std_5"]  = d["ret_1"].rolling(5).std()
    d["std_20"] = d["ret_1"].rolling(20).std()

    return d


FEATURE_COLS = [
    "ret_1", "ret_3", "ret_5", "ret_10",
    "ema_ratio",
    "rsi_norm",
    "atr_ratio",
    "hl_ratio", "body_ratio", "upper_shadow", "lower_shadow",
    "vol_ratio",
    "std_5", "std_20",
]


# ─────────────────────────────────────────────────────────────────────────────
# DATA FETCH
# ─────────────────────────────────────────────────────────────────────────────
def fetch_history(days=TRAIN_DAYS) -> pd.DataFrame | None:
    """Fetch up to `days` calendar days of 15-min NIFTY data in 60-day chunks."""
    all_candles = []
    end   = datetime.datetime.now()
    start = end - datetime.timedelta(days=days)

    chunk = start
    while chunk < end:
        chunk_end = min(chunk + datetime.timedelta(days=58), end)
        try:
            candles = kite.historical_data(
                config.NIFTY_TOKEN,
                chunk.strftime("%Y-%m-%d"),
                chunk_end.strftime("%Y-%m-%d"),
                "15minute"
            )
            all_candles.extend(candles)
        except Exception as e:
            print(f"⚠️ History fetch error: {e}")
        chunk = chunk_end + datetime.timedelta(days=1)

    if not all_candles:
        return None

    df = pd.DataFrame(all_candles)
    df["date"] = pd.to_datetime(df["date"])
    df.set_index("date", inplace=True)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    df = df.between_time("09:15", "15:30")
    return df


def fetch_live(minutes_back=60) -> pd.DataFrame | None:
    """Fetch recent 15-min bars for live prediction (cached 10s)."""
    now = time.time()
    if _live_cache["df"] is not None and now - _live_cache["ts"] < 10:
        return _live_cache["df"]

    try:
        to_dt   = datetime.datetime.now()
        from_dt = to_dt - datetime.timedelta(days=5)   # a few days for feature warmup
        candles = kite.historical_data(
            config.NIFTY_TOKEN,
            from_dt.strftime("%Y-%m-%d"),
            to_dt.strftime("%Y-%m-%d"),
            "15minute"
        )
        df = pd.DataFrame(candles)
        df["date"] = pd.to_datetime(df["date"])
        df.set_index("date", inplace=True)
        df = df[~df.index.duplicated(keep="first")].sort_index()
        _live_cache["df"] = df
        _live_cache["ts"] = now
        return df
    except Exception as e:
        print(f"⚠️ Live fetch error: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# TRAIN
# ─────────────────────────────────────────────────────────────────────────────
def train_model():
    """
    Fetch history → build features → train RandomForest.
    Target: next bar close > current bar close  (1=CALL, 0=PUT)
    Stores model + scaler in globals.
    """
    global model, scaler, MODEL_LOADED, MODEL_ACCURACY, LAST_TRAIN_TIME

    print("🔄 ML: fetching training data...", flush=True)
    df = fetch_history(days=TRAIN_DAYS)

    if df is None or len(df) < 200:
        print("❌ ML: not enough history to train", flush=True)
        return False

    print(f"📊 ML: {len(df)} bars fetched  ({df.index[0].date()} → {df.index[-1].date()})", flush=True)

    # Features
    df = compute_features(df)

    # Target: next bar direction (shift -1 so label = what happens NEXT)
    df["target"] = (df["close"].shift(-1) > df["close"]).astype(int)

    # Drop NaN rows (warmup + last bar has no target)
    df = df.dropna(subset=FEATURE_COLS + ["target"])

    X = df[FEATURE_COLS].values
    y = df["target"].values.astype(int)

    if len(X) < 100:
        print("❌ ML: not enough clean rows after dropna", flush=True)
        return False

    # Train / test split (time-ordered — no shuffle to avoid lookahead)
    split = int(len(X) * 0.8)
    X_train, X_test = X[:split], X[split:]
    y_train, y_test = y[:split], y[split:]

    # Scale
    sc = StandardScaler()
    X_train_s = sc.fit_transform(X_train)
    X_test_s  = sc.transform(X_test)

    # Train Random Forest
    rf = RandomForestClassifier(
        n_estimators = N_ESTIMATORS,
        max_depth    = MAX_DEPTH,
        min_samples_leaf = 10,    # prevents overfit on tiny leaf nodes
        class_weight = "balanced",
        random_state = 42,
        n_jobs       = -1
    )
    rf.fit(X_train_s, y_train)

    # Evaluate
    acc = accuracy_score(y_test, rf.predict(X_test_s)) * 100

    with _train_lock:
        model          = rf
        scaler         = sc
        MODEL_LOADED   = True
        MODEL_ACCURACY = acc
        LAST_TRAIN_TIME = datetime.datetime.now(IST)

    print(f"✅ ML model trained | rows={len(X)} | test accuracy={acc:.1f}%", flush=True)
    return True


# ─────────────────────────────────────────────────────────────────────────────
# DAILY RETRAIN THREAD
# ─────────────────────────────────────────────────────────────────────────────
def retrain_scheduler():
    """Background thread — retrains model every day at RETRAIN_HOUR_IST."""
    time.sleep(10)   # let server start first
    while True:
        now = datetime.datetime.now(IST)
        if now.weekday() < 5 and now.hour == RETRAIN_HOUR_IST and now.minute < 5:
            print("🔁 ML: scheduled daily retrain...", flush=True)
            train_model()
            time.sleep(360)   # sleep 6 min so we don't retrain twice in same window
        time.sleep(60)


# ─────────────────────────────────────────────────────────────────────────────
# PREDICT
# ─────────────────────────────────────────────────────────────────────────────
def predict_signal():
    """
    Build features from live data and run ML prediction.
    Returns (signal, confidence, reason).
    signal: "CALL" | "PUT" | "HOLD"
    """
    df = fetch_live()
    if df is None or len(df) < 30:
        return "HOLD", 50.0, "Insufficient live data"

    df = compute_features(df)
    df = df.dropna(subset=FEATURE_COLS)

    if len(df) < 2:
        return "HOLD", 50.0, "Not enough bars after feature computation"

    last = df.iloc[-2]   # last CLOSED bar (avoid partially formed current bar)

    # Low-volatility guard
    if last["hl_ratio"] < 0.0005:
        return "HOLD", 50.0, "Low volatility — market flat"

    with _train_lock:
        if not MODEL_LOADED or model is None:
            return "HOLD", 50.0, "Model not yet trained"
        _model, _scaler = model, scaler

    X_live = last[FEATURE_COLS].values.reshape(1, -1)
    X_live_s = _scaler.transform(X_live)

    proba      = _model.predict_proba(X_live_s)[0]
    call_prob  = float(proba[1])
    put_prob   = float(proba[0])
    signal     = "CALL" if call_prob >= put_prob else "PUT"
    confidence = max(call_prob, put_prob) * 100

    if confidence < MIN_CONFIDENCE:
        return "HOLD", round(confidence, 2), f"Low confidence ({confidence:.1f}% below {MIN_CONFIDENCE}%)"

    return signal, round(confidence, 2), f"RF prediction (acc={MODEL_ACCURACY:.1f}%)"


# ─────────────────────────────────────────────────────────────────────────────
# FLASK ROUTES
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/set_token", methods=["POST"])
def set_token():
    """
    Called by main.py after every daily auto-login.
    Body: {"access_token": "xxxx"}
    main.py sends this right after it generates a fresh Kite token.
    """
    global TOKEN_READY
    from flask import request
    data  = request.get_json(force=True, silent=True) or {}
    token = data.get("access_token", "").strip()

    if not token:
        return jsonify({"status": "error", "reason": "access_token missing"}), 400

    kite.set_access_token(token)
    TOKEN_READY = True
    print(f"✅ ML server: access token updated ({token[:8]}...)", flush=True)

    # Trigger retrain now that we have a valid token
    threading.Thread(target=train_model, daemon=True).start()
    return jsonify({"status": "ok", "message": "token set, retrain started"})


@app.route("/signal")
def get_signal():
    if not TOKEN_READY:
        return jsonify({"signal": "HOLD", "confidence": 0,
                        "reason": "Waiting for access token from main bot"})
    try:
        signal, confidence, reason = predict_signal()
        print(f"🤖 /signal → {signal} | {confidence:.1f}% | {reason}", flush=True)
        return jsonify({
            "signal":     signal,
            "confidence": confidence,
            "reason":     reason
        })
    except Exception as e:
        print(f"❌ /signal error: {e}", flush=True)
        return jsonify({"signal": "HOLD", "confidence": 0, "reason": str(e)})


@app.route("/health")
def health():
    return jsonify({
        "status":       "ok",
        "token_ready":  TOKEN_READY,
        "model_loaded": MODEL_LOADED,
        "accuracy":     round(MODEL_ACCURACY, 1),
        "last_trained": LAST_TRAIN_TIME.isoformat() if LAST_TRAIN_TIME else None,
        "features":     len(FEATURE_COLS),
    })


@app.route("/retrain")
def retrain():
    """Manual retrain trigger."""
    if not TOKEN_READY:
        return jsonify({"status": "error", "reason": "No token yet"}), 400
    threading.Thread(target=train_model, daemon=True).start()
    return jsonify({"status": "retrain started"})


# ─────────────────────────────────────────────────────────────────────────────
# STARTUP — train on launch, then start scheduler
# ─────────────────────────────────────────────────────────────────────────────
def startup():
    print("🚀 ML Signal Server starting...", flush=True)
    print("⏳ Waiting for access token from main bot via POST /set_token ...", flush=True)
    print("   (main.py will send it automatically after daily login)", flush=True)
    # Do NOT train yet — no token available until main.py sends it.
    # Training is triggered inside /set_token after token is received.
    t = threading.Thread(target=retrain_scheduler, daemon=True)
    t.start()
    print("🕐 Daily retrain scheduler started", flush=True)


if __name__ == "__main__":
    startup()
    port = int(os.environ.get("PORT", 10000))
    print(f"🌐 ML server listening on port {port}", flush=True)
    app.run(host="0.0.0.0", port=port)
