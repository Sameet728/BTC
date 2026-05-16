import sys
import os
import time
import requests
import numpy as np
import pandas as pd
import warnings
warnings.simplefilter(action='ignore', category=pd.errors.PerformanceWarning)
warnings.simplefilter(action='ignore', category=DeprecationWarning)
pd.set_option('future.no_silent_downcasting', True)
from datetime import datetime, timedelta, timezone

# Settings
SYMBOL = "BTCUSDT"
INTERVAL = "1h"
DURATION = "5y"
INITIAL_BALANCE = 1000.0
TAKER_FEE = 0.0005
MAKER_FEE = 0.0002
SLIPPAGE = 0.0005
COOLDOWN = 3
RISK_PCT = 0.02
SL_ATR_MULT = 3.0

def ema(series, period): return series.ewm(span=period, adjust=False).mean()
def rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).ewm(alpha=1/period, adjust=False).mean()
    loss = (-delta.where(delta < 0, 0)).ewm(alpha=1/period, adjust=False).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))
def atr(high, low, close, period=14):
    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low  - prev_close).abs()], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

def _compute_tf_indicators(df, prefix):
    out = pd.DataFrame(index=df.index)
    c = df["close"]; h = df["high"]; l = df["low"]
    out[f"{prefix}atr_14"] = atr(h, l, c, 14)
    out[f"{prefix}rsi_14"] = rsi(c, 14)
    out[f"{prefix}ema_200"] = ema(c, 200)
    return out

def _resample_ohlcv(df_1h, rule):
    df = df_1h.set_index("datetime").copy()
    return df.resample(rule).agg({"open":"first","high":"max","low":"min","close":"last","volume":"sum"}).dropna(subset=["close"])

def compute_all_indicators(df):
    df = df.copy()
    if "datetime" not in df.columns:
        df["datetime"] = pd.to_datetime(df.index, utc=True)
    else:
        df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
        
    result = df.copy()
    tf_1h = _compute_tf_indicators(df, "tf_1h_")
    result = pd.concat([result, tf_1h], axis=1)

    for tf_label, resample_rule in [("1d","1D")]:
        try:
            htf_ohlcv = _resample_ohlcv(df, resample_rule)
            htf_indicators = _compute_tf_indicators(htf_ohlcv, f"tf_{tf_label}_")
            htf_indicators.index = pd.to_datetime(htf_indicators.index, utc=True)
            htf_indicators = htf_indicators.shift(1) # PREVENT LOOKAHEAD BIAS
            df_dt_index = pd.to_datetime(result["datetime"], utc=True)
            htf_reindexed = htf_indicators.reindex(df_dt_index).ffill()
            htf_reindexed.index = result.index
            result = pd.concat([result, htf_reindexed], axis=1)
        except Exception as e:
            print(f"Failed to compute {tf_label}: {e}")

    result = result.iloc[200:].reset_index(drop=True)
    return result

def generate_signals(df, rsi_buy, rsi_sell):
    df = df.copy()
    signals = pd.Series(0.0, index=df.index)
    rsi_1h = df["tf_1h_rsi_14"]
    rsi_prev = rsi_1h.shift(1)
    close = df["close"]
    ema200_1d = df["tf_1d_ema_200"]
    
    # Enter Long when RSI drops below rsi_buy during an uptrend
    buy_mask = (rsi_1h < rsi_buy) & (rsi_prev >= rsi_buy) & (close > ema200_1d)
    signals[buy_mask] = 1.0

    # Sell Long when RSI crosses above rsi_sell
    sell_mask = (rsi_1h > rsi_sell) & (rsi_prev <= rsi_sell)
    signals[sell_mask] = -1.0 

    df["signal"] = signals
    return df

def fetch_bybit_data(symbol, interval, duration):
    days = int(duration[:-1])*365 if duration.endswith('y') else 30
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp()*1000)
    end_ms = int(datetime.now(timezone.utc).timestamp()*1000)
    all_rows = []
    while True:
        resp = requests.get("https://api.bybit.com/v5/market/kline", params={"category":"linear","symbol":symbol,"interval":"60","start":start_ms,"end":end_ms,"limit":1000}).json()
        rows = resp.get("result",{}).get("list",[])
        if not rows: break
        all_rows.extend(reversed(rows))
        if len(rows) < 1000: break
        end_ms = int(rows[-1][0]) - 1
        time.sleep(0.1)
    df = pd.DataFrame(all_rows, columns=["time","open","high","low","close","volume","turnover"]).astype(float)
    df["datetime"] = pd.to_datetime(df["time"], unit="ms")
    return df.sort_values("time").reset_index(drop=True)

def execute_backtest(df, initial_balance, risk_pct, sl_atr_mult):
    balance = initial_balance
    position = None
    trades = []
    
    opens, highs = df["open"].values, df["high"].values
    lows, closes = df["low"].values, df["close"].values
    atrs, signals = df["tf_1h_atr_14"].values, df["signal"].values
    datetimes = df["datetime"].values

    balance_by_date = {}

    for i in range(1, len(df)):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        bar_dt = pd.Timestamp(datetimes[i])
        
        # Signals are from i-1 (the previously closed bar)
        prev_sig = signals[i-1]

        if position is not None:
            sl, risk = position["sl"], position["risk"]
            exit_reason = None
            exit_price = 0.0

            if o <= sl:
                exit_reason = "SL_GAP"
                exit_price = o * (1 - SLIPPAGE)
            elif l <= sl:
                exit_reason = "SL"
                exit_price = sl * (1 - SLIPPAGE)
            elif prev_sig == -1.0:
                exit_reason = "RSI_EXIT"
                exit_price = o * (1 - SLIPPAGE)

            if exit_reason:
                price_diff = exit_price - position["entry"]
                raw_pnl = (price_diff / position["entry"]) * position["notional"]
                actual_pnl = raw_pnl - (position["notional"] * TAKER_FEE * 2)
                
                is_win = actual_pnl > 0
                balance += actual_pnl
                
                trades.append({
                    "entry_datetime": position["entry_dt"].strftime("%Y-%m-%d %H:%M"),
                    "exit_datetime": bar_dt.strftime("%Y-%m-%d %H:%M"),
                    "side": "LONG",
                    "entry_price": round(position["entry"], 2),
                    "exit_price": round(exit_price, 2),
                    "sl": round(sl, 2),
                    "pnl": round(actual_pnl, 2),
                    "result": exit_reason,
                    "is_win": is_win,
                    "balance": round(balance, 2),
                })
                position = None

        if position is None and prev_sig == 1.0:
            atr_val = atrs[i-1]
            entry = o * (1 + SLIPPAGE)
            sl_dist = atr_val * sl_atr_mult
            
            position = {
                "entry": entry,
                "entry_dt": bar_dt,
                "sl": entry - sl_dist,
                "risk": balance * risk_pct,
                "notional": (balance * risk_pct) / (sl_dist / entry) if sl_dist > 0 else 0
            }

        balance_by_date[bar_dt.strftime("%Y-%m-%d")] = balance

    return trades, balance_by_date
