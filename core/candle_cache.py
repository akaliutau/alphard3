from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.ledger import Ledger, EventType
from core.mt5_api import MT5Client
from core.timeframes import timeframe_minutes
from utilities.settings import logger


class CandleCache:
    """Stateful SQLite candle cache that only asks MT5 for missing closed bars."""

    def __init__(self, ledger: Ledger, api: MT5Client, warmup_bars: int = 220):
        self.ledger = ledger
        self.api = api
        self.warmup_bars = warmup_bars

    async def sync_to(self, symbol: str, timeframe: str, end_boundary: datetime) -> int:
        """Fetch missing candles with open times before/at end_boundary as supported by the API."""
        end_boundary = end_boundary.astimezone(timezone.utc)
        tf_min = timeframe_minutes(timeframe)
        latest = self.ledger.latest_candle_time(symbol, timeframe)

        if latest is None:
            start = end_boundary - timedelta(minutes=tf_min * self.warmup_bars)
        else:
            start = datetime.fromtimestamp(latest, timezone.utc) + timedelta(minutes=tf_min)

        if start >= end_boundary:
            return 0

        candles = await self.api.bars(symbol, timeframe, start=start, end=end_boundary)
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
                "received": len(candles),
                "upserted": inserted,
                "first_bar": first_bar,
                "last_bar": last_bar,
            },
        )
        logger.info("%s %s candle sync: %s rows [%s -> %s]", symbol, timeframe, inserted, first_bar, last_bar)
        return inserted

    def load_chart_frame(self, symbol: str, timeframe: str, bars: int, end_time: int | None = None):
        return self.ledger.load_candles_df(symbol, timeframe, limit=bars, end_time=end_time)
