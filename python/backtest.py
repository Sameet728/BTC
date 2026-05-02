# ╔══════════════════════════════════════════════════════════════════╗
# ║         STRATEGY BACKTEST — EMA 13/34/89 · RSI · ATR · VOL     ║
# ║         Local runner  |  python main.py                        ║
# ╚══════════════════════════════════════════════════════════════════╝

# ── 1. IMPORTS ───────────────────────────────────────────────────────
import os, time, requests
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec
from dotenv import load_dotenv
from ta.trend import EMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange

load_dotenv()

# ══════════════════════════════════════════════════════════════════════
#  ★  USER SETTINGS  — edit here (or override via .env)
# ══════════════════════════════════════════════════════════════════════
SYMBOL          = os.getenv("SYMBOL",         "BTCUSDT")
INTERVAL        = os.getenv("INTERVAL",       "1h")       # 15m | 1h | 4h | 1d
DURATION        = os.getenv("DURATION",       "5y")       # 30d | 6m | 1y | 3y | 5y

INITIAL_BALANCE = float(os.getenv("INITIAL_BALANCE", "1000"))   # USD
RISK_PER_TRADE  = float(os.getenv("RISK_PER_TRADE",  "0.01"))  # 1 % of balance per trade
RR_RATIO        = float(os.getenv("RR_RATIO",        "2.5"))   # reward : risk
FEE             = float(os.getenv("FEE",             "0.0005"))  # 0.05 % per side
COOLDOWN        = int(os.getenv("COOLDOWN",          "3"))      # min candles between two signals
# ══════════════════════════════════════════════════════════════════════

# ── 2. HELPERS ────────────────────────────────────────────────────────
def parse_duration(dur: str) -> int:
    now = datetime.utcnow()
    unit = dur[-1]
    val  = int(dur[:-1])
    days = val * {"d": 1, "m": 30, "y": 365}[unit]
    return int((now - timedelta(days=days)).timestamp() * 1000)

# ── Bybit base URL & interval mapping ─────────────────────────────────
BYBIT_BASE_URL = "https://api.bybit.com/v5/market/kline"
_INTERVAL_MAP = {
    "1m":  "1",   "3m":  "3",   "5m":  "5",   "15m": "15",
    "30m": "30",  "1h":  "60",  "2h":  "120",  "4h":  "240",
    "6h":  "360", "12h": "720", "1d":  "D",    "1w":  "W",
    "1M":  "M",
}

def bybit_interval(interval: str) -> str:
    mapped = _INTERVAL_MAP.get(interval)
    if mapped is None:
        raise ValueError(f"Unsupported interval '{interval}'.")
    return mapped

def fetch_all_candles(symbol, interval, duration) -> pd.DataFrame:
    bv_interval = bybit_interval(interval)
    start_ms    = parse_duration(duration)
    end_ms      = int(datetime.utcnow().timestamp() * 1_000)
    all_rows    = []

    while True:
        resp = requests.get(BYBIT_BASE_URL, params={
            "category": "linear",
            "symbol":   symbol,
            "interval": bv_interval,
            "start":    start_ms,
            "end":      end_ms,
            "limit":    1000,
        }, timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        if payload.get("retCode") != 0:
            raise RuntimeError(f"Bybit API error {payload.get('retCode')}: {payload.get('retMsg')}")
        rows = payload["result"]["list"]
        if not rows:
            break
        rows_asc = list(reversed(rows))
        all_rows.extend(rows_asc)
        print(f"\r  Fetched {len(all_rows):,} candles…", end="")
        if len(rows) < 1000:
            break
        oldest_ts = int(rows_asc[0][0])
        if oldest_ts <= start_ms:
            break
        end_ms = oldest_ts - 1
        time.sleep(0.2)

    print(f"\r  ✅ Total candles fetched: {len(all_rows):,}          ")
    df = pd.DataFrame(all_rows, columns=[
        "time", "open", "high", "low", "close", "volume", "turnover",
    ]).astype(float)
    df["datetime"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    df = df.sort_values("time").reset_index(drop=True)
    return df

# ── 3. INDICATORS ─────────────────────────────────────────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df["ema13"]  = EMAIndicator(df["close"], 13).ema_indicator()
    df["ema34"]  = EMAIndicator(df["close"], 34).ema_indicator()
    df["ema89"]  = EMAIndicator(df["close"], 89).ema_indicator()
    df["rsi"]    = RSIIndicator(df["close"], 14).rsi()
    df["atr"]    = AverageTrueRange(df["high"], df["low"], df["close"], 14).average_true_range()
    df["atr_ma"] = df["atr"].rolling(14).mean()
    df["vol_ma"] = df["volume"].rolling(20).mean()
    return df

# ── 4. SIGNAL  (exact same logic as live runner) ──────────────────────
def get_signal(row) -> str:
    if pd.isna(row["atr_ma"]) or row["atr"] < row["atr_ma"]:
        return "HOLD"

    vol_ok     = row["volume"] > row["vol_ma"]
    trend_up   = row["ema34"] > row["ema89"]
    trend_down = row["ema34"] < row["ema89"]

    if trend_up:
        score = sum([
            row["ema13"] > row["ema34"],
            row["close"] > row["ema34"],
            row["rsi"]   > 55,
            vol_ok,
        ])
        if score >= 4:
            return "BUY"

    if trend_down:
        score = sum([
            row["ema13"] < row["ema34"],
            row["close"] < row["ema34"],
            row["rsi"]   < 45,
            vol_ok,
        ])
        if score >= 4:
            return "SELL"

    return "HOLD"

# ── 5. BACKTEST ───────────────────────────────────────────────────────
def backtest(df: pd.DataFrame):
    balance          = INITIAL_BALANCE
    position         = None
    trades           = []
    equity           = []
    last_signal_tick = -(COOLDOWN + 1)

    for i in range(100, len(df)):
        row    = df.iloc[i]
        signal = get_signal(row)
        equity.append(balance)

        # ── manage open position ──────────────────────────────────────
        if position:
            h, l = row["high"], row["low"]
            reason = price_exit = None

            if position["side"] == "BUY":
                if l <= position["sl"]:   reason, price_exit = "SL", position["sl"]
                elif h >= position["tp"]: reason, price_exit = "TP", position["tp"]
            else:
                if h >= position["sl"]:   reason, price_exit = "SL", position["sl"]
                elif l <= position["tp"]: reason, price_exit = "TP", position["tp"]

            if reason:
                multiplier = RR_RATIO if reason == "TP" else -1.0
                pnl        = position["risk"] * multiplier
                pnl       -= position["risk"] * FEE * 2   # open + close fee
                balance   += pnl

                trades.append({
                    "side":         position["side"],
                    "entry_time":   position["entry_time"],
                    "exit_time":    row["datetime"],
                    "entry":        position["entry"],
                    "exit":         price_exit,
                    "sl":           position["sl"],
                    "tp":           position["tp"],
                    "risk_usd":     round(position["risk"], 4),
                    "result":       reason,
                    "pnl_usd":      round(pnl, 4),
                    "balance":      round(balance, 4),
                })
                position = None

        # ── open new position ─────────────────────────────────────────
        if position is None and signal in ("BUY", "SELL"):
            if i - last_signal_tick >= COOLDOWN:
                entry   = row["close"]
                atr     = row["atr"]
                sl_dist = atr * 1.5
                tp_dist = atr * RR_RATIO
                risk    = balance * RISK_PER_TRADE

                position = {
                    "side":       signal,
                    "entry":      entry,
                    "sl":         entry - sl_dist if signal == "BUY" else entry + sl_dist,
                    "tp":         entry + tp_dist if signal == "BUY" else entry - tp_dist,
                    "risk":       risk,
                    "entry_time": row["datetime"],
                }
                last_signal_tick = i

    return pd.DataFrame(trades), equity

# ── 6. STATS ──────────────────────────────────────────────────────────
def print_stats(trades_df: pd.DataFrame, equity: list):
    final  = equity[-1] if equity else INITIAL_BALANCE
    total  = len(trades_df)
    wins   = (trades_df["result"] == "TP").sum()
    losses = (trades_df["result"] == "SL").sum()
    wr     = wins / total * 100 if total else 0
    pf     = (wins * RR_RATIO) / losses if losses else float("inf")
    ret    = (final - INITIAL_BALANCE) / INITIAL_BALANCE * 100

    eq  = pd.Series(equity)
    dd  = (eq - eq.cummax()) / eq.cummax() * 100
    mdd = dd.min()

    # Sharpe (hourly → annualised)
    ret_series = eq.pct_change().dropna()
    periods    = {"15m": 35040, "1h": 8760, "4h": 2190, "1d": 365}.get(INTERVAL, 8760)
    sharpe     = (ret_series.mean() / ret_series.std() * np.sqrt(periods)
                  if ret_series.std() > 0 else 0)

    print("\n" + "═"*52)
    print(f"  {'BACKTEST RESULTS':^50}")
    print("═"*52)
    print(f"  Symbol       : {SYMBOL}  ({INTERVAL})  [{DURATION}]")
    print(f"  Start Balance: ${INITIAL_BALANCE:,.2f}")
    print(f"  Final Balance: ${final:,.2f}")
    print(f"  Net Return   : {ret:+.2f}%")
    print(f"  Max Drawdown : {mdd:.2f}%")
    print(f"  Sharpe Ratio : {sharpe:.2f}")
    print("─"*52)
    print(f"  Total Trades : {total}")
    print(f"  Wins / Losses: {wins} / {losses}")
    print(f"  Win Rate     : {wr:.1f}%")
    print(f"  Profit Factor: {pf:.2f}")
    print("═"*52)

    # ── Monthly breakdown — PERCENTAGE returns ──────────────────────
    if not trades_df.empty:
        trades_df = trades_df.copy()
        trades_df["month"] = trades_df["exit_time"].dt.to_period("M")

        monthly_rows = []
        for month, grp in trades_df.groupby("month"):
            grp = grp.sort_values("exit_time")
            idx_first = grp.index[0]
            bal_before = grp.loc[idx_first, "balance"] - grp.loc[idx_first, "pnl_usd"]
            total_pnl  = grp["pnl_usd"].sum()
            pct_return = (total_pnl / bal_before * 100) if bal_before != 0 else 0
            monthly_rows.append({"Month": str(month), "Return (%)": round(pct_return, 2)})

        monthly = pd.DataFrame(monthly_rows)
        print("\n  📅 Monthly Returns (%):")
        print(monthly.to_string(index=False))

        # ── Yearly breakdown — PERCENTAGE returns ───────────────────
        trades_df["year"] = trades_df["exit_time"].dt.to_period("Y")
        yearly_rows = []
        for year, grp in trades_df.groupby("year"):
            grp = grp.sort_values("exit_time")
            idx_first  = grp.index[0]
            bal_before = grp.loc[idx_first, "balance"] - grp.loc[idx_first, "pnl_usd"]
            total_pnl  = grp["pnl_usd"].sum()
            pct_return = (total_pnl / bal_before * 100) if bal_before != 0 else 0
            yearly_rows.append({"Year": str(year), "Return (%)": round(pct_return, 2)})

        yearly = pd.DataFrame(yearly_rows)
        print("\n  📆 Yearly Returns (%):")
        print(yearly.to_string(index=False))

    return trades_df  # return with month/year columns added

# ── 7. CSV EXPORT ─────────────────────────────────────────────────────
def export_csv(trades_df: pd.DataFrame):
    if trades_df.empty:
        print("  ⚠️  No trades to export.")
        return
    fname = f"trade_history_{SYMBOL}_{INTERVAL}_{DURATION}.csv"
    export = trades_df.copy()
    # Add pnl_pct column: pnl as % of balance before the trade
    export["pnl_pct"] = (export["pnl_usd"] / (export["balance"] - export["pnl_usd"]) * 100).round(4)
    export.to_csv(fname, index=False)
    print(f"\n  💾 Trade history saved → {fname}")
    print(f"     Columns: {list(export.columns)}")

# ── 8. CHARTS ─────────────────────────────────────────────────────────
def plot_results(trades_df: pd.DataFrame, equity: list, df_raw: pd.DataFrame):
    fig = plt.figure(figsize=(18, 18))
    gs  = gridspec.GridSpec(4, 2, figure=fig, hspace=0.50, wspace=0.35)

    eq = pd.Series(equity)
    dd = (eq - eq.cummax()) / eq.cummax() * 100

    # ① Equity curve
    ax1 = fig.add_subplot(gs[0, :])
    ax1.plot(eq.values, color="#00c897", linewidth=1.5, label="Equity")
    ax1.fill_between(range(len(eq)), INITIAL_BALANCE, eq.values,
                     where=(eq.values >= INITIAL_BALANCE),
                     alpha=0.15, color="#00c897")
    ax1.fill_between(range(len(eq)), INITIAL_BALANCE, eq.values,
                     where=(eq.values < INITIAL_BALANCE),
                     alpha=0.15, color="#ff4c4c")
    ax1.axhline(INITIAL_BALANCE, color="white", linewidth=0.6, linestyle="--", alpha=0.5)
    ax1.set_title("Equity Curve", color="white", fontsize=13, pad=8)
    ax1.set_facecolor("#0d1117"); ax1.tick_params(colors="grey")
    for sp in ax1.spines.values(): sp.set_color("#333")

    # ② Drawdown
    ax2 = fig.add_subplot(gs[1, :])
    ax2.fill_between(range(len(dd)), dd.values, 0, color="#ff4c4c", alpha=0.6)
    ax2.set_title("Drawdown %", color="white", fontsize=13, pad=8)
    ax2.set_facecolor("#0d1117"); ax2.tick_params(colors="grey")
    for sp in ax2.spines.values(): sp.set_color("#333")

    if not trades_df.empty:
        # ③ Monthly Returns % bar chart
        ax3 = fig.add_subplot(gs[2, :])
        trades_df = trades_df.copy()
        trades_df["month"] = trades_df["exit_time"].dt.to_period("M")

        monthly_pct = []
        for month, grp in trades_df.groupby("month"):
            grp = grp.sort_values("exit_time")
            idx_first  = grp.index[0]
            bal_before = grp.loc[idx_first, "balance"] - grp.loc[idx_first, "pnl_usd"]
            total_pnl  = grp["pnl_usd"].sum()
            pct_return = (total_pnl / bal_before * 100) if bal_before != 0 else 0
            monthly_pct.append((str(month), round(pct_return, 2)))

        months_df = pd.DataFrame(monthly_pct, columns=["month", "pct"])
        m_colors = ["#00c897" if v >= 0 else "#ff4c4c" for v in months_df["pct"]]
        bars = ax3.bar(range(len(months_df)), months_df["pct"], color=m_colors, width=0.7)
        ax3.axhline(0, color="white", linewidth=0.5)
        ax3.set_xticks(range(len(months_df)))
        ax3.set_xticklabels(months_df["month"], rotation=45, ha="right", fontsize=7, color="grey")
        ax3.set_ylabel("%", color="grey", fontsize=10)
        ax3.set_title("Monthly Returns (%)", color="white", fontsize=12, pad=8)
        ax3.set_facecolor("#0d1117"); ax3.tick_params(colors="grey")
        for sp in ax3.spines.values(): sp.set_color("#333")

        # Add value labels on bars
        for bar, val in zip(bars, months_df["pct"]):
            if abs(val) > 0.1:
                ax3.text(bar.get_x() + bar.get_width() / 2,
                         bar.get_height() + (0.1 if val >= 0 else -0.3),
                         f"{val:.1f}%", ha="center", va="bottom" if val >= 0 else "top",
                         fontsize=6, color="white", alpha=0.8)

        # ④ Yearly Returns % bar chart
        ax4 = fig.add_subplot(gs[3, 0])
        trades_df["year"] = trades_df["exit_time"].dt.to_period("Y")

        yearly_pct = []
        for year, grp in trades_df.groupby("year"):
            grp = grp.sort_values("exit_time")
            idx_first  = grp.index[0]
            bal_before = grp.loc[idx_first, "balance"] - grp.loc[idx_first, "pnl_usd"]
            total_pnl  = grp["pnl_usd"].sum()
            pct_return = (total_pnl / bal_before * 100) if bal_before != 0 else 0
            yearly_pct.append((str(year), round(pct_return, 2)))

        years_df = pd.DataFrame(yearly_pct, columns=["year", "pct"])
        y_colors = ["#00c897" if v >= 0 else "#ff4c4c" for v in years_df["pct"]]
        y_bars   = ax4.bar(range(len(years_df)), years_df["pct"], color=y_colors, width=0.6)
        ax4.axhline(0, color="white", linewidth=0.5)
        ax4.set_xticks(range(len(years_df)))
        ax4.set_xticklabels(years_df["year"], rotation=30, ha="right", fontsize=9, color="grey")
        ax4.set_ylabel("%", color="grey", fontsize=10)
        ax4.set_title("Yearly Returns (%)", color="white", fontsize=12, pad=8)
        ax4.set_facecolor("#0d1117"); ax4.tick_params(colors="grey")
        for sp in ax4.spines.values(): sp.set_color("#333")

        for bar, val in zip(y_bars, years_df["pct"]):
            ax4.text(bar.get_x() + bar.get_width() / 2,
                     bar.get_height() + (0.5 if val >= 0 else -1.5),
                     f"{val:.1f}%", ha="center", va="bottom" if val >= 0 else "top",
                     fontsize=9, color="white", fontweight="bold")

        # ⑤ PnL per trade bar
        ax5 = fig.add_subplot(gs[3, 1])
        colors = ["#00c897" if p > 0 else "#ff4c4c" for p in trades_df["pnl_usd"]]
        ax5.bar(range(len(trades_df)), trades_df["pnl_usd"], color=colors, width=0.8)
        ax5.axhline(0, color="white", linewidth=0.5)
        ax5.set_title("PnL per Trade ($)", color="white", fontsize=12, pad=8)
        ax5.set_facecolor("#0d1117"); ax5.tick_params(colors="grey")
        for sp in ax5.spines.values(): sp.set_color("#333")

    fig.patch.set_facecolor("#0d1117")
    plt.suptitle(
        f"{SYMBOL} · {INTERVAL} · {DURATION}  |  RR {RR_RATIO}  Risk {RISK_PER_TRADE*100:.0f}%",
        color="white", fontsize=14, y=0.998,
    )
    plt.show()

# ── 9. RUN ────────────────────────────────────────────────────────────
def main():
    print(f"Fetching {SYMBOL} {INTERVAL} data for {DURATION}…")
    df_raw  = fetch_all_candles(SYMBOL, INTERVAL, DURATION)
    df_raw  = add_indicators(df_raw)

    print("Running backtest…")
    trades_df, equity = backtest(df_raw)

    trades_df = print_stats(trades_df, equity)
    export_csv(trades_df)
    plot_results(trades_df, equity, df_raw)

if __name__ == "__main__":
    main()