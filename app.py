from __future__ import annotations

import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from core.candle_cache import CandleCache
from core.execution import ExecutionEngine
from core.ledger import EventType, Ledger
from core.models import Symbol
from core.mt5_api import MT5Client, MT5Config
from core.risk import RiskEngine
from core.strategy import Strategy
from core.timeframes import broker_now_from_tick, candle_uid, current_basket_open, next_wakeup_seconds, timeframe_minutes
from middleware.llm_middleware import ModelConfig
from utilities.settings import config, logger


class AlphardApp:
    """Async stateful trading daemon.

    Loop:
      wake -> read broker tick time -> determine current broker basket -> sync bars -> render chart -> call VLM -> risk -> execute -> sleep.
    """

    def __init__(self):
        self.ledger = Ledger(config.sqlite_path)
        self.mt5 = MT5Client(MT5Config(config.mt5_base_url, config.mt5_api_key, config.mt5_timeout_seconds))
        self.cache = CandleCache(self.ledger, self.mt5, warmup_bars=config.candle_warmup_bars)
        self.risk = RiskEngine()
        self.exec = ExecutionEngine(self.mt5, self.ledger)
        self.model_conf = ModelConfig(name=config.model_name, model=config.model_id)
        self._stop = asyncio.Event()

    async def close(self) -> None:
        await self.mt5.close()

    async def run_forever(self) -> None:
        logger.info(
            "Alphard started env=%s dry_run=%s symbols=%s timeframe=%s interval=%sm db=%s",
            config.env,
            config.dry_run,
            ",".join(config.symbols),
            config.timeframe,
            config.run_interval_minutes,
            config.sqlite_path,
        )
        while not self._stop.is_set():
            try:
                await self.run_once(datetime.now(timezone.utc))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception("cycle failed: %s", exc)
                self.ledger.log(EventType.ERROR, None, None, None, {"error": str(exc)}, timeframe=config.timeframe)
            await asyncio.sleep(next_wakeup_seconds(config.run_interval_minutes))

    async def run_once(self, now: datetime) -> None:
        app_now = now.astimezone(timezone.utc)
        logger.info("cycle app_now_utc=%s symbols=%s timeframe=%s", app_now.isoformat(), ",".join(config.symbols), config.timeframe)

        ready = await self.mt5.ready()
        self.ledger.log(EventType.SYSTEM, None, None, None, {"ready": ready, "app_now_utc": app_now.isoformat()}, timeframe=config.timeframe)

        tasks = [self.process_symbol(Symbol(name=s), app_now=app_now) for s in config.symbols]
        await asyncio.gather(*tasks)

    async def process_symbol(self, symbol: Symbol, app_now: datetime) -> None:
        # The broker/MetaQuotes server clock is the source of truth for bar baskets.
        # Local wall-clock time can be offset from the MT5 server clock.
        tick, info = await self.mt5.tick(symbol.name)
        broker_now = broker_now_from_tick(tick.time, tick.time_msc, fallback=app_now)
        target_open = current_basket_open(broker_now, config.timeframe)
        target_close = target_open + timedelta(minutes=timeframe_minutes(config.timeframe))
        target_uid = candle_uid(target_open)
        end_boundary = target_open

        logger.info(
            "%s broker_now=%s tick_time=%s tick_time_msc=%s target_uid=%s target_open=%s target_close=%s app_now_utc=%s",
            symbol.name,
            broker_now.isoformat(),
            tick.time,
            tick.time_msc,
            target_uid,
            target_open.isoformat(),
            target_close.isoformat(),
            app_now.isoformat(),
        )

        if self.ledger.is_basket_processed(symbol.name, config.timeframe, target_uid, config.strategy_name):
            logger.info("%s uid=%s broker basket already processed", symbol.name, target_uid)
            return

        await self.cache.sync_to(symbol.name, config.timeframe, end_boundary=end_boundary, refresh_end_bar=True)
        candles = self.cache.load_chart_frame(
            symbol.name,
            config.timeframe,
            max(config.chart_window_bars, config.candle_warmup_bars),
            end_time=int(end_boundary.timestamp()),
        )
        if candles.empty or target_uid not in set(candles["uid"].astype(int)):
            logger.warning("%s uid=%s target broker basket not available in cache yet", symbol.name, target_uid)
            self.ledger.log(
                EventType.SYSTEM,
                symbol.name,
                target_uid,
                config.strategy_name,
                {
                    "skipped": "target broker basket not available",
                    "rows": len(candles),
                    "broker_now": broker_now.isoformat(),
                    "target_open": target_open.isoformat(),
                    "target_close": target_close.isoformat(),
                },
                timeframe=config.timeframe,
            )
            return

        positions = await self.mt5.positions(symbol=symbol.name)
        orders = await self.mt5.orders(symbol=symbol.name)

        strategy = Strategy(config.strategy_name, symbol, self.model_conf, self.ledger)
        decision = await strategy.analyze(uid=target_uid, candles=candles, positions=positions)

        risk = self.risk.validate(decision, tick, info, positions=positions, orders=orders)
        self.ledger.log(
            EventType.RISK,
            symbol=symbol.name,
            uid=target_uid,
            strategy=config.strategy_name,
            timeframe=config.timeframe,
            data=asdict(risk),
        )
        logger.info("%s uid=%s risk approved=%s reason=%s", symbol.name, target_uid, risk.approved, risk.reason)

        cleanup = await self.exec.cleanup_pending_orders(symbol.name) if risk.approved else []
        if cleanup:
            self.ledger.log(
                EventType.TRADE,
                symbol=symbol.name,
                uid=target_uid,
                strategy=config.strategy_name,
                timeframe=config.timeframe,
                data={"cleanup": cleanup},
            )
        execution = await self.exec.execute(symbol.name, target_uid, config.strategy_name, decision, risk, tick, info)
        self.ledger.mark_basket_processed(
            symbol=symbol.name,
            timeframe=config.timeframe,
            uid=target_uid,
            strategy=config.strategy_name,
            data={
                "broker_now": broker_now.isoformat(),
                "app_now_utc": app_now.isoformat(),
                "target_open": target_open.isoformat(),
                "target_close": target_close.isoformat(),
                "decision": decision.status,
                "risk_approved": risk.approved,
                "execution_attempted": execution.attempted,
                "execution_ok": execution.ok,
            },
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alphard MT5 async VLM trading app")
    parser.add_argument("--once", action="store_true", help="run one cycle then exit")
    parser.add_argument("--now", help="override current UTC datetime for tests/backfills, e.g. 2026-06-17T10:30:00Z")
    return parser.parse_args()


def _parse_now(value: str | None) -> datetime:
    if not value:
        return datetime.now(timezone.utc)
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


async def main() -> None:
    args = parse_args()
    app = AlphardApp()
    try:
        if args.once:
            await app.run_once(_parse_now(args.now))
        else:
            await app.run_forever()
    finally:
        await app.close()


if __name__ == "__main__":
    asyncio.run(main())
