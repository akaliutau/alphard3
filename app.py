from __future__ import annotations

import argparse
import asyncio
import json
import re
from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from core.candle_cache import CandleCache
from core.ledger import EventType, Ledger
from core.models import Symbol
from utilities.ImageStorage import ImageStorage
from core.mt5_api import MT5Client, MT5Config
from core.strategy import Strategy
from core.timeframes import broker_now_from_tick, candle_uid, current_basket_open, next_wakeup_seconds, timeframe_minutes
from middleware.llm_middleware import ModelConfig
from utilities.settings import config, logger


class AlphardApp:
    """Async advisory daemon.

    Loop:
      wake -> read broker tick time -> determine current broker basket -> sync bars
      -> render global/detail charts -> call VLM -> store recommendation -> sleep.

    This app intentionally does not call MT5 trading endpoints. It reads market,
    position, and order state only, then emits a structured plan for a separate
    executor/human review process.
    """

    def __init__(self):
        self.ledger = Ledger(config.sqlite_path)
        self.mt5 = MT5Client(MT5Config(config.mt5_base_url, config.mt5_api_key, config.mt5_timeout_seconds))
        self.cache = CandleCache(
            self.ledger,
            self.mt5,
            warmup_bars=max(
                config.candle_warmup_bars,
                config.chart_window_bars,
                config.chart_context_window_bars,
                config.detailed_analysis_bars,
                config.global_analysis_bars,
            ),
        )
        self.model_conf = ModelConfig(name=config.model_name, model=config.model_id, max_tokens=config.litellm_max_tokens)
        self.artifact_storage = ImageStorage(
            provider=config.image_provider,
            bucket_name=config.gcs_bucket_name,
            public_read=config.gcs_public_read,
        )
        self._stop = asyncio.Event()

    async def close(self) -> None:
        await self.mt5.close()

    async def run_forever(self) -> None:
        logger.info(
            "Alphard advisory started env=%s advisory_only=%s symbols=%s timeframe=%s interval=%sm db=%s",
            config.env,
            config.advisory_only,
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
        logger.info(
            "cycle app_now_utc=%s symbols=%s timeframe=%s advisory_strategy=%s",
            app_now.isoformat(),
            ",".join(config.symbols),
            config.timeframe,
            config.advisory_strategy_name,
        )

        ready = await self.mt5.ready()
        self.ledger.log(
            EventType.SYSTEM,
            None,
            None,
            None,
            {"ready": ready, "app_now_utc": app_now.isoformat(), "advisory_only": True},
            timeframe=config.timeframe,
        )

        symbols = [Symbol(name=s) for s in config.symbols]
        tasks = [self.process_symbol(symbol, app_now=app_now) for symbol in symbols]
        raw_results = await asyncio.gather(*tasks, return_exceptions=True)
        results: list[dict[str, object]] = []
        for symbol, result in zip(symbols, raw_results):
            if isinstance(result, Exception):
                logger.error(
                    "%s advisory processing failed: %s",
                    symbol.name,
                    result,
                    exc_info=(result.__class__, result, result.__traceback__),
                )
                self.ledger.log(
                    EventType.ERROR,
                    symbol.name,
                    None,
                    config.advisory_strategy_name,
                    {"stage": "process_symbol", "error": str(result), "error_type": result.__class__.__name__},
                    timeframe=config.timeframe,
                )
                results.append(
                    {
                        "symbol": symbol.name,
                        "uid": None,
                        "processed": False,
                        "status": "ERROR",
                        "reason": str(result),
                    }
                )
            elif result is not None:
                results.append(result)

        self.publish_latest_advice_pointer(app_now=app_now, results=results)

    async def process_symbol(self, symbol: Symbol, app_now: datetime) -> dict[str, object] | None:
        tick, info = await self.mt5.tick(symbol.name)
        broker_now = broker_now_from_tick(tick.time, tick.time_msc, fallback=app_now)
        target_open = current_basket_open(broker_now, config.timeframe)
        target_close = target_open + timedelta(minutes=timeframe_minutes(config.timeframe))
        target_uid = candle_uid(target_open)
        end_boundary = target_open
        strategy_name = config.advisory_strategy_name

        logger.info(
            "%s broker_now=%s target_uid=%s target_open=%s target_close=%s app_now_utc=%s",
            symbol.name,
            broker_now.isoformat(),
            target_uid,
            target_open.isoformat(),
            target_close.isoformat(),
            app_now.isoformat(),
        )

        if self.ledger.is_basket_processed(symbol.name, config.timeframe, target_uid, strategy_name):
            logger.info("%s uid=%s broker basket already processed", symbol.name, target_uid)
            return {
                "symbol": symbol.name,
                "uid": target_uid,
                "timeframe": config.timeframe,
                "strategy": strategy_name,
                "processed": False,
                "status": "SKIPPED",
                "reason": "basket already processed",
                "target_open": target_open.isoformat(),
                "target_close": target_close.isoformat(),
                "broker_now": broker_now.isoformat(),
            }

        await self.cache.sync_to(symbol.name, config.timeframe, end_boundary=end_boundary, refresh_end_bar=True)
        required_bars = max(config.global_analysis_bars, config.detailed_analysis_bars, config.candle_warmup_bars)
        candles = self.cache.load_chart_frame(
            symbol.name,
            config.timeframe,
            required_bars,
            end_time=int(end_boundary.timestamp()),
        )
        if candles.empty or target_uid not in set(candles["uid"].astype(int)):
            logger.warning("%s uid=%s target broker basket not available in cache yet", symbol.name, target_uid)
            self.ledger.log(
                EventType.SYSTEM,
                symbol.name,
                target_uid,
                strategy_name,
                {
                    "skipped": "target broker basket not available",
                    "rows": len(candles),
                    "broker_now": broker_now.isoformat(),
                    "target_open": target_open.isoformat(),
                    "target_close": target_close.isoformat(),
                },
                timeframe=config.timeframe,
            )
            return {
                "symbol": symbol.name,
                "uid": target_uid,
                "timeframe": config.timeframe,
                "strategy": strategy_name,
                "processed": False,
                "status": "SKIPPED",
                "reason": "target broker basket not available",
                "rows": len(candles),
                "target_open": target_open.isoformat(),
                "target_close": target_close.isoformat(),
                "broker_now": broker_now.isoformat(),
            }

        positions = await self.mt5.positions(symbol=symbol.name)
        orders = await self.mt5.orders(symbol=symbol.name)

        strategy = Strategy(strategy_name, symbol, self.model_conf, self.ledger)
        recommendation = await strategy.analyze_recommendation(
            uid=target_uid,
            candles=candles,
            positions=positions,
            orders=orders,
            tick=tick,
            symbol_info=info,
        )

        if recommendation.status == "ERROR":
            logger.error(
                "%s uid=%s advisory recommendation=ERROR confidence=%.2f error=%s debug=%s artifact=%s positions=%s orders=%s",
                symbol.name,
                target_uid,
                recommendation.confidence,
                recommendation.error,
                _json_preview(recommendation.debug),
                _json_preview(recommendation.artifact, limit=1000),
                len(positions),
                len(orders),
            )
        else:
            logger.info(
                "%s uid=%s advisory recommendation=%s confidence=%.2f positions=%s orders=%s",
                symbol.name,
                target_uid,
                recommendation.status,
                recommendation.confidence,
                len(positions),
                len(orders),
            )
        self.ledger.mark_basket_processed(
            symbol=symbol.name,
            timeframe=config.timeframe,
            uid=target_uid,
            strategy=strategy_name,
            data={
                "broker_now": broker_now.isoformat(),
                "app_now_utc": app_now.isoformat(),
                "target_open": target_open.isoformat(),
                "target_close": target_close.isoformat(),
                "advisory_only": True,
                "recommendation": recommendation.status,
                "confidence": recommendation.confidence,
                "execution_attempted": False,
                "execution_ok": None,
                "recommendation_payload": asdict(recommendation),
            },
        )
        return {
            "symbol": symbol.name,
            "uid": target_uid,
            "timeframe": config.timeframe,
            "strategy": strategy_name,
            "processed": True,
            "status": "PROCESSED",
            "recommendation": recommendation.status,
            "confidence": recommendation.confidence,
            "error": recommendation.error,
            "target_open": target_open.isoformat(),
            "target_close": target_close.isoformat(),
            "broker_now": broker_now.isoformat(),
            "artifact": recommendation.artifact,
            "plan_legs": len(recommendation.action_plan.get("order_plan", []) or []),
            "risk_notes": recommendation.risk_notes,
        }

    def publish_latest_advice_pointer(self, *, app_now: datetime, results: list[dict[str, object]]) -> None:
        processed = [r for r in results if r.get("processed") and r.get("uid") is not None]
        if not processed:
            logger.info("advisory latest pointer not updated: no newly processed symbol results")
            return

        latest_uid = max(int(r["uid"]) for r in processed)
        slot_results = [r for r in results if r.get("uid") == latest_uid]
        created_at = datetime.now(timezone.utc).isoformat()
        manifest_blob = f"advice/{latest_uid}/{config.advisory_slot_manifest_name.strip('/')}"
        slot_processed = [r for r in slot_results if r.get("processed")]
        status_counts: dict[str, int] = {}
        for item in slot_results:
            key = str(item.get("status") or "UNKNOWN")
            status_counts[key] = status_counts.get(key, 0) + 1

        manifest_payload: dict[str, object] = {
            "schema_version": "alphard.advisory_slot_manifest.v1",
            "created_at": created_at,
            "app_now_utc": app_now.isoformat(),
            "uid": latest_uid,
            "timeframe": config.timeframe,
            "strategy": config.advisory_strategy_name,
            "symbols_expected": list(config.symbols),
            "status_counts": status_counts,
            "processed_count": len(slot_processed),
            "results": slot_results,
        }

        try:
            manifest_ref = self.artifact_storage.put_json_blob(manifest_payload, manifest_blob).ledger_ref
            pointer_payload = {
                "schema_version": "alphard.advisory_latest_pointer.v1",
                "updated_at": created_at,
                "app_now_utc": app_now.isoformat(),
                "uid": latest_uid,
                "timeframe": config.timeframe,
                "strategy": config.advisory_strategy_name,
                "symbols_expected": list(config.symbols),
                "manifest": manifest_ref,
                "status_counts": status_counts,
                "processed_count": len(slot_processed),
            }
            pointer_ref = self.artifact_storage.put_json_blob(pointer_payload, config.advisory_latest_pointer_blob).ledger_ref
        except Exception as exc:
            logger.exception("uid=%s failed to upload advisory latest pointer: %s", latest_uid, exc)
            self.ledger.log(
                EventType.ERROR,
                None,
                latest_uid,
                config.advisory_strategy_name,
                {"stage": "upload_advisory_latest_pointer", "error": str(exc)},
                timeframe=config.timeframe,
            )
            return

        logger.info(
            "uid=%s advisory latest pointer updated manifest=%s pointer=%s",
            latest_uid,
            manifest_ref.get("gcs_uri") or manifest_ref.get("blob_name"),
            pointer_ref.get("gcs_uri") or pointer_ref.get("blob_name"),
        )
        self.ledger.log(
            EventType.SYSTEM,
            None,
            latest_uid,
            config.advisory_strategy_name,
            {
                "stage": "advisory_latest_pointer",
                "manifest": manifest_ref,
                "pointer": pointer_ref,
                "status_counts": status_counts,
                "processed_count": len(slot_processed),
            },
            timeframe=config.timeframe,
        )


def _json_preview(value: object, *, limit: int | None = None) -> str:
    max_chars = int(limit if limit is not None else config.advisory_error_preview_chars)
    text = json.dumps(value, default=str, ensure_ascii=False, sort_keys=True)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"...<truncated {len(text) - max_chars} chars>"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Alphard MT5 async advisory VLM app")
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
