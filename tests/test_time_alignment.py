from datetime import datetime, timezone

import pandas as pd

from core.strategy import _slice_to_uid
from core.timeframes import candle_uid, floor_to_timeframe, latest_closed_bar_open


def test_floor_to_timeframe_m15_baskets():
    dt = datetime(2026, 6, 17, 12, 29, 38, tzinfo=timezone.utc)
    assert floor_to_timeframe(dt, "M15") == datetime(2026, 6, 17, 12, 15, tzinfo=timezone.utc)
    dt = datetime(2026, 6, 17, 12, 45, 0, tzinfo=timezone.utc)
    assert floor_to_timeframe(dt, "M15") == datetime(2026, 6, 17, 12, 45, tzinfo=timezone.utc)


def test_latest_closed_bar_open_m15():
    now = datetime(2026, 6, 17, 12, 29, 38, tzinfo=timezone.utc)
    assert latest_closed_bar_open(now, "M15", close_delay_seconds=30) == datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)
    now = datetime(2026, 6, 17, 12, 15, 40, tzinfo=timezone.utc)
    assert latest_closed_bar_open(now, "M15", close_delay_seconds=30) == datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc)


def test_slice_to_uid_excludes_future_bars():
    base = pd.date_range("2026-06-17 11:00:00+00:00", periods=8, freq="15min")
    df = pd.DataFrame(
        {
            "datetime": base,
            "time_iso": base.strftime("%Y-%m-%d %H:%M UTC"),
            "uid": [int(x.strftime("%Y%m%d%H%M")) for x in base],
            "open": range(8),
            "high": [x + 0.4 for x in range(8)],
            "low": [x - 0.4 for x in range(8)],
            "close": [x + 0.2 for x in range(8)],
            "volume": [100] * 8,
        }
    )
    target_uid = candle_uid(datetime(2026, 6, 17, 12, 0, tzinfo=timezone.utc))
    out = _slice_to_uid(df, target_uid, window_size=8)
    assert int(out.iloc[-1]["uid"]) == target_uid
    assert len(out) == 5
    assert out.iloc[-1]["time_iso"] == "2026-06-17 12:00 UTC"
