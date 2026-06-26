#!/usr/bin/env python3
"""
Adaptive rule-based level trader PoC.

What this file is for
---------------------
This is a proof harness for the idea discussed in the chat:

1. Keep the deterministic fractal level engine from imagegenv2.py.
2. Keep the transparent rule baseline: BUY near support/lower range, SELL near resistance/upper range.
3. Add a *smart online adaptation layer* that chooses among several parameter arms while trading.
4. Add dynamic risk sizing from the same recent evidence.
5. Add Binance 1-minute kline download for multi-symbol testing.

The adaptation layer is deliberately a contextual bandit, not deep RL. With one week of data, full RL is
very likely to learn an all-hold policy or overfit. A bandit over interpretable rule-parameter arms gives
you a cleaner proof: "can the system learn that EURUSD-like conditions prefer min distance 50 while
EURGBP-like/choppy conditions prefer something else, using only closed-trade evidence from the last 1-3h?"

Examples
--------
Download Binance M1 candles:
    python adaptive_level_trader_poc.py download \
      --symbols BTCUSDT ETHUSDT EURUSDT \
      --start 2026-06-01 --end 2026-06-08 --market spot --data-dir data

Backtest downloaded CSVs:
    python adaptive_level_trader_poc.py backtest \
      --symbols BTCUSDT ETHUSDT EURUSDT --data-dir data --train-frac 0.65

Run a local no-internet smoke test:
    python adaptive_level_trader_poc.py synthetic --symbols EURUSD EURGBP --bars 5000

Notes
-----
- Binance spot does not usually provide true FX pairs like EURUSD/EURGBP. It can provide crypto/stablecoin
  or fiat-stablecoin pairs such as BTCUSDT, ETHUSDT, EURUSDT, depending on availability in your region/account.
  For EURUSD/EURGBP M1, keep using your MT5/OANDA/Dukascopy export and feed it to this same backtester.
- Binance klines do not include bid/ask spread. This script uses fee_bps/slippage_bps plus optional synthetic
  spread_bps unless your CSV has a spread column.
- This is not live-trading code and not proof of future profitability. It is a leakage-safe research harness.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Optional

import numpy as np
import pandas as pd

try:
    import requests
except Exception:  # pragma: no cover
    requests = None

RANDOM_SEED = 42
np.random.seed(RANDOM_SEED)

ACTIONS = {0: "hold", 1: "buy", 2: "sell"}


# -----------------------------------------------------------------------------
# Config
# -----------------------------------------------------------------------------

@dataclass(frozen=True)
class Arm:
    """One interpretable rule-parameter configuration.

    This is the object the adaptive layer learns to select online.
    """

    name: str
    broker_min_distance_points: int
    touch_band_points: float
    level_window: int
    context_window: int
    pivot_span: int
    max_levels: int
    tp_levels_ahead: int
    horizon_minutes: int
    sl_atr_mult: float
    min_rr: float
    max_rr: float
    risk_pct: float


@dataclass
class BacktestConfig:
    decision_every_minutes: int = 5
    atr_period: int = 30
    allow_shorts: bool = True
    initial_equity: float = 10_000.0
    train_frac: float = 0.65
    lookback_minutes: int = 180  # user requested latest history 1-3h
    fee_bps_per_side: float = 1.0
    slippage_bps: float = 0.5
    synthetic_spread_bps: float = 0.5
    max_position_notional_pct: float = 0.30
    min_samples_for_size_boost: int = 4
    max_size_multiplier: float = 2.5
    min_size_multiplier: float = 0.25
    exploration: float = 0.15
    cold_start_penalty: float = 0.03
    no_leakage_pending_updates: bool = True


def default_arms() -> list[Arm]:
    """Small grid focused on the user's known sensitivity issue.

    Include a EURUSD-like min-distance=50 arm, tighter EURGBP-like arms, and wider conservative arms.
    """
    return [
        Arm("tight_fast", 20, 6.0, 96, 180, 2, 18, 1, 30, 0.45, 0.55, 4.0, 0.0020),
        Arm("tight_range", 25, 10.0, 144, 240, 2, 22, 2, 60, 0.65, 0.60, 4.0, 0.0025),
        Arm("baseline_30", 30, 8.0, 144, 240, 2, 22, 2, 60, 0.70, 0.65, 4.0, 0.0025),
        Arm("eurusd_like_50", 50, 8.0, 120, 240, 4, 24, 3, 90, 0.50, 0.65, 4.0, 0.0030),
        Arm("wide_safe_60", 60, 12.0, 180, 360, 3, 24, 2, 120, 0.90, 0.70, 4.0, 0.0020),
        Arm("very_wide_80", 80, 14.0, 240, 360, 3, 26, 3, 150, 1.10, 0.75, 4.0, 0.0015),
    ]


# -----------------------------------------------------------------------------
# Binance download
# -----------------------------------------------------------------------------

BINANCE_BASE_URLS = {
    "spot": "https://api.binance.com/api/v3/klines",
    "usdm_futures": "https://fapi.binance.com/fapi/v1/klines",
}


def _to_utc_ms(x: str | int | float | pd.Timestamp | None) -> Optional[int]:
    if x is None:
        return None
    if isinstance(x, (int, float)):
        # Already ms if very large, seconds otherwise.
        return int(x if x > 10**11 else x * 1000)
    ts = pd.Timestamp(x, tz="UTC") if not isinstance(x, pd.Timestamp) else x.tz_convert("UTC")
    return int(ts.timestamp() * 1000)


def _interval_to_ms(interval: str) -> int:
    unit = interval[-1]
    n = int(interval[:-1])
    if unit == "m":
        return n * 60_000
    if unit == "h":
        return n * 3_600_000
    if unit == "d":
        return n * 86_400_000
    raise ValueError(f"Unsupported interval for this PoC: {interval!r}")


def binance_klines_to_frame(rows: list[list[Any]], symbol: str, interval: str) -> pd.DataFrame:
    cols = [
        "open_time", "open", "high", "low", "close", "volume", "close_time",
        "quote_asset_volume", "number_of_trades", "taker_buy_base_volume",
        "taker_buy_quote_volume", "ignore",
    ]
    df = pd.DataFrame(rows, columns=cols[: len(rows[0])]) if rows else pd.DataFrame(columns=cols)
    for col in ["open", "high", "low", "close", "volume", "quote_asset_volume", "taker_buy_base_volume", "taker_buy_quote_volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    df["datetime"] = pd.to_datetime(pd.to_numeric(df["open_time"], errors="coerce"), unit="ms", utc=True)
    df = df.dropna(subset=["datetime", "open", "high", "low", "close"]).sort_values("datetime")
    df = df.drop_duplicates("datetime").reset_index(drop=True)
    df["time"] = (df["datetime"].astype("int64") // 10**9).astype("int64")
    df["time_iso"] = df["datetime"].dt.strftime("%Y-%m-%dT%H:%M:%S%z").str.replace(r"(\+0000)$", "+00:00", regex=True)
    df["symbol"] = symbol.upper()
    df["timeframe"] = interval.upper()
    df["real_volume"] = df["volume"]
    df["spread"] = np.nan  # Binance klines do not include bid/ask spread.
    df["uid"] = np.arange(len(df), dtype=int)
    return df[["datetime", "time", "time_iso", "symbol", "timeframe", "open", "high", "low", "close", "volume", "spread", "real_volume", "uid"]]


def download_binance_klines(
    symbol: str,
    start: str,
    end: str,
    interval: str = "1m",
    market: str = "spot",
    limit: int = 1000,
    pause_s: float = 0.15,
) -> pd.DataFrame:
    """Download Binance historical klines using public REST.

    Uses startTime/endTime pagination. Spot max limit is 1000; futures also accepts 1000 safely.
    """
    if requests is None:
        raise RuntimeError("requests is not installed; install it or use CSV input")
    if market not in BINANCE_BASE_URLS:
        raise ValueError(f"market must be one of {sorted(BINANCE_BASE_URLS)}")
    url = BINANCE_BASE_URLS[market]
    symbol = symbol.upper().replace("/", "")
    start_ms = _to_utc_ms(start)
    end_ms = _to_utc_ms(end)
    step_ms = _interval_to_ms(interval)
    rows: list[list[Any]] = []

    current = start_ms
    while current is not None and current < end_ms:
        params = {
            "symbol": symbol,
            "interval": interval,
            "limit": int(min(limit, 1000)),
            "startTime": int(current),
            "endTime": int(end_ms),
        }
        r = requests.get(url, params=params, timeout=30)
        try:
            r.raise_for_status()
        except Exception as exc:
            raise RuntimeError(f"Binance request failed for {symbol}: {r.text[:500]}") from exc
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        last_open = int(batch[-1][0])
        next_start = last_open + step_ms
        if next_start <= current:
            break
        current = next_start
        time.sleep(pause_s)

    if not rows:
        raise RuntimeError(f"No klines returned for {symbol}. Check symbol availability on Binance {market}.")
    return binance_klines_to_frame(rows, symbol, interval)


def command_download(args: argparse.Namespace) -> None:
    out_dir = Path(args.data_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    for symbol in args.symbols:
        print(f"Downloading {symbol} {args.interval} {args.start} -> {args.end} ({args.market})")
        df = download_binance_klines(symbol, args.start, args.end, args.interval, args.market, args.limit)
        path = out_dir / f"{symbol.upper().replace('/', '')}_{args.interval}.csv"
        df.to_csv(path, index=False)
        print(f"  saved {len(df):,} rows to {path}")


# -----------------------------------------------------------------------------
# Data loading and synthetic data
# -----------------------------------------------------------------------------

def normalize_ohlcv(raw: pd.DataFrame, symbol: str | None = None) -> pd.DataFrame:
    df = raw.copy()
    df.columns = [str(c).strip().lower() for c in df.columns]
    aliases = {
        "timestamp": "datetime", "date": "datetime", "ts": "datetime",
        "bid_open": "open", "bid_high": "high", "bid_low": "low", "bid_close": "close",
        "tick_volume": "volume", "vol": "volume",
    }
    for src, dst in aliases.items():
        if src in df.columns and dst not in df.columns:
            df[dst] = df[src]
    if "datetime" not in df.columns:
        if "time_iso" in df.columns:
            df["datetime"] = df["time_iso"]
        elif "time" in df.columns:
            vals = pd.to_numeric(df["time"], errors="coerce")
            unit = "ms" if vals.dropna().median() > 10**11 else "s"
            df["datetime"] = pd.to_datetime(vals, unit=unit, utc=True, errors="coerce")
        elif "open_time" in df.columns:
            df["datetime"] = pd.to_datetime(pd.to_numeric(df["open_time"], errors="coerce"), unit="ms", utc=True, errors="coerce")
        else:
            raise ValueError("Need datetime/time_iso/time/open_time column")
    else:
        # If already datetime-like, keep it. If numeric, assume ms/seconds; else parse string.
        if pd.api.types.is_datetime64_any_dtype(df["datetime"]):
            df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce")
        else:
            numeric = pd.to_numeric(df["datetime"], errors="coerce")
            if numeric.notna().mean() > 0.8:
                unit = "ms" if numeric.dropna().median() > 10**11 else "s"
                df["datetime"] = pd.to_datetime(numeric, unit=unit, utc=True, errors="coerce")
            else:
                df["datetime"] = pd.to_datetime(df["datetime"], utc=True, errors="coerce")

    for col in ["open", "high", "low", "close"]:
        if col not in df.columns:
            raise ValueError(f"Missing OHLC column {col!r}")
        df[col] = pd.to_numeric(df[col], errors="coerce")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0.0)
    if "spread" not in df.columns:
        df["spread"] = np.nan
    df["spread"] = pd.to_numeric(df["spread"], errors="coerce")
    if "real_volume" not in df.columns:
        df["real_volume"] = df["volume"]
    df["real_volume"] = pd.to_numeric(df["real_volume"], errors="coerce").fillna(0.0)
    if "symbol" not in df.columns:
        df["symbol"] = symbol or "UNKNOWN"
    df["symbol"] = df["symbol"].astype(str).str.upper().str.replace("/", "", regex=False)
    if symbol:
        df["symbol"] = symbol.upper().replace("/", "")
    if "timeframe" not in df.columns:
        df["timeframe"] = "M1"

    df = df.dropna(subset=["datetime", "open", "high", "low", "close"])
    df = df.sort_values("datetime").drop_duplicates("datetime").reset_index(drop=True)
    df["time"] = (df["datetime"].astype("int64") // 10**9).astype("int64")
    df["time_iso"] = df["datetime"].dt.strftime("%Y-%m-%dT%H:%M:%S%z").str.replace(r"(\+0000)$", "+00:00", regex=True)
    df["uid"] = np.arange(len(df), dtype=int)
    return df[["datetime", "time", "time_iso", "symbol", "timeframe", "open", "high", "low", "close", "volume", "spread", "real_volume", "uid"]]


def load_symbol_csv(data_dir: str | Path, symbol: str, interval: str = "1m") -> pd.DataFrame:
    base = Path(data_dir)
    candidates = [
        base / f"{symbol.upper().replace('/', '')}_{interval}.csv",
        base / f"{symbol.upper().replace('/', '')}.csv",
        base / f"{symbol.lower().replace('/', '')}_{interval}.csv",
        base / f"{symbol.lower().replace('/', '')}.csv",
    ]
    for p in candidates:
        if p.exists():
            return normalize_ohlcv(pd.read_csv(p), symbol)
    raise FileNotFoundError(f"No CSV found for {symbol} in {base}. Tried: {', '.join(map(str, candidates))}")


def make_synthetic_symbol(symbol: str, n: int = 5000, seed: int = 42) -> pd.DataFrame:
    """Generate a deterministic M1 toy series with repeated ranges and occasional trends."""
    rng = np.random.default_rng(abs(hash(symbol)) % 2**32 + seed)
    ts = pd.date_range("2026-01-01", periods=n, freq="1min", tz="UTC")
    # EURGBP lower volatility; EURUSD moderately wider; crypto much wider.
    if symbol.upper().endswith("USDT") and not symbol.upper().startswith("EUR"):
        start_price = 100.0 if symbol.upper().startswith("ETH") else 50_000.0
        vol = start_price * 0.00045
        anchors = start_price * np.array([0.985, 0.995, 1.005, 1.018])
    elif symbol.upper() == "EURGBP":
        start_price, vol = 0.855, 0.000035
        anchors = np.array([0.8520, 0.8550, 0.8580, 0.8610])
    else:
        start_price, vol = 1.085, 0.000055
        anchors = np.array([1.078, 1.084, 1.089, 1.094])
    price = np.empty(n)
    price[0] = start_price
    regime = 0
    for i in range(1, n):
        if i % 900 == 0:
            regime = rng.choice([0, 1, -1], p=[0.60, 0.20, 0.20])
        nearest = anchors[np.argmin(np.abs(anchors - price[i - 1]))]
        mean_revert = 0.018 * (nearest - price[i - 1]) if regime == 0 else 0.0
        drift = regime * vol * 0.018
        shock = rng.normal(0, vol)
        if rng.random() < 0.004:
            shock += rng.choice([-1, 1]) * rng.uniform(2 * vol, 5 * vol)
        price[i] = max(1e-9, price[i - 1] + mean_revert + drift + shock)
    open_ = np.r_[price[0], price[:-1]]
    wick = np.abs(rng.normal(vol, vol * 0.5, n))
    high = np.maximum(open_, price) + wick
    low = np.minimum(open_, price) - wick
    df = pd.DataFrame({
        "datetime": ts,
        "symbol": symbol.upper(),
        "timeframe": "M1",
        "open": open_,
        "high": high,
        "low": low,
        "close": price,
        "volume": rng.integers(50, 500, n),
        "spread": np.nan,
        "real_volume": 0.0,
    })
    return normalize_ohlcv(df, symbol)


def infer_point(df: pd.DataFrame) -> float:
    """Infer a usable point size when no broker metadata is available.

    For real FX CSVs you should override this if needed. For Binance crypto, this rough tick-like point
    lets min_distance_points scale to price magnitude.
    """
    px = float(df["close"].median())
    if px >= 10_000:
        return 0.1
    if px >= 1_000:
        return 0.01
    if px >= 100:
        return 0.001
    if px >= 10:
        return 0.0001
    return 0.00001


# -----------------------------------------------------------------------------
# Level engine and features
# -----------------------------------------------------------------------------

def atr(frame: pd.DataFrame, period: int = 30) -> pd.Series:
    high, low, close = frame["high"], frame["low"], frame["close"]
    prev_close = close.shift(1)
    tr = pd.concat([(high - low), (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    return tr.rolling(period, min_periods=max(3, period // 3)).mean().bfill()


def detect_levels(chunk: pd.DataFrame, pivot_span: int = 2, max_levels: int = 24) -> list[dict[str, Any]]:
    """Fractal pivot levels equivalent to imagegenv2._detect_levels.

    Resistance = local pivot high. Support = local pivot low. Nearby same-type levels are merged,
    preferring newer/stronger levels.
    """
    if len(chunk) < pivot_span * 2 + 1:
        return []
    highs = chunk["high"].to_numpy(float)
    lows = chunk["low"].to_numpy(float)
    candidates: list[dict[str, Any]] = []
    for i in range(pivot_span, len(chunk) - pivot_span):
        high_window = highs[i - pivot_span: i + pivot_span + 1]
        low_window = lows[i - pivot_span: i + pivot_span + 1]
        if highs[i] == np.max(high_window):
            candidates.append({"idx": int(i), "price": float(highs[i]), "type": "resistance", "strength": float(highs[i] - np.min(low_window))})
        if lows[i] == np.min(low_window):
            candidates.append({"idx": int(i), "price": float(lows[i]), "type": "support", "strength": float(np.max(high_window) - lows[i])})
    if not candidates:
        return []
    price_range = max(float(chunk["high"].max() - chunk["low"].min()), 1e-12)
    min_sep = price_range * 0.012
    candidates = sorted(candidates, key=lambda x: (x["idx"], x["strength"]))
    filtered: list[dict[str, Any]] = []
    for item in candidates:
        replaced = False
        for j, existing in enumerate(filtered):
            if existing["type"] == item["type"] and abs(existing["price"] - item["price"]) < min_sep:
                if item["strength"] >= existing["strength"] or item["idx"] > existing["idx"]:
                    filtered[j] = item
                replaced = True
                break
        if not replaced:
            filtered.append(item)
    return sorted(filtered, key=lambda x: x["idx"])[-max_levels:]


def classify_regime(hist: pd.DataFrame, point: float) -> dict[str, Any]:
    if len(hist) < 40:
        return {"regime": "neutral_range", "score_buy": 0.5, "score_sell": 0.5, "range_pct": 0.5, "slope_pts": 0.0, "vol_pts": 0.0}
    close = hist["close"].to_numpy(float)
    y = close[-len(hist):]
    x = np.arange(len(y))
    slope = np.polyfit(x, y, 1)[0] / point
    rng = max(float(np.max(y) - np.min(y)), point)
    range_pct = float((y[-1] - np.min(y)) / rng)
    ret = np.diff(y) / point
    vol_pts = float(pd.Series(ret).rolling(30, min_periods=10).std().iloc[-1]) if len(ret) > 30 else float(np.std(ret) if len(ret) else 0.0)
    # Thresholds intentionally mild; pair-specific behaviour is delegated to arm selection.
    if vol_pts > 18 and abs(slope) < 0.05:
        regime = "high_vol_chop"
        buy_score = sell_score = 0.20
    elif slope > 0.06:
        regime = "bullish_trend"
        buy_score, sell_score = 1.0, 0.15
    elif slope < -0.06:
        regime = "bearish_trend"
        buy_score, sell_score = 0.15, 1.0
    else:
        regime = "neutral_range"
        buy_score = 1.0 if range_pct < 0.35 else 0.35 if range_pct < 0.55 else 0.10
        sell_score = 1.0 if range_pct > 0.65 else 0.35 if range_pct > 0.45 else 0.10
    return {"regime": regime, "score_buy": buy_score, "score_sell": sell_score, "range_pct": range_pct, "slope_pts": float(slope), "vol_pts": vol_pts}


def nearest_levels(levels: list[dict[str, Any]], price: float) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    below = sorted([l for l in levels if l["price"] < price], key=lambda x: x["price"], reverse=True)
    above = sorted([l for l in levels if l["price"] > price], key=lambda x: x["price"])
    return below, above


def build_event(i: int, df: pd.DataFrame, arm: Arm, point: float, cfg: BacktestConfig) -> Optional[dict[str, Any]]:
    min_history = max(arm.level_window, arm.context_window, cfg.atr_period) + 1
    if i < min_history or i >= len(df) - arm.horizon_minutes - 2:
        return None
    hist = df.iloc[: i + 1]
    chunk = hist.iloc[-arm.level_window:].reset_index(drop=True)
    levels = detect_levels(chunk, arm.pivot_span, arm.max_levels)
    if not levels:
        return None
    row = df.iloc[i]
    price = float(row["close"])
    below, above = nearest_levels(levels, price)
    if not below or not above:
        return None
    nearest = min(levels, key=lambda l: abs(l["price"] - price))
    dist_points = abs(price - nearest["price"]) / point
    if dist_points > arm.touch_band_points:
        return None
    if "_atr_points" in df.columns:
        atr_points = float(df.iloc[i]["_atr_points"])
    else:
        atr_points = float(atr(hist, cfg.atr_period).iloc[-1] / point)
    context = hist.iloc[-arm.context_window:]
    reg = classify_regime(context, point)
    range_high = float(chunk["high"].max())
    range_low = float(chunk["low"].min())
    range_pct_short = float((price - range_low) / max(range_high - range_low, point))
    spread_points = float(row["spread"]) if "spread" in df.columns and pd.notna(row["spread"]) else np.nan
    return {
        "i": int(i), "time": row["datetime"], "symbol": str(row["symbol"]), "price": price,
        "levels": levels, "below": below, "above": above, "nearest_type": nearest["type"],
        "nearest_price": float(nearest["price"]), "dist_points": float(dist_points), "atr_points": atr_points,
        "range_pct_short": range_pct_short, "spread_points": spread_points,
        "last_return_points": float(df.iloc[i]["_ret10_points"]) if "_ret10_points" in df.columns else float((df.iloc[i]["close"] - df.iloc[max(0, i - 10)]["close"]) / point),
        **reg,
    }


def rule_action(event: dict[str, Any]) -> int:
    """Rule baseline: buy support/lower range, sell resistance/upper range."""
    if event["regime"] == "high_vol_chop":
        return 0
    if event["nearest_type"] == "support" and event["range_pct_short"] < 0.58 and event["score_buy"] >= 0.25:
        return 1
    if event["nearest_type"] == "resistance" and event["range_pct_short"] > 0.42 and event["score_sell"] >= 0.25:
        return 2
    return 0


def context_key(event: dict[str, Any], action: int) -> tuple[str, str, str, str, str]:
    vol_bucket = "vol_low" if event["vol_pts"] < 6 else "vol_mid" if event["vol_pts"] < 16 else "vol_high"
    dist_bucket = "near" if event["dist_points"] <= 3 else "touch" if event["dist_points"] <= 8 else "far"
    return (event["symbol"], ACTIONS[action], event["regime"], vol_bucket, dist_bucket)


# -----------------------------------------------------------------------------
# Trade geometry and simulation
# -----------------------------------------------------------------------------

def estimate_spread_distance(row: pd.Series, price: float, point: float, cfg: BacktestConfig) -> float:
    if "spread" in row.index and pd.notna(row["spread"]):
        return float(row["spread"]) * point
    return price * cfg.synthetic_spread_bps / 10_000.0


def propose_trade(
    event: dict[str, Any],
    action: int,
    arm: Arm,
    df: pd.DataFrame,
    point: float,
    cfg: BacktestConfig,
    equity: float,
    size_multiplier: float = 1.0,
) -> Optional[dict[str, Any]]:
    if action == 0:
        return None
    if action == 2 and not cfg.allow_shorts:
        return None
    side = ACTIONS[action]
    exec_i = event["i"] + 1
    if exec_i >= len(df) - 2:
        return None
    row = df.iloc[exec_i]
    mid_open = float(row["open"])
    spread_dist = estimate_spread_distance(row, mid_open, point, cfg)
    slippage_dist = mid_open * cfg.slippage_bps / 10_000.0
    min_dist = arm.broker_min_distance_points * point

    if side == "buy":
        entry = mid_open + spread_dist / 2.0 + slippage_dist
        opposing = event["above"][: max(1, arm.tp_levels_ahead)]
        tp = float(opposing[-1]["price"]) if opposing else entry + min_dist
        support = event["below"][0]["price"] if event["below"] else event["nearest_price"]
        sl = min(float(support) - max(min_dist, arm.sl_atr_mult * event["atr_points"] * point), entry - min_dist)
        if tp - entry < min_dist:
            tp = entry + min_dist
        if entry - sl < min_dist:
            sl = entry - min_dist
        risk_dist = entry - sl
        reward_dist = tp - entry
    else:
        entry = mid_open - spread_dist / 2.0 - slippage_dist
        opposing = event["below"][: max(1, arm.tp_levels_ahead)]
        tp = float(opposing[-1]["price"]) if opposing else entry - min_dist
        resistance = event["above"][0]["price"] if event["above"] else event["nearest_price"]
        sl = max(float(resistance) + max(min_dist, arm.sl_atr_mult * event["atr_points"] * point), entry + min_dist)
        if entry - tp < min_dist:
            tp = entry - min_dist
        if sl - entry < min_dist:
            sl = entry + min_dist
        risk_dist = sl - entry
        reward_dist = entry - tp

    if risk_dist <= 0 or reward_dist <= 0:
        return None
    rr = reward_dist / risk_dist
    if not (arm.min_rr <= rr <= arm.max_rr):
        return None

    risk_money = max(1e-9, equity * arm.risk_pct * size_multiplier)
    qty = risk_money / risk_dist
    max_notional = equity * cfg.max_position_notional_pct
    qty = min(qty, max_notional / entry)
    if qty <= 0:
        return None

    return {
        "symbol": event["symbol"], "side": side, "arm": arm.name,
        "signal_i": int(event["i"]), "exec_i": int(exec_i), "event_time": event["time"], "exec_time": row["datetime"],
        "entry": float(entry), "sl": float(sl), "tp": float(tp),
        "risk_dist": float(risk_dist), "reward_dist": float(reward_dist), "rr": float(rr),
        "risk_points": float(risk_dist / point), "reward_points": float(reward_dist / point),
        "quantity": float(qty), "risk_money": float(risk_money), "size_multiplier": float(size_multiplier),
        "fee_bps_per_side": cfg.fee_bps_per_side, "slippage_bps": cfg.slippage_bps,
        "regime": event["regime"], "nearest_type": event["nearest_type"], "range_pct": event["range_pct_short"],
        "context_key": "|".join(context_key(event, action)),
    }


def simulate_trade(trade: dict[str, Any], df: pd.DataFrame, arm: Arm) -> dict[str, Any]:
    side = trade["side"]
    start = int(trade["exec_i"]) + 1
    end = min(len(df), start + int(arm.horizon_minutes))
    outcome = "timeout"
    exit_i = end - 1
    exit_price = float(df.iloc[exit_i]["close"])

    for j in range(start, end):
        row = df.iloc[j]
        hi, lo = float(row["high"]), float(row["low"])
        # Conservative same-minute tie-break: SL first.
        if side == "buy":
            if lo <= trade["sl"]:
                outcome, exit_i, exit_price = "sl", j, float(trade["sl"])
                break
            if hi >= trade["tp"]:
                outcome, exit_i, exit_price = "tp", j, float(trade["tp"])
                break
        else:
            if hi >= trade["sl"]:
                outcome, exit_i, exit_price = "sl", j, float(trade["sl"])
                break
            if lo <= trade["tp"]:
                outcome, exit_i, exit_price = "tp", j, float(trade["tp"])
                break

    qty = float(trade["quantity"])
    gross = qty * ((exit_price - trade["entry"]) if side == "buy" else (trade["entry"] - exit_price))
    entry_notional = qty * trade["entry"]
    exit_notional = qty * exit_price
    fees = (entry_notional + exit_notional) * trade["fee_bps_per_side"] / 10_000.0
    pnl = gross - fees
    r_mult = pnl / max(float(trade["risk_money"]), 1e-9)
    return {
        **trade,
        "exit_i": int(exit_i), "exit_time": df.iloc[exit_i]["datetime"], "exit_price": float(exit_price),
        "outcome": outcome, "gross_pnl": float(gross), "fees": float(fees), "pnl_money": float(pnl), "r_mult": float(r_mult),
    }


# -----------------------------------------------------------------------------
# Adaptive contextual bandit
# -----------------------------------------------------------------------------

class AdaptiveArmSelector:
    """Online model selection over rule-parameter arms.

    The selector receives only closed-trade outcomes. In backtests, updates are delayed until exit_i <= current_i,
    so later decisions cannot see future trade outcomes.
    """

    def __init__(self, arms: list[Arm], cfg: BacktestConfig):
        self.arms = {a.name: a for a in arms}
        self.cfg = cfg
        self.by_context: dict[tuple[str, str, str, str, str], deque[dict[str, Any]]] = defaultdict(deque)
        self.by_arm: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
        self.global_results: deque[dict[str, Any]] = deque()

    def update(self, result: dict[str, Any]) -> None:
        rec = {
            "arm": result["arm"], "r_mult": float(result["r_mult"]), "pnl_money": float(result["pnl_money"]),
            "win": float(result["pnl_money"] > 0), "exit_i": int(result["exit_i"]), "context_key": result["context_key"],
        }
        key_tuple = tuple(result["context_key"].split("|"))
        self.by_context[key_tuple].append(rec)
        self.by_arm[result["arm"]].append(rec)
        self.global_results.append(rec)

    def _recent(self, records: Iterable[dict[str, Any]], current_i: int) -> list[dict[str, Any]]:
        # With M1 bars, i difference is minutes.
        cut = current_i - self.cfg.lookback_minutes
        return [r for r in records if int(r["exit_i"]) >= cut and int(r["exit_i"]) <= current_i]

    @staticmethod
    def _ewma(values: list[float], alpha: float = 0.35) -> float:
        if not values:
            return 0.0
        out = values[0]
        for v in values[1:]:
            out = alpha * v + (1 - alpha) * out
        return float(out)

    def stats_for(self, arm_name: str, key: tuple[str, str, str, str, str], current_i: int) -> dict[str, float]:
        ctx = [r for r in self._recent(self.by_context.get(key, []), current_i) if r["arm"] == arm_name]
        arm_recent = self._recent(self.by_arm.get(arm_name, []), current_i)
        glob = self._recent(self.global_results, current_i)
        # Prefer exact context; otherwise fall back to arm-level then global.
        records = ctx if len(ctx) >= 2 else arm_recent if len(arm_recent) >= 2 else glob
        r_values = [float(r["r_mult"]) for r in records]
        wins = [float(r["win"]) for r in records]
        n = len(records)
        mean_r = float(np.mean(r_values)) if r_values else 0.0
        ewma_r = self._ewma(r_values) if r_values else 0.0
        win_rate = float(np.mean(wins)) if wins else 0.0
        return {"n": n, "mean_r": mean_r, "ewma_r": ewma_r, "win_rate": win_rate}

    def score(self, arm_name: str, key: tuple[str, str, str, str, str], current_i: int) -> float:
        st = self.stats_for(arm_name, key, current_i)
        total = max(1, len(self._recent(self.global_results, current_i)))
        n = st["n"]
        exploration = self.cfg.exploration * math.sqrt(math.log(total + 1) / (n + 1))
        cold_penalty = self.cfg.cold_start_penalty if n == 0 else 0.0
        # Positive recent R and win rate are good; cold arms get exploration but not a free pass.
        return 0.65 * st["ewma_r"] + 0.35 * st["mean_r"] + 0.10 * (st["win_rate"] - 0.5) + exploration - cold_penalty

    def size_multiplier(self, arm_name: str, key: tuple[str, str, str, str, str], current_i: int) -> float:
        st = self.stats_for(arm_name, key, current_i)
        if st["n"] < self.cfg.min_samples_for_size_boost:
            return 1.0
        # Map recent edge to a bounded multiplier. Negative edge shrinks size, strong edge grows size.
        raw = 1.0 + 1.25 * st["ewma_r"] + 0.75 * (st["win_rate"] - 0.5)
        return float(np.clip(raw, self.cfg.min_size_multiplier, self.cfg.max_size_multiplier))

    def choose(self, candidates: list[dict[str, Any]], current_i: int) -> Optional[dict[str, Any]]:
        if not candidates:
            return None
        best = None
        best_score = -1e9
        for cand in candidates:
            key = context_key(cand["event"], cand["action"])
            s = self.score(cand["arm"].name, key, current_i)
            # Tiny structural prior: prefer not too-low RR.
            s += 0.02 * min(float(cand["trade"]["rr"]), 3.0)
            if s > best_score:
                best_score = s
                best = {**cand, "selector_score": float(s), "selector_key": key}
        if best is not None:
            mult = self.size_multiplier(best["arm"].name, best["selector_key"], current_i)
            best["size_multiplier"] = mult
        return best


def candidate_for_arm(i: int, df: pd.DataFrame, arm: Arm, point: float, cfg: BacktestConfig, equity: float, size_multiplier: float = 1.0) -> Optional[dict[str, Any]]:
    event = build_event(i, df, arm, point, cfg)
    if event is None:
        return None
    action = rule_action(event)
    if action == 0:
        return None
    trade = propose_trade(event, action, arm, df, point, cfg, equity, size_multiplier=size_multiplier)
    if trade is None:
        return None
    return {"event": event, "action": action, "arm": arm, "trade": trade}


def seed_selector_from_training(
    df: pd.DataFrame,
    arms: list[Arm],
    point: float,
    cfg: BacktestConfig,
    train_end_i: int,
) -> AdaptiveArmSelector:
    """Offline train priors on all arms using only the training slice.

    This is not future leakage because it stops at train_end_i. It lets the selector start test with some
    symbol-specific prior knowledge, then adapt online based on the latest 1-3h of closed trades.
    """
    selector = AdaptiveArmSelector(arms, cfg)
    max_hist = max(max(a.level_window, a.context_window) for a in arms) + cfg.atr_period + 1
    idxs = range(max_hist, train_end_i, cfg.decision_every_minutes)
    for i in idxs:
        for arm in arms:
            cand = candidate_for_arm(i, df, arm, point, cfg, cfg.initial_equity, size_multiplier=1.0)
            if cand is None:
                continue
            res = simulate_trade(cand["trade"], df, arm)
            if int(res["exit_i"]) < train_end_i:
                selector.update(res)
    return selector


def run_static_policy(
    df: pd.DataFrame,
    arm: Arm,
    point: float,
    cfg: BacktestConfig,
    start_i: int,
    end_i: Optional[int] = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    end_i = end_i or len(df) - arm.horizon_minutes - 2
    equity = cfg.initial_equity
    logs: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    for i in range(start_i, end_i, cfg.decision_every_minutes):
        cand = candidate_for_arm(i, df, arm, point, cfg, equity, size_multiplier=1.0)
        action = "hold"
        if cand is not None:
            res = simulate_trade(cand["trade"], df, arm)
            equity += res["pnl_money"]
            trades.append(res)
            action = res["side"]
        logs.append({"i": i, "time": df.iloc[i]["datetime"], "equity": equity, "action": action, "policy": f"static_{arm.name}"})
    return pd.DataFrame(logs), pd.DataFrame(trades)


def run_adaptive_policy(
    df: pd.DataFrame,
    arms: list[Arm],
    point: float,
    cfg: BacktestConfig,
    start_i: int,
    selector: AdaptiveArmSelector,
    end_i: Optional[int] = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    end_i = end_i or len(df) - max(a.horizon_minutes for a in arms) - 2
    max_hist = max(max(a.level_window, a.context_window) for a in arms) + cfg.atr_period + 1
    start_i = max(start_i, max_hist)
    equity = cfg.initial_equity
    logs: list[dict[str, Any]] = []
    trades: list[dict[str, Any]] = []
    pending: list[dict[str, Any]] = []

    for i in range(start_i, end_i, cfg.decision_every_minutes):
        # Release only outcomes whose exit is now known. This avoids look-ahead leakage with overlapping trades.
        if cfg.no_leakage_pending_updates and pending:
            still_pending = []
            for res in pending:
                if int(res["exit_i"]) <= i:
                    selector.update(res)
                else:
                    still_pending.append(res)
            pending = still_pending

        # First propose with neutral size; selector chooses arm. Then re-propose selected arm with adapted size.
        neutral_candidates = []
        for arm in arms:
            cand = candidate_for_arm(i, df, arm, point, cfg, equity, size_multiplier=1.0)
            if cand is not None:
                neutral_candidates.append(cand)
        chosen = selector.choose(neutral_candidates, i)
        action = "hold"
        arm_name = None
        score = None
        if chosen is not None:
            arm = chosen["arm"]
            mult = chosen.get("size_multiplier", 1.0)
            # Rebuild exact trade with adapted sizing.
            event = chosen["event"]
            trade = propose_trade(event, chosen["action"], arm, df, point, cfg, equity, size_multiplier=mult)
            if trade is not None:
                trade["selector_score"] = chosen.get("selector_score")
                res = simulate_trade(trade, df, arm)
                equity += res["pnl_money"]
                trades.append(res)
                pending.append(res)
                action = res["side"]
                arm_name = res["arm"]
                score = res.get("selector_score")
        logs.append({"i": i, "time": df.iloc[i]["datetime"], "equity": equity, "action": action, "arm": arm_name, "selector_score": score, "policy": "adaptive_bandit"})

    # After evaluation, release pending for completeness. Does not affect decisions already made.
    for res in pending:
        selector.update(res)
    return pd.DataFrame(logs), pd.DataFrame(trades)


# -----------------------------------------------------------------------------
# Metrics and reporting
# -----------------------------------------------------------------------------

def max_drawdown_pct(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    peak = equity.cummax()
    dd = (equity - peak) / peak
    return float(100.0 * dd.min())


def summarize(policy: str, logs: pd.DataFrame, trades: pd.DataFrame, cfg: BacktestConfig) -> dict[str, Any]:
    end_equity = float(logs["equity"].iloc[-1]) if len(logs) else cfg.initial_equity
    if len(trades):
        wins = trades[trades["pnl_money"] > 0]
        losses = trades[trades["pnl_money"] < 0]
        gross_profit = float(wins["pnl_money"].sum()) if len(wins) else 0.0
        gross_loss = float(-losses["pnl_money"].sum()) if len(losses) else 0.0
        pf = gross_profit / gross_loss if gross_loss > 0 else np.inf if gross_profit > 0 else 0.0
        win_rate = float((trades["pnl_money"] > 0).mean())
        avg_r = float(trades["r_mult"].mean())
        avg_win = float(wins["pnl_money"].mean()) if len(wins) else 0.0
        avg_loss = float(losses["pnl_money"].mean()) if len(losses) else 0.0
    else:
        pf = win_rate = avg_r = avg_win = avg_loss = 0.0
    return {
        "policy": policy,
        "events": int(len(logs)),
        "trades": int(len(trades)),
        "end_equity": round(end_equity, 2),
        "net_pnl": round(end_equity - cfg.initial_equity, 2),
        "return_pct": round(100.0 * (end_equity / cfg.initial_equity - 1.0), 4),
        "max_drawdown_pct": round(max_drawdown_pct(logs["equity"]) if len(logs) else 0.0, 4),
        "win_rate": round(win_rate, 4),
        "profit_factor": round(float(pf), 4) if np.isfinite(pf) else np.inf,
        "avg_R": round(avg_r, 5),
        "avg_win_money": round(avg_win, 5),
        "avg_loss_money": round(avg_loss, 5),
    }


def backtest_one_symbol(df: pd.DataFrame, symbol: str, arms: list[Arm], cfg: BacktestConfig, output_dir: Path | None = None) -> pd.DataFrame:
    df = normalize_ohlcv(df, symbol)
    point = infer_point(df)
    df = df.copy()
    df["_atr_points"] = (atr(df, cfg.atr_period) / point).astype(float)
    df["_ret10_points"] = ((df["close"] - df["close"].shift(10)) / point).fillna(0.0).astype(float)
    max_hist = max(max(a.level_window, a.context_window) for a in arms) + cfg.atr_period + 1
    train_end_i = max(max_hist + 1, int(len(df) * cfg.train_frac))
    if train_end_i >= len(df) - max(a.horizon_minutes for a in arms) - 2:
        raise RuntimeError(f"Not enough bars for {symbol}: {len(df)}")

    baseline_arm = next((a for a in arms if a.name == "baseline_30"), arms[0])
    eurusd_arm = next((a for a in arms if a.name == "eurusd_like_50"), arms[-1])

    # Train priors using train slice only, then evaluate on test slice.
    selector = seed_selector_from_training(df, arms, point, cfg, train_end_i=train_end_i)
    adaptive_logs, adaptive_trades = run_adaptive_policy(df, arms, point, cfg, start_i=train_end_i, selector=selector)
    baseline_logs, baseline_trades = run_static_policy(df, baseline_arm, point, cfg, start_i=train_end_i)
    eurusd_logs, eurusd_trades = run_static_policy(df, eurusd_arm, point, cfg, start_i=train_end_i)

    rows = []
    for name, logs, trades in [
        ("Adaptive dynamic rule", adaptive_logs, adaptive_trades),
        (f"Static {baseline_arm.name}", baseline_logs, baseline_trades),
        (f"Static {eurusd_arm.name}", eurusd_logs, eurusd_trades),
    ]:
        row = summarize(name, logs, trades, cfg)
        row["symbol"] = symbol
        row["point"] = point
        row["train_end_time"] = str(df.iloc[train_end_i]["datetime"])
        rows.append(row)

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        adaptive_logs.to_csv(output_dir / f"{symbol}_adaptive_logs.csv", index=False)
        adaptive_trades.to_csv(output_dir / f"{symbol}_adaptive_trades.csv", index=False)
        baseline_trades.to_csv(output_dir / f"{symbol}_baseline_trades.csv", index=False)
        if len(adaptive_trades):
            by_arm = adaptive_trades.groupby("arm").agg(
                trades=("pnl_money", "size"),
                pnl=("pnl_money", "sum"),
                avg_R=("r_mult", "mean"),
                win_rate=("pnl_money", lambda s: float((s > 0).mean())),
                avg_size_mult=("size_multiplier", "mean"),
            ).reset_index()
            by_arm.to_csv(output_dir / f"{symbol}_adaptive_by_arm.csv", index=False)
    return pd.DataFrame(rows)


def print_symbol_diagnostics(symbol: str, output_dir: Path) -> None:
    path = output_dir / f"{symbol}_adaptive_by_arm.csv"
    if path.exists():
        print(f"\nAdaptive arm usage for {symbol}:")
        print(pd.read_csv(path).to_string(index=False))


def command_backtest(args: argparse.Namespace) -> None:
    cfg = BacktestConfig(
        decision_every_minutes=args.decision_every,
        train_frac=args.train_frac,
        lookback_minutes=args.lookback_minutes,
        allow_shorts=not args.no_shorts,
        fee_bps_per_side=args.fee_bps,
        slippage_bps=args.slippage_bps,
        synthetic_spread_bps=args.spread_bps,
    )
    arms = default_arms()
    output_dir = Path(args.output_dir)
    all_rows = []
    for symbol in args.symbols:
        print(f"\nBacktesting {symbol}...")
        df = load_symbol_csv(args.data_dir, symbol, args.interval)
        summary = backtest_one_symbol(df, symbol.upper().replace("/", ""), arms, cfg, output_dir=output_dir)
        print(summary.to_string(index=False))
        all_rows.append(summary)
        print_symbol_diagnostics(symbol.upper().replace("/", ""), output_dir)
    out = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / "summary.csv"
    out.to_csv(out_path, index=False)
    print(f"\nSaved combined summary to {out_path}")


def command_synthetic(args: argparse.Namespace) -> None:
    cfg = BacktestConfig(
        decision_every_minutes=args.decision_every,
        train_frac=args.train_frac,
        lookback_minutes=args.lookback_minutes,
        allow_shorts=not args.no_shorts,
        fee_bps_per_side=args.fee_bps,
        slippage_bps=args.slippage_bps,
        synthetic_spread_bps=args.spread_bps,
    )
    arms = default_arms()
    output_dir = Path(args.output_dir)
    all_rows = []
    for symbol in args.symbols:
        print(f"\nSynthetic backtest {symbol}...")
        df = make_synthetic_symbol(symbol, n=args.bars)
        summary = backtest_one_symbol(df, symbol.upper().replace("/", ""), arms, cfg, output_dir=output_dir)
        print(summary.to_string(index=False))
        all_rows.append(summary)
        print_symbol_diagnostics(symbol.upper().replace("/", ""), output_dir)
    out = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
    output_dir.mkdir(parents=True, exist_ok=True)
    out.to_csv(output_dir / "synthetic_summary.csv", index=False)


def command_all(args: argparse.Namespace) -> None:
    command_download(args)
    # Reuse backtest args namespace fields.
    command_backtest(args)


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Adaptive rule-based level trader PoC with Binance M1 downloader")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("download", help="download Binance klines to CSV")
    d.add_argument("--symbols", nargs="+", required=True)
    d.add_argument("--start", required=True, help="UTC start, e.g. 2026-06-01")
    d.add_argument("--end", required=True, help="UTC end, e.g. 2026-06-08")
    d.add_argument("--interval", default="1m")
    d.add_argument("--market", default="spot", choices=sorted(BINANCE_BASE_URLS))
    d.add_argument("--limit", type=int, default=1000)
    d.add_argument("--data-dir", default="data")
    d.set_defaults(func=command_download)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--symbols", nargs="+", required=True)
    common.add_argument("--interval", default="1m")
    common.add_argument("--decision-every", type=int, default=5)
    common.add_argument("--train-frac", type=float, default=0.65)
    common.add_argument("--lookback-minutes", type=int, default=180)
    common.add_argument("--fee-bps", type=float, default=1.0)
    common.add_argument("--slippage-bps", type=float, default=0.5)
    common.add_argument("--spread-bps", type=float, default=0.5)
    common.add_argument("--no-shorts", action="store_true")
    common.add_argument("--output-dir", default="results_adaptive")

    b = sub.add_parser("backtest", parents=[common], help="backtest downloaded/local CSVs")
    b.add_argument("--data-dir", default="data")
    b.set_defaults(func=command_backtest)

    s = sub.add_parser("synthetic", parents=[common], help="run no-internet synthetic smoke test")
    s.add_argument("--bars", type=int, default=5000)
    s.set_defaults(func=command_synthetic)

    a = sub.add_parser("all", parents=[common], help="download then backtest")
    a.add_argument("--start", required=True)
    a.add_argument("--end", required=True)
    a.add_argument("--market", default="spot", choices=sorted(BINANCE_BASE_URLS))
    a.add_argument("--limit", type=int, default=1000)
    a.add_argument("--data-dir", default="data")
    a.set_defaults(func=command_all)
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.func(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


