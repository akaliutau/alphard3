from __future__ import annotations

from datetime import datetime, timezone, timedelta

_TIMEFRAME_MINUTES = {
    "M1": 1,
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
}


def timeframe_minutes(timeframe: str) -> int:
    tf = timeframe.upper()
    if tf not in _TIMEFRAME_MINUTES:
        raise ValueError(f"Unsupported timeframe {timeframe!r}; add it to core/timeframes.py")
    return _TIMEFRAME_MINUTES[tf]


def floor_to_timeframe(dt: datetime, timeframe: str) -> datetime:
    dt = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
    minutes = timeframe_minutes(timeframe)
    if minutes >= 1440:
        return dt.replace(hour=0, minute=0)
    total = dt.hour * 60 + dt.minute
    floored = total - (total % minutes)
    return dt.replace(hour=floored // 60, minute=floored % 60)


def latest_closed_bar_open(now: datetime, timeframe: str, close_delay_seconds: int = 30) -> datetime:
    safe_now = now.astimezone(timezone.utc) - timedelta(seconds=close_delay_seconds)
    end_boundary = floor_to_timeframe(safe_now, timeframe)
    return end_boundary - timedelta(minutes=timeframe_minutes(timeframe))


def next_wakeup_seconds(interval_minutes: int, now: datetime | None = None) -> float:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    interval = max(1, interval_minutes)
    minute = now.minute
    next_minute = ((minute // interval) + 1) * interval
    if next_minute >= 60:
        target = now.replace(hour=(now.hour + 1) % 24, minute=0, second=5, microsecond=0)
        if now.hour == 23:
            target = target + timedelta(days=1)
    else:
        target = now.replace(minute=next_minute, second=5, microsecond=0)
    return max(1.0, (target - now).total_seconds())


def candle_uid(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).strftime("%Y%m%d%H%M"))
