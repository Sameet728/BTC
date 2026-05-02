# ╔══════════════════════════════════════════════════════════════════════════╗
# ║  PRODUCTION-READY HYBRID SYSTEM                                          ║
# ║  ── EMA 13/34/89 · RSI · ATR · VOL ──                                   ║
# ║  ── BACKTEST  +  LIVE SIGNAL ENGINE ──                                   ║
# ║                                                                          ║
# ║  DATA SOURCE : Bybit v5 /v5/market/kline                                 ║
# ║  BACKTEST    : Runs first, full intra-candle SL/TP resolution            ║
# ║  LIVE ENGINE : Polls Bybit every candle close, POSTs signals to API      ║
# ║  CSV         : Full indicator snapshot + boolean conditions per trade     ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# ── 0. INSTALL DEPENDENCIES ──────────────────────────────────────────────────
import subprocess, sys
sys.stdout.reconfigure(encoding="utf-8")
subprocess.check_call([
    sys.executable, "-m", "pip", "install", "-q",
    "pandas", "numpy", "requests", "ta", "matplotlib",
])

# ── 1. IMPORTS ───────────────────────────────────────────────────────────────
import time
import json
import logging
import requests
import os
from pathlib     import Path
from collections import deque

from datetime    import datetime, timedelta, timezone
from typing      import Optional, Tuple
from dotenv      import load_dotenv

load_dotenv()

import numpy             as np
import pandas            as pd
import matplotlib.pyplot as plt
import matplotlib.gridspec as gridspec

from ta.trend      import EMAIndicator
from ta.momentum   import RSIIndicator
from ta.volatility import AverageTrueRange

# ── ML Enhancement modules (optional — gracefully degrade if missing) ─────
try:
    from modules.ml_model import MLTradeFilter
    from modules.position_sizing import PositionSizer
    ML_AVAILABLE = True
except ImportError:
    ML_AVAILABLE = False


# ══════════════════════════════════════════════════════════════════════════════
#  ★  USER SETTINGS  (edit here — or override via .env)
# ══════════════════════════════════════════════════════════════════════════════
SYMBOL          = os.getenv("SYMBOL",        "BTCUSDT")
INTERVAL        = os.getenv("INTERVAL",      "1h")
DURATION        = "5y"

INITIAL_BALANCE = 1_000
RISK_PER_TRADE  = 0.019
POSITION_SIZE   = float(os.getenv("POSITION_SIZE", "0.019"))
RR_RATIO        = float(os.getenv("RR_RATIO",      "1.9"))
FEE             = 0.0005
ML_FILTER_ENABLED = os.getenv("ML_FILTER", "false").lower() == "true"
COOLDOWN        = int(os.getenv("COOLDOWN",        "5"))

ATR_GATE        = float(os.getenv("ATR_GATE",      "0.001"))
RSI_BUY_THRESH  = int(os.getenv("RSI_BUY_THRESH",  "59"))
RSI_SELL_THRESH = int(os.getenv("RSI_SELL_THRESH", "36"))
VOL_PERIOD      = 20
SL_MULT         = float(os.getenv("SL_MULT",       "2.7"))

LIVE_CANDLE_LIMIT    = 300
SIGNAL_API_URL       = os.getenv("API_URL", "https://btc-92mq.onrender.com/api/trade")
API_TIMEOUT          = 10
API_RETRY_ATTEMPTS   = 3
API_RETRY_DELAY      = 5
PERIODIC_PRINT_EVERY = 3_600

# ── Bybit base URL ────────────────────────────────────────────────────────────
BYBIT_BASE_URL = "https://api.bybit.com/v5/market/kline"

# ── Interval mapping: human → Bybit v5 format ────────────────────────────────
_INTERVAL_MAP = {
    "1m":  "1",   "3m":  "3",   "5m":  "5",   "15m": "15",
    "30m": "30",  "1h":  "60",  "2h":  "120",  "4h":  "240",
    "6h":  "360", "12h": "720", "1d":  "D",    "1w":  "W",
    "1M":  "M",
}

def bybit_interval(interval: str) -> str:
    mapped = _INTERVAL_MAP.get(interval)
    if mapped is None:
        raise ValueError(
            f"Unsupported interval '{interval}'. "
            f"Valid options: {list(_INTERVAL_MAP.keys())}"
        )
    return mapped
# ══════════════════════════════════════════════════════════════════════════════


# ── 2. LOGGING SETUP ─────────────────────────────────────────────────────────

class MemoryLogHandler(logging.Handler):
    """Stores the last N log lines in memory for sending to the backend."""
    def __init__(self, capacity=50):
        super().__init__()
        self.buffer = deque(maxlen=capacity)

    def emit(self, record):
        try:
            self.buffer.append(self.format(record))
        except Exception:
            pass

    def get_lines(self):
        return list(self.buffer)


memory_handler = MemoryLogHandler(capacity=50)
memory_handler.setFormatter(
    logging.Formatter(
        "%(asctime)s  [%(levelname)s]  %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S UTC",
    )
)

logging.basicConfig(
    level   = logging.INFO,
    format  = "%(asctime)s  [%(levelname)s]  %(message)s",
    datefmt = "%Y-%m-%d %H:%M:%S UTC",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("live_engine.log", encoding="utf-8"),
        memory_handler,
    ],
)
log = logging.getLogger("HybridSystem")


# ── 3. DATA FETCH (backtest — bulk historical) ────────────────────────────────
def parse_duration(dur: str) -> int:
    """Convert '5y', '6m', '30d' → Unix ms timestamp."""
    now  = datetime.utcnow()
    unit = dur[-1]
    val  = int(dur[:-1])
    days = val * {"d": 1, "m": 30, "y": 365}[unit]
    return int((time.time() - (days * 86400)) * 1_000)


def fetch_all_candles(symbol: str, interval: str, duration: str) -> pd.DataFrame:
    """
    Fetch ALL historical klines from Bybit v5 in 1 000-candle pages.
    Returns a DataFrame sorted oldest → newest.
    """
    bv_interval = bybit_interval(interval)
    start_ms    = parse_duration(duration)
    end_ms      = int(time.time() * 1_000)
    all_rows    = []

    while True:
        resp = requests.get(
            BYBIT_BASE_URL,
            params={
                "category": "linear",
                "symbol":   symbol,
                "interval": bv_interval,
                "start":    start_ms,
                "end":      end_ms,
                "limit":    1_000,
            },
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()

        if payload.get("retCode") != 0:
            raise RuntimeError(
                f"Bybit API error {payload.get('retCode')}: {payload.get('retMsg')}"
            )

        rows = payload["result"]["list"]   # newest-first
        if not rows:
            break

        rows_asc = list(reversed(rows))
        all_rows.extend(rows_asc)
        print(f"\r  Fetched {len(all_rows):,} candles…", end="")

        if len(rows) < 1_000:
            break

        oldest_ts = int(rows_asc[0][0])
        if oldest_ts <= start_ms:
            break
        end_ms = oldest_ts - 1
        time.sleep(0.2)

    print(f"\r  ✅ Total candles fetched: {len(all_rows):,}          ")

    df = pd.DataFrame(
        all_rows,
        columns=["time", "open", "high", "low", "close", "volume", "turnover"],
    ).astype(float)
    df["datetime"] = pd.to_datetime(df["time"], unit="ms", utc=True)
    df = df.sort_values("time").reset_index(drop=True)
    return df


# ── 4. INDICATORS ─────────────────────────────────────────────────────────────

def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple Moving Average."""
    return series.rolling(period).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index (Wilder's smoothing)."""
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100.0 - (100.0 / (1.0 + rs))


def atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average True Range."""
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=period, adjust=False).mean()


def macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """MACD line, signal line, histogram."""
    ema_fast = ema(series, fast)
    ema_slow = ema(series, slow)
    macd_line = ema_fast - ema_slow
    signal_line = ema(macd_line, signal)
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def bollinger_bands(series: pd.Series, period: int = 20, std_dev: float = 2.0):
    """Bollinger Bands: upper, middle, lower."""
    middle = sma(series, period)
    std = series.rolling(period).std()
    upper = middle + std_dev * std
    lower = middle - std_dev * std
    return upper, middle, lower


def adx(high: pd.Series, low: pd.Series, close: pd.Series, period: int = 14) -> pd.Series:
    """Average Directional Index."""
    prev_high = high.shift(1)
    prev_low = low.shift(1)
    prev_close = close.shift(1)

    plus_dm = (high - prev_high).where((high - prev_high) > (prev_low - low), 0.0)
    plus_dm = plus_dm.where(plus_dm > 0, 0.0)

    minus_dm = (prev_low - low).where((prev_low - low) > (high - prev_high), 0.0)
    minus_dm = minus_dm.where(minus_dm > 0, 0.0)

    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)

    atr_val = tr.ewm(span=period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(span=period, adjust=False).mean() / atr_val.replace(0, np.nan))
    minus_di = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / atr_val.replace(0, np.nan))

    dx = 100 * ((plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan))
    return dx.ewm(span=period, adjust=False).mean()


def stochastic(high: pd.Series, low: pd.Series, close: pd.Series,
               k_period: int = 14, d_period: int = 3):
    """Stochastic Oscillator %K and %D."""
    lowest = low.rolling(k_period).min()
    highest = high.rolling(k_period).max()
    k = 100 * (close - lowest) / (highest - lowest).replace(0, np.nan)
    d = k.rolling(d_period).mean()
    return k, d


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    """On-Balance Volume."""
    direction = np.sign(close.diff()).fillna(0)
    return (volume * direction).cumsum()


def volume_ratio(volume: pd.Series, period: int = 20) -> pd.Series:
    """Volume / Volume SMA ratio."""
    vol_ma = volume.rolling(period).mean()
    return volume / vol_ma.replace(0, np.nan)


# ═══════════════════════════════════════════════════════════════════════════════
#  MASTER INDICATOR COMPUTATION
# ═══════════════════════════════════════════════════════════════════════════════

# Registry of all available indicators and their column names
INDICATOR_REGISTRY = {
    # Trend
    "ema_8": ("ema", 8), "ema_13": ("ema", 13), "ema_21": ("ema", 21),
    "ema_34": ("ema", 34), "ema_55": ("ema", 55), "ema_89": ("ema", 89),
    "ema_200": ("ema", 200),
    "sma_20": ("sma", 20), "sma_50": ("sma", 50), "sma_200": ("sma", 200),
    "adx_14": ("adx", 14),
    # Momentum
    "rsi_14": ("rsi", 14), "rsi_7": ("rsi", 7), "rsi_21": ("rsi", 21),
    "macd_line": ("macd_line",), "macd_signal": ("macd_signal",), "macd_hist": ("macd_hist",),
    "stoch_k": ("stoch_k",), "stoch_d": ("stoch_d",),
    # Volatility
    "atr_14": ("atr", 14),
    "bb_upper": ("bb_upper",), "bb_middle": ("bb_middle",), "bb_lower": ("bb_lower",),
    "atr_pct": ("atr_pct",),
    # Volume
    "volume_ma_20": ("volume_ma", 20),
    "volume_ratio": ("volume_ratio",),
    "obv": ("obv",),
    # Custom
    "candle_body_ratio": ("candle_body_ratio",),
}

# Columns that strategies can reference
ALL_INDICATOR_COLUMNS = list(INDICATOR_REGISTRY.keys()) + [
    "open", "high", "low", "close", "volume",
]

def compute_all_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute ALL indicators at once. Returns enriched DataFrame.
    This is called ONCE per data load — strategies reference columns by name.
    """
    df = df.copy()
    c = df["close"]
    h = df["high"]
    l = df["low"]
    v = df["volume"]

    # ── Trend ──────────────────────────────────────────────────────────────
    for period in [8, 13, 21, 34, 55, 89, 200]:
        df[f"ema_{period}"] = ema(c, period)
    for period in [20, 50, 200]:
        df[f"sma_{period}"] = sma(c, period)
    df["adx_14"] = adx(h, l, c, 14)

    # ── Momentum ───────────────────────────────────────────────────────────
    for period in [7, 14, 21]:
        df[f"rsi_{period}"] = rsi(c, period)
    ml, sl, mh = macd(c)
    df["macd_line"] = ml
    df["macd_signal"] = sl
    df["macd_hist"] = mh
    sk, sd = stochastic(h, l, c)
    df["stoch_k"] = sk
    df["stoch_d"] = sd

    # ── Volatility ─────────────────────────────────────────────────────────
    df["atr_14"] = atr(h, l, c, 14)
    bb_u, bb_m, bb_l = bollinger_bands(c)
    df["bb_upper"] = bb_u
    df["bb_middle"] = bb_m
    df["bb_lower"] = bb_l
    df["atr_pct"] = df["atr_14"] / c

    # ── Volume ─────────────────────────────────────────────────────────────
    df["volume_ma_20"] = sma(v, 20)
    df["volume_ratio"] = volume_ratio(v, 20)
    df["obv"] = obv(c, v)

    # ── Custom ─────────────────────────────────────────────────────────────
    body = (c - df["open"]).abs()
    wick = h - l
    df["candle_body_ratio"] = body / wick.replace(0, np.nan)
    df["candle_body_ratio"] = df["candle_body_ratio"].fillna(0.5)

    # ── Warmup NaN handling ────────────────────────────────────────────────
    df = df.iloc[200:].reset_index(drop=True)

    print(f"Indicators computed: {len(df)} rows, {len(df.columns)} columns")
    return df

def generate_signals(df: pd.DataFrame) -> pd.DataFrame:
    """Evaluate expression trees to generate signals."""
    df = df.copy()
    signals = pd.Series(0.0, index=df.index)
    
    buy_mask = ((((df['adx_14'] <= 39.7) | ((df['ema_55'] < df['bb_upper']) | (df['stoch_k'] < 72.6))) | ((df['ema_21'] > df['ema_13']) & (df['ema_21'].shift(1) <= df['ema_13'].shift(1)))) | (((df['adx_14'] < 32.9) & (df['bb_upper'] > df['ema_13'])) | ((df['ema_8'] < df['ema_55']) & (df['ema_8'].shift(1) >= df['ema_55'].shift(1)))))
    signals[buy_mask > 0] = 1.0
    
    sell_mask = ((((df['macd_signal'] <= df['close']) | ((df['rsi_21'] <= 30.6) | (df['rsi_21'] > 23.3))) & ((df['low'] < df['ema_21']) & ((df['stoch_d'] <= 39.2) & (df['rsi_21'] > 54.1)))) | ((((df['sma_200'] > df['ema_21']) | (df['stoch_d'] > 75.0)) | ((df['bb_lower'] <= df['low']) & (df['bb_lower'] <= df['ema_13']))) & (((df['rsi_21'] <= 27.2) | (df['ema_200'] > df['ema_21'])) | ((df['ema_21'] > df['ema_200']) & (df['rsi_21'] <= 45.3)))))
    signals[sell_mask > 0] = -1.0
    
    df["signal"] = signals
    return df

def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = compute_all_indicators(df)
    df = generate_signals(df)
    return df


# ── 5. SIGNAL ─────────────────────────────────────────────────────────────────
def get_signal(row) -> str:
    if row.get("signal", 0.0) == 1.0:
        return "BUY"
    if row.get("signal", 0.0) == -1.0:
        return "SELL"
    return "HOLD"


# ── 6. INTRA-CANDLE EXIT RESOLVER ─────────────────────────────────────────────
def resolve_exit(
    position: dict, o: float, h: float, l: float
) -> Tuple[Optional[str], Optional[float]]:
    side = position["side"]
    sl   = position["sl"]
    tp   = position["tp"]

    if side == "BUY":
        if o <= sl: return "SL_GAP", o
        if o >= tp: return "TP_GAP", o
    else:
        if o >= sl: return "SL_GAP", o
        if o <= tp: return "TP_GAP", o

    if side == "BUY":
        sl_hit = l <= sl
        tp_hit = h >= tp
        if sl_hit and tp_hit:
            return ("SL", sl) if abs(o - sl) <= abs(tp - o) else ("TP", tp)
        if sl_hit: return "SL", sl
        if tp_hit: return "TP", tp
    else:
        sl_hit = h >= sl
        tp_hit = l <= tp
        if sl_hit and tp_hit:
            return ("SL", sl) if abs(sl - o) <= abs(o - tp) else ("TP", tp)
        if sl_hit: return "SL", sl
        if tp_hit: return "TP", tp

    return None, None


# ── 7. BACKTEST ENGINE ────────────────────────────────────────────────────────
def backtest(df: pd.DataFrame):
    balance          = INITIAL_BALANCE
    position         = None
    trades           = []
    equity_curve     = [INITIAL_BALANCE]
    last_signal_tick = -(COOLDOWN + 1)
    warmup           = max(89, 14, VOL_PERIOD) + 10
    both_hit_count   = 0
    gap_count        = 0

    for i in range(warmup, len(df)):
        row = df.iloc[i]
        o, h, l = row["open"], row["high"], row["low"]

        # ── Check exit on open position ──────────────────────────────────────
        if position:
            reason, price_exit = resolve_exit(position, o, h, l)
            if reason:
                if "GAP" in reason:
                    gap_count += 1
                if position["side"] == "BUY":
                    if l <= position["sl"] and h >= position["tp"]:
                        both_hit_count += 1
                else:
                    if h >= position["sl"] and l <= position["tp"]:
                        both_hit_count += 1

                is_win     = reason in ("TP", "TP_GAP")
                multiplier = RR_RATIO if is_win else -1.0
                pnl        = position["risk"] * multiplier
                pnl       -= position["risk"] * FEE * 2
                balance   += pnl

                # ── All exit-level derived metrics ───────────────────────────
                entry_close   = position["entry_close"]
                # removed detailed metrics for custom strategy
                sl_dist       = round(abs(position["entry"] - position["sl"]), 4)
                tp_dist       = round(abs(position["tp"]    - position["entry"]), 4)

                trades.append({
                    # ── Identity ─────────────────────────────────────────────
                    "trade_no":              len(trades) + 1,
                    "side":                  position["side"],
                    "entry_time":            position["entry_time"],
                    "exit_time":             row["datetime"],

                    # ── Price levels ─────────────────────────────────────────
                    "entry":                 position["entry"],
                    "exit_price":            round(price_exit, 4),
                    "sl":                    round(position["sl"], 4),
                    "tp":                    round(position["tp"], 4),
                    "sl_dist":               sl_dist,
                    "tp_dist":               tp_dist,
                    "rr_ratio":              RR_RATIO,
                    "sl_mult":               SL_MULT,

                    # ── Result ───────────────────────────────────────────────
                    "exit_type":             reason,
                    "result":                "TP" if is_win else "SL",
                    "risk_usd":              round(position["risk"], 4),
                    "pnl_usd":               round(pnl, 4),
                    "balance":               round(balance, 4),

                    # ── Indicator snapshot at entry ──────────────────────────
                    "entry_close":           round(entry_close,  4),
                    "entry_atr":             round(position["entry_atr"],     4),
                    "entry_atr_pct":         round(position["entry_atr_pct"], 6),

                    # ── EMA alignment strength ───────────────────────────────
                    "placeholder_stat": 0,

                    # ── Boolean conditions (why trade fired) ─────────────────
                    "cond_buy": position["cond_buy"],
                    "cond_sell": position["cond_sell"],

                    # ── Human-readable signal reason ─────────────────────────
                    "signal_reason":         position["signal_reason"],
                })
                equity_curve.append(round(balance, 4))
                position = None

        # ── Check entry signal ────────────────────────────────────────────────
        if position is None and get_signal(row) in ("BUY", "SELL"):
            if i - last_signal_tick >= COOLDOWN:
                signal  = get_signal(row)
                entry   = row["close"]
                atr     = row["atr_14"]
                sl_dist = atr * SL_MULT
                tp_dist = atr * RR_RATIO
                risk    = balance * RISK_PER_TRADE

                # ── Evaluate every individual condition ───────────────────────
                cond_buy  = bool(signal == "BUY")
                cond_sell = bool(signal == "SELL")

                # ── Build human-readable reason string ────────────────────────
                if signal == "BUY":
                    reason_parts = ["BUY SIGNAL"]
                else:
                    reason_parts = ["SELL SIGNAL"]

                position = {
                    # ── Trade mechanics ───────────────────────────────────────
                    "side":               signal,
                    "entry":              entry,
                    "sl":                 entry - sl_dist if signal == "BUY" else entry + sl_dist,
                    "tp":                 entry + tp_dist if signal == "BUY" else entry - tp_dist,
                    "risk":               risk,
                    "entry_time":         row["datetime"],

                    # ── Indicator snapshot ────────────────────────────────────
                    "entry_close":        round(row["close"], 4),
                    "entry_atr":          round(row["atr_14"], 4),
                    "entry_atr_pct":      round(row["atr_pct"], 6),

                    # ── Boolean conditions ────────────────────────────────────
                    "cond_buy": cond_buy,
                    "cond_sell": cond_sell,

                    # ── Human summary ─────────────────────────────────────────
                    "signal_reason":        " | ".join(reason_parts),
                }
                last_signal_tick = i

    # ── Record any open position at end of data ─────────────────────────────
    if position:
        last_row       = df.iloc[-1]
        last_close     = last_row["close"]
        entry_close    = position["entry_close"]
        sl_dist_open   = round(abs(position["entry"] - position["sl"]), 4)
        tp_dist_open   = round(abs(position["tp"]    - position["entry"]), 4)

        # Unrealised PnL
        if position["side"] == "BUY":
            unreal_pnl = (last_close - position["entry"]) / position["entry"] * position["risk"]
        else:
            unreal_pnl = (position["entry"] - last_close) / position["entry"] * position["risk"]
        unreal_pnl -= position["risk"] * FEE * 2

        trades.append({
            "trade_no":              len(trades) + 1,
            "side":                  position["side"],
            "entry_time":            position["entry_time"],
            "exit_time":             last_row["datetime"],
            "entry":                 position["entry"],
            "exit_price":            round(last_close, 4),
            "sl":                    round(position["sl"], 4),
            "tp":                    round(position["tp"], 4),
            "sl_dist":               sl_dist_open,
            "tp_dist":               tp_dist_open,
            "rr_ratio":              RR_RATIO,
            "sl_mult":               SL_MULT,
            "exit_type":             "OPEN",
            "result":                "OPEN",
            "risk_usd":              round(position["risk"], 4),
            "pnl_usd":               round(unreal_pnl, 4),
            "balance":               round(balance, 4),
            "entry_close":           round(entry_close, 4),
            "entry_atr":             round(position["entry_atr"], 4),
            "entry_atr_pct":         round(position["entry_atr_pct"], 6),
            "placeholder_stat":      0,
            "cond_buy":              position["cond_buy"],
            "cond_sell":             position["cond_sell"],
            "signal_reason":         position["signal_reason"],
        })
        print(f"\n  ** OPEN POSITION recorded: {position['side']} @ {position['entry']:.2f}")
        print(f"     SL={position['sl']:.2f}  TP={position['tp']:.2f}  Current={last_close:.2f}")
        print(f"     Unrealised PnL: ${unreal_pnl:.2f}")

    return pd.DataFrame(trades), equity_curve, both_hit_count, gap_count


# ── 8. STATS ──────────────────────────────────────────────────────────────────
def print_stats(
    trades_df: pd.DataFrame,
    equity: list,
    both_hit: int,
    gap_hits: int,
) -> pd.DataFrame:
    final  = equity[-1] if len(equity) > 1 else INITIAL_BALANCE
    total  = len(trades_df)
    wins   = (trades_df["result"] == "TP").sum()
    losses = (trades_df["result"] == "SL").sum()
    wr     = wins / total * 100 if total else 0
    pf     = (wins * RR_RATIO) / losses if losses > 0 else float("inf")
    ret    = (final - INITIAL_BALANCE) / INITIAL_BALANCE * 100

    eq  = pd.Series(equity)
    dd  = (eq - eq.cummax()) / eq.cummax() * 100
    mdd = dd.min()

    ret_series = eq.pct_change().dropna()
    years  = {"30d": 1/12, "6m": 0.5, "1y": 1, "3y": 3, "5y": 5}.get(DURATION, 1)
    tpy    = total / years if years > 0 else total
    sharpe = (
        ret_series.mean() / ret_series.std() * np.sqrt(tpy)
        if ret_series.std() > 0 and len(ret_series) > 1
        else 0
    )

    exit_counts = trades_df["exit_type"].value_counts()

    print("\n" + "═" * 58)
    print(f"  {'BACKTEST — FIXED INTRA-CANDLE SL/TP RESOLUTION':^56}")
    print("═" * 58)
    print(f"  Symbol        : {SYMBOL}  ({INTERVAL})  [{DURATION}]")
    print(f"  Start Balance : ${INITIAL_BALANCE:,.2f}")
    print(f"  Final Balance : ${final:,.2f}")
    print(f"  Net Return    : {ret:+.2f}%")
    print(f"  Max Drawdown  : {mdd:.2f}%")
    print(f"  Sharpe Ratio  : {sharpe:.2f}")
    print("─" * 58)
    print(f"  Total Trades  : {total}")
    print(f"  Wins / Losses : {wins} / {losses}")
    print(f"  Win Rate      : {wr:.1f}%")
    print(f"  Profit Factor : {pf:.2f}")
    print("─" * 58)
    print(f"  ★ EXIT TYPE BREAKDOWN:")
    for etype, cnt in exit_counts.items():
        print(f"     {etype:<12}: {cnt:>5}  ({cnt / total * 100:.1f}%)")
    print(f"  ★ Same-candle both-hit (proximity used) : {both_hit}")
    print(f"  ★ Gap-open exits (slippage fill at open): {gap_hits}")
    print("═" * 58)

    if not trades_df.empty:
        trades_df = trades_df.copy()

        trades_df["month"] = trades_df["exit_time"].dt.to_period("M")
        monthly_rows = []
        for month, grp in trades_df.groupby("month"):
            grp  = grp.sort_values("exit_time")
            idx0 = grp.index[0]
            bal0 = grp.loc[idx0, "balance"] - grp.loc[idx0, "pnl_usd"]
            pct  = grp["pnl_usd"].sum() / bal0 * 100 if bal0 else 0
            monthly_rows.append({"Month": str(month), "Return (%)": round(pct, 2)})
        print("\n  📅 Monthly Returns (%):")
        print(pd.DataFrame(monthly_rows).to_string(index=False))

        trades_df["year"] = trades_df["exit_time"].dt.to_period("Y")
        yearly_rows = []
        for year, grp in trades_df.groupby("year"):
            grp  = grp.sort_values("exit_time")
            idx0 = grp.index[0]
            bal0 = grp.loc[idx0, "balance"] - grp.loc[idx0, "pnl_usd"]
            pct  = grp["pnl_usd"].sum() / bal0 * 100 if bal0 else 0
            yearly_rows.append({"Year": str(year), "Return (%)": round(pct, 2)})
        print("\n  📆 Yearly Returns (%):")
        print(pd.DataFrame(yearly_rows).to_string(index=False))

    return trades_df


# ── 9. CSV EXPORT ─────────────────────────────────────────────────────────────
def export_csv(trades_df: pd.DataFrame) -> None:
    if trades_df.empty:
        print("  ⚠️  No trades to export.")
        return

    fname  = f"trade_history_{SYMBOL}_{INTERVAL}_{DURATION}_fixed_test2.csv"
    export = trades_df.copy()

    # ── Derived metrics ───────────────────────────────────────────────────────
    export["pnl_pct"] = (
        export["pnl_usd"] / (export["balance"] - export["pnl_usd"]) * 100
    ).round(4)

    export["holding_hours"] = (
        (export["exit_time"] - export["entry_time"])
        .dt.total_seconds() / 3600
    ).round(2)

    

    # ── Column ordering ───────────────────────────────────────────────────────
    col_order = [
        # identity
        "trade_no", "side", "entry_time", "exit_time", "holding_hours",
        # price levels
        "entry", "exit_price", "sl", "tp",
        "sl_dist", "tp_dist", "rr_ratio", "sl_mult",
        # result
        "exit_type", "result", "risk_usd", "pnl_usd", "pnl_pct", "balance",
        # indicator snapshot
        "entry_close",
        "entry_atr", "entry_atr_pct",
        "cond_buy", "cond_sell",
        # human-readable full reason
        "signal_reason",
    ]

    # Keep any unexpected columns at the end
    extra = [c for c in export.columns if c not in col_order]
    export = export[[c for c in col_order if c in export.columns] + extra]

    export.to_csv(fname, index=False)
    print(f"\n  💾 CSV saved → {fname}")
    print(f"      Rows    : {len(export)}")
    print(f"      Columns : {len(export.columns)}")
    print(f"      Fields  : {list(export.columns)}")
    
    # ── Export Simplified CSV matching requested format ──────────────────────
    simplified_cols = [
        "trade_no", "side", "entry_time", "exit_time", "entry", "exit_price", 
        "sl", "tp", "exit_type", "result", "risk_usd", "pnl_usd", "balance",
        "month", "year", "pnl_pct"
    ]
    simp_df = export.copy()
    # Add month and year since they aren't explicitly in the original export columns yet
    simp_df["month"] = simp_df["exit_time"].dt.to_period("M")
    simp_df["year"] = simp_df["exit_time"].dt.to_period("Y")
    
    simp_df = simp_df[[c for c in simplified_cols if c in simp_df.columns]]
    simp_fname = f"trade_history_{SYMBOL}_{INTERVAL}_{DURATION}_simplified_test2.csv"
    simp_df.to_csv(simp_fname, index=False)
    print(f"  💾 Simplified CSV saved → {simp_fname}")


# ── 10. CHARTS ────────────────────────────────────────────────────────────────
def plot_results(trades_df: pd.DataFrame, equity: list) -> None:
    if len(equity) < 2:
        print("  ⚠️  Not enough trades to plot.")
        return

    fig = plt.figure(figsize=(18, 20), facecolor="#0d1117")
    gs  = gridspec.GridSpec(5, 2, figure=fig, hspace=0.55, wspace=0.35)
    plt.rcParams.update({
        "text.color":       "white",
        "axes.labelcolor":  "white",
        "xtick.color":      "grey",
        "ytick.color":      "grey",
    })

    eq         = pd.Series(equity)
    trade_nums = range(len(eq))
    dd         = (eq - eq.cummax()) / eq.cummax() * 100

    # Equity curve
    ax1 = fig.add_subplot(gs[0, :])
    ax1.plot(trade_nums, eq.values, color="#00c897", linewidth=1.5)
    ax1.fill_between(trade_nums, INITIAL_BALANCE, eq.values,
                     where=(eq.values >= INITIAL_BALANCE), alpha=0.15, color="#00c897")
    ax1.fill_between(trade_nums, INITIAL_BALANCE, eq.values,
                     where=(eq.values < INITIAL_BALANCE),  alpha=0.15, color="#ff4c4c")
    ax1.axhline(INITIAL_BALANCE, color="white", linewidth=0.6, linestyle="--", alpha=0.5)
    ax1.set_xlabel("Trade Number", fontsize=10)
    ax1.set_ylabel("Balance (USD)", fontsize=10)
    ax1.set_title("Equity Curve  (per TP/SL execution)", fontsize=13, pad=8)
    ax1.set_facecolor("#0d1117")
    for sp in ax1.spines.values(): sp.set_color("#333")

    # Drawdown
    ax2 = fig.add_subplot(gs[1, :])
    ax2.fill_between(trade_nums, dd.values, 0, color="#ff4c4c", alpha=0.6)
    ax2.set_xlabel("Trade Number", fontsize=10)
    ax2.set_ylabel("DD %", fontsize=10)
    ax2.set_title("Drawdown %  (per trade)", fontsize=13, pad=8)
    ax2.set_facecolor("#0d1117")
    for sp in ax2.spines.values(): sp.set_color("#333")

    if not trades_df.empty:
        trades_df = trades_df.copy()

        # Exit type pie
        ax3 = fig.add_subplot(gs[2, 0])
        exit_counts = trades_df["exit_type"].value_counts()
        pie_colors  = {
            "TP": "#00c897", "SL": "#ff4c4c",
            "TP_GAP": "#00ffcc", "SL_GAP": "#ff8888",
        }
        colors = [pie_colors.get(k, "#aaaaaa") for k in exit_counts.index]
        ax3.pie(
            exit_counts.values,
            labels=exit_counts.index,
            colors=colors,
            autopct="%1.1f%%",
            textprops={"fontsize": 9},
        )
        ax3.set_title("Exit Type Distribution", fontsize=11, pad=8)
        ax3.set_facecolor("#0d1117")

        # PnL per trade bars
        ax4 = fig.add_subplot(gs[2, 1])
        colors4 = ["#00c897" if p > 0 else "#ff4c4c" for p in trades_df["pnl_usd"]]
        ax4.bar(trades_df["trade_no"], trades_df["pnl_usd"], color=colors4, width=0.8)
        ax4.axhline(0, color="white", linewidth=0.5)
        ax4.set_xlabel("Trade Number", fontsize=10)
        ax4.set_ylabel("PnL (USD)", fontsize=10)
        ax4.set_title("PnL per Trade ($)", fontsize=12, pad=8)
        ax4.set_facecolor("#0d1117")
        for sp in ax4.spines.values(): sp.set_color("#333")

        # Monthly returns
        ax5 = fig.add_subplot(gs[3, :])
        trades_df["month"] = trades_df["exit_time"].dt.to_period("M")
        monthly_pct = []
        for month, grp in trades_df.groupby("month"):
            grp  = grp.sort_values("exit_time")
            idx0 = grp.index[0]
            bal0 = grp.loc[idx0, "balance"] - grp.loc[idx0, "pnl_usd"]
            pct  = grp["pnl_usd"].sum() / bal0 * 100 if bal0 else 0
            monthly_pct.append((str(month), round(pct, 2)))
        if monthly_pct:
            m_df = pd.DataFrame(monthly_pct, columns=["month", "pct"])
            m_c  = ["#00c897" if v >= 0 else "#ff4c4c" for v in m_df["pct"]]
            bars = ax5.bar(range(len(m_df)), m_df["pct"], color=m_c, width=0.7)
            ax5.axhline(0, color="white", linewidth=0.5)
            ax5.set_xticks(range(len(m_df)))
            ax5.set_xticklabels(m_df["month"], rotation=45, ha="right", fontsize=7)
            ax5.set_ylabel("%", fontsize=10)
            ax5.set_title("Monthly Returns (%)", fontsize=12, pad=8)
            ax5.set_facecolor("#0d1117")
            for sp in ax5.spines.values(): sp.set_color("#333")
            for bar, val in zip(bars, m_df["pct"]):
                if abs(val) > 0.1:
                    ax5.text(
                        bar.get_x() + bar.get_width() / 2,
                        bar.get_height() + (0.1 if val >= 0 else -0.3),
                        f"{val:.1f}%",
                        ha="center",
                        va="bottom" if val >= 0 else "top",
                        fontsize=6, color="white", alpha=0.8,
                    )

        # Yearly returns
        ax6 = fig.add_subplot(gs[4, :])
        trades_df["year"] = trades_df["exit_time"].dt.to_period("Y")
        yearly_pct = []
        for year, grp in trades_df.groupby("year"):
            grp  = grp.sort_values("exit_time")
            idx0 = grp.index[0]
            bal0 = grp.loc[idx0, "balance"] - grp.loc[idx0, "pnl_usd"]
            pct  = grp["pnl_usd"].sum() / bal0 * 100 if bal0 else 0
            yearly_pct.append((str(year), round(pct, 2)))
        if yearly_pct:
            y_df = pd.DataFrame(yearly_pct, columns=["year", "pct"])
            y_c  = ["#00c897" if v >= 0 else "#ff4c4c" for v in y_df["pct"]]
            y_b  = ax6.bar(range(len(y_df)), y_df["pct"], color=y_c, width=0.6)
            ax6.axhline(0, color="white", linewidth=0.5)
            ax6.set_xticks(range(len(y_df)))
            ax6.set_xticklabels(y_df["year"], rotation=30, ha="right", fontsize=9)
            ax6.set_ylabel("%", fontsize=10)
            ax6.set_title("Yearly Returns (%)", fontsize=12, pad=8)
            ax6.set_facecolor("#0d1117")
            for sp in ax6.spines.values(): sp.set_color("#333")
            for bar, val in zip(y_b, y_df["pct"]):
                ax6.text(
                    bar.get_x() + bar.get_width() / 2,
                    bar.get_height() + (0.5 if val >= 0 else -1.5),
                    f"{val:.1f}%",
                    ha="center",
                    va="bottom" if val >= 0 else "top",
                    fontsize=9, color="white", fontweight="bold",
                )

    fig.suptitle(
        f"{SYMBOL} · {INTERVAL} · {DURATION}  |  RR {RR_RATIO}  "
        f"Risk {RISK_PER_TRADE * 100:.0f}%  |  FIXED: gap-open + proximity heuristic  |  src: Bybit",
        fontsize=13, y=0.999,
    )

    chart_path = f"backtest_chart_{SYMBOL}_{INTERVAL}_{DURATION}_test2.png"
    plt.savefig(chart_path, dpi=120, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    print(f"\n  📊 Chart saved → {chart_path}")


def export_advanced_stats(trades_df: pd.DataFrame, equity: list) -> None:
    if trades_df.empty: return
    
    # ── Calculations ────────────────────────────────────────────────────────
    initial_balance = INITIAL_BALANCE
    final_balance = equity[-1] if equity else initial_balance
    total_return_pct = ((final_balance - initial_balance) / initial_balance) * 100
    total_return_x = final_balance / initial_balance
    
    wins = trades_df[trades_df["result"] == "TP"]
    losses = trades_df[trades_df["result"] == "SL"]
    
    gross_profit = wins["pnl_usd"].sum() if not wins.empty else 0
    gross_loss = abs(losses["pnl_usd"].sum()) if not losses.empty else 0
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
    
    eq_series = pd.Series(equity)
    drawdowns = (eq_series - eq_series.cummax()) / eq_series.cummax() * 100
    max_drawdown = abs(drawdowns.min())
    
    total_trades = len(trades_df)
    tp_exits = len(wins)
    sl_exits = len(losses)
    win_rate = (tp_exits / total_trades * 100) if total_trades > 0 else 0
    
    avg_win = wins["pnl_usd"].mean() if not wins.empty else 0
    avg_loss = abs(losses["pnl_usd"].mean()) if not losses.empty else 0
    
    trades_df["hold_time"] = trades_df["exit_time"] - trades_df["entry_time"]
    avg_hold_time = trades_df["hold_time"].mean().total_seconds() / 3600 # in hours
    
    # Consecutive streaks
    results = trades_df["result"].values
    max_consec_wins = max_consec_losses = current_wins = current_losses = 0
    for r in results:
        if r == "TP":
            current_wins += 1
            current_losses = 0
            if current_wins > max_consec_wins: max_consec_wins = current_wins
        else:
            current_losses += 1
            current_wins = 0
            if current_losses > max_consec_losses: max_consec_losses = current_losses
            
    # Directional Win Rates
    buy_trades = trades_df[trades_df["side"] == "BUY"]
    sell_trades = trades_df[trades_df["side"] == "SELL"]
    buy_wr = (len(buy_trades[buy_trades["result"] == "TP"]) / len(buy_trades) * 100) if not buy_trades.empty else 0
    sell_wr = (len(sell_trades[sell_trades["result"] == "TP"]) / len(sell_trades) * 100) if not sell_trades.empty else 0
    
    # Time-based Averages
    total_days = (trades_df["exit_time"].max() - trades_df["entry_time"].min()).days
    if total_days <= 0: total_days = 1
    total_months = total_days / 30.44
    total_weeks = total_days / 7
    
    avg_trades_month = total_trades / total_months
    avg_trades_week = total_trades / total_weeks
    avg_return_month_pct = total_return_pct / total_months
    avg_return_week_pct = total_return_pct / total_weeks

    # ── Formatting Output ──────────────────────────────────────────────────
    report = f"""==================================================
  ADVANCED STRATEGY REPORT
==================================================
Final balance           : ${final_balance:,.2f} (Started ${initial_balance:,.0f})
Total return            : {total_return_x:.2f}× ({total_return_pct:,.2f}%)
Profit factor           : {profit_factor:.2f}
Max drawdown            : {max_drawdown:.2f}%

Total trades            : {total_trades} ({tp_exits} TP / {sl_exits} SL)
Win rate                : {win_rate:.1f}% (TP exits)
Avg win                 : ${avg_win:,.2f} (Per TP trade)
Avg loss                : ${avg_loss:,.2f} (Per SL trade)

Avg hold time           : {avg_hold_time:.1f}h (Per trade)
Max consec wins         : {max_consec_wins} (In a row)
Max consec losses       : {max_consec_losses} (In a row)

Avg trades per week     : {avg_trades_week:.1f}
Avg trades per month    : {avg_trades_month:.1f}
Avg return per week     : {avg_return_week_pct:,.2f}%
Avg return per month    : {avg_return_month_pct:,.2f}%

BUY win rate            : {buy_wr:.1f}%
SELL win rate           : {sell_wr:.1f}%
=================================================="""

    report_path = f"advanced_stats_{SYMBOL}_{INTERVAL}_{DURATION}_test2.txt"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)
    print(f"  📝 Advanced stats saved → {report_path}")

# ── 11. FULL BACKTEST RUNNER ──────────────────────────────────────────────────
def run_backtest() -> None:
    print("=" * 60)
    print(f"  🚀 BACKTEST: {SYMBOL} {INTERVAL} for {DURATION}  [Bybit]")
    print("  🔧 Fix: intra-candle SL/TP via gap-open + proximity heuristic")
    print("=" * 60 + "\n")

    df_raw = fetch_all_candles(SYMBOL, INTERVAL, DURATION)
    df_raw = add_indicators(df_raw)

    print("⚙️  Running backtest…")
    trades_df, equity, both_hit, gap_hits = backtest(df_raw)

    trades_df = print_stats(trades_df, equity, both_hit, gap_hits)
    export_csv(trades_df)
    plot_results(trades_df, equity)
    export_advanced_stats(trades_df, equity)

    if not trades_df.empty:
        json_path = f"backtest_results_{SYMBOL}_{INTERVAL}_{DURATION}_fixed_test2.json"
        # Convert bool columns for JSON serialisation
        serialisable = trades_df.copy()
        bool_cols = serialisable.select_dtypes(include="bool").columns
        serialisable[bool_cols] = serialisable[bool_cols].astype(int)

        output = {
            "summary": {
                "symbol":          SYMBOL,
                "interval":        INTERVAL,
                "duration":        DURATION,
                "data_source":     "Bybit v5",
                "initial_balance": INITIAL_BALANCE,
                "final_balance":   equity[-1] if len(equity) > 1 else INITIAL_BALANCE,
                "total_trades":    len(trades_df),
                "win_rate":        round(
                    (trades_df["result"] == "TP").sum() / len(trades_df) * 100, 2
                ),
                "fix_stats": {
                    "same_candle_both_hit_resolved": both_hit,
                    "gap_open_exits":                gap_hits,
                },
            },
            "trades": serialisable.to_dict(orient="records"),
        }
        with open(json_path, "w") as f:
            json.dump(output, f, indent=2, default=str)
        print(f"📁 Full results saved → {json_path}")

    print("\n✅ Backtest complete.\n")


# ══════════════════════════════════════════════════════════════════════════════
#  ★  LIVE SIGNAL ENGINE
# ══════════════════════════════════════════════════════════════════════════════

def get_latest_data(
    symbol: str   = SYMBOL,
    interval: str = INTERVAL,
    limit: int    = LIVE_CANDLE_LIMIT,
) -> Optional[pd.DataFrame]:
    bv_interval = bybit_interval(interval)
    try:
        resp = requests.get(
            BYBIT_BASE_URL,
            params={
                "category": "linear",
                "symbol":   symbol,
                "interval": bv_interval,
                "limit":    limit + 1,
            },
            timeout=15,
        )
        resp.raise_for_status()
        payload = resp.json()

        if payload.get("retCode") != 0:
            log.error(
                f"get_latest_data: Bybit error {payload.get('retCode')}: "
                f"{payload.get('retMsg')}"
            )
            return None

        rows = payload["result"]["list"]
        if not rows or len(rows) < 2:
            log.warning("get_latest_data: insufficient candles returned.")
            return None

        rows_asc = list(reversed(rows))
        df = pd.DataFrame(
            rows_asc,
            columns=["time", "open", "high", "low", "close", "volume", "turnover"],
        ).astype(float)
        df["datetime"] = pd.to_datetime(df["time"], unit="ms", utc=True)
        df = add_indicators(df)

        log.info(
            f"get_latest_data: fetched {len(df)} candles "
            f"(live candle: {df.iloc[-1]['datetime']})"
        )
        return df

    except requests.exceptions.RequestException as exc:
        log.error(f"get_latest_data: network error — {exc}")
        return None
    except Exception as exc:
        log.error(f"get_latest_data: unexpected error — {exc}")
        return None


def send_signal_to_api(signal_data: dict) -> bool:
    headers = {"Content-Type": "application/json"}

    for attempt in range(1, API_RETRY_ATTEMPTS + 1):
        try:
            log.info(
                f"send_signal_to_api: attempt {attempt}/{API_RETRY_ATTEMPTS} — "
                f"POST {SIGNAL_API_URL}  payload={signal_data}"
            )
            resp = requests.post(
                SIGNAL_API_URL,
                json=signal_data,
                headers=headers,
                timeout=API_TIMEOUT,
            )
            if resp.ok:
                log.info(
                    f"send_signal_to_api: ✅ SUCCESS  "
                    f"status={resp.status_code}  body={resp.text[:200]}"
                )
                return True
            else:
                log.warning(
                    f"send_signal_to_api: ⚠️  HTTP {resp.status_code}  "
                    f"body={resp.text[:200]}"
                )
        except requests.exceptions.Timeout:
            log.error(f"send_signal_to_api: ⏱️  Timeout on attempt {attempt}")
        except requests.exceptions.ConnectionError as exc:
            log.error(f"send_signal_to_api: 🔌 Connection error — {exc}")
        except requests.exceptions.RequestException as exc:
            log.error(f"send_signal_to_api: ❌ Request error — {exc}")

        if attempt < API_RETRY_ATTEMPTS:
            log.info(f"send_signal_to_api: retrying in {API_RETRY_DELAY}s…")
            time.sleep(API_RETRY_DELAY)

    log.error(
        f"send_signal_to_api: ❌ All {API_RETRY_ATTEMPTS} attempts failed."
    )
    return False


def _print_periodic_summary(cycle_count: int, last_trade: Optional[dict]) -> None:
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print("\n" + "─" * 58)
    print(f"  ⏱️  PERIODIC SUMMARY  [{now_str}]")
    print(f"  Cycles completed : {cycle_count}")
    if last_trade:
        side_icon = "🟢" if last_trade["signal"] == "BUY" else "🔴"
        print(
            f"  Last signal sent : {side_icon} {last_trade['signal']}"
            f"  @  ${last_trade['price']:.2f}"
            f"  |  candle: {last_trade['time']}"
            f"  |  interval: {last_trade['interval']}"
        )
    else:
        print("  Last signal sent : — none yet —")
    print("─" * 58 + "\n")


def run_live_engine() -> None:
    print("\n" + "═" * 60)
    print("  🟢  LIVE SIGNAL ENGINE STARTED  [Bybit]")
    print(f"  Symbol   : {SYMBOL}  |  Interval : {INTERVAL}")
    print(f"  API URL  : {SIGNAL_API_URL}")
    print(f"  Candles  : last {LIVE_CANDLE_LIMIT} per cycle")
    print(f"  Polling  : every 60 seconds")
    print(f"  Summary  : printed every {PERIODIC_PRINT_EVERY}s")
    print("  Press Ctrl+C to stop.")
    print("═" * 60 + "\n")

    log.info("Live engine initialised (Bybit).")
    log.info(f"  Symbol={SYMBOL}  Interval={INTERVAL}  API={SIGNAL_API_URL}")

    # ── ML Filter initialisation ──────────────────────────────────────────
    ml_filter = None
    position_sizer = None
    if ML_AVAILABLE and ML_FILTER_ENABLED:
        try:
            ml_filter = MLTradeFilter()
            ml_filter.load_model()
            position_sizer = PositionSizer()
            log.info("🤖 ML Trade Filter LOADED — filtering active")
        except FileNotFoundError:
            log.warning(
                "⚠️  No trained ML model found. Run run_ml_pipeline.py first. "
                "Proceeding WITHOUT ML filter."
            )
            ml_filter = None
    else:
        log.info("ℹ️  ML filter disabled or modules not available")

    last_sent_signal: Optional[str] = None
    last_trade:       Optional[dict] = None
    cycle_count:      int            = 0
    last_periodic_ts: float          = time.time()
    last_processed_candle_time: Optional[str] = None
    last_signal_time_ms: Optional[float] = None

    while True:
        time.sleep(60)

        cycle_count += 1
        now_utc = datetime.now(timezone.utc)
        log.info(f"{'─' * 55}")
        log.info(f"🔄 CYCLE #{cycle_count:04d}  —  {now_utc.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        log.info(f"{'─' * 55}")

        df = get_latest_data(symbol=SYMBOL, interval=INTERVAL, limit=LIVE_CANDLE_LIMIT)

        if df is None or df.empty:
            log.warning("run_live_engine: ⚠️  Data fetch failed. Skipping cycle.")
        else:
            latest_row   = df.iloc[-2]
            candle_time  = str(latest_row["datetime"])

            if candle_time == last_processed_candle_time:
                pass # Just wait for next close, heartbeat will still fire
            else:
                last_processed_candle_time = candle_time
                current_sig  = get_signal(latest_row)
                latest_close = float(latest_row["close"])

                log.info(
                    f"📊 Closed candle  close={latest_close:.2f}  "
                    f"atr_pct={latest_row['atr_pct']:.4f}  "
                    f"signal={latest_row['signal']}" 
                )

                cooldown_active = False
                if last_signal_time_ms is not None:
                    signal_row_idx = df.index[df['time'] == last_signal_time_ms].tolist()
                    if signal_row_idx:
                        intervals_passed = df.index[-2] - signal_row_idx[0]
                        if intervals_passed < COOLDOWN:
                            cooldown_active = True
                            log.info(f"⏳ Cooldown active. {intervals_passed}/{COOLDOWN} candles passed. Skipping signal.")

                if not cooldown_active:
                    log.info(
                        f"🔍 Signal evaluation → [{current_sig}]  "
                        f"(last sent: [{last_sent_signal}])"
                    )

                    if current_sig in ("BUY", "SELL"):
                        if current_sig == last_sent_signal:
                            log.info(
                                f"⏭️  Duplicate signal [{current_sig}] — already sent. Skipping."
                            )
                        else:
                            # ── Pre-flight: check for open position ──────────────────
                            try:
                                check_url  = SIGNAL_API_URL.replace("/api/trade", "/api/trade/open")
                                check_resp = requests.get(check_url, timeout=API_TIMEOUT)
                                if check_resp.ok:
                                    open_trades = check_resp.json()
                                    has_open = any(
                                        t.get("symbol") == SYMBOL and t.get("status") == "OPEN"
                                        for t in open_trades
                                    )
                                    if has_open:
                                        log.info(
                                            f"🚫 Open position already exists for {SYMBOL}. "
                                            "Skipping signal."
                                        )
                                        last_sent_signal = current_sig
                                        log.info(f"✔️  Cycle #{cycle_count:04d} complete.\n")
                                        continue
                            except Exception as exc:
                                log.warning(f"⚠️  Pre-flight check failed: {exc}. Proceeding.")

                            atr     = float(latest_row["atr_14"])
                            rsi     = float(latest_row["rsi_14"]) if "rsi_14" in latest_row else 0.0
                            atr_pct = float(latest_row["atr_pct"]) if "atr_pct" in latest_row else 0.0

                            # ── HARD VALIDATION GATE ──────────────────────────────
                            # Re-verify signal value before sending to API.
                            valid = True
                            rejection_reasons = []

                            if atr_pct <= ATR_GATE:
                                valid = False
                                rejection_reasons.append(
                                    f"ATR%={atr_pct:.4f} <= gate={ATR_GATE}"
                                )

                            sig_val = float(latest_row.get("signal", 0.0))
                            if current_sig == "BUY" and sig_val != 1.0:
                                valid = False
                                rejection_reasons.append(
                                    f"Signal value={sig_val} != 1.0 for BUY"
                                )
                            elif current_sig == "SELL" and sig_val != -1.0:
                                valid = False
                                rejection_reasons.append(
                                    f"Signal value={sig_val} != -1.0 for SELL"
                                )

                            if not valid:
                                log.warning(
                                    f"SIGNAL BLOCKED [{current_sig}] — "
                                    f"failed validation: {'; '.join(rejection_reasons)}"
                                )

                                # ── Notify via backend → Telegram ─────────────
                                try:
                                    alert_url = SIGNAL_API_URL.replace("/api/trade", "/api/health/alert")
                                    reason_lines = "\n".join(f"  • {r}" for r in rejection_reasons)
                                    tg_msg = (
                                        f"🚫 <b>SIGNAL BLOCKED: {current_sig} {SYMBOL}</b>\n"
                                        f"<i>Validation gate rejected this signal</i>\n\n"
                                        f"<b>Price:</b> ${latest_close:,.2f}\n"
                                        f"<b>RSI:</b> {rsi:.1f}\n"
                                        f"<b>ATR%:</b> {atr_pct:.4f}\n\n"
                                        f"<b>Rejection reasons:</b>\n{reason_lines}\n\n"
                                        f"⏰ Cycle #{cycle_count}"
                                    )
                                    requests.post(
                                        alert_url,
                                        json={"message": tg_msg},
                                        timeout=5,
                                    )
                                except Exception as exc:
                                    log.warning(f"[ALERT] Failed to notify backend: {exc}")

                                log.info(f"Cycle #{cycle_count:04d} complete.\n")
                                continue

                            log.info(
                                f"SIGNAL VALIDATED [{current_sig}] — "
                                f"all conditions confirmed"
                            )

                            sl_dist_val = atr * SL_MULT
                            tp_dist_val = atr * RR_RATIO
                            sl = latest_close - sl_dist_val if current_sig == "BUY" else latest_close + sl_dist_val
                            tp = latest_close + tp_dist_val if current_sig == "BUY" else latest_close - tp_dist_val

                            # ── ML FILTER GATE ─────────────────────────────────────
                            ml_prob = None
                            ml_confidence = "N/A"
                            pos_size = POSITION_SIZE

                            if ml_filter is not None:
                                try:
                                    ml_features = {"side": current_sig}
                                    ml_prob = ml_filter.predict_trade_probability(ml_features)
                                    ml_confidence = ml_filter.classify_confidence(ml_prob)

                                    log.info(
                                        f"🤖 ML Filter: prob={ml_prob:.3f} "
                                        f"confidence={ml_confidence}"
                                    )

                                    if ml_confidence == "SKIP":
                                        log.info(
                                            f"🚫 ML FILTER REJECTED [{current_sig}] — "
                                            f"prob={ml_prob:.3f} < 0.70. Skipping."
                                        )
                                        last_sent_signal = current_sig
                                        log.info(f"✔️  Cycle #{cycle_count:04d} complete.\n")
                                        continue

                                    # Adjust position size based on confidence
                                    if position_sizer is not None:
                                        sizing = position_sizer.calculate_risk(
                                            balance=INITIAL_BALANCE,
                                            probability=ml_prob,
                                        )
                                        if ml_confidence == "HIGH_CONFIDENCE":
                                            pos_size = round(POSITION_SIZE * 1.5, 4)
                                            log.info(
                                                f"💎 HIGH CONFIDENCE — position size "
                                                f"increased to {pos_size}"
                                            )

                                except Exception as exc:
                                    log.warning(
                                        f"⚠️  ML filter error: {exc}. "
                                        "Proceeding with unfiltered signal."
                                    )

                            signal_payload = {
                                "symbol":        SYMBOL,
                                "side":          current_sig,
                                "entry":         round(latest_close, 2),
                                "sl":            round(sl, 2),
                                "tp":            round(tp, 2),
                                "position_size": pos_size,
                                "atr":           round(atr, 4),
                                "rsi":           round(rsi, 2),
                                "ml_probability": ml_prob,
                                "ml_confidence":  ml_confidence,
                            }
                            success = send_signal_to_api(signal_payload)

                            if success:
                                last_sent_signal = current_sig
                                last_signal_time_ms = latest_row["time"]
                                last_trade = {
                                    "signal":   current_sig,
                                    "price":    latest_close,
                                    "time":     candle_time,
                                    "interval": INTERVAL,
                                }
                                log.info(f"✅ Signal [{current_sig}] sent & recorded.")
                            else:
                                log.error(
                                    f"❌ Signal [{current_sig}] NOT delivered. "
                                    "Will retry next cycle."
                                )
                    else:
                        log.info(
                            f"💤 Signal = HOLD. No API call. "
                            f"(last_sent_signal stays [{last_sent_signal}])."
                        )

        log.info(f"✔️  Cycle #{cycle_count:04d} complete.\n")

        # ── Heartbeat ────────────────────────────────────────────────────────
        try:
            hb_url = SIGNAL_API_URL.replace("/api/trade", "/api/health/heartbeat")
            row = df.iloc[-1] if df is not None and not df.empty else None
            requests.post(
                hb_url,
                json={
                    "symbol":     SYMBOL,
                    "interval":   INTERVAL,
                    "atrPct":     float(row["atr_pct"]) if row is not None else None,
                    "rsi":        float(row.get("rsi_14", 0.0)) if row is not None else None,
                    "lastSignal": last_sent_signal,
                    "logs":       memory_handler.get_lines(),
                },
                timeout=5,
            )
            log.debug("[HEARTBEAT] Sent")
        except Exception as e:
            log.warning(f"[HEARTBEAT] Failed: {e}")

        if time.time() - last_periodic_ts >= PERIODIC_PRINT_EVERY:
            _print_periodic_summary(cycle_count, last_trade)
            last_periodic_ts = time.time()


# ══════════════════════════════════════════════════════════════════════════════
#  ★  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":

    # Phase 1: Full historical backtest
    run_backtest()

    # Phase 2: Live signal engine (runs forever)
    try:
        run_live_engine()
    except KeyboardInterrupt:
        log.info("\n🛑 Live engine stopped by user (KeyboardInterrupt).")
        print("\n🛑 Live engine stopped. Goodbye.")