"""
╔══════════════════════════════════════════════════════════════════════════╗
║  ORB + VWAP OPTION-BUYING BACKTEST                                       ║
║  Opening-range breakout, VWAP confirmation, ATM option, stop on the      ║
║  index, R target, 20–30 min time stop, no entries after 14:30.           ║
╚══════════════════════════════════════════════════════════════════════════╝

HOW TO RUN (Railway shell, after market hours — uses the bot's auto-login)

  Compare the main variants (recommended first run):
    python3 backtest_orb.py --instrument NIFTY  --days 365 --compare-variants
    python3 backtest_orb.py --instrument SENSEX --days 365 --compare-variants

  One variant in detail (+ random-direction baseline):
    python3 backtest_orb.py --instrument NIFTY --stop mid --target-r 2 --time-stop 30 --compare

  Self-test on synthetic prices (mechanics only, NOT evidence):
    python3 backtest_orb.py --synthetic --compare-variants

RULES SIMULATED
  • 5-minute candles. Opening range = first --or-minutes (default 15: 09:15–09:30).
  • CALL: first 5-min close above the range high AND above VWAP.
    PUT : first 5-min close below the range low  AND below VWAP.
    (--no-vwap turns the VWAP condition off.)
  • Entries 09:30–--entry-cutoff (14:30). --max-trades per day (default 1).
  • Buys the ATM option (--strike-offset, + = OTM, − = ITM) of the nearest weekly expiry.
  • Stop on the INDEX: --stop mid (range midpoint) or opposite (other side of range).
    R = index distance from entry to stop.
  • Target on the INDEX: --target-r × R (0 = no target; hold to time stop / 15:12).
  • Time stop: after --time-stop minutes, exit if the option is not in profit
    (--time-stop-mode loss), or exit regardless (--time-stop-mode any). 0 = off.
  • Force exit 15:12. Daily loss cap --daily-loss-pct of capital (default 2%).
  • Charges (brokerage, STT, exchange, GST, stamp) + slippage on every fill.

VWAP: index candles have no volume, so VWAP uses NIFTY/SENSEX FUTURES volume when
Kite returns continuous futures data; otherwise it falls back to a time-weighted
average price (TWAP) and says so.

NOT simulated: real option premiums (Kite has no history for expired contracts —
prices are Black-Scholes with India VIX on a trading-time clock), bid/ask
jumps on fast moves, and the bot's other filters. Treat results as "the core
idea"; confirm anything promising with 4–8 weeks of paper trading.
"""

import os, sys, math, argparse, random, copy
from datetime import datetime, timedelta, date, time as dtime
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import backtest_credit_spread as cs     # BS pricing, trading-time clock, expiry calendar, VIX prep

SPEC = {"NIFTY":  {"token": 256265, "step": 50,  "lot": int(os.environ.get("NIFTY_LOT_SIZE", 65)),
                   "fut_name": "NIFTY", "fut_exch": "NFO"},
        "SENSEX": {"token": 265,    "step": 100, "lot": int(os.environ.get("SENSEX_LOT_SIZE", 20)),
                   "fut_name": "SENSEX", "fut_exch": "BFO"}}


def _hm(s, d):
    try:
        h, m = str(s).split(":")
        return int(h), int(m)
    except Exception:
        return d


# ── data ─────────────────────────────────────────────────────────────────────
def _kite():
    if os.environ.get("KITE_ACCESS_TOKEN"):
        from kiteconnect import KiteConnect
        k = KiteConnect(api_key=os.environ["KITE_API_KEY"])
        k.set_access_token(os.environ["KITE_ACCESS_TOKEN"])
        return k
    print("No KITE_ACCESS_TOKEN — logging in via main.py auto-login…")
    import main as _bot
    return _bot.kite


def _chunks(k, token, start, end, interval, days_per_call, **kw):
    rows, cur = [], start
    while cur < end:
        nxt = min(cur + timedelta(days=days_per_call), end)
        rows += k.historical_data(token, cur, nxt, interval, **kw)
        cur = nxt + timedelta(minutes=1)
    return rows


def load_kite(inst, days):
    k = _kite()
    end = datetime.now().replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    spot = pd.DataFrame(_chunks(k, SPEC[inst]["token"], start, end, "5minute", 90))
    vix = pd.DataFrame(k.historical_data(cs.VIX_TOKEN, start - timedelta(days=5), end, "day"))
    fut = None
    try:   # continuous futures for volume (VWAP)
        sp = SPEC[inst]
        futs = sorted([i for i in k.instruments(sp["fut_exch"])
                       if i["name"] == sp["fut_name"] and i["instrument_type"] == "FUT"],
                      key=lambda i: i["expiry"])
        if futs:
            fut = pd.DataFrame(_chunks(k, futs[0]["instrument_token"], start, end, "5minute", 90,
                                       continuous=True))
            if fut.empty or fut["volume"].sum() == 0:
                fut = None
    except Exception as e:
        print(f"ℹ️ Futures volume unavailable ({e}) — VWAP will use TWAP")
        fut = None
    return spot, vix, fut


def synthetic5(days=365, seed=7, rv_ratio=0.85, mu=0.10, trendiness=0.0):
    """5-min GBM; trendiness>0 plants intraday trend days (to test detection)."""
    rng = np.random.default_rng(seed)
    rows, vix_rows, px, v = [], [], 24000.0, 14.0
    d = datetime(2025, 1, 1, 9, 15)
    while len(vix_rows) < days * 5 // 7:
        if d.weekday() < 5:
            v = float(np.clip(v + rng.normal(0, 0.6) + (14 - v) * 0.05, 9, 35))
            vix_rows.append({"date": d.date(), "close": v})
            day_drift = rng.choice([-1, 0, 0, 1]) * trendiness       # some trend days
            for k in range(75):
                ts, dt, sv = d + timedelta(minutes=5 * k), 1 / (252 * 75), v / 100 * rv_ratio
                o = px
                drift = mu + (day_drift if k >= 3 else 0.0)          # trend starts after the range
                c = o * math.exp((drift - 0.5 * sv * sv) * dt + rng.normal(0, sv * math.sqrt(dt)))
                ext = abs(rng.normal(0, sv * math.sqrt(dt) * 0.4))
                rows.append({"date": ts, "open": o, "high": max(o, c) * (1 + ext),
                             "low": min(o, c) * (1 - ext), "close": c,
                             "volume": float(rng.integers(1000, 5000))})
                px = c
        d += timedelta(days=1)
    return pd.DataFrame(rows), pd.DataFrame(vix_rows)


def build(spot, vix, fut=None):
    df = cs.prep(spot, vix)
    df["d"] = df["date"].dt.date
    tp = (df["high"] + df["low"] + df["close"]) / 3
    vol = None
    if fut is not None and len(fut):
        f = fut.copy()
        f["date"] = pd.to_datetime(f["date"]).dt.tz_localize(None)
        vol = df[["date"]].merge(f[["date", "volume"]], on="date", how="left")["volume"].fillna(0).to_numpy()
    elif "volume" in df and df["volume"].fillna(0).sum() > 0:
        vol = df["volume"].fillna(0).to_numpy()
    if vol is not None and vol.sum() > 0:
        w = pd.Series(vol, index=df.index)
        df["vwap"] = (tp * w).groupby(df["d"]).cumsum() / w.groupby(df["d"]).cumsum().replace(0, np.nan)
        df["vwap"] = df["vwap"].fillna(tp.groupby(df["d"]).expanding().mean().reset_index(level=0, drop=True))
        src = "futures/real volume VWAP"
    else:
        df["vwap"] = tp.groupby(df["d"]).expanding().mean().reset_index(level=0, drop=True)
        src = "TWAP (no volume available)"
    return df, src


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
def run(df, a, mode="signal", seed=0):
    """mode 'signal' = trade the breakout direction │ 'random' = same entry bars, random CE/PE."""
    rng = random.Random(seed)
    sp = SPEC[a.instrument]
    qty, step = sp["lot"] * a.lots, sp["step"]
    O, H, L, C, V, VW = (df[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "vix", "vwap"))
    T = pd.to_datetime(df["date"]).dt.to_pydatetime()
    D = df["d"].to_numpy()
    or_end = (9, 15 + a.or_minutes) if a.or_minutes < 45 else (9 + (15 + a.or_minutes) // 60, (15 + a.or_minutes) % 60)
    cut, fex = _hm(a.entry_cutoff, (14, 30)), _hm(a.force_exit, (15, 12))
    slip = lambda p: max(a.slip_min, p * a.slip_pct)
    price = lambda S, K, Tt, call, vix: cs.bs(S, K, Tt, max(0.05, vix / 100.0), call)

    trades = []
    i, n = 0, len(df)
    while i < n:
        day = D[i]
        j = i
        while j < n and D[j] == day:
            j += 1
        idx = range(i, j)                                   # bars of this day
        rng_bars = [k for k in idx if (T[k].hour, T[k].minute) < or_end]
        if len(rng_bars) < max(1, a.or_minutes // 5):
            i = j
            continue
        orh, orl = max(H[k] for k in rng_bars), min(L[k] for k in rng_bars)
        if a.max_or_pct and (orh - orl) / C[rng_bars[-1]] * 100 > a.max_or_pct:
            i = j
            continue
        day_trades, day_pnl, pos, done_dirs = 0, 0.0, None, set()
        for k in idx:
            t_close = T[k] + timedelta(minutes=5)
            hm = (t_close.hour, t_close.minute)
            if hm <= or_end:
                continue
            # ── manage ───────────────────────────────────────────────────────
            if pos:
                Tt = cs.years_to(pos["exp"], t_close)
                call = pos["call"]
                up = pos["dir"] == 1                       # direction of the INDEX bet
                adv_px = L[k] if up else H[k]
                fav_px = H[k] if up else L[k]
                reason, exit_S = None, None
                stop_hit = adv_px <= pos["stop"] if up else adv_px >= pos["stop"]
                tgt_hit = pos["tgt"] is not None and (fav_px >= pos["tgt"] if up else fav_px <= pos["tgt"])
                if stop_hit:                                # assume stop first if both in one bar
                    gap = O[k] <= pos["stop"] if up else O[k] >= pos["stop"]
                    reason, exit_S = "STOP", (O[k] if gap else pos["stop"])
                elif tgt_hit:
                    reason, exit_S = "TARGET", pos["tgt"]
                elif a.time_stop and (t_close - pos["t"]).total_seconds() >= a.time_stop * 60 and not pos["ts_done"]:
                    pos["ts_done"] = True
                    p_now = price(C[k], pos["K"], Tt, call, V[k])
                    if a.time_stop_mode == "any" or p_now <= pos["entry"]:
                        reason, exit_S = "TIME_STOP", C[k]
                if not reason and hm >= fex:
                    reason, exit_S = "FORCE_EXIT", C[k]
                if not reason and k == j - 1:
                    reason, exit_S = "FORCE_EXIT", C[k]
                if reason:
                    p = price(exit_S, pos["K"], Tt, call, V[k])
                    fill = max(0.05, p - slip(p))
                    ch = charges(pos["entry"] * qty, fill * qty)
                    pnl = (fill - pos["entry"]) * qty - ch
                    risk_rs = max(1.0, (pos["entry"] - price(pos["stop"], pos["K"],
                                   cs.years_to(pos["exp"], pos["t"]), call, pos["vix"])) * qty)
                    trades.append({"entry_time": pos["t"], "exit_time": t_close,
                                   "dir": "CALL" if call else "PUT", "strike": pos["K"],
                                   "entry": round(pos["entry"], 2), "exit": round(fill, 2),
                                   "reason": reason, "pnl": round(pnl, 0), "charges": round(ch, 0),
                                   "r_mult": round(pnl / risk_rs, 2)})
                    day_pnl += pnl
                    pos = None
                continue
            # ── entry ────────────────────────────────────────────────────────
            if hm > cut or day_trades >= a.max_trades or day_pnl <= -a.capital * a.daily_loss_pct / 100:
                continue
            brk = 1 if C[k] > orh else (-1 if C[k] < orl else 0)
            if brk == 0 or brk in done_dirs:
                continue
            if a.vwap and ((brk == 1 and C[k] <= VW[k]) or (brk == -1 and C[k] >= VW[k])):
                continue
            done_dirs.add(brk)
            direction = brk if mode == "signal" else rng.choice([1, -1])
            mid = (orh + orl) / 2
            if a.stop == "mid":
                dist = abs(C[k] - mid)
            else:
                dist = (C[k] - orl) if brk == 1 else (orh - C[k])
            dist = max(dist, C[k] * 0.0005)                 # at least 0.05% of index
            stop = C[k] - dist if direction == 1 else C[k] + dist
            tgt = (C[k] + a.target_r * dist if direction == 1 else C[k] - a.target_r * dist) if a.target_r else None
            call = direction == 1
            exp = nearest_expiry(a.instrument, day, a.skip_expiry_day)
            Tt = cs.years_to(exp, t_close)
            K = round(C[k] / step) * step + (a.strike_offset * step if call else -a.strike_offset * step)
            p = price(C[k], K, Tt, call, V[k])
            if p < a.min_premium:
                continue
            entry = p + slip(p)
            if entry * qty > a.capital * 0.7:
                continue
            pos = {"t": t_close, "dir": direction, "call": call, "K": K, "exp": exp,
                   "entry": entry, "stop": stop, "tgt": tgt, "ts_done": False, "vix": V[k]}
            day_trades += 1
        i = j
    return pd.DataFrame(trades)


def stats(t, capital):
    if t.empty:
        return dict(trades=0, win=0, avg_w=0, avg_l=0, total=0, pct=0, dd=0, avg_wr=0, exp_r=0)
    eq = t.pnl.cumsum()
    yrs = max(1, (t.exit_time.max() - t.entry_time.min()).days) / 365
    w, l = t[t.pnl > 0], t[t.pnl <= 0]
    return dict(trades=len(t), win=len(w) / len(t) * 100,
                avg_w=w.pnl.mean() if len(w) else 0, avg_l=l.pnl.mean() if len(l) else 0,
                total=t.pnl.sum(), pct=t.pnl.sum() / yrs / capital * 100, dd=(eq - eq.cummax()).min(),
                avg_wr=w.r_mult.mean() if len(w) else 0, exp_r=t.r_mult.mean())


def _beat(df, a, total, runs):
    rnd = []
    for s in range(runs):
        r = run(df, a, "random", seed=s)
        rnd.append(float(r["pnl"].sum()) if not r.empty else 0.0)
    return sum(total > x for x in rnd), float(np.median(rnd))


VARIANTS = [
    ("ORB only (no VWAP), stop mid, 2R",        dict(vwap=False, stop="mid",      target_r=2.0, time_stop=0)),
    ("ORB+VWAP, stop mid, 2R",                  dict(vwap=True,  stop="mid",      target_r=2.0, time_stop=0)),
    ("ORB+VWAP, stop mid, 2R, 30-min stop",     dict(vwap=True,  stop="mid",      target_r=2.0, time_stop=30)),
    ("ORB+VWAP, stop mid, 2R, 20-min stop",     dict(vwap=True,  stop="mid",      target_r=2.0, time_stop=20)),
    ("ORB+VWAP, stop opposite, 2R, 30-min",     dict(vwap=True,  stop="opposite", target_r=2.0, time_stop=30)),
    ("ORB+VWAP, stop mid, 3R, 30-min",          dict(vwap=True,  stop="mid",      target_r=3.0, time_stop=30)),
    ("ORB+VWAP, stop mid, no target, 30-min",   dict(vwap=True,  stop="mid",      target_r=0.0, time_stop=30)),
]


def compare_variants(df, a):
    n = a.random_runs
    rows = []
    for name, over in VARIANTS:
        b = copy.copy(a)
        for k, v in over.items():
            setattr(b, k, v)
        t = run(df, b, "signal")
        st = stats(t, a.capital)
        st["beat"], st["rnd_med"] = _beat(df, b, st["total"], n)
        st["name"] = name
        rows.append(st)
        print(f"  … {name} done", flush=True)
    print(f"\n━━ ORB OPTION-BUYING VARIANTS — {a.instrument}, {len(df)} 5-min candles, {a.lots} lot, "
          f"ATM{a.strike_offset:+d}, all costs included ━━")
    print(f"{'variant':<40}{'trades':>7}{'win%':>6}{'avg win':>9}{'avg loss':>10}{'avg win R':>10}"
          f"{'exp R':>7}{'total ₹':>10}{'%/yr':>7}{'max DD ₹':>10}{'beats rnd':>11}")
    for r in sorted(rows, key=lambda r: r["total"], reverse=True):
        print(f"{r['name']:<40}{r['trades']:>7}{r['win']:>6.0f}{r['avg_w']:>9,.0f}{r['avg_l']:>10,.0f}"
              f"{r['avg_wr']:>10.2f}{r['exp_r']:>7.2f}{r['total']:>10,.0f}{r['pct']:>7.1f}{r['dd']:>10,.0f}"
              f"{str(r['beat']) + '/' + str(n):>11}")
    need = int(np.ceil(0.95 * n))
    good = [r for r in rows if r["beat"] >= need and r["total"] > 0]
    print(f"\nHow to read: 'avg win R' = average winner as a multiple of the planned risk "
          f"(the claim to check is ≈2.5R at a 40% win rate); 'exp R' = average result per trade in R.")
    print(f"A variant is worth paper-trading only if total > 0 AND it beats random CALL/PUT in ≥{need}/{n} runs.")
    if good:
        best = max(good, key=lambda r: r["total"] / max(1, -r["dd"]))
        print(f"✅ Passes both tests: {best['name']}")
        print(f"   Caution: {len(rows)} variants were tested — re-run on the other index and a different "
              f"--days window before trusting it, then paper-trade 4–8 weeks.")
    else:
        print("⚠️ No ORB variant passes both tests on this data — ORB+VWAP option buying is not expected "
              "to make money after costs here. Don't trade it live.")


def main():
    env = os.environ.get
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", default="NIFTY", choices=list(SPEC))
    ap.add_argument("--days", type=int, default=365)
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--trendiness", type=float, default=0.0)
    ap.add_argument("--or-minutes", type=int, default=15)
    ap.add_argument("--no-vwap", dest="vwap", action="store_false")
    ap.add_argument("--stop", default="mid", choices=["mid", "opposite"])
    ap.add_argument("--target-r", type=float, default=2.0)
    ap.add_argument("--time-stop", type=int, default=30)
    ap.add_argument("--time-stop-mode", default="loss", choices=["loss", "any"])
    ap.add_argument("--max-trades", type=int, default=1)
    ap.add_argument("--max-or-pct", type=float, default=0.0, help="skip days whose range is wider than this %% (0=off)")
    ap.add_argument("--entry-cutoff", default="14:30")
    ap.add_argument("--force-exit", default=env("FORCE_EXIT_TIME", "15:12"))
    ap.add_argument("--daily-loss-pct", type=float, default=2.0)
    ap.add_argument("--capital", type=float, default=float(env("PAPER_CAPITAL", 25000)))
    ap.add_argument("--lots", type=int, default=1)
    ap.add_argument("--strike-offset", type=int, default=0)
    ap.add_argument("--min-premium", type=float, default=0.0)
    ap.add_argument("--skip-expiry-day", action="store_true")
    ap.add_argument("--slip-pct", type=float, default=0.005)
    ap.add_argument("--slip-min", type=float, default=0.10)
    ap.add_argument("--compare", action="store_true")
    ap.add_argument("--compare-variants", action="store_true")
    ap.add_argument("--random-runs", type=int, default=40)
    ap.add_argument("--out", default="orb_backtest_trades.csv")
    a = ap.parse_args()
    if a.min_premium == 0:
        a.min_premium = float(env(f"MIN_PREMIUM_{a.instrument}", 60 if a.instrument == "SENSEX" else 40))

    if a.synthetic:
        spot, vix = synthetic5(a.days, trendiness=a.trendiness)
        fut = None
        print("⚠️  SYNTHETIC DATA — checks mechanics only, results mean nothing about real markets")
    else:
        spot, vix, fut = load_kite(a.instrument, a.days)
    df, vsrc = build(spot, vix, fut)
    print(f"Data: {len(df)} 5-min candles {df['date'].min():%Y-%m-%d} → {df['date'].max():%Y-%m-%d} │ "
          f"VWAP source: {vsrc}")

    if a.compare_variants:
        compare_variants(df, a)
        return
    t = run(df, a, "signal")
    st = stats(t, a.capital)
    print(f"\n━━ {a.instrument} ORB{'+VWAP' if a.vwap else ''} option buying — stop {a.stop}, "
          f"target {a.target_r or 'none'}R, time stop {a.time_stop or 'off'} min ━━")
    print(f"Trades {st['trades']} │ win {st['win']:.0f}% │ avg win ₹{st['avg_w']:,.0f} ({st['avg_wr']:.2f}R) │ "
          f"avg loss ₹{st['avg_l']:,.0f} │ expectancy {st['exp_r']:.2f}R/trade")
    print(f"Total ₹{st['total']:,.0f} ({st['pct']:.1f}%/yr on ₹{a.capital:,.0f}) │ max DD ₹{st['dd']:,.0f}")
    if not t.empty:
        print("Exit reasons:", t.reason.value_counts().to_dict())
        print("P&L by exit:", t.groupby("reason").pnl.sum().round(0).to_dict())
        t.to_csv(a.out, index=False)
        print(f"Trades saved → {a.out}")
    if a.compare:
        beat, med = _beat(df, a, st["total"], 40)
        print(f"\nRandom CALL/PUT (40 runs, same entry bars & exits): median ₹{med:,.0f} │ "
              f"ORB beat {beat}/40 → " + ("direction adds value" if beat >= 38 else "NOT better than a coin flip"))


if __name__ == "__main__":
    main()
