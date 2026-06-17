from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from utilities.settings import IMAGE_CACHE_DIR, logger


def resolve_overlaps(levels: list[dict], min_dist: float, base_x: float = 0.6, shift_step: float = 1.2) -> list[dict]:
    if not levels:
        return []
    sorted_levels = sorted([dict(x) for x in levels], key=lambda x: x["price"])
    sorted_levels[0]["x_shift"] = base_x
    for i in range(1, len(sorted_levels)):
        current = sorted_levels[i]
        prev = sorted_levels[i - 1]
        current["x_shift"] = prev["x_shift"] + shift_step if abs(current["price"] - prev["price"]) < min_dist else base_x
    return sorted_levels


def generate_chart_image_v2(df: pd.DataFrame, target_uid: int, window_size: int = 96, symbol: str | None = None) -> Path | None:
    """Generate a deterministic state chart for a VLM from cached OHLCV bars.

    Required columns: ts(ms), open, high, low, close, volume, uid.
    """
    from matplotlib import pyplot as plt
    import mplfinance as mpf

    if df is None or df.empty:
        logger.error("No dataframe supplied for chart generation")
        return None

    df = df.copy()
    if "datetime" not in df.columns:
        df["datetime"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df.set_index("datetime", inplace=True, drop=True)
    df.sort_index(inplace=True)

    matches = df.index[df["uid"] == target_uid].tolist()
    if not matches:
        logger.error("UID %s not found in chart dataframe", target_uid)
        return None

    target_date = matches[0]
    target_idx = df.index.get_loc(target_date)
    if isinstance(target_idx, slice):
        target_idx = target_idx.start
    elif isinstance(target_idx, (list, np.ndarray)):
        target_idx = int(target_idx[0])

    if target_idx < max(8, min(window_size // 2, window_size - 1)):
        logger.warning("Not enough history for chart target_uid=%s", target_uid)
        return None

    start_idx = max(0, target_idx - window_size + 1)
    chunk = df.iloc[start_idx: target_idx + 1].copy()
    current = chunk.iloc[-1]

    last_date = chunk.index[-1]
    time_delta = chunk.index[-1] - chunk.index[-2] if len(chunk) > 1 else pd.Timedelta(minutes=15)
    chunk_extended = chunk.reindex(chunk.index.union([last_date + time_delta]))

    levels: list[dict] = []
    for i in range(1, len(chunk) - 1):
        prev_c = chunk_extended.iloc[i - 1]
        curr_c = chunk_extended.iloc[i]
        next_c = chunk_extended.iloc[i + 1]
        if curr_c["high"] > prev_c["high"] and curr_c["high"] > next_c["high"]:
            levels.append({"idx": i, "price": float(curr_c["high"]), "type": "resistance", "color": "blue"})
        if curr_c["low"] < prev_c["low"] and curr_c["low"] < next_c["low"]:
            levels.append({"idx": i, "price": float(curr_c["low"]), "type": "support", "color": "green"})

    recent_levels = levels[-15:]
    addplots = []
    for lvl in recent_levels:
        line_series = pd.Series(np.nan, index=chunk_extended.index)
        line_series.iloc[lvl["idx"]:] = lvl["price"]
        addplots.append(mpf.make_addplot(line_series, color=lvl["color"], width=1.5, linestyle="-", alpha=0.8))

    mc = mpf.make_marketcolors(
        up="black", down="red",
        edge={"up": "black", "down": "red"},
        wick={"up": "black", "down": "red"},
        volume="inherit",
    )
    style = mpf.make_mpf_style(marketcolors=mc, gridstyle=":", gridaxis="vertical", rc={"font.family": "monospace"})

    fig, axlist = mpf.plot(
        chunk_extended,
        type="candle",
        style=style,
        volume=True,
        volume_panel=1,
        panel_ratios=(4, 1),
        addplot=addplots,
        returnfig=True,
        figsize=(10, 14),
        tight_layout=True,
    )

    ax_main = axlist[0]
    ax_main.tick_params(axis="y", left=False, labelleft=False, right=True, labelright=False)

    price_range = max(float(chunk["high"].max() - chunk["low"].min()), 1e-9)
    adjusted_levels = resolve_overlaps(recent_levels, min_dist=price_range * 0.04, base_x=0.5, shift_step=3.5)
    x_limit = len(chunk_extended) - 1

    for lvl in adjusted_levels:
        ax_main.text(
            x_limit + lvl["x_shift"],
            lvl["price"],
            f"{lvl['price']:.5g}",
            color=lvl["color"],
            fontsize=9,
            fontweight="bold",
            va="center",
            ha="left",
            bbox=dict(boxstyle="round,pad=0.2", facecolor="white", edgecolor=lvl["color"], alpha=1.0, linewidth=1.0),
        )
        ax_main.plot([x_limit, x_limit + lvl["x_shift"]], [lvl["price"], lvl["price"]], color=lvl["color"], linewidth=0.8)

    legend_text = (
        f"{symbol + ' ' if symbol else ''}{current.name.strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"Open: {current['open']:.5g}\n"
        f"High: {current['high']:.5g}\n"
        f"Low:  {current['low']:.5g}\n"
        f"Close:{current['close']:.5g}\n"
        f"Vol:  {current['volume']:.0f}"
    )
    ax_main.text(
        0.02,
        0.98,
        legend_text,
        transform=ax_main.transAxes,
        fontsize=10,
        verticalalignment="top",
        bbox=dict(boxstyle="round", facecolor="white", alpha=0.9, edgecolor="grey"),
        fontfamily="monospace",
    )
    ax_main.spines["top"].set_visible(False)
    ax_main.spines["right"].set_visible(False)

    prefix = f"{symbol}_" if symbol else ""
    save_path = IMAGE_CACHE_DIR / f"{prefix}{target_uid}.png"
    plt.savefig(save_path, bbox_inches="tight", dpi=100)
    plt.close(fig)
    return save_path
