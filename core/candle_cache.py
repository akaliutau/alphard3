from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.ledger import Ledger, EventType
from core.models import Candle
from core.mt5_api import MT5Client
from core.timeframes import timeframe_minutes
from utilities.settings import logger


class CandleCache:
    """Stateful SQLite candle cache that asks MT5 only for missing bars and the current forming broker bar when requested."""

    def __init__(self, ledger: Ledger, api: MT5Client, warmup_bars: int = 220):
        self.ledger = ledger
        self.api = api
        self.warmup_bars = warmup_bars

    async def sync_to(self, symbol: str, timeframe: str, end_boundary: datetime, refresh_end_bar: bool = False) -> int:
        """Fetch missing candles up to and including `end_boundary`.

        When `refresh_end_bar=True`, the final basket is treated as the currently
        forming MetaQuotes server-time bar. It is re-requested and upserted so the
        local cache has the freshest snapshot before analysis.
        """
        end_boundary = end_boundary.astimezone(timezone.utc)
        tf_min = timeframe_minutes(timeframe)
        tf_delta = timedelta(minutes=tf_min)
        latest = self.ledger.latest_candle_time(symbol, timeframe)

        if latest is None:
            start = end_boundary - timedelta(minutes=tf_min * self.warmup_bars)
        else:
            latest_dt = datetime.fromtimestamp(latest, timezone.utc)
            if refresh_end_bar:
                if latest_dt >= end_boundary:
                    # Re-fetch the current forming basket plus one prior bar. Some MT5
                    # range APIs return nothing when start == end.
                    start = end_boundary - tf_delta
                else:
                    # Re-fetch from the latest known bar so gaps and the end bar are both covered.
                    start = latest_dt
            else:
                start = latest_dt + tf_delta

        if start >= end_boundary:
            start = end_boundary - tf_delta

        candles = await self._fetch_bars_day_safe(
            symbol,
            timeframe,
            start=start,
            end_boundary=end_boundary,
            tf_delta=tf_delta,
        )
        inserted = self.ledger.upsert_candles(candles)
        first_bar = candles[0].time_iso if candles else None
        last_bar = candles[-1].time_iso if candles else None
        self.ledger.log(
            EventType.DATA_SYNC,
            symbol=symbol,
            uid=None,
            strategy=None,
            timeframe=timeframe,
            data={
                "requested_start": start.isoformat(),
                "requested_end": end_boundary.isoformat(),
                "refresh_end_bar": refresh_end_bar,
                "received": len(candles),
                "upserted": inserted,
                "first_bar": first_bar,
                "last_bar": last_bar,
            },
        )
        logger.info(
            "%s %s candle sync: %s rows [%s -> %s] requested=[%s -> %s] refresh_end_bar=%s",
            symbol, timeframe, inserted, first_bar, last_bar, start.isoformat(), end_boundary.isoformat(), refresh_end_bar,
        )
        return inserted

    def load_chart_frame(self, symbol: str, timeframe: str, bars: int, end_time: int | None = None):
        return self.ledger.load_candles_df(symbol, timeframe, limit=bars, end_time=end_time)

    async def _fetch_bars_day_safe(
        self,
        symbol: str,
        timeframe: str,
        *,
        start: datetime,
        end_boundary: datetime,
        tf_delta: timedelta,
    ) -> list[Candle]:
        """Fetch bars without sending a single MT5 range across midnight.

        Some MT5/proxy range paths are brittle when the request starts on the
        previous broker day and the requested final bar is the first bar of the
        new day. Query same-day chunks and ask the final chunk one timeframe past
        the desired boundary, then filter back to the requested inclusive window.
        """
        start = start.astimezone(timezone.utc)
        end_boundary = end_boundary.astimezone(timezone.utc)
        request_end = end_boundary + tf_delta
        start_ts = int(start.timestamp())
        end_ts = int(end_boundary.timestamp())
        by_time: dict[int, Candle] = {}

        for chunk_start, chunk_end in _same_day_ranges(start, request_end):
            chunk = await self.api.bars(symbol, timeframe, start=chunk_start, end=chunk_end)
            for candle in chunk:
                candle_time = int(candle.time)
                if start_ts <= candle_time <= end_ts:
                    by_time[candle_time] = candle

        return [by_time[t] for t in sorted(by_time)]


def _same_day_ranges(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    start = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    ranges: list[tuple[datetime, datetime]] = []
    cursor = start

    while cursor < end:
        next_midnight = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        chunk_end = min(end, next_midnight)
        request_end = chunk_end
        if chunk_end == next_midnight:
            request_end = chunk_end - timedelta(microseconds=1)
        if cursor < request_end:
            ranges.append((cursor, request_end))
        cursor = chunk_end

    return ranges


