#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone

from core.mt5_api import MT5Client, MT5Config
from utilities.settings import config


async def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test the MT5 proxy endpoints used by Alphard")
    parser.add_argument("--symbol", default=config.symbols[0])
    args = parser.parse_args()

    api = MT5Client(MT5Config(config.mt5_base_url, config.mt5_api_key, config.mt5_timeout_seconds))
    try:
        print(await api.health())
        print(await api.ready())
        print(await api.status())
        tick, info = await api.tick(args.symbol)
        print("tick", tick)
        print("info", info)
        end = datetime.now(timezone.utc)
        start = end - timedelta(hours=2)
        bars = await api.bars(args.symbol, config.timeframe, start, end)
        print(f"bars={len(bars)} last={bars[-1] if bars else None}")
        print("positions", await api.positions(args.symbol))
        print("orders", await api.orders(args.symbol))
    finally:
        await api.close()


if __name__ == "__main__":
    asyncio.run(main())
