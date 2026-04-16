"""
=============================================================================
  NIFTY HalfTrend Backtest — Option Premium Simulation
=============================================================================

What this does:
  1. Fetches 6 months of NIFTY 15-min data from Kite
  2. Runs the exact same halftrend_tv() indicator used by the live bot
  3. On every CALL arrow → simulate buying ATM CE option
     On every PUT  arrow → simulate buying ATM PE option
  4. Exit when:
       a) Opposite HalfTrend arrow fires (flip)
       b) Option premium drops 20% from entry (Hard SL)
       c) Trailing SL fires after ₹1000 peak profit (profit lock)
  5. Prints a clean summary: Win%, Avg P&L, Max Drawdown, etc.

Option Premium Simulation Model:
  - ATM CE/PE entry premium  ≈ NIFTY_spot × 0.008  (0.8% of spot)
    (empirical average for weekly NIFTY ATM option on entry day)
  - Premium tracking uses delta = 0.5 (ATM approximation):
      CE premium at t = entry_premium + (nifty_at_t - nifty_entry) × 0.5
      PE premium at t = entry_premium - (nifty_at_t - nifty_entry) × 0.5
  - Lot size = 65  (current NIFTY F&O lot size)
  - No brokerage / STT / slippage modelled (conservative estimate)

Usage:
  python backtest_halftrend.py
=============================================================================
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from kiteconnect import KiteConnect
import config   # your existing config.py with API_KEY and ACCESS_TOKEN

# ─────────────────────────────────────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────────────────────────────────────
LOT_SIZE        = 65      # NIFTY lot size
SL_PCT          = 0.20    # Hard SL: exit if premium drops 20% from entry
PROFIT_LOCK_MIN = 1000    # Start trailing after ₹1000 peak P&L (in rupees, 1 lot basis scaled to lots)
AMPLITUDE       = 2       # HalfTrend amplitude (must match live bot)
CHANNEL_DEV     = 2       # HalfTrend channel deviation
BACKTEST_DAYS   = 182     # ~6 months
NIFTY_TOKEN     = config.NIFTY_TOKEN   # 15-min historical data token

# ─────────────────────────────────────────────────────────────────────────────
# KITE CONNECT
# ─────────────────────────────────────────────────────────────────────────────
kite = KiteConnect(api_key=config.API_KEY)
kite.set_access_token(config.ACCESS_TOKEN)


# ─────────────────────────────────────────────────────────────────────────────
# INDICATOR: Wilder RMA ATR  (exact TradingView ta.atr())
# ─────────────────────────────────────────────────────────────────────────────
def ATR(df, period=100):
    high       = df["high"]
    low        = df["low"]
    close      = df["close"]
    prev_close = close.shift()

    tr = np.maximum(
        high - low,
        np.maximum((high - prev_close).abs(), (low - prev_close).abs())
    )
    return tr.ewm(alpha=1/period, min_periods=period, adjust=False).mean()


# ─────────────────────────────────────────────────────────────────────────────
# INDICATOR: HalfTrend (exact Pine Script v6 port)
# ─────────────────────────────────────────────────────────────────────────────
def halftrend_tv(df, amplitude=2, channel_deviation=2):
    df        = df.copy()
    n         = len(df)
    atr_series = ATR(df, 100)
    atr2_arr   = (atr_series / 2).to_numpy()
    dev_arr    = channel_deviation * atr2_arr

    high_arr  = df["high"].to_numpy(dtype=float)
    low_arr   = df["low"].to_numpy(dtype=float)
    close_arr = df["close"].to_numpy(dtype=float)

    hp_arr  = df["high"].rolling(window=amplitude).max().to_numpy(dtype=float)
    lp_arr  = df["low"].rolling(window=amplitude).min().to_numpy(dtype=float)
    hma_arr = df["high"].rolling(window=amplitude).mean().to_numpy(dtype=float)
    lma_arr = df["low"].rolling(window=amplitude).mean().to_numpy(dtype=float)

    trend        = np.zeros(n, dtype=float)
    nextTrend    = np.zeros(n, dtype=float)
    maxLowPrice  = np.zeros(n, dtype=float)
    minHighPrice = np.zeros(n, dtype=float)
    up           = np.zeros(n, dtype=float)
    down         = np.zeros(n, dtype=float)

    maxLowPrice[0]  = low_arr[0]
    minHighPrice[0] = high_arr[0]

    arrowUp_arr   = np.full(n, np.nan)
    arrowDown_arr = np.full(n, np.nan)

    for i in range(1, n):
        trend[i]        = trend[i-1]
        nextTrend[i]    = nextTrend[i-1]
        maxLowPrice[i]  = maxLowPrice[i-1]
        minHighPrice[i] = minHighPrice[i-1]
        up[i]           = up[i-1]
        down[i]         = down[i-1]

        if i < amplitude:
            continue

        close_i  = close_arr[i]
        low_prev = low_arr[i-1]
        high_prev= high_arr[i-1]
        hp       = hp_arr[i]
        lp       = lp_arr[i]
        hma      = hma_arr[i]
        lma      = lma_arr[i]
        atr2_i   = atr2_arr[i]

        if nextTrend[i] == 1:
            maxLowPrice[i] = max(lp, maxLowPrice[i])
            if hma < maxLowPrice[i] and close_i < low_prev:
                trend[i]        = 1
                nextTrend[i]    = 0
                minHighPrice[i] = hp
        else:
            minHighPrice[i] = min(hp, minHighPrice[i])
            if lma > minHighPrice[i] and close_i > high_prev:
                trend[i]       = 0
                nextTrend[i]   = 1
                maxLowPrice[i] = lp

        prev_trend = trend[i-1]
        prev_up    = up[i-1]
        prev_down  = down[i-1]

        if trend[i] == 0:
            if prev_trend != 0:
                up[i]          = prev_down if prev_down != 0 else down[i]
                arrowUp_arr[i] = up[i] - atr2_i
            else:
                up[i] = max(maxLowPrice[i], prev_up) if prev_up != 0 else maxLowPrice[i]
        else:
            if prev_trend != 1:
                down[i]          = prev_up if prev_up != 0 else up[i]
                arrowDown_arr[i] = down[i] + atr2_i
            else:
                down[i] = min(minHighPrice[i], prev_down) if prev_down != 0 else minHighPrice[i]

    ht_arr = np.where(trend == 0, up, down)
    df["trend"]     = trend
    df["ht"]        = ht_arr
    df["atr2"]      = atr2_arr
    df["atrHigh"]   = ht_arr + dev_arr
    df["atrLow"]    = ht_arr - dev_arr
    df["arrowUp"]   = arrowUp_arr
    df["arrowDown"] = arrowDown_arr

    trend_series = df["trend"]
    df["buy"]  = (~np.isnan(arrowUp_arr))   & (trend_series == 0) & (trend_series.shift(1) == 1)
    df["sell"] = (~np.isnan(arrowDown_arr)) & (trend_series == 1) & (trend_series.shift(1) == 0)

    return df


# ─────────────────────────────────────────────────────────────────────────────
# FETCH DATA
# ─────────────────────────────────────────────────────────────────────────────
def fetch_nifty_data(days=BACKTEST_DAYS):
    print(f"📥 Fetching NIFTY 15-min data for last {days} days...")
    to_date   = datetime.now()
    from_date = to_date - timedelta(days=days)

    data = kite.historical_data(NIFTY_TOKEN, from_date, to_date, "15minute")
    df   = pd.DataFrame(data)
    df.set_index("date", inplace=True)
    df.index = pd.to_datetime(df.index)

    # Keep only NIFTY market hours: 9:15 AM – 3:30 PM
    df = df.between_time("09:15", "15:30")
    print(f"✅ {len(df)} candles fetched  ({df.index[0].date()} → {df.index[-1].date()})")
    return df.reset_index()


# ─────────────────────────────────────────────────────────────────────────────
# OPTION PREMIUM SIMULATION
# ─────────────────────────────────────────────────────────────────────────────
def estimate_entry_premium(spot):
    """
    Approximate ATM option premium at entry.
    NIFTY ATM weekly option typically trades at ~0.8% of spot on entry.
    """
    return round(spot * 0.008, 2)


def option_premium_at(entry_premium, entry_spot, current_spot, signal):
    """
    Approximate current option premium using delta = 0.5 (ATM).
    CE: premium rises when spot rises
    PE: premium rises when spot falls
    Premium floored at 0.05 (options never go negative).
    """
    if signal == "CALL":
        prem = entry_premium + (current_spot - entry_spot) * 0.5
    else:
        prem = entry_premium - (current_spot - entry_spot) * 0.5
    return max(prem, 0.05)


# ─────────────────────────────────────────────────────────────────────────────
# BACKTEST ENGINE
# ─────────────────────────────────────────────────────────────────────────────
def run_backtest(df, ht_df):
    trades     = []
    in_trade   = False
    signal     = None
    entry_idx  = None
    entry_spot = None
    entry_prem = None
    sl_prem    = None
    peak_pnl   = 0

    print(f"\n🔄 Running backtest on {len(ht_df)} bars...\n")

    for i in range(2, len(ht_df) - 1):
        bar      = ht_df.iloc[i]
        next_bar = ht_df.iloc[i + 1]   # entry on NEXT bar open (closed candle rule)

        # ── ENTRY ─────────────────────────────────────────────────────────
        if not in_trade:
            if bar["buy"]:
                signal     = "CALL"
                entry_spot = next_bar["open"]
                entry_prem = estimate_entry_premium(entry_spot)
                sl_prem    = round(entry_prem * (1 - SL_PCT), 2)
                entry_idx  = i + 1
                entry_time = ht_df.index[entry_idx] if hasattr(ht_df, 'index') else i+1
                peak_pnl   = 0
                in_trade   = True

            elif bar["sell"]:
                signal     = "PUT"
                entry_spot = next_bar["open"]
                entry_prem = estimate_entry_premium(entry_spot)
                sl_prem    = round(entry_prem * (1 + SL_PCT), 2)   # PE: SL above entry
                entry_idx  = i + 1
                entry_time = ht_df.index[entry_idx] if hasattr(ht_df, 'index') else i+1
                peak_pnl   = 0
                in_trade   = True

            continue

        # ── IN TRADE — evaluate exit conditions ───────────────────────────
        current_spot = bar["close"]
        current_prem = option_premium_at(entry_prem, entry_spot, current_spot, signal)

        # P&L in rupees (for current lot)
        if signal == "CALL":
            pnl_pts = current_prem - entry_prem
        else:
            pnl_pts = entry_prem - current_prem   # PE: profit when spot falls

        current_pnl = pnl_pts * LOT_SIZE
        peak_pnl    = max(peak_pnl, current_pnl)

        exit_reason = None
        exit_prem   = current_prem

        # 1. Hard SL — premium dropped 20% from entry
        if signal == "CALL" and current_prem <= sl_prem:
            exit_reason = "HARD_SL"
        elif signal == "PUT" and current_prem <= entry_prem * (1 - SL_PCT):
            exit_reason = "HARD_SL"

        # 2. Profit lock — after ₹1000 peak, lock 50%/70%/80%
        if exit_reason is None and peak_pnl >= PROFIT_LOCK_MIN:
            if peak_pnl < 1500:
                lock_pct = 0.50
            elif peak_pnl < 3000:
                lock_pct = 0.70
            else:
                lock_pct = 0.80

            lock_level = peak_pnl * lock_pct
            if current_pnl < lock_level:
                exit_reason = f"PROFIT_LOCK_{int(lock_pct*100)}%"

        # 3. HalfTrend flip — opposite arrow on closed candle
        if exit_reason is None:
            if signal == "CALL" and bar["sell"]:
                exit_reason = "HT_FLIP"
            elif signal == "PUT" and bar["buy"]:
                exit_reason = "HT_FLIP"

        if exit_reason:
            # Exit at next bar open
            exit_spot = next_bar["open"]
            exit_prem = option_premium_at(entry_prem, entry_spot, exit_spot, signal)

            if signal == "CALL":
                final_pnl = (exit_prem - entry_prem) * LOT_SIZE
            else:
                final_pnl = (entry_prem - exit_prem) * LOT_SIZE

            exit_time = ht_df.index[i+1] if hasattr(ht_df, 'index') else i+1

            trades.append({
                "entry_time"  : entry_time,
                "exit_time"   : exit_time,
                "signal"      : signal,
                "entry_spot"  : round(entry_spot, 2),
                "exit_spot"   : round(exit_spot, 2),
                "entry_prem"  : round(entry_prem, 2),
                "exit_prem"   : round(exit_prem, 2),
                "pnl_pts"     : round(exit_prem - entry_prem if signal == "CALL" else entry_prem - exit_prem, 2),
                "pnl_rs"      : round(final_pnl, 2),
                "exit_reason" : exit_reason,
                "peak_pnl"    : round(peak_pnl, 2),
            })

            in_trade = False
            signal   = None

    return pd.DataFrame(trades)


# ─────────────────────────────────────────────────────────────────────────────
# REPORT
# ─────────────────────────────────────────────────────────────────────────────
def print_report(trades_df):
    if trades_df.empty:
        print("❌ No trades found in backtest period.")
        return

    total     = len(trades_df)
    wins      = int((trades_df["pnl_rs"] > 0).sum())
    losses    = total - wins
    win_rate  = wins / total * 100

    gross_profit = trades_df[trades_df["pnl_rs"] > 0]["pnl_rs"].sum()
    gross_loss   = trades_df[trades_df["pnl_rs"] <= 0]["pnl_rs"].sum()
    net_pnl      = trades_df["pnl_rs"].sum()

    avg_win  = trades_df[trades_df["pnl_rs"] > 0]["pnl_rs"].mean() if wins > 0  else 0
    avg_loss = trades_df[trades_df["pnl_rs"] <= 0]["pnl_rs"].mean() if losses > 0 else 0

    profit_factor = abs(gross_profit / gross_loss) if gross_loss != 0 else float("inf")

    # Max drawdown (running equity curve)
    equity       = trades_df["pnl_rs"].cumsum()
    rolling_peak = equity.cummax()
    drawdown     = equity - rolling_peak
    max_dd       = drawdown.min()

    # Consecutive stats
    streaks    = []
    cur_streak = 0
    cur_sign   = None
    max_win_streak  = 0
    max_loss_streak = 0
    for pnl in trades_df["pnl_rs"]:
        s = "W" if pnl > 0 else "L"
        if s == cur_sign:
            cur_streak += 1
        else:
            cur_sign   = s
            cur_streak = 1
        if s == "W":
            max_win_streak  = max(max_win_streak,  cur_streak)
        else:
            max_loss_streak = max(max_loss_streak, cur_streak)

    # Exit reason breakdown
    exit_counts = trades_df["exit_reason"].value_counts()

    # Monthly breakdown
    trades_df["month"] = pd.to_datetime(trades_df["entry_time"]).dt.to_period("M")
    monthly = trades_df.groupby("month")["pnl_rs"].sum()

    sep = "=" * 55

    print(f"\n{sep}")
    print(f"  📊 NIFTY HalfTrend Backtest — 6 Month Summary")
    print(f"{sep}")
    print(f"  Period      : {pd.to_datetime(trades_df['entry_time'].iloc[0]).strftime('%d %b %Y')} "
          f"→ {pd.to_datetime(trades_df['entry_time'].iloc[-1]).strftime('%d %b %Y')}")
    print(f"  Instrument  : NIFTY  |  Lot size: {LOT_SIZE}")
    print(f"  Timeframe   : 15-min  |  Indicator: HalfTrend (amplitude=2, dev=2)")
    print(f"{sep}")
    print(f"  Total trades    : {total}")
    print(f"  Wins            : {wins}   ({win_rate:.1f}%)")
    print(f"  Losses          : {losses}   ({100-win_rate:.1f}%)")
    print(f"  Win Rate        : {win_rate:.1f}%")
    print(f"{sep}")
    print(f"  Net P&L         : ₹{net_pnl:>10,.0f}")
    print(f"  Gross Profit    : ₹{gross_profit:>10,.0f}")
    print(f"  Gross Loss      : ₹{gross_loss:>10,.0f}")
    print(f"  Avg Win/trade   : ₹{avg_win:>10,.0f}")
    print(f"  Avg Loss/trade  : ₹{avg_loss:>10,.0f}")
    print(f"  Profit Factor   : {profit_factor:>10.2f}  (>1.5 = good)")
    print(f"  Max Drawdown    : ₹{max_dd:>10,.0f}")
    print(f"{sep}")
    print(f"  Max Win streak  : {max_win_streak}")
    print(f"  Max Loss streak : {max_loss_streak}")
    print(f"{sep}")
    print(f"  Exit Reasons:")
    for reason, count in exit_counts.items():
        pct = count / total * 100
        print(f"    {reason:<22}: {count:>3}  ({pct:.1f}%)")
    print(f"{sep}")
    print(f"  Monthly P&L:")
    for month, pnl in monthly.items():
        bar_str = "█" * int(abs(pnl) / 500)
        sign    = "+" if pnl >= 0 else ""
        print(f"    {str(month):<8}: {sign}₹{pnl:>8,.0f}  {bar_str}")
    print(f"{sep}")

    # Signal breakdown
    call_trades = trades_df[trades_df["signal"] == "CALL"]
    put_trades  = trades_df[trades_df["signal"] == "PUT"]

    print(f"  CALL trades : {len(call_trades)}  |  P&L: ₹{call_trades['pnl_rs'].sum():,.0f}  "
          f"|  Win: {int((call_trades['pnl_rs']>0).sum())}/{len(call_trades)}")
    print(f"  PUT  trades : {len(put_trades)}  |  P&L: ₹{put_trades['pnl_rs'].sum():,.0f}  "
          f"|  Win: {int((put_trades['pnl_rs']>0).sum())}/{len(put_trades)}")
    print(f"{sep}\n")

    print("📌 Assumptions used in this simulation:")
    print(f"   • ATM option entry premium  = NIFTY_spot × 0.008  (0.8% of spot)")
    print(f"   • Premium tracking          = delta 0.5 approximation (ATM)")
    print(f"   • Hard SL                   = 20% drop in option premium")
    print(f"   • Profit lock               = 50%/70%/80% of peak after ₹{PROFIT_LOCK_MIN:,}")
    print(f"   • Exit on HalfTrend flip    = opposite arrow on closed candle")
    print(f"   • No brokerage/STT/slippage modelled\n")


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    # 1. Fetch data
    df = fetch_nifty_data(days=BACKTEST_DAYS)

    # 2. Run HalfTrend indicator
    print("⚙️  Computing HalfTrend indicator...")
    ht_df = halftrend_tv(df, amplitude=AMPLITUDE, channel_deviation=CHANNEL_DEV)
    ht_df = ht_df.reset_index(drop=True)

    signals_found = int(ht_df["buy"].sum() + ht_df["sell"].sum())
    print(f"✅ HalfTrend computed  |  Arrows found: {signals_found} "
          f"(CALL: {int(ht_df['buy'].sum())}  PUT: {int(ht_df['sell'].sum())})")

    # 3. Run backtest
    trades_df = run_backtest(df, ht_df)

    # 4. Print report
    print_report(trades_df)