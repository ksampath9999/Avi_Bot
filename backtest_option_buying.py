"""
╔══════════════════════════════════════════════════════════════════════════╗
║  OPTION-BUYING BACKTEST — HalfTrend arrow + BUY_SIGNAL confirmation      ║
║  Mirrors the bot: entry trigger, R-based exits, cut-off/force-exit times ║
║  and uses the bot's own indicator code (loaded from main.py).            ║
╚══════════════════════════════════════════════════════════════════════════╝

HOW TO RUN (Railway shell, after market hours — uses the bot's auto-login)

  python3 backtest_option_buying.py --instrument SENSEX --days 365 --compare-signals
  python3 backtest_option_buying.py --instrument NIFTY  --days 365 --compare-signals --adx-min 20

  One signal in detail (+ random-direction baseline):
  python3 backtest_option_buying.py --instrument SENSEX --signal supertrend --compare

  Self-test on synthetic prices (mechanics only, NOT evidence):
  python3 backtest_option_buying.py --synthetic --compare-signals

WHAT IT SIMULATES (defaults = your Railway settings; override with flags)
  • 15-min candles. A HalfTrend arrow (amplitude HT_AMPLITUDE) opens a trend leg.
  • Entry when the chosen signal agrees (halftrend = no extra check), between
    09:30 and ENTRY_CUTOFF_TIME, one trade per leg, MAX_<INST>_TRADES per day,
    leg may carry over to the next trading day (HT_MAX_CARRYOVER_DAYS).
  • Buys the ATM option (BUY_STRIKE_OFFSET) of the nearest weekly expiry.
  • Exits (EXIT_MODE=R): −1R max loss │ +1R → breakeven floor │ trail 1R below
    peak │ HalfTrend flip │ FORCE_EXIT_TIME. R = MAX_LOSS_PER_TRADE.
  • Charges per round trip (Zerodha-style) + slippage on both sides.
  • DAILY_MAX_LOSS stops the day.

  NOT simulated: the bot's other entry filters (ATR, CPR, first-candle, Hull band,
  whipsaw, etc.) — they only remove trades, so treat this as "the core idea".
  Option prices are MODELLED (Black-Scholes, IV = India VIX); real option
  history for expired contracts isn't available from Kite. Confirm with paper trading.
"""

import os, sys, math, argparse, random, copy
from datetime import datetime, timedelta, date, time as dtime
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backtest_credit_spread as cs          # shares bot indicator code, BS, expiry calendar, VIX prep

cs_direction_series = cs.cs_direction_series
halftrend_tv = cs.halftrend_tv
SIGNALS = cs.SIGNALS
SPEC = {"NIFTY":  {"token": 256265, "step": 50,  "lot": int(os.environ.get("NIFTY_LOT_SIZE", 65))},
        "SENSEX": {"token": 265,    "step": 100, "lot": int(os.environ.get("SENSEX_LOT_SIZE", 20))}}


def _hm(s, d):
    try:
        h, m = str(s).split(":"); return int(h), int(m)
    except Exception:
        return d


# ── data ─────────────────────────────────────────────────────────────────────
def load_kite(inst, days):
    if os.environ.get("KITE_ACCESS_TOKEN"):
        from kiteconnect import KiteConnect
        k = KiteConnect(api_key=os.environ["KITE_API_KEY"])
        k.set_access_token(os.environ["KITE_ACCESS_TOKEN"])
    else:
        print("No KITE_ACCESS_TOKEN — logging in via main.py auto-login…")
        import main as _bot
        k = _bot.kite
    end = datetime.now().replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    rows, cur = [], start
    while cur < end:                                   # 15minute: ≤200 days per call
        nxt = min(cur + timedelta(days=190), end)
        rows += k.historical_data(SPEC[inst]["token"], cur, nxt, "15minute")
        cur = nxt + timedelta(minutes=1)
    vix = pd.DataFrame(k.historical_data(cs.VIX_TOKEN, start - timedelta(days=5), end, "day"))
    return pd.DataFrame(rows), vix


def synthetic15(days=365, seed=7, rv_ratio=0.85, mu=0.10, trendiness=0.0):
    """15-min GBM; trendiness>0 adds persistent drift regimes (to test signal detection)."""
    rng = np.random.default_rng(seed)
    rows, vix_rows, px, v, regime = [], [], 24000.0, 14.0, 0.0
    d = datetime(2025, 1, 1, 9, 15)
    while len(vix_rows) < days * 5 // 7:
        if d.weekday() < 5:
            v = float(np.clip(v + rng.normal(0, 0.6) + (14 - v) * 0.05, 9, 35))
            vix_rows.append({"date": d.date(), "close": v})
            if rng.random() < 0.15:
                regime = rng.choice([-1, 0, 1]) * trendiness
            for k in range(25):
                ts, dt, sv = d + timedelta(minutes=15 * k), 1 / (252 * 25), v / 100 * rv_ratio
                o = px
                r = (mu + regime - 0.5 * sv * sv) * dt + rng.normal(0, sv * math.sqrt(dt))
                c = o * math.exp(r)
                ext = abs(rng.normal(0, sv * math.sqrt(dt) * 0.4))
                rows.append({"date": ts, "open": o, "high": max(o, c) * (1 + ext),
                             "low": min(o, c) * (1 - ext), "close": c})
                px = c
        d += timedelta(days=1)
    return pd.DataFrame(rows), pd.DataFrame(vix_rows)


def nearest_expiry(inst, d, skip_expiry_day):
    for k in range(0, 10):
        e = d + timedelta(days=k)
        if e.weekday() == cs.expiry_weekday(inst, e):
            if k == 0 and skip_expiry_day:
                continue
            return e
    return d


def charges(buy_val, sell_val):
    return 2 * 20 * 1.18 + 0.001 * sell_val + 0.00035 * (buy_val + sell_val) * 1.18 + 0.00003 * buy_val


# ── engine ───────────────────────────────────────────────────────────────────
_CACHE = {}

def _arrays(df, a):
    key = (id(df), len(df), a.amplitude)
    if key not in _CACHE:
        ht = halftrend_tv(df[["open", "high", "low", "close"]].copy(), amplitude=a.amplitude, channel_deviation=2)
        _CACHE[key] = np.where(ht["trend"].to_numpy() == 0, 1, -1)
    return _CACHE[key]

def _confirm(df, a):
    key = (id(df), len(df), a.signal, a.amplitude, a.adx_min, a.vote_min)
    if key not in _CACHE:
        _CACHE[key] = cs_direction_series(df[["open", "high", "low", "close"]].copy(), a.signal,
                                          ht_amplitude=a.amplitude, adx_min=a.adx_min, vote_min=a.vote_min)
    return _CACHE[key]


def run(df, a, mode="signal", seed=0):
    """mode: 'signal' = trade the leg direction │ 'random' = same entry bars, random CE/PE."""
    rng = random.Random(seed)
    sp = SPEC[a.instrument]
    qty, step = sp["lot"] * a.lots, sp["step"]
    ht = _arrays(df, a)
    conf = _confirm(df, a)
    dates = df["date"].to_numpy()
    tsl = pd.to_datetime(df["date"]).dt.to_pydatetime()
    O, C, H, L, V = (df[k].to_numpy(dtype=float) for k in ("open", "close", "high", "low", "vix"))
    cut, fex = _hm(a.entry_cutoff, (14, 45)), _hm(a.force_exit, (15, 12))
    R = float(a.risk)
    slip = lambda p: max(a.slip_min, p * a.slip_pct)

    trades, pos = [], None
    leg_dir, leg_day, leg_traded = 0, None, False
    day, day_trades, day_pnl = None, 0, 0.0

    def price(S, K, T, call, vix):
        return cs.bs(S, K, T, max(0.05, vix / 100.0), call)

    for i in range(2, len(df)):
        t_close = tsl[i] + timedelta(minutes=15)
        d = t_close.date()
        if d != day:
            day, day_trades, day_pnl = d, 0, 0.0
        # new leg on a HalfTrend arrow (trend change on the closed bar)
        if ht[i] != ht[i - 1]:
            leg_dir, leg_day, leg_traded = int(ht[i]), d, False

        # ── manage open position ─────────────────────────────────────────────
        if pos:
            T = cs.years_to(pos["exp"], t_close)
            call = pos["call"]
            fav, adv = (H[i], L[i]) if call else (L[i], H[i])
            p_close = price(C[i], pos["K"], T, call, V[i])
            p_open = price(O[i], pos["K"], T, call, V[i])        # gap at the bar's open
            pnl_open = (p_open - pos["entry"]) * qty
            p_adv = price(adv, pos["K"], T, call, V[i])
            p_fav = price(fav, pos["K"], T, call, V[i])
            pnl_adv = (p_adv - pos["entry"]) * qty
            pnl_close = (p_close - pos["entry"]) * qty
            reason, exit_p = None, None
            floor = pos["floor"]
            if pnl_adv <= -R:                                   # hard −1R (gap through → worse fill)
                stop_p = pos["entry"] - R / qty
                reason, exit_p = "MAX_LOSS", (p_open if pnl_open <= -R else stop_p)   # gapped through → open
            elif floor is not None and pnl_adv <= floor:        # breakeven / trail floor
                fl_p = pos["entry"] + floor / qty
                reason, exit_p = "R_TRAIL", (p_open if pnl_open <= floor else fl_p)
            elif getattr(a, "flip_exit", True) and ht[i] != pos["dir"]:   # HalfTrend flip on closed bar
                reason, exit_p = "HT_FLIP", p_close
            elif (t_close.hour, t_close.minute) >= fex or (i + 1 < len(df) and tsl[i + 1].date() != tsl[i].date()):
                reason, exit_p = "FORCE_EXIT", p_close
            if reason:
                fill = max(0.05, exit_p - slip(exit_p))
                ch = charges(pos["entry"] * qty, fill * qty)
                pnl = (fill - pos["entry"]) * qty - ch
                trades.append({"entry_time": pos["t"], "exit_time": t_close, "dir": "CALL" if call else "PUT",
                               "strike": pos["K"], "entry": round(pos["entry"], 2), "exit": round(fill, 2),
                               "reason": reason, "pnl": round(pnl, 0), "charges": round(ch, 0),
                               "peak": round(pos["peak"], 0)})
                day_pnl += pnl
                pos = None
            else:
                pos["peak"] = max(pos["peak"], (p_fav - pos["entry"]) * qty)   # applies from next bar
                if a.exit_mode == "R" and pos["peak"] >= a.be_trigger * R:
                    new_floor = max(a.be_buffer, pos["peak"] - a.giveback * R)
                    pos["floor"] = new_floor if pos["floor"] is None else max(pos["floor"], new_floor)
            continue

        # ── entry ────────────────────────────────────────────────────────────
        hm = (t_close.hour, t_close.minute)
        if hm < (9, 30) or hm > cut or leg_dir == 0 or leg_traded:
            continue
        if day_trades >= a.max_trades or day_pnl <= -a.daily_max_loss:
            continue
        if leg_day is not None and np.busday_count(leg_day, d) > a.carryover_days:
            continue
        if ht[i] != leg_dir or conf[i] != leg_dir:            # trend intact AND confirmation agrees
            continue
        direction = leg_dir if mode == "signal" else rng.choice([1, -1])
        call = direction == 1
        exp = nearest_expiry(a.instrument, d, a.skip_expiry_day)
        T = cs.years_to(exp, t_close)
        K = round(C[i] / step) * step + (a.strike_offset * step if call else -a.strike_offset * step)
        p = price(C[i], K, T, call, V[i])
        if p < a.min_premium:
            continue
        entry = p + slip(p)
        if entry * qty > a.capital * 0.7:
            continue
        pos = {"t": t_close, "dir": leg_dir, "call": call, "K": K, "exp": exp, "entry": entry,
               "peak": 0.0, "floor": None}
        leg_traded = True
        day_trades += 1
    return pd.DataFrame(trades)


def stats(t, capital):
    if t.empty:
        return dict(trades=0, win=0, total=0, pct=0, dd=0, avg_w=0, avg_l=0, worst=0)
    eq = t.pnl.cumsum()
    yrs = max(1, (t.exit_time.max() - t.entry_time.min()).days) / 365
    w, l = t[t.pnl > 0], t[t.pnl <= 0]
    return dict(trades=len(t), win=len(w) / len(t) * 100, total=t.pnl.sum(), pct=t.pnl.sum() / yrs / capital * 100,
                dd=(eq - eq.cummax()).min(), avg_w=w.pnl.mean() if len(w) else 0,
                avg_l=l.pnl.mean() if len(l) else 0, worst=t.pnl.min())


def compare_signals(df, a):
    rows = []
    variants = [(m, 0.0) for m in SIGNALS] + ([(m, a.adx_min) for m in SIGNALS] if a.adx_min else [])
    for m, adx in variants:
        b = copy.copy(a); b.signal, b.adx_min = m, adx
        st = stats(run(df, b, "signal"), a.capital)
        rnd = [run(df, b, "random", seed=s).pnl.sum() for s in range(a.random_runs)]
        st["beat"] = sum(st["total"] > x for x in rnd)
        st["name"] = ("halftrend only" if m == "halftrend" else f"HT + {m}") + (f" +ADX≥{adx:.0f}" if adx else "")
        rows.append(st)
        print(f"  … {st['name']} done", flush=True)
    n = a.random_runs
    print(f"\n━━ OPTION-BUYING SIGNAL COMPARISON — {a.instrument}, {len(df)} candles (15-min), "
          f"{a.lots} lot, R=₹{a.risk:,.0f}, all costs included ━━")
    print(f"{'entry rule':<30}{'trades':>7}{'win%':>6}{'avg win':>9}{'avg loss':>10}{'total ₹':>10}{'%/yr':>7}{'max DD ₹':>10}{'beats random':>14}")
    for r in sorted(rows, key=lambda r: r["total"], reverse=True):
        beat = f"{r['beat']}/{n}"
        print(f"{r['name']:<30}{r['trades']:>7}{r['win']:>6.0f}{r['avg_w']:>9,.0f}{r['avg_l']:>10,.0f}"
              f"{r['total']:>10,.0f}{r['pct']:>7.1f}{r['dd']:>10,.0f}{beat:>14}")
    need = int(np.ceil(0.95 * n))
    good = [r for r in rows if r["beat"] >= need and r["total"] > 0]
    print(f"\nHow to read: an entry rule is worth trading only if its total is positive AND it beats "
          f"random CALL/PUT choice in ≥{need}/{n} runs (same entry times, exits, costs).")
    if good:
        best = max(good, key=lambda r: r["total"] / max(1, -r["dd"]))
        m = best["name"].replace("HT + ", "").replace("halftrend only", "halftrend").split()[0]
        print(f"✅ Best rule passing both tests: {best['name']} → set BUY_SIGNAL={m}"
              + (f" and BUY_ADX_MIN={a.adx_min:.0f}" if "ADX" in best["name"] else ""))
        print(f"   Caution: {len(rows)} rules were tested, so one can pass by luck. Treat it as a "
              f"candidate — re-run on the other instrument / a different --days window, then paper-trade it.")
    else:
        print("⚠️ No entry rule passes both tests. On this data the direction signals do not beat a coin flip "
              "after costs — option buying with these rules is not expected to make money. "
              "Do not go live; consider credit spreads instead.")


def main():
    ap = argparse.ArgumentParser()
    env = os.environ.get
    ap.add_argument("--instrument", default="SENSEX", choices=list(SPEC))
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--spot-csv"); ap.add_argument("--vix-csv")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--trendiness", type=float, default=0.0, help="synthetic: add persistent trends (e.g. 0.5)")
    ap.add_argument("--signal", default=env("BUY_SIGNAL", "halftrend").lower(), choices=list(SIGNALS))
    ap.add_argument("--adx-min", type=float, default=float(env("BUY_ADX_MIN", 0)))
    ap.add_argument("--vote-min", type=int, default=int(env("BUY_VOTE_MIN", 4)))
    ap.add_argument("--amplitude", type=int, default=int(env("HT_AMPLITUDE", 3)))
    ap.add_argument("--compare-signals", action="store_true")
    ap.add_argument("--compare", action="store_true", help="random-direction baseline for one signal")
    ap.add_argument("--random-runs", type=int, default=40)
    ap.add_argument("--capital", type=float, default=float(env("PAPER_CAPITAL", 25000)))
    ap.add_argument("--lots", type=int, default=1)
    ap.add_argument("--risk", type=float, default=float(env("RISK_PER_TRADE", "") or env("MAX_LOSS_PER_TRADE", 750)))
    ap.add_argument("--exit-mode", default=env("EXIT_MODE", "R").upper(), choices=["R"])
    ap.add_argument("--be-trigger", type=float, default=float(env("BE_TRIGGER_R", 1.0)))
    ap.add_argument("--giveback", type=float, default=float(env("TRAIL_GIVEBACK_R", 1.0)))
    ap.add_argument("--be-buffer", type=float, default=float(env("BE_BUFFER_RS", 60)))
    ap.add_argument("--strike-offset", type=int, default=int(env("BUY_STRIKE_OFFSET", "") or 0))
    ap.add_argument("--min-premium", type=float, default=0.0)
    ap.add_argument("--entry-cutoff", default=env("ENTRY_CUTOFF_TIME", "14:45"))
    ap.add_argument("--force-exit", default=env("FORCE_EXIT_TIME", "15:12"))
    ap.add_argument("--max-trades", type=int, default=0, help="per day (default = MAX_<INST>_TRADES or 2)")
    ap.add_argument("--daily-max-loss", type=float, default=float(env("DAILY_MAX_LOSS", 1500)))
    ap.add_argument("--carryover-days", type=int, default=int(env("HT_MAX_CARRYOVER_DAYS", 1)))
    ap.add_argument("--skip-expiry-day", action="store_true", help="on expiry day buy next week's expiry")
    ap.add_argument("--slip-pct", type=float, default=0.005)
    ap.add_argument("--slip-min", type=float, default=0.10)
    ap.add_argument("--out", default="ob_backtest_trades.csv")
    a = ap.parse_args()
    if not a.max_trades:
        a.max_trades = int(env(f"MAX_{a.instrument}_TRADES", 2))
    if a.min_premium == 0:
        a.min_premium = float(env(f"MIN_PREMIUM_{a.instrument}", 60 if a.instrument == "SENSEX" else 40))

    if a.synthetic:
        spot, vix = synthetic15(a.days, trendiness=a.trendiness)
        print("⚠️  SYNTHETIC DATA — checks mechanics only, results mean nothing about real markets")
    elif a.spot_csv and a.vix_csv:
        spot, vix = pd.read_csv(a.spot_csv), pd.read_csv(a.vix_csv)
    else:
        spot, vix = load_kite(a.instrument, a.days)
    df = cs.prep(spot, vix)
    print(f"Data: {len(df)} 15-min candles {df['date'].min():%Y-%m-%d} → {df['date'].max():%Y-%m-%d} │ "
          f"HT amplitude {a.amplitude} │ R ₹{a.risk:,.0f} │ max {a.max_trades}/day │ ATM{a.strike_offset:+d}")

    if a.compare_signals:
        compare_signals(df, a)
        return
    t = run(df, a, "signal")
    st = stats(t, a.capital)
    print(f"\n━━ {a.instrument} option buying — HT arrow + {a.signal}"
          + (f" +ADX≥{a.adx_min:.0f}" if a.adx_min else "") + " ━━")
    print(f"Trades {st['trades']} │ win {st['win']:.0f}% │ avg win ₹{st['avg_w']:,.0f} │ avg loss ₹{st['avg_l']:,.0f}")
    print(f"Total ₹{st['total']:,.0f} ({st['pct']:.1f}%/yr on ₹{a.capital:,.0f}) │ max DD ₹{st['dd']:,.0f} │ worst ₹{st['worst']:,.0f}")
    if not t.empty:
        print("Exit reasons:", t.reason.value_counts().to_dict())
        print("P&L by exit:", t.groupby("reason").pnl.sum().round(0).to_dict())
        t.to_csv(a.out, index=False); print(f"Trades saved → {a.out}")
    if a.compare:
        rnd = [run(df, a, "random", seed=s).pnl.sum() for s in range(40)]
        beat = sum(st["total"] > x for x in rnd)
        print(f"\nRandom CALL/PUT (40 runs, same entry times & exits): median ₹{np.median(rnd):,.0f} │ "
              f"signal beat {beat}/40 → " + ("direction adds value" if beat >= 36 else "NOT better than a coin flip"))


if __name__ == "__main__":
    main()
