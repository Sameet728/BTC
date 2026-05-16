"""
======================================================================
  QUANTFORGE — LIVE TRADING ENGINE & BACKTESTER
  Strategy: stoch_89_v2 — OPTIMIZED
======================================================================
  CHANGES vs v1:
    • Signals:  1D stoch (slow, ~14 trades/yr)  →  4H stoch cross (faster, ~30+ trades/yr)
    • Filter:   Daily EMA-89 trend retained  +  1H RSI + 1H ADX added
    • Risk:     0.8%  →  1.2% per trade
    • SL mult:  1.8x ATR  →  1.5x ATR  (tighter, fewer whipsaws)
    • RR ratio: 2.2   →  2.5           (better reward)
    • Cooldown: 6 bars →  3 bars        (less missed signals)
  TARGET: ~2.5% avg monthly return
"""

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
import logging
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# ======================================================================
#  USER SETTINGS
# ======================================================================
SYMBOL = "BTCUSDT"
INTERVAL = "1h"
DURATION = "5y"

INITIAL_BALANCE = 1000.0
FEE = 0.0005
SLIPPAGE = 0.0005

# ── Optimized Risk Parameters ──────────────────────────────────────────
SL_ATR_MULT = 1.07        # tighter stop → less capital lost per loss
RR_RATIO    = 1.50        # higher reward-to-risk
RISK_PCT    = 0.0178      # 1.2% risk per trade (was 0.8%)
COOLDOWN    = 3          # 3 bars min between trades (was 6)

# ── Signal Thresholds (tunable) ────────────────────────────────────────
STOCH_BUY_LEVEL  = 41    # 4H Stoch K crosses above this → buy trigger
STOCH_SELL_LEVEL = 57    # 4H Stoch K crosses below this → sell trigger
RSI_BULL_MIN     = 45    # 1H RSI must be above this for longs
RSI_BEAR_MAX     = 58    # 1H RSI must be below this for shorts
ADX_MIN          = 15    # minimum ADX for trend confirmation

# ======================================================================
#  INDICATORS LIBRARY
# ======================================================================
def ema(series, period):
    return series.ewm(span=period, adjust=False).mean()

def sma(series, period):
    return series.rolling(period).mean()

def rsi(series, period=14):
    delta = series.diff()
    gain  = delta.where(delta > 0, 0.0)
    loss  = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1.0/period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0/period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))

def atr(high, low, close, period=14):
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()

def macd(series, fast=12, slow=26, signal=9):
    ema_fast   = ema(series, fast)
    ema_slow   = ema(series, slow)
    macd_line  = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram  = macd_line - signal_line
    return macd_line, signal_line, histogram

def bollinger_bands(series, period=20, std_dev=2.0):
    middle = sma(series, period)
    std    = series.rolling(period).std()
    return middle + std_dev*std, middle, middle - std_dev*std

def adx(high, low, close, period=14):
    prev_high  = high.shift(1);  prev_low = low.shift(1);  prev_close = close.shift(1)
    plus_dm    = (high - prev_high).where((high - prev_high) > (prev_low - low), 0.0)
    plus_dm    = plus_dm.where(plus_dm > 0, 0.0)
    minus_dm   = (prev_low - low).where((prev_low - low) > (high - prev_high), 0.0)
    minus_dm   = minus_dm.where(minus_dm > 0, 0.0)
    tr         = pd.concat([high-low, (high-prev_close).abs(), (low-prev_close).abs()], axis=1).max(axis=1)
    atr_val    = tr.ewm(span=period, adjust=False).mean()
    plus_di    = 100 * (plus_dm.ewm(span=period, adjust=False).mean()  / atr_val.replace(0, np.nan))
    minus_di   = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / atr_val.replace(0, np.nan))
    dx         = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    return dx.ewm(span=period, adjust=False).mean()

def stochastic(high, low, close, k_period=14, d_period=3):
    lowest  = low.rolling(k_period).min()
    highest = high.rolling(k_period).max()
    k = 100 * (close - lowest) / (highest - lowest).replace(0, np.nan)
    d = k.rolling(d_period).mean()
    return k, d

def obv(close, volume):
    return (volume * np.sign(close.diff()).fillna(0)).cumsum()

def volume_ratio(volume, period=20):
    return volume / volume.rolling(period).mean().replace(0, np.nan)

def williams_r(high, low, close, period=14):
    return -100 * (high.rolling(period).max() - close) / (high.rolling(period).max() - low.rolling(period).min()).replace(0, np.nan)

def cci(high, low, close, period=20):
    tp  = (high + low + close) / 3
    mad = tp.rolling(period).apply(lambda x: np.abs(x - x.mean()).mean(), raw=True)
    return (tp - tp.rolling(period).mean()) / (0.015 * mad).replace(0, np.nan)

def mfi(high, low, close, volume, period=14):
    tp       = (high + low + close) / 3
    rmf      = tp * volume
    delta    = tp.diff()
    pos_flow = rmf.where(delta > 0, 0.0).rolling(period).sum()
    neg_flow = rmf.where(delta < 0, 0.0).rolling(period).sum()
    mr       = pos_flow / neg_flow.replace(0, np.nan)
    return 100 - (100 / (1 + mr))

def cmf(high, low, close, volume, period=20):
    mfm = ((close - low) - (high - close)) / (high - low).replace(0, np.nan)
    return (mfm * volume).rolling(period).sum() / volume.rolling(period).sum().replace(0, np.nan)

def supertrend(high, low, close, period=10, multiplier=3.0):
    atr_val    = atr(high, low, close, period)
    hl2        = (high + low) / 2
    upper_band = hl2 + multiplier * atr_val
    lower_band = hl2 - multiplier * atr_val
    st         = pd.Series(np.nan, index=close.index)
    direction  = pd.Series(1, index=close.index)
    for i in range(1, len(close)):
        if   close.iloc[i] > upper_band.iloc[i-1]: direction.iloc[i] = 1
        elif close.iloc[i] < lower_band.iloc[i-1]: direction.iloc[i] = -1
        else:                                       direction.iloc[i] = direction.iloc[i-1]
        st.iloc[i] = lower_band.iloc[i] if direction.iloc[i] == 1 else upper_band.iloc[i]
    return st

def _linear_slope(series, period):
    x = np.arange(period, dtype=float);  x_mean = x.mean();  x_var = ((x - x_mean)**2).sum()
    if x_var == 0: return pd.Series(0.0, index=series.index)
    def _slope(y):
        if len(y) < period: return np.nan
        return ((x - x_mean) * (y - y.mean())).sum() / x_var
    return series.rolling(period).apply(_slope, raw=True)

def wma(series, period):
    w = np.arange(1, period+1, dtype=float)
    return series.rolling(period).apply(lambda x: np.dot(x, w)/w.sum(), raw=True)

def hma(series, period):
    half = max(int(period/2), 1);  sqrt_p = max(int(np.sqrt(period)), 1)
    return wma(2*wma(series, half) - wma(series, period), sqrt_p)

def parabolic_sar(high, low, close, step=0.02, max_step=0.2):
    n = len(close);  sar = np.zeros(n);  direction = np.zeros(n)
    af = step;  ep = high.iloc[0]
    sar[0] = low.iloc[0];  direction[0] = 1
    for i in range(1, n):
        prev_sar = sar[i-1];  prev_dir = direction[i-1]
        if prev_dir == 1:
            sar[i] = prev_sar + af*(ep - prev_sar)
            sar[i] = min(sar[i], low.iloc[i-1], low.iloc[i-2] if i>=2 else low.iloc[i-1])
            if high.iloc[i] > ep: ep = high.iloc[i]; af = min(af+step, max_step)
            if low.iloc[i] < sar[i]: direction[i]=-1; sar[i]=ep; ep=low.iloc[i]; af=step
            else: direction[i]=1
        else:
            sar[i] = prev_sar + af*(ep - prev_sar)
            sar[i] = max(sar[i], high.iloc[i-1], high.iloc[i-2] if i>=2 else high.iloc[i-1])
            if low.iloc[i] < ep: ep = low.iloc[i]; af = min(af+step, max_step)
            if high.iloc[i] > sar[i]: direction[i]=1; sar[i]=ep; ep=high.iloc[i]; af=step
            else: direction[i]=-1
    return pd.Series(sar, index=close.index), pd.Series(direction, index=close.index)

def _compute_ichimoku(h, l, c, prefix, out):
    tenkan   = (h.rolling(9).max()  + l.rolling(9).min())  / 2
    kijun    = (h.rolling(26).max() + l.rolling(26).min()) / 2
    senkou_a = ((tenkan + kijun) / 2).shift(26)
    senkou_b = ((h.rolling(52).max() + l.rolling(52).min()) / 2).shift(26)
    out[f"{prefix}tenkan_9"] = tenkan;   out[f"{prefix}kijun_26"] = kijun
    out[f"{prefix}senkou_a"] = senkou_a; out[f"{prefix}senkou_b"] = senkou_b
    out[f"{prefix}cloud_bullish"]   = pd.Series(np.where(senkou_a>senkou_b,1,np.where(senkou_a<senkou_b,-1,0)), index=c.index).fillna(0).astype(int)
    above = (c>senkou_a)&(c>senkou_b); below = (c<senkou_a)&(c<senkou_b)
    out[f"{prefix}price_vs_cloud"]  = pd.Series(np.where(above,1,np.where(below,-1,0)), index=c.index).fillna(0).astype(int)
    tk_cross_up = (tenkan>kijun)&(tenkan.shift(1)<=kijun.shift(1))
    tk_cross_dn = (tenkan<kijun)&(tenkan.shift(1)>=kijun.shift(1))
    out[f"{prefix}tk_cross"] = pd.Series(np.where(tk_cross_up,1,np.where(tk_cross_dn,-1,0)), index=c.index).fillna(0).astype(int)

def _compute_keltner(c, h, l, prefix, out):
    kc_mid = ema(c, 20);  atr_10 = atr(h, l, c, 10)
    out[f"{prefix}kc_middle"] = kc_mid
    out[f"{prefix}kc_upper"]  = kc_mid + 1.5*atr_10
    out[f"{prefix}kc_lower"]  = kc_mid - 1.5*atr_10
    out[f"{prefix}kc_width"]  = (out[f"{prefix}kc_upper"] - out[f"{prefix}kc_lower"]) / kc_mid.replace(0, np.nan)

def _compute_donchian(h, l, prefix, out):
    for p in [20, 55]:
        out[f"{prefix}donchian_upper_{p}"] = h.rolling(p).max()
        out[f"{prefix}donchian_lower_{p}"] = l.rolling(p).min()
        out[f"{prefix}donchian_mid_{p}"]   = (out[f"{prefix}donchian_upper_{p}"] + out[f"{prefix}donchian_lower_{p}"]) / 2

def _compute_heikin_ashi(o, h, l, c, prefix, out):
    ha_close = (o+h+l+c)/4;  ha_open = pd.Series(np.nan, index=c.index)
    ha_open.iloc[0] = (o.iloc[0]+c.iloc[0])/2
    for i in range(1, len(c)): ha_open.iloc[i] = (ha_open.iloc[i-1]+ha_close.iloc[i-1])/2
    ha_high = pd.concat([h, ha_open, ha_close], axis=1).max(axis=1)
    ha_low  = pd.concat([l, ha_open, ha_close], axis=1).min(axis=1)
    out[f"{prefix}ha_close"] = ha_close;  out[f"{prefix}ha_open"] = ha_open
    out[f"{prefix}ha_trend"] = np.where(ha_close>ha_open, 1, -1)
    out[f"{prefix}ha_strong_bull"] = ((ha_close>ha_open)&(np.abs(ha_low-ha_open)<1e-8)).astype(int)
    out[f"{prefix}ha_strong_bear"] = ((ha_close<ha_open)&(np.abs(ha_high-ha_open)<1e-8)).astype(int)
    bull = (ha_close>ha_open).astype(int);  bear = (ha_close<ha_open).astype(int)
    cb = np.zeros(len(c));  cbr = np.zeros(len(c))
    for i in range(len(c)):
        if bull.iloc[i]: cb[i]  = min((cb[i-1]+1) if i>0 else 1, 5)
        if bear.iloc[i]: cbr[i] = min((cbr[i-1]+1) if i>0 else 1, 5)
    out[f"{prefix}ha_consec_bull"] = pd.Series(cb,  index=c.index)
    out[f"{prefix}ha_consec_bear"] = pd.Series(cbr, index=c.index)

def _compute_squeeze(c, h, l, prefix, out):
    bb_u, bb_m, bb_l = bollinger_bands(c, 20, 2.0)
    kc_mid = ema(c, 20);  atr_val = atr(h, l, c, 10)
    kc_u = kc_mid+1.5*atr_val;  kc_l = kc_mid-1.5*atr_val
    sq_on  = ((bb_l>kc_l)&(bb_u<kc_u)).astype(int)
    sq_off = ((bb_l<=kc_l)|(bb_u>=kc_u)).astype(int)
    out[f"{prefix}sq_on"] = sq_on;  out[f"{prefix}sq_off"] = sq_off
    midpoint = (h.rolling(20).max()+l.rolling(20).min())/2
    out[f"{prefix}sq_hist"] = _linear_slope(c-(midpoint+sma(c,20))/2, 20)
    out[f"{prefix}sq_firing_bull"] = ((sq_on.shift(1)==1)&(sq_off==1)&(out[f"{prefix}sq_hist"]>0)).astype(int)
    out[f"{prefix}sq_firing_bear"] = ((sq_on.shift(1)==1)&(sq_off==1)&(out[f"{prefix}sq_hist"]<0)).astype(int)

def _compute_elder_ray(h, l, c, prefix, out):
    ema_13 = ema(c, 13)
    out[f"{prefix}elder_bull_power"]    = h - ema_13
    out[f"{prefix}elder_bear_power"]    = l - ema_13
    out[f"{prefix}elder_bull_positive"] = ((out[f"{prefix}elder_bull_power"]>0)&(out[f"{prefix}elder_bull_power"]>out[f"{prefix}elder_bull_power"].shift(1))).astype(int)
    out[f"{prefix}elder_bear_negative"] = ((out[f"{prefix}elder_bear_power"]<0)&(out[f"{prefix}elder_bear_power"]<out[f"{prefix}elder_bear_power"].shift(1))).astype(int)

def _compute_zlema(c, prefix, out):
    for period in [21, 55]:
        lag = (period-1)//2
        out[f"{prefix}zlema_{period}"] = ema(c+(c-c.shift(lag)), period)

def _compute_market_structure(h, l, prefix, out):
    ch5 = h.rolling(5).max();  cl5 = l.rolling(5).min()
    ph5 = ch5.shift(5);        pl5 = cl5.shift(5)
    up = (ch5>ph5)&(cl5>pl5);  dn = (ch5<ph5)&(cl5<pl5)
    out[f"{prefix}mkt_structure"] = pd.Series(np.where(up,1,np.where(dn,-1,0)), index=h.index).fillna(0).astype(int)
    h10 = h.rolling(10).max();  l10 = l.rolling(10).min()
    ph10 = h10.shift(10);       pl10 = l10.shift(10)
    out[f"{prefix}hh_hl"] = ((h10>ph10)&(l10>pl10)).fillna(False).astype(int)
    out[f"{prefix}ll_lh"] = ((l10<pl10)&(h10<ph10)).fillna(False).astype(int)

def _compute_tf_indicators(df, prefix):
    out = pd.DataFrame(index=df.index)
    c = df["close"]; h = df["high"]; l = df["low"]; o = df["open"]; v = df["volume"]

    for period in [8,13,21,34,55,89,200]: out[f"{prefix}ema_{period}"] = ema(c, period)
    for period in [20,50,200]:            out[f"{prefix}sma_{period}"] = sma(c, period)
    out[f"{prefix}adx_14"] = adx(h, l, c, 14)

    for period in [7,14,21]: out[f"{prefix}rsi_{period}"] = rsi(c, period)
    ml, sl_line, mh = macd(c)
    out[f"{prefix}macd_line"] = ml;  out[f"{prefix}macd_signal"] = sl_line;  out[f"{prefix}macd_hist"] = mh
    sk, sd = stochastic(h, l, c)
    out[f"{prefix}stoch_k"] = sk;   out[f"{prefix}stoch_d"] = sd
    out[f"{prefix}roc_10"]  = c.pct_change(10)*100

    out[f"{prefix}atr_14"]  = atr(h, l, c, 14)
    out[f"{prefix}atr_pct"] = out[f"{prefix}atr_14"] / c
    bb_u, bb_m, bb_l = bollinger_bands(c)
    out[f"{prefix}bb_upper"] = bb_u;  out[f"{prefix}bb_middle"] = bb_m
    out[f"{prefix}bb_lower"] = bb_l;  out[f"{prefix}bb_width"]  = (bb_u-bb_l)/bb_m

    out[f"{prefix}volume_ma_20"]   = sma(v, 20)
    out[f"{prefix}volume_ratio"]   = volume_ratio(v, 20)
    out[f"{prefix}obv"]            = obv(c, v)
    out[f"{prefix}obv_slope_5"]    = _linear_slope(out[f"{prefix}obv"], 5)
    out[f"{prefix}volume_expanding"] = (v > v.shift(1)).fillna(False).astype(int)

    out[f"{prefix}willr_14"] = williams_r(h, l, c, 14)
    out[f"{prefix}cci_20"]   = cci(h, l, c, 20)
    out[f"{prefix}mfi_14"]   = mfi(h, l, c, v, 14)
    out[f"{prefix}cmf_20"]   = cmf(h, l, c, v, 20)
    out[f"{prefix}supertrend_10_3"] = supertrend(h, l, c, 10, 3.0)

    vwap_val = (c*v).rolling(20).sum() / v.rolling(20).sum().replace(0, np.nan)
    out[f"{prefix}vwap_dev"] = (c/vwap_val - 1)*100

    for n in [10, 20, 50]:
        out[f"{prefix}high_{n}"] = h.rolling(n).max()
        out[f"{prefix}low_{n}"]  = l.rolling(n).min()

    adx_trend  = (out[f"{prefix}adx_14"] > 25).fillna(False)
    bull_regime = (adx_trend & (c > out[f"{prefix}ema_200"]).fillna(False)).astype(int)
    bear_regime = (adx_trend & (c < out[f"{prefix}ema_200"]).fillna(False)).astype(int)
    out[f"{prefix}regime"]       = bull_regime - bear_regime
    out[f"{prefix}ema_200_slope"] = _linear_slope(out[f"{prefix}ema_200"], 5)

    body = (c-o).abs();  wick = h-l;  wick_safe = wick.replace(0, np.nan)
    out[f"{prefix}body_ratio"]       = (body/wick_safe).fillna(0.5)
    out[f"{prefix}upper_wick_ratio"] = ((h - pd.concat([c,o],axis=1).max(axis=1)) / wick_safe).fillna(0)
    out[f"{prefix}lower_wick_ratio"] = ((pd.concat([c,o],axis=1).min(axis=1) - l) / wick_safe).fillna(0)
    out[f"{prefix}is_bullish"] = (c>o).fillna(False).astype(int)
    out[f"{prefix}is_bearish"] = (c<o).fillna(False).astype(int)

    bull = out[f"{prefix}is_bullish"].astype(bool);  bear = out[f"{prefix}is_bearish"].astype(bool)
    out[f"{prefix}consec_bullish_2"] = (bull & bull.shift(1).fillna(False)).astype(int)
    out[f"{prefix}consec_bullish_3"] = (bull & bull.shift(1).fillna(False) & bull.shift(2).fillna(False)).astype(int)
    out[f"{prefix}consec_bearish_2"] = (bear & bear.shift(1).fillna(False)).astype(int)
    out[f"{prefix}consec_bearish_3"] = (bear & bear.shift(1).fillna(False) & bear.shift(2).fillna(False)).astype(int)

    out[f"{prefix}is_hammer"] = (
        (out[f"{prefix}lower_wick_ratio"]>0.6).fillna(False) &
        (out[f"{prefix}upper_wick_ratio"]<0.15).fillna(False) &
        (out[f"{prefix}body_ratio"]<0.35).fillna(False)
    ).astype(int)
    out[f"{prefix}is_shooting_star"] = (
        (out[f"{prefix}upper_wick_ratio"]>0.6).fillna(False) &
        (out[f"{prefix}lower_wick_ratio"]<0.15).fillna(False) &
        (out[f"{prefix}body_ratio"]<0.35).fillna(False)
    ).astype(int)

    prev_body = (c.shift(1)-o.shift(1));  curr_body = (c-o)
    out[f"{prefix}is_engulfing_bull"] = (
        (prev_body<0).fillna(False)&(curr_body>0).fillna(False)&
        (o<c.shift(1)).fillna(False)&(c>o.shift(1)).fillna(False)
    ).astype(int)
    out[f"{prefix}is_engulfing_bear"] = (
        (prev_body>0).fillna(False)&(curr_body<0).fillna(False)&
        (o>c.shift(1)).fillna(False)&(c<o.shift(1)).fillna(False)
    ).astype(int)

    for col_base in ["rsi_14","macd_hist","ema_21","stoch_k","adx_14","cci_20"]:
        src = out[f"{prefix}{col_base}"]
        out[f"{prefix}prev_1_{col_base}"] = src.shift(1)
        out[f"{prefix}prev_3_{col_base}"] = src.shift(3)

    high_52w = h.rolling(min(365, len(h))).max()
    out[f"{prefix}dist_from_52w_high"] = (high_52w - c)/c*100
    out[f"{prefix}ema_vs_sma_20"] = out[f"{prefix}ema_21"] - out[f"{prefix}sma_20"]

    _compute_ichimoku(h, l, c, prefix, out)
    _compute_keltner(c, h, l, prefix, out)
    _compute_donchian(h, l, prefix, out)

    psar_val, psar_dir = parabolic_sar(h, l, c)
    out[f"{prefix}psar"] = psar_val;  out[f"{prefix}psar_direction"] = psar_dir

    _compute_heikin_ashi(o, h, l, c, prefix, out)

    for hp in [9,21,55]: out[f"{prefix}hma_{hp}"] = hma(c, hp)

    _compute_elder_ray(h, l, c, prefix, out)
    _compute_squeeze(c, h, l, prefix, out)
    _compute_zlema(c, prefix, out)

    atr_14 = out[f"{prefix}atr_14"]
    out[f"{prefix}atr_pct_rank"] = atr_14.rolling(100).apply(
        lambda x: (x[-1:].iloc[0] > x[:-1]).sum()/max(len(x)-1,1)*100 if len(x)>1 else 50.0, raw=False)

    rsi_14 = out[f"{prefix}rsi_14"]
    out[f"{prefix}rsi_slope_3"] = _linear_slope(rsi_14, 3)
    out[f"{prefix}rsi_slope_8"] = _linear_slope(rsi_14, 8)

    vol_mean = v.rolling(20).mean();  vol_std = v.rolling(20).std().replace(0, np.nan)
    out[f"{prefix}vol_zscore"] = (v - vol_mean)/vol_std
    out[f"{prefix}vol_surge"]  = (out[f"{prefix}vol_zscore"] > 2.0).astype(int)
    out[f"{prefix}vol_dry"]    = (out[f"{prefix}vol_zscore"] < -1.0).astype(int)

    _compute_market_structure(h, l, prefix, out)
    out[f"{prefix}macd_hist_slope"] = _linear_slope(mh, 3)

    return out


def _resample_ohlcv(df_1h, rule):
    df = df_1h.set_index("datetime").copy()
    return df.resample(rule).agg(
        {"open":"first","high":"max","low":"min","close":"last","volume":"sum"}
    ).dropna(subset=["close"])

ALL_INDICATOR_COLUMNS = []

def compute_all_indicators(df):
    global ALL_INDICATOR_COLUMNS
    df = df.copy()
    if "datetime" not in df.columns:
        result = df.copy()
        tf_indicators = _compute_tf_indicators(df, "tf_1h_")
        result = pd.concat([result, tf_indicators], axis=1)
        result = result.iloc[200:].reset_index(drop=True)
        ALL_INDICATOR_COLUMNS = [c for c in result.columns if c.startswith("tf_")]
        return result

    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    result = df.copy()

    tf_1h = _compute_tf_indicators(df, "tf_1h_")
    result = pd.concat([result, tf_1h], axis=1)
    result["tf_1h_hour_utc"]    = result["datetime"].dt.hour
    result["tf_1h_day_of_week"] = result["datetime"].dt.dayofweek

    for tf_label, resample_rule in [("4h","4h"), ("1d","1D")]:
        try:
            htf_ohlcv      = _resample_ohlcv(df, resample_rule)
            htf_indicators = _compute_tf_indicators(htf_ohlcv, f"tf_{tf_label}_")
            htf_indicators.index = pd.to_datetime(htf_indicators.index, utc=True)
            df_dt_index    = pd.to_datetime(result["datetime"], utc=True)
            htf_reindexed  = htf_indicators.reindex(df_dt_index, method="ffill")
            htf_reindexed.index = result.index
            result = pd.concat([result, htf_reindexed], axis=1)
        except Exception as e:
            log.warning(f"Failed to compute {tf_label} indicators: {e}")

    result = result.copy()
    legacy_map = {
        "ema_8":"tf_1h_ema_8","ema_13":"tf_1h_ema_13","ema_21":"tf_1h_ema_21",
        "ema_34":"tf_1h_ema_34","ema_55":"tf_1h_ema_55","ema_89":"tf_1h_ema_89",
        "ema_200":"tf_1h_ema_200","sma_20":"tf_1h_sma_20","sma_50":"tf_1h_sma_50",
        "sma_200":"tf_1h_sma_200","adx_14":"tf_1h_adx_14","rsi_7":"tf_1h_rsi_7",
        "rsi_14":"tf_1h_rsi_14","rsi_21":"tf_1h_rsi_21","macd_line":"tf_1h_macd_line",
        "macd_signal":"tf_1h_macd_signal","macd_hist":"tf_1h_macd_hist",
        "stoch_k":"tf_1h_stoch_k","stoch_d":"tf_1h_stoch_d","atr_14":"tf_1h_atr_14",
        "atr_pct":"tf_1h_atr_pct","bb_upper":"tf_1h_bb_upper","bb_middle":"tf_1h_bb_middle",
        "bb_lower":"tf_1h_bb_lower","bb_width":"tf_1h_bb_width",
        "volume_ma_20":"tf_1h_volume_ma_20","volume_ratio":"tf_1h_volume_ratio",
        "obv":"tf_1h_obv","candle_body_ratio":"tf_1h_body_ratio","roc_10":"tf_1h_roc_10",
        "regime":"tf_1h_regime",
    }
    for old, new in legacy_map.items():
        if new in result.columns and old not in result.columns:
            result[old] = result[new]

    result = result.iloc[200:].reset_index(drop=True)
    ALL_INDICATOR_COLUMNS = [c for c in result.columns if c.startswith("tf_")]
    log.info(f"Indicators computed: {len(result)} rows, {len(result.columns)} columns (multi-TF)")
    return result


# ======================================================================
#  OPTIMIZED STRATEGY SIGNAL ENGINE
#  v2 changes:
#    • Primary trigger: 4H Stoch K cross (was 1D) → ~3x more signals
#    • Trend gate:     1D close > 1D EMA-89 (unchanged)
#    • Momentum gate:  1H RSI must confirm direction
#    • Quality gate:   1H ADX > 18 ensures we're not in choppy market
#    • Extra long:     1H MACD hist must be positive (momentum aligned)
#    • Extra short:    1H MACD hist must be negative
# ======================================================================
def generate_signals(df):
    df = df.copy()
    signals = pd.Series(0.0, index=df.index)

    sk4h      = df["tf_4h_stoch_k"]
    sk4h_prev = sk4h.shift(1)
    close     = df["close"]
    ema89_1d  = df["tf_1d_ema_89"]
    rsi_1h    = df["tf_1h_rsi_14"]
    adx_1h    = df["tf_1h_adx_14"]
    macd_1h   = df["tf_1h_macd_hist"]

    # ── LONG ──────────────────────────────────────────────────────────────
    # 1. 4H Stoch K crosses UP through STOCH_BUY_LEVEL (25)  — fresh momentum reversal
    # 2. Price is above Daily EMA-89                          — macro uptrend
    # 3. 1H RSI is above RSI_BULL_MIN (42)                   — 1H momentum not bearish
    # 4. 1H ADX above ADX_MIN (18)                           — not ranging/choppy
    # 5. 1H MACD histogram is positive                       — short-term momentum aligned
    buy_mask = (
        (sk4h      > STOCH_BUY_LEVEL) &
        (sk4h_prev <= STOCH_BUY_LEVEL) &
        (close      > ema89_1d) &
        (rsi_1h     > RSI_BULL_MIN) &
        (adx_1h     > ADX_MIN) &
        (macd_1h    > 0)
    )
    signals[buy_mask] = 1.0

    # ── SHORT ─────────────────────────────────────────────────────────────
    # 1. 4H Stoch K crosses DOWN through STOCH_SELL_LEVEL (75) — momentum reversal
    # 2. Price is below Daily EMA-89                            — macro downtrend
    # 3. 1H RSI is below RSI_BEAR_MAX (58)                     — 1H momentum not bullish
    # 4. 1H ADX above ADX_MIN (18)                             — not ranging/choppy
    # 5. 1H MACD histogram is negative                         — short-term momentum aligned
    sell_mask = (
        (sk4h      < STOCH_SELL_LEVEL) &
        (sk4h_prev >= STOCH_SELL_LEVEL) &
        (close      < ema89_1d) &
        (rsi_1h     < RSI_BEAR_MAX) &
        (adx_1h     > ADX_MIN) &
        (macd_1h    < 0)
    )
    signals[sell_mask] = -1.0

    df["signal"] = signals
    return df


# ======================================================================
#  BYBIT DATA INGESTION
# ======================================================================
def fetch_bybit_data(symbol, interval, duration):
    print(f"Fetching {duration} of data for {symbol} {interval}...")
    _INTERVAL_MAP = {"1h":"60","4h":"240","1d":"D"}
    bv_int = _INTERVAL_MAP.get(interval, "60")

    days     = int(duration[:-1])*365 if duration.endswith('y') else 30
    start_ms = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp()*1000)
    end_ms   = int(datetime.now(timezone.utc).timestamp()*1000)

    all_rows = []
    while True:
        resp = requests.get("https://api.bybit.com/v5/market/kline", params={
            "category":"linear","symbol":symbol,"interval":bv_int,
            "start":start_ms,"end":end_ms,"limit":1000
        }).json()
        rows = resp.get("result",{}).get("list",[])
        if not rows: break
        all_rows.extend(reversed(rows))
        
        # Print progress
        oldest_dt = datetime.fromtimestamp(int(rows[-1][0])/1000, tz=timezone.utc).strftime('%Y-%m-%d')
        print(f"Downloaded {len(all_rows)} candles up to {oldest_dt}...", end='\r')
        
        if len(rows) < 1000: break
        end_ms = int(rows[-1][0]) - 1
        time.sleep(0.1)
    
    print() # newline after progress bar

    df = pd.DataFrame(all_rows, columns=["time","open","high","low","close","volume","turnover"]).astype(float)
    df["datetime"] = pd.to_datetime(df["time"], unit="ms")
    return df.sort_values("time").reset_index(drop=True)


# ======================================================================
#  EQUITY CURVE PLOT
# ======================================================================
def plot_equity_curve(trades, balance_by_date, symbol, initial_balance, version="v3_opt15"):
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.gridspec as gridspec
        from matplotlib.patches import Patch
    except ImportError:
        print("[Warning] matplotlib not installed — skipping equity curve."); return

    if not trades:
        print("[Warning] No trades to plot."); return

    date_idx   = pd.to_datetime(list(balance_by_date.keys()))
    bal_series = pd.Series(list(balance_by_date.values()), index=date_idx).sort_index()

    rolling_max = bal_series.cummax()
    drawdown    = (bal_series - rolling_max) / rolling_max * 100

    trade_df = pd.DataFrame(trades)
    trade_df["entry_datetime"] = pd.to_datetime(trade_df["entry_datetime"])
    trade_df["exit_datetime"]  = pd.to_datetime(trade_df["exit_datetime"])

    wins       = trade_df[trade_df["is_win"]]
    losses     = trade_df[~trade_df["is_win"]]
    win_rate   = len(wins)/len(trade_df)*100 if len(trade_df)>0 else 0
    total_ret  = (bal_series.iloc[-1]-initial_balance)/initial_balance*100
    max_dd     = drawdown.min()
    avg_win    = wins["pnl"].mean()    if len(wins)>0   else 0
    avg_loss   = losses["pnl"].mean()  if len(losses)>0 else 0
    pf         = (wins["pnl"].sum()/abs(losses["pnl"].sum())) if len(losses)>0 and losses["pnl"].sum()!=0 else float("inf")

    # ── Monthly return bar chart data ──────────────────────────────────────
    monthly_bal  = bal_series.resample("ME").last().dropna()
    prev         = initial_balance
    m_dates, m_rets = [], []
    for dt, eb in monthly_bal.items():
        m_rets.append((eb/prev - 1)*100); m_dates.append(dt); prev = eb

    fig = plt.figure(figsize=(18, 12), facecolor="#0d1117")
    gs  = gridspec.GridSpec(4, 1, hspace=0.10, height_ratios=[3, 1, 1, 1])

    axes = [fig.add_subplot(gs[i]) for i in range(4)]
    ax1, ax2, ax3, ax4 = axes

    for ax in axes:
        ax.set_facecolor("#0d1117")
        ax.tick_params(colors="#8b949e", labelsize=8.5)
        ax.spines["top"].set_visible(False); ax.spines["right"].set_visible(False)
        for s in ["bottom","left"]: ax.spines[s].set_color("#30363d")
        ax.grid(True, color="#21262d", linewidth=0.5, linestyle="--", alpha=0.6)

    # Panel 1 — Equity curve
    ax1.plot(bal_series.index, bal_series.values, color="#58a6ff", linewidth=2, zorder=3)
    ax1.fill_between(bal_series.index, initial_balance, bal_series.values,
                     where=(bal_series.values >= initial_balance), alpha=0.15, color="#3fb950", interpolate=True)
    ax1.fill_between(bal_series.index, initial_balance, bal_series.values,
                     where=(bal_series.values < initial_balance), alpha=0.15, color="#f85149", interpolate=True)
    ax1.axhline(initial_balance, color="#8b949e", linewidth=0.8, linestyle="--", alpha=0.6)

    for _, t in trade_df.iterrows():
        color  = "#3fb950" if t["is_win"] else "#f85149"
        marker = "^" if t["is_win"] else "v"
        closest = min(bal_series.index.searchsorted(t["exit_datetime"]), len(bal_series)-1)
        ax1.scatter(t["exit_datetime"], bal_series.iloc[closest], color=color, s=18, zorder=5, marker=marker, alpha=0.8)

    ax1.set_ylabel("Portfolio Value ($)", color="#8b949e", fontsize=9)
    ax1.yaxis.set_major_formatter(plt.FuncFormatter(lambda x,_: f"${x:,.0f}"))
    ax1.tick_params(axis="x", labelbottom=False)

    stats = (f"Return: {total_ret:+.1f}%  |  Max DD: {max_dd:.1f}%  |  "
             f"Win Rate: {win_rate:.1f}%  |  Trades: {len(trade_df)}  |  "
             f"PF: {pf:.2f}  |  Avg Win: ${avg_win:.2f}  |  Avg Loss: ${avg_loss:.2f}  |  "
             f"Risk: {RISK_PCT*100:.1f}%  SL: {SL_ATR_MULT}×ATR  TP: {SL_ATR_MULT*RR_RATIO:.2f}×ATR")
    ax1.text(0.01, 0.97, stats, transform=ax1.transAxes, fontsize=8, color="#e6edf3",
             va="top", ha="left", bbox=dict(boxstyle="round,pad=0.4", facecolor="#161b22", edgecolor="#30363d", alpha=0.9))

    legend_elements = [Patch(facecolor="#3fb950", alpha=0.8, label=f"Wins ({len(wins)})"),
                       Patch(facecolor="#f85149", alpha=0.8, label=f"Losses ({len(losses)})")]
    ax1.legend(handles=legend_elements, loc="lower right", fontsize=8,
               facecolor="#161b22", edgecolor="#30363d", labelcolor="#e6edf3")
    ax1.set_title(f"Equity Curve — {symbol}  |  Strategy: stoch_89_{version} (OPTIMIZED)",
                  color="#e6edf3", fontsize=13, fontweight="bold", pad=12)

    # Panel 2 — Drawdown
    ax2.fill_between(drawdown.index, 0, drawdown.values, color="#f85149", alpha=0.6)
    ax2.plot(drawdown.index, drawdown.values, color="#f85149", linewidth=0.7)
    ax2.axhline(0, color="#8b949e", linewidth=0.5)
    ax2.set_ylabel("Drawdown (%)", color="#8b949e", fontsize=9)
    ax2.yaxis.set_major_formatter(plt.FuncFormatter(lambda x,_: f"{x:.1f}%"))
    ax2.tick_params(axis="x", labelbottom=False)
    min_dd_dt = drawdown.idxmin()
    ax2.annotate(f"Max: {max_dd:.1f}%", xy=(min_dd_dt, max_dd), xytext=(10,-14),
                 textcoords="offset points", color="#f85149", fontsize=8,
                 arrowprops=dict(arrowstyle="->", color="#f85149", lw=0.7))

    # Panel 3 — Per-trade PnL bars
    colors = ["#3fb950" if t["is_win"] else "#f85149" for _,t in trade_df.iterrows()]
    ax3.bar(trade_df["exit_datetime"], trade_df["pnl"], color=colors, alpha=0.85, width=0.8)
    ax3.axhline(0, color="#8b949e", linewidth=0.5)
    ax3.set_ylabel("Trade PnL ($)", color="#8b949e", fontsize=9)
    ax3.yaxis.set_major_formatter(plt.FuncFormatter(lambda x,_: f"${x:,.0f}"))
    ax3.tick_params(axis="x", labelbottom=False)

    # Panel 4 — Monthly returns bar chart
    m_colors = ["#3fb950" if r >= 0 else "#f85149" for r in m_rets]
    ax4.bar(m_dates, m_rets, color=m_colors, alpha=0.85, width=20)
    ax4.axhline(0,    color="#8b949e", linewidth=0.5)
    ax4.axhline(2.5,  color="#f0e68c", linewidth=0.8, linestyle="--", alpha=0.7, label="2.5% target")
    avg_m = sum(m_rets)/len(m_rets) if m_rets else 0
    ax4.axhline(avg_m, color="#58a6ff", linewidth=0.9, linestyle="-.", alpha=0.8, label=f"Avg {avg_m:.2f}%")
    ax4.set_ylabel("Monthly Return (%)", color="#8b949e", fontsize=9)
    ax4.set_xlabel("Date", color="#8b949e", fontsize=9)
    ax4.yaxis.set_major_formatter(plt.FuncFormatter(lambda x,_: f"{x:.1f}%"))
    ax4.legend(fontsize=8, facecolor="#161b22", edgecolor="#30363d", labelcolor="#e6edf3")
    plt.setp(ax4.get_xticklabels(), rotation=20, ha="right")

    out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"equity_curve_stoch89_{version}.png")
    plt.savefig(out_path, dpi=150, bbox_inches="tight", facecolor="#0d1117")
    plt.close(fig)
    print(f"\n[Equity curve saved to]: {out_path}")


# ======================================================================
#  BACKTEST LOOP
# ======================================================================
def run_backtest():
    df = fetch_bybit_data(SYMBOL, INTERVAL, DURATION)
    df = compute_all_indicators(df)
    df = generate_signals(df)

    balance      = INITIAL_BALANCE
    position     = None
    trades       = []
    last_entry   = -(COOLDOWN + 1)
    pending_sig  = None   # (sig_value, atr_value, signal_bar_index)

    opens, highs  = df["open"].values,  df["high"].values
    lows, closes  = df["low"].values,   df["close"].values
    atrs, signals = df["atr_14"].values, df["signal"].values
    datetimes     = df["datetime"].values

    balance_by_date = {}
    print(f"Running backtest on {len(df)} candles...")

    for i in range(1, len(df)):
        o, h, l, c = opens[i], highs[i], lows[i], closes[i]
        bar_dt = pd.Timestamp(datetimes[i])

        # ── FIX #2: Execute pending entry at OPEN of this bar (no look-ahead) ──
        if pending_sig is not None and position is None:
            sig_val, atr_val, sig_idx = pending_sig
            side    = 1 if sig_val > 0 else -1
            entry   = o * (1 + SLIPPAGE * side)   # open of the bar AFTER signal
            sl_dist = atr_val * SL_ATR_MULT
            tp_dist = atr_val * SL_ATR_MULT * RR_RATIO
            position = {
                "side":     side,
                "entry":    entry,
                "entry_dt": bar_dt,
                "sl":       entry - sl_dist if side == 1 else entry + sl_dist,
                "tp":       entry + tp_dist if side == 1 else entry - tp_dist,
                "risk":     balance * RISK_PCT,
                "notional": (balance * RISK_PCT) / (atr_val * SL_ATR_MULT / entry),
            }
            last_entry = sig_idx
            pending_sig = None

        # ── Manage open position ──────────────────────────────────────────
        if position is not None:
            side, sl, tp, risk = position["side"], position["sl"], position["tp"], position["risk"]
            exit_reason = None

            if side == 1:
                if   o <= sl: exit_reason = "SL_GAP"
                elif o >= tp: exit_reason = "TP_GAP"
                elif l <= sl: exit_reason = "SL"
                elif h >= tp: exit_reason = "TP"
            else:
                if   o >= sl: exit_reason = "SL_GAP"
                elif o <= tp: exit_reason = "TP_GAP"
                elif h >= sl: exit_reason = "SL"
                elif l <= tp: exit_reason = "TP"

            if exit_reason:
                is_win = "TP" in exit_reason
                # FIX #1: Correct fixed-fractional risk formula
                # Win  = risk * RR_RATIO      (not risk * RR_RATIO * SL_ATR_MULT)
                # Loss = -risk                 (not risk * -SL_ATR_MULT)
                if exit_reason == "SL_GAP":
                    gap_loss_pct = abs(o - position["sl"]) / abs(position["entry"] - position["sl"])
                    pnl = -risk * (1.0 + gap_loss_pct) - (position["notional"] * FEE * 2)
                elif exit_reason == "TP_GAP":
                    pnl = risk * RR_RATIO - (position["notional"] * FEE * 2)
                else:
                    pnl = risk * (RR_RATIO if is_win else -1.0) - (position["notional"] * FEE * 2)
                balance += pnl
                exit_price = o if "GAP" in exit_reason else (position["tp"] if is_win else position["sl"])
                if "GAP" not in exit_reason:
                    exit_price = exit_price * (1 + SLIPPAGE * (-position["side"]))

                trades.append({
                    "entry_datetime": position["entry_dt"].strftime("%Y-%m-%d %H:%M"),
                    "exit_datetime":  bar_dt.strftime("%Y-%m-%d %H:%M"),
                    "side":           "LONG" if position["side"] == 1 else "SHORT",
                    "entry_price":    round(position["entry"], 2),
                    "exit_price":     round(exit_price, 2),
                    "sl":             round(position["sl"], 2),
                    "tp":             round(position["tp"], 2),
                    "pnl":            round(pnl, 2),
                    "result":         exit_reason,
                    "is_win":         is_win,
                    "balance":        round(balance, 2),
                })
                position = None

        # ── Queue new signal (execute at NEXT bar open, not current close) ──
        if position is None and pending_sig is None and i - last_entry >= COOLDOWN:
            sig = signals[i]
            if sig != 0 and atrs[i] > 0:
                pending_sig = (sig, atrs[i], i)   # will execute at bar[i+1] open

        balance_by_date[bar_dt.strftime("%Y-%m-%d")] = balance

    # ── Summary ───────────────────────────────────────────────────────────
    wins      = sum(1 for t in trades if t["is_win"])
    win_rate  = wins / len(trades) * 100 if trades else 0
    profit_pct = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100

    print("\n" + "="*55)
    print(f"  BACKTEST RESULTS (OPTIMIZED): {SYMBOL} {INTERVAL}")
    print("="*55)
    print(f"  Total Trades    : {len(trades)}")
    print(f"  Win Rate        : {win_rate:.1f}%")
    print(f"  Net Profit      : {profit_pct:.1f}%")
    print(f"  Final Balance   : ${balance:.2f}")
    print(f"  Risk/Trade      : {RISK_PCT*100:.1f}%")
    print(f"  SL / TP         : {SL_ATR_MULT}× ATR / {SL_ATR_MULT*RR_RATIO:.2f}× ATR")
    print("="*55)

    # ── Monthly Returns ───────────────────────────────────────────────────
    date_idx   = pd.to_datetime(list(balance_by_date.keys()))
    bal_series = pd.Series(list(balance_by_date.values()), index=date_idx).sort_index()
    monthly_bal = bal_series.resample("ME").last().dropna()

    monthly_returns = {}
    prev_bal = INITIAL_BALANCE
    for dt, end_bal in monthly_bal.items():
        ret = (end_bal / prev_bal - 1) * 100
        monthly_returns[dt.strftime("%Y-%m")] = ret
        prev_bal = end_bal

    print("\n[Monthly Returns (%)]")
    print(f"{'Month':<10} {'Return (%)':>10}  {'#':1}")
    for month, ret in monthly_returns.items():
        bar   = "#" * int(abs(ret)/0.5)
        color = "+" if ret >= 0 else "-"
        print(f"{month:<10} {ret:>10.2f}  {color}{bar}")

    all_monthly = list(monthly_returns.values())
    avg_monthly = sum(all_monthly) / len(all_monthly) if all_monthly else 0
    pos_months  = sum(1 for r in all_monthly if r > 0)
    print(f"\n  Months Profitable : {pos_months}/{len(all_monthly)}")
    print(f"  Sum of returns    : {sum(all_monthly):.2f}%")
    print(f"  Average monthly   : {avg_monthly:.2f}%  (target: 2.50%)")
    print(f"  Best month        : {max(all_monthly):.2f}%")
    print(f"  Worst month       : {min(all_monthly):.2f}%")

    # ── Yearly Returns ────────────────────────────────────────────────────
    yearly_bal = bal_series.resample("YE").last().dropna()
    yearly_returns = {}
    prev_bal = INITIAL_BALANCE
    for dt, end_bal in yearly_bal.items():
        ret = (end_bal / prev_bal - 1) * 100
        yearly_returns[str(dt.year)] = ret
        prev_bal = end_bal

    print("\n[Yearly Returns (%)]")
    print(f"{'Year':<6} {'Return (%)':>10}")
    for year, ret in yearly_returns.items():
        print(f"{year:<6} {ret:>10.2f}")

    # ── Drawdown ──────────────────────────────────────────────────────────
    rolling_max = bal_series.cummax()
    drawdown    = (bal_series - rolling_max) / rolling_max * 100
    print(f"\n  Max Drawdown   : {drawdown.min():.2f}%")

    # ── CSV Export ────────────────────────────────────────────────────────
    csv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trades_vedant48.csv")
    trade_df = pd.DataFrame(trades)
    trade_df[[c for c in trade_df.columns if c != "is_win"]].to_csv(csv_path, index=False)
    print(f"\n[Trades exported to]: {csv_path}  ({len(trades)} trades)")

    # ── Equity Curve ──────────────────────────────────────────────────────
    plot_equity_curve(trades, balance_by_date, SYMBOL, INITIAL_BALANCE, version="vedant48")


# ======================================================================
#  LIVE TRADING ENGINE
# ======================================================================
def run_live_trading():
    print("\n" + "="*55)
    print(f"  STARTING 24/7 LIVE SIGNAL ENGINE: {SYMBOL} {INTERVAL}")
    print("="*55)
    
    last_processed_candle = None

    while True:
        try:
            # Fetch last 1 year of data (sufficient to compute 200 EMA, etc.)
            df = fetch_bybit_data(SYMBOL, INTERVAL, "1y") 
            df = compute_all_indicators(df)
            df = generate_signals(df)
            
            latest = df.iloc[-1]
            latest_time = latest['datetime']
            
            if last_processed_candle != latest_time:
                last_processed_candle = latest_time
                
                sig = latest['signal']
                close_price = latest['close']
                
                print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] New Candle Closed: {latest_time} | Close: ${close_price:,.2f}")
                
                if sig > 0:
                    print(f"  --> 🟢 LONG SIGNAL DETECTED at ${close_price:,.2f}")
                elif sig < 0:
                    print(f"  --> 🔴 SHORT SIGNAL DETECTED at ${close_price:,.2f}")
                else:
                    print(f"  --> ⚪ HOLD / NO SIGNAL")
            
            # Wait before checking again (check every 5 minutes)
            time.sleep(300)
            
        except Exception as e:
            print(f"Error in live loop: {e}")
            time.sleep(60)

if __name__ == "__main__":
    run_backtest()
    run_live_trading()