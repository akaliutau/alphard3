import asyncio
from datetime import datetime, timezone

from core.candle_cache import CandleCache
from core.models import Candle


class FakeLedger:
    def __init__(self):
        self.candles: list[Candle] = []
        self.logs: list[dict] = []

    def latest_candle_time(self, symbol: str, timeframe: str) -> int | None:
        return None

    def upsert_candles(self, candles: list[Candle]) -> int:
        self.candles = candles
        return len(candles)

    def log(self, event_type, symbol, uid, strategy, timeframe, data):
        self.logs.append(data)


class FakeAPI:
    def __init__(self, opens):
        self.opens = opens
        self.calls: list[tuple[datetime, datetime]] = []

    async def bars(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[Candle]:
        self.calls.append((start, end))
        return [
            Candle(
                symbol=symbol,
                timeframe=timeframe,
                time=int(open_time.timestamp()),
                time_iso=open_time.isoformat(),
                open=1.0,
                high=1.1,
                low=0.9,
                close=1.0,
                volume=100,
            )
            for open_time in self.opens
            if start <= open_time <= end
        ]


def test_sync_to_splits_midnight_range_and_keeps_target_bar():
    async def run():
        end_boundary = datetime(2026, 6, 18, 0, 0, tzinfo=timezone.utc)
        opens = [
            datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc),
            datetime(2026, 6, 17, 23, 45, tzinfo=timezone.utc),
            datetime(2026, 6, 18, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 6, 18, 0, 15, tzinfo=timezone.utc),
        ]
        ledger = FakeLedger()
        api = FakeAPI(opens)
        cache = CandleCache(ledger, api, warmup_bars=2)

        inserted = await cache.sync_to("EURUSD", "M15", end_boundary=end_boundary, refresh_end_bar=True)

        assert inserted == 3
        assert [c.time_iso for c in ledger.candles] == [opens[0].isoformat(), opens[1].isoformat(), opens[2].isoformat()]
        assert all(start.date() == end.date() for start, end in api.calls)
        assert api.calls == [
            (datetime(2026, 6, 17, 23, 30, tzinfo=timezone.utc), datetime(2026, 6, 17, 23, 59, 59, 999999, tzinfo=timezone.utc)),
            (datetime(2026, 6, 18, 0, 0, tzinfo=timezone.utc), datetime(2026, 6, 18, 0, 15, tzinfo=timezone.utc)),
        ]

    asyncio.run(run())
