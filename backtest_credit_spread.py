"""
╔══════════════════════════════════════════════════════════════════════════╗
║  CREDIT-SPREAD BACKTEST — trend-directional bull put / bear call spreads ║
║  Mirrors the bot's credit_spread_loop rules (same HalfTrend code).       ║
╚══════════════════════════════════════════════════════════════════════════╝

HOW TO RUN (pick one data source)

  A) From Kite — on Railway (uses the bot's own auto-login, no token needed):
       python3 backtest_credit_spread.py --days 730 --compare
     or anywhere with a token:
       KITE_API_KEY=xxx KITE_ACCESS_TOKEN=yyy python3 backtest_credit_spread.py --days 730 --compare

  B) From CSV files you export yourself:
       python3 backtest_credit_spread.py --spot-csv nifty_60m.csv --vix-csv vix_daily.csv
       (spot CSV: date,open,high,low,close at 60-minute bars; vix CSV: date,close daily)

  C) Self-test on synthetic prices (checks the mechanics only — NOT evidence):
       python3 backtest_credit_spread.py --synthetic

Useful flags
  --instrument NIFTY|SENSEX     --compare   (also runs random-direction baseline)
  --capital 150000              --out cs_backtest_trades.csv

WHAT THIS IS — AND IS NOT
  Kite does not provide history for expired option contracts, so option prices
  are MODELLED with Black-Scholes using India VIX (+ a simple put skew) as the
  implied volatility. Real fills, skew changes and liquidity will differ.
  Use it to compare rule variants and to see the shape of results (win rate,
  loss size, drawdown) — then confirm with 3–4 weeks of PAPER_TRADE.
  Slippage and Zerodha-style charges are deducted on every leg.
"""

import os, sys, ast, math, argparse, random
from datetime import datetime, timedelta, date, time as dtime
import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))

# ── Reuse the bot's exact ATR + HalfTrend implementation ─────────────────────
def _load_bot_functions():
    src = open(os.path.join(HERE, "main.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    want = {"ATR", "halftrend_tv"}
    code = [ast.get_source_segment(src, n) for n in tree.body
            if isinstance(n, ast.FunctionDef) and n.name in want]
    ns = {"np": np, "pd": pd}
    for c in code:
        exec(c, ns)
    missing = want - set(ns)
    if missing:
        sys.exit(f"Could not load {missing} from main.py")
    return ns["halftrend_tv"]

halftrend_tv = _load_bot_functions()

# ── Defaults = bot defaults (override with flags) ────────────────────────────
SPEC = {"NIFTY":  {"token": 256265, "step": 50,  "lot": 65, "width": 100},
        "SENSEX": {"token": 265,    "step": 100, "lot": 20, "width": 300}}
VIX_TOKEN = 264969
R = 0.065


# ── Black-Scholes ────────────────────────────────────────────────────────────
def ncdf(x): return 0.5 * (1 + math.erf(x / math.sqrt(2)))

def bs(S, K, T, sig, call):
    if T <= 0 or sig <= 0:
        return max(0.0, (S - K) if call else (K - S))
    d1 = (math.log(S / K) + (R + sig * sig / 2) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if call:
        return S * ncdf(d1) - K * math.exp(-R * T) * ncdf(d2)
    return K * math.exp(-R * T) * ncdf(-d2) - S * ncdf(-d1)

def delta(S, K, T, sig, call):
    d1 = (math.log(S / K) + (R + sig * sig / 2) * T) / (sig * math.sqrt(T))
    return ncdf(d1) if call else ncdf(d1) - 1

def iv_for(S, K, vix, call, put_skew, call_skew):
    """Simple smile: OTM puts priced richer, OTM calls slightly cheaper."""
    atm = vix / 100.0
    m = abs(K - S) / S                       # 0.03 = 3% OTM
    if call:
        return max(0.05, atm * (1 - call_skew * 10 * m))
    return max(0.05, atm * (1 + put_skew * 10 * m))


# ── Expiry calendar (approximate; ignores exchange holidays) ─────────────────
def expiry_weekday(inst, d):
    if inst == "NIFTY":
        return 1 if d >= date(2025, 9, 1) else 3            # Tue from Sep-2025, Thu before
    if d >= date(2025, 9, 1):  return 3                    # SENSEX Thu
    if d >= date(2025, 1, 1):  return 1                    # SENSEX Tue (2025)
    return 4                                               # SENSEX Fri (earlier)

def next_expiry(inst, d, min_dte, max_dte):
    for k in range(0, 15):
        e = d + timedelta(days=k)
        if e.weekday() == expiry_weekday(inst, e) and min_dte <= (e - d).days <= max_dte:
            return e
    return None

def years_to(exp, ts):
    end = datetime.combine(exp, dtime(15, 30))
    return max((end - ts).total_seconds(), 60) / (365 * 24 * 3600)


# ── Data loading ─────────────────────────────────────────────────────────────
def load_kite(inst, days):
    if os.environ.get("KITE_ACCESS_TOKEN"):
        from kiteconnect import KiteConnect
        k = KiteConnect(api_key=os.environ["KITE_API_KEY"])
        k.set_access_token(os.environ["KITE_ACCESS_TOKEN"])
    else:
        # Reuse the bot's own auto-login (Railway env vars). Importing main.py
        # logs in but does NOT start any trading loops.
        print("No KITE_ACCESS_TOKEN — logging in via main.py auto-login…")
        sys.path.insert(0, HERE)
        import main as _bot
        k = _bot.kite
    end = datetime.now().replace(second=0, microsecond=0)
    start = end - timedelta(days=days)
    rows, cur = [], start
    while cur < end:                                   # 60minute: ≤400 days per call
        nxt = min(cur + timedelta(days=390), end)
        rows += k.historical_data(SPEC[inst]["token"], cur, nxt, "60minute")
        cur = nxt + timedelta(minutes=1)
    spot = pd.DataFrame(rows)
    vix = pd.DataFrame(k.historical_data(VIX_TOKEN, start - timedelta(days=5), end, "day"))
    return spot, vix

def load_csv(spot_csv, vix_csv):
    return pd.read_csv(spot_csv), pd.read_csv(vix_csv)

def synthetic(days=730, seed=7, rv_ratio=0.85, mu=0.10):
    """GBM with 10%/yr drift, VIX-driven vol scaled by rv_ratio, rare jumps."""
    rng = np.random.default_rng(seed)
    rows, vix_rows, px, v = [], [], 22000.0, 14.0
    d = datetime(2024, 1, 1, 9, 15)
    while len(vix_rows) < days * 5 // 7:
        if d.weekday() < 5:
            v = float(np.clip(v + rng.normal(0, 0.6) + (14 - v) * 0.05, 9, 35))
            vix_rows.append({"date": d.date(), "close": v})
            for h in range(7):
                ts, dt, sv = d + timedelta(hours=h), 1 / (252 * 7), v / 100 * rv_ratio
                o = px
                r = (mu - 0.5 * sv * sv) * dt + rng.normal(0, sv * math.sqrt(dt))
                if rng.random() < 0.004:
                    r += rng.normal(0, sv * math.sqrt(dt) * 4)
                c = o * math.exp(r)
                ext = abs(rng.normal(0, sv * math.sqrt(dt) * 0.4))
                rows.append({"date": ts, "open": o, "high": max(o, c) * (1 + ext),
                             "low": min(o, c) * (1 - ext), "close": c})
                px = c
        d += timedelta(days=1)
    return pd.DataFrame(rows), pd.DataFrame(vix_rows)

def prep(spot, vix):
    spot = spot.copy()
    spot["date"] = pd.to_datetime(spot["date"]).dt.tz_localize(None)
    spot = spot.sort_values("date").reset_index(drop=True)
    vix = vix.copy()
    vix["d"] = pd.to_datetime(vix["date"]).dt.tz_localize(None).dt.date
    vmap = dict(zip(vix["d"], vix["close"].astype(float)))
    last, vv = 15.0, []
    for d in spot["date"].dt.date:
        last = vmap.get(d, last)          # previous known VIX if missing
        vv.append(last)
    spot["vix"] = vv
    return spot


# ── Charges (Zerodha-style, per spread round trip of 4 orders) ───────────────
def charges(sell_turnover, total_turnover):
    brokerage = 4 * 20 * 1.18
    stt = 0.001 * sell_turnover
    exch = 0.00035 * total_turnover * 1.18
    stamp = 0.00003 * (total_turnover - sell_turnover)
    return brokerage + stt + exch + stamp


# ── Backtest engine ──────────────────────────────────────────────────────────
def run(df, a, direction_mode="trend", seed=0):
    rng = random.Random(seed)
    sp = SPEC[a.instrument]
    lot, step, width = sp["lot"] * a.lots, sp["step"], a.width or sp["width"]
    ht = halftrend_tv(df[["open", "high", "low", "close"]].copy(), amplitude=a.amplitude, channel_deviation=2)
    trend = ht["trend"].to_numpy()
    trades, pos = [], None
    blocked_dir, last_exit_day = None, None
    slip = lambda p: max(a.slip_min, p * a.slip_pct)

    for i in range(3, len(df)):
        bar = df.iloc[i]
        ts_close = bar["date"] + timedelta(minutes=60)
        if ts_close.hour == 16:                       # 15:15 bar closes 15:30
            ts_close = ts_close.replace(hour=15, minute=30)
        S, vix, d = float(bar["close"]), float(bar["vix"]), ts_close.date()
        tr = "BULL" if trend[i] == 0 else "BEAR"
        stable = trend[i] == trend[i - 1]
        last_bar_of_day = (i + 1 >= len(df)) or (df.iloc[i + 1]["date"].date() != bar["date"].date())

        if pos:
            call = pos["call"]
            T = years_to(pos["exp"], ts_close)
            def legs(spot):
                return (bs(spot, pos["ks"], T, iv_for(spot, pos["ks"], vix, call, a.put_skew, a.call_skew), call),
                        bs(spot, pos["kl"], T, iv_for(spot, pos["kl"], vix, call, a.put_skew, a.call_skew), call))
            adverse = float(bar["high"]) if call else float(bar["low"])
            s_adv, l_adv = legs(adverse)
            s_cl, l_cl = legs(S)
            v_adv, v_close = s_adv - l_adv, s_cl - l_cl
            dte = (pos["exp"] - d).days
            reason, fill = None, None
            if a.stop_mult > 0 and v_adv >= pos["credit"] * (1 + a.stop_mult):
                reason, fill = "STOP", (s_adv, l_adv)       # filled at the bar's worst price
            elif v_close <= pos["credit"] * (1 - a.target):
                reason, fill = "TARGET", (s_cl, l_cl)
            elif a.exit_on_flip and tr != pos["tr_entry"]:
                reason, fill = "FLIP", (s_cl, l_cl)
            elif dte < 0 or (dte <= a.exit_dte and last_bar_of_day):
                reason, fill = "TIME", (s_cl, l_cl)
            if reason:
                xs = fill[0] + slip(fill[0])                  # buy back short, pay up
                xl = max(0.05, fill[1] - slip(fill[1]))       # sell hedge, receive less
                cost_close = xs - xl
                sell_to = (pos["entry_short"] + xl) * lot
                tot_to = (pos["entry_short"] + pos["entry_long"] + xs + xl) * lot
                ch = charges(sell_to, tot_to)
                pnl = (pos["credit"] - cost_close) * lot - ch
                trades.append({**{k: v for k, v in pos.items() if k != "call"},
                               "exit_time": ts_close, "reason": reason, "exit_val": round(cost_close, 2),
                               "pnl": round(pnl, 0), "charges": round(ch, 0)})
                if reason in ("STOP", "FLIP"):
                    blocked_dir = pos["tr_entry"]
                last_exit_day = d
                pos = None
            continue

        # ── entry ────────────────────────────────────────────────────────────
        hm = (ts_close.hour, ts_close.minute)
        if not ((10, 15) <= hm <= (14, 30)) or last_exit_day == d:
            continue
        if blocked_dir and blocked_dir != tr:
            blocked_dir = None
        # Same gates for both modes; only the spread direction differs
        if not stable or blocked_dir == tr:
            continue
        if direction_mode == "trend":
            dirn = tr
        elif direction_mode == "bull":
            dirn = "BULL"                       # put spreads only
        elif direction_mode == "bull_trend":
            if tr != "BULL":
                continue                        # put spreads only, and only in uptrends
            dirn = "BULL"
        else:
            dirn = rng.choice(["BULL", "BEAR"])
        if vix > a.vix_max:
            continue
        exp = next_expiry(a.instrument, d, a.min_dte, a.max_dte)
        if not exp:
            continue
        call = dirn == "BEAR"
        T = years_to(exp, ts_close)
        atm = round(S / step) * step
        best = None
        for n in range(1, 60):
            K = atm + n * step if call else atm - n * step
            if abs(K - S) > S * 0.07:
                break
            dl = abs(delta(S, K, T, iv_for(S, K, vix, call, a.put_skew, a.call_skew), call))
            if dl > a.short_delta + 0.07:
                continue
            if best is None or abs(dl - a.short_delta) < best[0]:
                best = (abs(dl - a.short_delta), K, dl)
        if not best:
            continue
        ks = best[1]
        kl = ks + width if call else ks - width
        p_s = bs(S, ks, T, iv_for(S, ks, vix, call, a.put_skew, a.call_skew), call)
        p_l = bs(S, kl, T, iv_for(S, kl, vix, call, a.put_skew, a.call_skew), call)
        e_s, e_l = p_s - slip(p_s), p_l + slip(p_l)      # sell lower, buy higher
        credit = e_s - e_l
        if credit < a.min_credit * width:
            continue
        pos = {"entry_time": ts_close, "dir": dirn, "tr_entry": tr, "call": call, "exp": exp, "spot": round(S, 1),
               "ks": ks, "kl": kl, "entry_short": round(e_s, 2), "entry_long": round(e_l, 2),
               "credit": round(credit, 2), "vix": round(vix, 1), "delta": round(best[2], 3)}
    return pd.DataFrame(trades)


def summarize(t, capital, label):
    if t.empty:
        print(f"\n{label}: no trades"); return {}
    eq = t["pnl"].cumsum()
    dd = (eq - eq.cummax()).min()
    w, l = t[t.pnl > 0], t[t.pnl <= 0]
    days = max(1, (t["exit_time"].max() - t["entry_time"].min()).days)
    yrs = days / 365
    out = {"trades": len(t), "win_rate": len(w) / len(t), "avg_win": w.pnl.mean() if len(w) else 0,
           "avg_loss": l.pnl.mean() if len(l) else 0, "total": t.pnl.sum(), "max_dd": dd,
           "per_year": t.pnl.sum() / yrs, "ret_pct_year": t.pnl.sum() / yrs / capital * 100,
           "charges": t.charges.sum()}
    print(f"\n━━ {label} ━━")
    print(f"Trades {out['trades']} over {days} days │ win rate {out['win_rate']:.0%}")
    print(f"Avg win ₹{out['avg_win']:,.0f} │ avg loss ₹{out['avg_loss']:,.0f} │ charges paid ₹{out['charges']:,.0f}")
    print(f"Total ₹{out['total']:,.0f} │ per year ₹{out['per_year']:,.0f} "
          f"({out['ret_pct_year']:.1f}% on ₹{capital:,.0f}) │ max drawdown ₹{out['max_dd']:,.0f}")
    print("Exit reasons:", t["reason"].value_counts().to_dict())
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instrument", default="NIFTY", choices=list(SPEC))
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--spot-csv"); ap.add_argument("--vix-csv")
    ap.add_argument("--synthetic", action="store_true")
    ap.add_argument("--rv-ratio", type=float, default=0.85,
                    help="synthetic only: realised vol / VIX (0.85 = options 15%% overpriced)")
    ap.add_argument("--compare", action="store_true", help="also run random-direction baseline")
    ap.add_argument("--capital", type=float, default=150000)
    ap.add_argument("--lots", type=int, default=1)
    ap.add_argument("--width", type=int, default=0)
    ap.add_argument("--short-delta", type=float, default=float(os.environ.get("CS_SHORT_DELTA", 0.15)))
    ap.add_argument("--min-credit", type=float, default=float(os.environ.get("CS_MIN_CREDIT_PCT", 0.10)))
    ap.add_argument("--target", type=float, default=float(os.environ.get("CS_TARGET_PCT", 0.80)))
    ap.add_argument("--stop-mult", type=float, default=float(os.environ.get("CS_STOP_MULT", 0)), help="0 = no stop (hedge caps loss)")
    ap.add_argument("--min-dte", type=int, default=int(os.environ.get("CS_MIN_DTE", 4)))
    ap.add_argument("--max-dte", type=int, default=int(os.environ.get("CS_MAX_DTE", 7)))
    ap.add_argument("--exit-dte", type=int, default=int(os.environ.get("CS_EXIT_DTE", 1)))
    ap.add_argument("--amplitude", type=int, default=int(os.environ.get("CS_HT_AMPLITUDE", 2)))
    ap.add_argument("--vix-max", type=float, default=float(os.environ.get("CS_VIX_MAX", 24)))
    ap.add_argument("--flip-exit", dest="exit_on_flip", action="store_true",
                    default=os.environ.get("CS_EXIT_ON_FLIP", "false").lower() == "true")
    ap.add_argument("--slip-pct", type=float, default=0.01, help="slippage per leg per side, fraction of premium")
    ap.add_argument("--slip-min", type=float, default=0.10, help="minimum slippage per leg per side (points)")
    ap.add_argument("--put-skew", type=float, default=0.15)
    ap.add_argument("--call-skew", type=float, default=0.05)
    ap.add_argument("--direction", default=os.environ.get("CS_DIRECTION", "trend"),
                    choices=["trend", "bull", "bull_trend"],
                    help="trend = put spreads in uptrend / call spreads in downtrend; "
                         "bull = put spreads always; bull_trend = put spreads only in uptrends")
    ap.add_argument("--out", default="cs_backtest_trades.csv")
    a = ap.parse_args()

    if a.synthetic:
        spot, vix = synthetic(a.days, rv_ratio=a.rv_ratio)
        print("⚠️  SYNTHETIC DATA — checks mechanics only, results mean nothing about real markets")
    elif a.spot_csv and a.vix_csv:
        spot, vix = load_csv(a.spot_csv, a.vix_csv)
    else:
        spot, vix = load_kite(a.instrument, a.days)
    df = prep(spot, vix)
    print(f"Data: {len(df)} hourly bars {df['date'].min():%Y-%m-%d} → {df['date'].max():%Y-%m-%d}")

    t = run(df, a, a.direction)
    res = summarize(t, a.capital, f"{a.instrument} credit spreads — direction={a.direction}")
    if not t.empty:
        t.to_csv(a.out, index=False)
        print(f"Trades saved → {a.out}")
        t["month"] = pd.to_datetime(t["exit_time"]).dt.to_period("M")
        print("\nMonthly P&L:\n" + t.groupby("month")["pnl"].sum().round(0).to_string())

    if a.compare:
        totals = []
        for s_ in range(40):
            r = run(df, a, "random", seed=s_)   # same gates, random direction
            totals.append(r["pnl"].sum() if not r.empty else 0)
        print(f"\n━━ RANDOM-DIRECTION baseline (40 runs, same entry timing & exits) ━━\n"
              f"Median total ₹{np.median(totals):,.0f} │ range ₹{min(totals):,.0f} … ₹{max(totals):,.0f}")
        if res:
            beat = sum(res["total"] > x for x in totals)
            verdict = ("trend direction is adding value" if beat >= 36 else
                       "trend direction NOT clearly better than random")
            print(f"Trend version beat {beat}/40 random runs — {verdict}")
            print("Also check: is the TREND total itself positive after charges? "
                  "Both must be true before trading real money.")


if __name__ == "__main__":
    main()
