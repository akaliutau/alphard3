from __future__ import annotations

from dataclasses import asdict
from typing import Any

from core.ledger import EventType, Ledger
from core.models import Decision, ExecutionResult, RiskResult, SymbolInfo, Tick
from core.mt5_api import MT5Client
from utilities.settings import config, logger


class ExecutionEngine:
    def __init__(self, api: MT5Client, ledger: Ledger):
        self.api = api
        self.ledger = ledger

    async def cleanup_pending_orders(self, symbol: str) -> list[dict[str, Any]]:
        if not config.cancel_stale_pending_orders:
            return []
        removed = []
        for order in await self.api.orders(symbol=symbol):
            if order.get("magic") == config.mt5_magic or str(order.get("comment", "")).startswith(config.app_name):
                ticket = int(order["ticket"])
                removed.append(await self.api.cancel_order(ticket, comment=f"{config.app_name}-cleanup"))
        return removed

    async def execute(
        self,
        symbol: str,
        uid: int,
        strategy: str,
        decision: Decision,
        risk: RiskResult,
        tick: Tick,
        info: SymbolInfo,
    ) -> ExecutionResult:
        if not risk.approved:
            result = ExecutionResult(attempted=False, dry_run=config.dry_run, error=risk.reason)
            self._log(symbol, uid, strategy, result)
            return result

        assert decision.side is not None
        entry = risk.entry_price or (tick.ask if decision.side == "buy" else tick.bid)
        body = {
            "symbol": symbol,
            "side": decision.side,
            "volume": risk.volume,
            "sl": round(float(decision.stop_loss), info.digits) if decision.stop_loss is not None else None,
            "tp": round(float(decision.take_profit), info.digits) if decision.take_profit is not None else None,
            "deviation": config.mt5_deviation,
            "magic": config.mt5_magic,
            #"comment": f"{config.app_name}-{strategy}-{uid}",
            "type_filling": config.mt5_type_filling,
        }
        if config.execution_mode == "pending_limit":
            body.update({"order_kind": "limit", "price": round(float(entry), info.digits)})

        if config.dry_run:
            result = ExecutionResult(attempted=True, dry_run=True, request=body, ok=True)
            self._log(symbol, uid, strategy, result)
            logger.info("DRY_RUN: would execute %s", body)
            return result

        try:
            if config.execution_mode == "market":
                response = await self.api.open_deal(body)
            else:
                response = await self.api.place_pending_order(body)
            retcode = response.get("retcode") or (response.get("result") or {}).get("retcode")
            result = ExecutionResult(attempted=True, dry_run=False, request=body, response=response, ok=True, retcode=retcode)
        except Exception as exc:
            result = ExecutionResult(attempted=True, dry_run=False, request=body, ok=False, error=str(exc))

        self._log(symbol, uid, strategy, result)
        return result

    def _log(self, symbol: str, uid: int, strategy: str, result: ExecutionResult) -> None:
        self.ledger.log(EventType.TRADE, symbol=symbol, uid=uid, strategy=strategy, timeframe=config.timeframe, data=asdict(result))
