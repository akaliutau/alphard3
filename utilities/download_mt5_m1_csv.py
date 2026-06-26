#!/usr/bin/env python3
"""
Download 1-minute OHLCV candles from the Alphard/MT5 proxy and store them as CSV.

The MT5Client implementation is intentionally copied/simplified from the prior
project's core/mt5_api.py: same env-driven base URL/API key, same /v1/bars
endpoint shape, same async httpx client, same retry pattern, and same Candle
normalisation.

Usage:
  python download_mt5_m1_csv.py --symbol EURUSD --start 2026-06-01 --end 2026-06-23 --out eurusd.csv

Required .env.cloud values:
  MT5_BASE_URL=http://your-mt5-proxy:8000
  MT5_API_KEY=...

Optional .env.cloud values:
  MT5_TIMEOUT_SECONDS=30
  MT5_MAX_RETRIES=3
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, date, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx
from dotenv import load_dotenv


class MT5APIError(RuntimeError):
    pass


@dataclass(frozen=True)
class MT5Config:
    base_url: str
    api_key: str
    timeout_seconds: float = 30.0
    max_retries: int = 3


@dataclass(frozen=True)
class Candle:
    symbol: str
    timeframe: str
    time: int  # MT5/API bar-open time, seconds since epoch UTC
    time_iso: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    spread: float = 0.0
    real_volume: float = 0.0


class MT5Client:
    """Tiny async client for the MT5 Wine Proxy API.

    This keeps only what the downloader needs from core/mt5_api.py.
    """

    def __init__(self, cfg: MT5Config):
        self.cfg = cfg
        self.base_url = cfg.base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-API-Key": cfg.api_key},
            timeout=cfg.timeout_seconds,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                resp = await self._client.request(method, path, **kwargs)
                data = resp.json() if resp.content else {}
                if resp.status_code >= 400:
                    raise MT5APIError(f"{method} {path} -> HTTP {resp.status_code}: {data}")
                if isinstance(data, dict) and data.get("ok") is False:
                    raise MT5APIError(f"{method} {path} returned ok=false: {data}")
                if not isinstance(data, dict):
                    raise MT5APIError(f"{method} {path} returned non-object JSON: {type(data).__name__}")
                return data
            except Exception as exc:  # noqa: BLE001 - CLI should show final useful error
                last_error = exc
                if attempt >= self.cfg.max_retries:
                    break
                await asyncio.sleep(min(2**attempt, 10))
        raise MT5APIError(str(last_error))

    async def ready(self) -> dict[str, Any]:
        return await self._request("GET", "/health/ready")

    async def bars(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[Candle]:
        params = {
            "symbol": symbol,
            "timeframe": timeframe,
            "start": iso_z(start),
            "end": iso_z(end),
        }
        data = await self._request("GET", "/v1/bars", params=params)
        raw_bars = data.get("bars") or data.get("data") or []
        bars: list[Candle] = []
        for row in raw_bars:
            bar_time = parse_bar_time(row)
            volume = row.get("tick_volume") if row.get("tick_volume") is not None else row.get("volume")
            if volume is None:
                volume = row.get("real_volume", 0)
            bars.append(
                Candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    time=bar_time,
                    time_iso=row.get("time_iso") or datetime.fromtimestamp(bar_time, timezone.utc).isoformat().replace("+00:00", "Z"),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(volume or 0),
                    spread=float(row.get("spread") or 0),
                    real_volume=float(row.get("real_volume") or 0),
                )
            )
        return bars


CSV_FIELDS = [
    "time",
    "time_iso",
    "symbol",
    "timeframe",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "spread",
    "real_volume",
]


def parse_bar_time(row: dict[str, Any]) -> int:
    raw = row.get("time") or row.get("timestamp") or row.get("ts")
    if raw is None and row.get("time_iso"):
        return int(parse_datetime(row["time_iso"]).timestamp())
    if raw is None:
        raise MT5APIError(f"bar row has no time: {row}")
    if isinstance(raw, str) and not raw.isdigit():
        return int(parse_datetime(raw).timestamp())
    return int(raw)


def parse_datetime(value: str) -> datetime:
    value = value.strip()
    # Date-only args mean UTC start-of-day. For --end this script treats the
    # timestamp as exclusive; use the next date when you want a full day.
    if len(value) == 10:
        return datetime.combine(date.fromisoformat(value), time.min, tzinfo=timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def iter_ranges(start: datetime, end: datetime, batch_minutes: int, split_days: bool = True) -> Iterable[tuple[datetime, datetime]]:
    if start >= end:
        raise ValueError("start must be before end")
    if batch_minutes <= 0:
        raise ValueError("batch_minutes must be > 0")

    cursor = start.astimezone(timezone.utc)
    final = end.astimezone(timezone.utc)
    delta = timedelta(minutes=batch_minutes)

    while cursor < final:
        chunk_end = min(cursor + delta, final)
        if split_days:
            next_midnight = (cursor + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            chunk_end = min(chunk_end, next_midnight)
        if cursor < chunk_end:
            yield cursor, chunk_end
        cursor = chunk_end


def candle_to_row(c: Candle) -> dict[str, Any]:
    row = asdict(c)
    # Keep a predictable column order and full precision-friendly string values.
    return {field: row[field] for field in CSV_FIELDS}


def read_existing(path: Path) -> dict[int, dict[str, Any]]:
    if not path.exists():
        return {}
    rows: dict[int, dict[str, Any]] = {}
    with path.open("r", newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            if not row.get("time"):
                continue
            rows[int(row["time"])] = row
    return rows


def write_csv_atomic(path: Path, rows_by_time: dict[int, dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sorted_rows = [rows_by_time[t] for t in sorted(rows_by_time)]
    with tempfile.NamedTemporaryFile("w", newline="", encoding="utf-8", delete=False, dir=str(path.parent)) as tmp:
        writer = csv.DictWriter(tmp, fieldnames=CSV_FIELDS)
        writer.writeheader()
        writer.writerows(sorted_rows)
        tmp_path = Path(tmp.name)
    tmp_path.replace(path)


def env_config(env_file: str) -> MT5Config:
    load_dotenv(env_file, override=False)
    base_url = os.getenv("MT5_BASE_URL", "").strip()
    api_key = os.getenv("MT5_API_KEY", "").strip()
    if not base_url:
        raise SystemExit("Missing MT5_BASE_URL in env")
    if not api_key:
        raise SystemExit("Missing MT5_API_KEY in env")
    return MT5Config(
        base_url=base_url,
        api_key=api_key,
        timeout_seconds=float(os.getenv("MT5_TIMEOUT_SECONDS", "30")),
        max_retries=int(os.getenv("MT5_MAX_RETRIES", "3")),
    )


async def download(args: argparse.Namespace) -> None:
    cfg = env_config(args.env_file)
    start = parse_datetime(args.start)
    end = parse_datetime(args.end)
    output = Path(args.out)
    rows_by_time = read_existing(output) if args.append else {}

    client = MT5Client(cfg)
    try:
        if not args.skip_ready_check:
            print("checking api")
            ready = await client.ready()
            print(f"ready: {ready}")

        total_received = 0
        total_kept = 0
        ranges = list(iter_ranges(start, end, args.batch_minutes, split_days=not args.no_split_days))
        for idx, (chunk_start, chunk_end) in enumerate(ranges, start=1):
            print(f"[{idx}/{len(ranges)}] {args.symbol} {args.timeframe} {iso_z(chunk_start)} -> {iso_z(chunk_end)}")
            candles = await client.bars(args.symbol, args.timeframe, chunk_start, chunk_end)
            total_received += len(candles)

            start_ts = int(chunk_start.timestamp())
            end_ts = int(chunk_end.timestamp())
            kept_this_batch = 0
            for candle in candles:
                # Treat end as exclusive to avoid duplicates across batches.
                if start_ts <= int(candle.time) < end_ts:
                    rows_by_time[int(candle.time)] = candle_to_row(candle)
                    kept_this_batch += 1
            total_kept += kept_this_batch
            print(f"    received={len(candles)} kept={kept_this_batch} total_unique={len(rows_by_time)}")

            if args.sleep_seconds > 0:
                await asyncio.sleep(args.sleep_seconds)

        write_csv_atomic(output, rows_by_time)
        print(
            f"done: wrote {len(rows_by_time)} unique rows to {output} "
            f"(received={total_received}, kept_in_range={total_kept})"
        )
    finally:
        await client.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Download MT5 proxy 1-minute OHLCV bars to CSV")
    parser.add_argument("--symbol", default="EURGBP", help="MT5 symbol, default: EURUSD")
    parser.add_argument("--timeframe", default="M1", help="MT5 timeframe, default: M1")
    parser.add_argument("--start", required=True, help="UTC start date/datetime, e.g. 2026-06-01 or 2026-06-01T00:00:00Z")
    parser.add_argument("--end", required=True, help="UTC exclusive end date/datetime, e.g. 2026-06-23 or 2026-06-23T23:59:00Z")
    parser.add_argument("--out", default="eurusd.csv", help="Output CSV path, default: eurusd.csv")
    parser.add_argument("--env-file", default=".env.cloud", help="Env file path, default: .env.cloud")
    parser.add_argument("--batch-minutes", type=int, default=12 * 60, help="Request size in minutes, default: 720 / 12h")
    parser.add_argument("--append", action="store_true", help="Merge with existing CSV and de-duplicate by bar time")
    parser.add_argument("--no-split-days", action="store_true", help="Do not force chunks to stop at UTC midnight")
    parser.add_argument("--skip-ready-check", action="store_true", help="Skip /health/ready before downloading")
    parser.add_argument("--sleep-seconds", type=float, default=0.0, help="Optional delay between batch requests")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    asyncio.run(download(args))


if __name__ == "__main__":
    main()


