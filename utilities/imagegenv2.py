from __future__ import annotations

from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from utilities.settings import IMAGE_CACHE_DIR, logger

ChartProfile = Literal["standard", "global", "detail"]


def resolve_overlaps(levels: list[dict], min_dist: float, base_x: float = 0.8, shift_step: float = 1.4) -> list[dict]:
    if not levels:
        return []
    sorted_levels = sorted([dict(x) for x in levels], key=lambda x: x["price"])
    sorted_levels[0]["x_shift"] = base_x
    for i in range(1, len(sorted_levels)):
        current = sorted_levels[i]
        prev = sorted_levels[i - 1]
        current["x_shift"] = prev["x_shift"] + shift_step if abs(current["price"] - prev["price"]) < min_dist else base_x
    return sorted_levels


def generate_chart_image_v2(
    df: pd.DataFrame,
    target_uid: int,
    window_size: int = 96,
    symbol: str | None = None,
    *,
    aspect_ratio: str | tuple[float, float] | None = None,
    profile: ChartProfile = "standard",
    highlight_extrema: bool = False,
    max_levels: int = 12,
) -> Path | None:
    """Generate a deterministic state chart for a VLM from cached OHLCV bars.

    Required columns: ts(ms), open, high, low, close, volume, uid.
    The chart is always clipped so the final candle is exactly `target_uid`.

    `aspect_ratio` accepts "16:9", "4:3", "1:1" or a `(width, height)` tuple.
    The refactored advisory path uses:
      - global: 16:9 or 4:3, with max/min highlights.
      - detail: 1:1, last 2-3 hours.
    """
    from matplotlib import pyplot as plt
    import mplfinance as mpf

    if df is None or df.empty:
        logger.error("No dataframe supplied for chart generation")
        return None

    work = _prepare_frame(df)
    matches = work.index[work["uid"].astype(int) == int(target_uid)].tolist()
    if not matches:
        logger.error("UID %s not found in chart dataframe", target_uid)
        return None

    target_pos = int(matches[-1])
    start_pos = max(0, target_pos - window_size + 1)
    chunk = work.iloc[start_pos: target_pos + 1].copy().reset_index(drop=True)
    if chunk.empty:
        logger.error("Chart chunk is empty for uid=%s", target_uid)
        return None

    current = chunk.iloc[-1]
    levels = _detect_levels(chunk)
    recent_levels = levels[-max_levels:]
    logger.info(
        "chart render %s profile=%s uid=%s rows=%s start=%s end=%s low=%.5f high=%.5f levels=%s aspect=%s",
        symbol or "?",
        profile,
        target_uid,
        len(chunk),
        chunk.iloc[0]["time_iso"],
        chunk.iloc[-1]["time_iso"],
        float(chunk["low"].min()),
        float(chunk["high"].max()),
        len(recent_levels),
        aspect_ratio,
    )

    mpf_frame = chunk.set_index("datetime")[["open", "high", "low", "close", "volume"]].copy()

    addplots = []
    for lvl in recent_levels:
        line_series = pd.Series(np.nan, index=mpf_frame.index)
        start_i = max(0, min(int(lvl["idx"]), len(line_series) - 1))
        line_series.iloc[start_i:] = float(lvl["price"])
        addplots.append(
            mpf.make_addplot(
                line_series,
                color=lvl["color"],
                width=1.0 if profile != "global" else 0.8,
                linestyle="-",
                alpha=0.8,
            )
        )

    current_line = pd.Series(float(current["close"]), index=mpf_frame.index)
    addplots.append(mpf.make_addplot(current_line, color="gray", width=0.8, linestyle="--", alpha=0.5))

    mc = mpf.make_marketcolors(
        up="black",
        down="red",
        edge={"up": "black", "down": "red"},
        wick={"up": "black", "down": "red"},
        volume="inherit",
    )
    style = mpf.make_mpf_style(
        marketcolors=mc,
        gridstyle=":" if profile != "global" else "-.",
        gridaxis="vertical",
        facecolor="#f2f2f2",
        rc={"font.family": "monospace"},
    )

    fig, axlist = mpf.plot(
        mpf_frame,
        type="candle",
        style=style,
        volume=True,
        volume_panel=1,
        panel_ratios=(4, 1),
        addplot=addplots,
        returnfig=True,
        figsize=_figsize(aspect_ratio, profile),
        tight_layout=True,
        datetime_format="%b %d, %H:%M",
        xrotation=25 if profile == "global" else 35,
        warn_too_much_data=max(2500, len(chunk) + 1),
    )

    ax_main = axlist[0]
    ax_main.tick_params(axis="y", left=False, labelleft=False, right=True, labelright=False)

    price_range = max(float(chunk["high"].max() - chunk["low"].min()), 1e-9)
    adjusted_levels = resolve_overlaps(recent_levels, min_dist=price_range * 0.015)
    x_limit = len(chunk) - 1
    right_padding = 12.0 if profile == "global" else 8.5
    ax_main.set_xlim(-0.5, x_limit + right_padding)

    for lvl in adjusted_levels:
        precision = _price_precision(float(lvl["price"]))
        ax_main.text(
            x_limit + lvl["x_shift"],
            lvl["price"],
            f"{lvl['price']:.{precision}f}",
            color=lvl["color"],
            fontsize=8 if profile == "global" else 9,
            fontweight="bold",
            va="center",
            ha="left",
            bbox=dict(boxstyle="round,pad=0.15", facecolor="white", edgecolor=lvl["color"], alpha=0.95, linewidth=1.0),
        )
        ax_main.plot([x_limit, x_limit + lvl["x_shift"]], [lvl["price"], lvl["price"]], color=lvl["color"], linewidth=0.8)

    if highlight_extrema:
        _highlight_extrema(ax_main, chunk, x_limit=x_limit, profile=profile)

    legend_text = _legend_text(symbol, current, chunk, profile)
    ax_main.text(
        0.02,
        0.98,
        legend_text,
        transform=ax_main.transAxes,
        fontsize=8 if profile == "global" else 10,
        verticalalignment="top",
        bbox=dict(boxstyle="round", facecolor="white", alpha=0.92, edgecolor="grey"),
        fontfamily="monospace",
    )
    ax_main.spines["top"].set_visible(False)
    ax_main.spines["right"].set_visible(False)

    prefix = f"{symbol}_" if symbol else ""
    suffix = f"{profile}_{_safe_aspect_name(aspect_ratio, profile)}"
    save_path = IMAGE_CACHE_DIR / f"{prefix}{target_uid}_{len(chunk)}bars_{suffix}.png"
    plt.savefig(save_path, bbox_inches="tight", dpi=120 if profile == "global" else 110)
    plt.close(fig)
    return save_path


def detect_chart_levels(df: pd.DataFrame, target_uid: int, window_size: int, *, max_levels: int = 20) -> dict[str, list[float]]:
    """Return visible support/resistance levels for prompts and tests.

    This reuses the chart renderer's pivot logic so prompt text and chart labels stay aligned.
    """
    if df is None or df.empty:
        return {"support": [], "resistance": []}
    work = _prepare_frame(df)
    matches = work.index[work["uid"].astype(int) == int(target_uid)].tolist()
    if not matches:
        return {"support": [], "resistance": []}
    target_pos = int(matches[-1])
    start_pos = max(0, target_pos - window_size + 1)
    chunk = work.iloc[start_pos: target_pos + 1].copy().reset_index(drop=True)
    levels = _detect_levels(chunk)[-max_levels:]
    out = {"support": [], "resistance": []}
    for level in levels:
        out[level["type"]].append(float(level["price"]))
    return out


def _figsize(aspect_ratio: str | tuple[float, float] | None, profile: ChartProfile) -> tuple[float, float]:
    if isinstance(aspect_ratio, tuple) and len(aspect_ratio) == 2:
        return float(aspect_ratio[0]), float(aspect_ratio[1])
    aspect = (aspect_ratio or ("16:9" if profile == "global" else "1:1" if profile == "detail" else "4:3")).strip().lower()
    if aspect == "16:9":
        return (16, 9)
    if aspect == "4:3":
        return (12, 9)
    if aspect == "1:1":
        return (10, 10)
    logger.warning("Unknown chart aspect_ratio=%r, falling back to profile default", aspect_ratio)
    return (16, 9) if profile == "global" else (10, 10) if profile == "detail" else (12, 9)


def _safe_aspect_name(aspect_ratio: str | tuple[float, float] | None, profile: ChartProfile) -> str:
    if isinstance(aspect_ratio, tuple):
        return f"{aspect_ratio[0]}x{aspect_ratio[1]}".replace(".", "_")
    return str(aspect_ratio or ("16:9" if profile == "global" else "1:1" if profile == "detail" else "4:3")).replace(":", "x")


def _highlight_extrema(ax_main, chunk: pd.DataFrame, *, x_limit: int, profile: ChartProfile) -> None:
    max_idx = int(chunk["high"].idxmax())
    min_idx = int(chunk["low"].idxmin())
    max_price = float(chunk.loc[max_idx, "high"])
    min_price = float(chunk.loc[min_idx, "low"])
    precision = _price_precision(max(max_price, min_price))

    ax_main.scatter([max_idx], [max_price], marker="v", s=70 if profile == "global" else 90, color="blue", zorder=5)
    ax_main.scatter([min_idx], [min_price], marker="^", s=70 if profile == "global" else 90, color="green", zorder=5)

    text_x = min(x_limit + (2 if profile == "global" else 1), max_idx + 25)
    ax_main.annotate(
        f"MAX {max_price:.{precision}f}",
        xy=(max_idx, max_price),
        xytext=(text_x, max_price),
        fontsize=8 if profile == "global" else 9,
        ha="left",
        va="bottom",
        arrowprops=dict(arrowstyle="->", linewidth=0.8, color="blue"),
        bbox=dict(boxstyle="round,pad=0.12", facecolor="white", edgecolor="blue", alpha=0.9),
        color="blue",
    )
    text_x = min(x_limit + (2 if profile == "global" else 1), min_idx + 25)
    ax_main.annotate(
        f"MIN {min_price:.{precision}f}",
        xy=(min_idx, min_price),
        xytext=(text_x, min_price),
        fontsize=8 if profile == "global" else 9,
        ha="left",
        va="top",
        arrowprops=dict(arrowstyle="->", linewidth=0.8, color="green"),
        bbox=dict(boxstyle="round,pad=0.12", facecolor="white", edgecolor="green", alpha=0.9),
        color="green",
    )


def _legend_text(symbol: str | None, current: pd.Series, chunk: pd.DataFrame, profile: ChartProfile) -> str:
    label = "GLOBAL 26h" if profile == "global" else "DETAIL 2-3h" if profile == "detail" else "CHART"
    precision = _price_precision(float(current["close"]))
    return (
        f"{symbol + ' ' if symbol else ''}{label}\n"
        f"End: {current['time_iso']}\n"
        f"Bars: {len(chunk)}\n"
        f"Open: {current['open']:.{precision}f}\n"
        f"High: {current['high']:.{precision}f}\n"
        f"Low:  {current['low']:.{precision}f}\n"
        f"Close:{current['close']:.{precision}f}\n"
        f"Vol:  {current['volume']:.0f}\n"
        f"Window Low/High: {chunk['low'].min():.{precision}f} / {chunk['high'].max():.{precision}f}"
    )


def _price_precision(price: float) -> int:
    return 2 if abs(price) >= 100 else 3 if abs(price) >= 10 else 5


def _prepare_frame(df: pd.DataFrame) -> pd.DataFrame:
    work = df.copy()
    if "datetime" not in work.columns:
        if "ts" not in work.columns:
            raise ValueError("chart dataframe must have ts or datetime column")
        work["datetime"] = pd.to_datetime(work["ts"], unit="ms", utc=True)
    else:
        work["datetime"] = pd.to_datetime(work["datetime"], utc=True)
    if "time_iso" not in work.columns:
        work["time_iso"] = work["datetime"].dt.strftime("%Y-%m-%d %H:%M UTC")
    if "uid" not in work.columns:
        work["uid"] = work["datetime"].dt.strftime("%Y%m%d%H%M").astype(int)
    work = work.sort_values("datetime").reset_index(drop=True)
    numeric_cols = ["open", "high", "low", "close", "volume"]
    for col in numeric_cols:
        work[col] = pd.to_numeric(work[col], errors="coerce")
    work = work.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)
    return work


def _detect_levels(chunk: pd.DataFrame, pivot_span: int = 2) -> list[dict]:
    if len(chunk) < pivot_span * 2 + 1:
        return []
    highs = chunk["high"].tolist()
    lows = chunk["low"].tolist()
    candidates: list[dict] = []
    for i in range(pivot_span, len(chunk) - pivot_span):
        high_window = highs[i - pivot_span:i + pivot_span + 1]
        low_window = lows[i - pivot_span:i + pivot_span + 1]
        high = highs[i]
        low = lows[i]
        if high == max(high_window):
            candidates.append({"idx": i, "price": float(high), "type": "resistance", "color": "blue", "strength": high - min(low_window)})
        if low == min(low_window):
            candidates.append({"idx": i, "price": float(low), "type": "support", "color": "green", "strength": max(high_window) - low})

    if not candidates:
        return []

    price_range = max(float(chunk["high"].max() - chunk["low"].min()), 1e-9)
    min_sep = price_range * 0.012
    candidates = sorted(candidates, key=lambda x: (x["idx"], x["strength"]))
    filtered: list[dict] = []
    for item in candidates:
        if any(existing["type"] == item["type"] and abs(existing["price"] - item["price"]) < min_sep for existing in filtered):
            replaced = False
            for j, existing in enumerate(filtered):
                if existing["type"] == item["type"] and abs(existing["price"] - item["price"]) < min_sep:
                    if item["strength"] >= existing["strength"] or item["idx"] > existing["idx"]:
                        filtered[j] = item
                    replaced = True
                    break
            if replaced:
                continue
        filtered.append(item)

    return sorted(filtered, key=lambda x: x["idx"])
