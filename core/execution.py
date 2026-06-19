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
        bodies = self._request_bodies(symbol, decision, risk, tick, info)
        request_payload = {"orders": bodies} if len(bodies) > 1 else bodies[0]

        if config.dry_run:
            result = ExecutionResult(attempted=True, dry_run=True, request=request_payload, ok=True)
            self._log(symbol, uid, strategy, result)
            logger.info("DRY_RUN: would execute %s", request_payload)
            return result

        responses = []
        try:
            for body in bodies:
                if config.execution_mode == "market":
                    response = await self.api.open_deal(body)
                else:
                    response = await self.api.place_pending_order(body)
                responses.append(response)
            first_response = responses[0] if responses else {}
            print(first_response)
            retcode = first_response.get("retcode") or (first_response.get("result") or {}).get("retcode")
            result = ExecutionResult(
                attempted=True,
                dry_run=False,
                request=request_payload,
                response={"orders": responses} if len(responses) > 1 else first_response,
                ok=True,
                retcode=retcode,
            )
        except Exception as exc:
            result = ExecutionResult(
                attempted=True,
                dry_run=False,
                request=request_payload,
                response={"orders": responses} if responses else {},
                ok=False,
                error=str(exc),
            )

        self._log(symbol, uid, strategy, result)
        return result

    def _request_bodies(
        self,
        symbol: str,
        decision: Decision,
        risk: RiskResult,
        tick: Tick,
        info: SymbolInfo,
    ) -> list[dict[str, Any]]:
        plans = risk.adjusted.get("orders") if isinstance(risk.adjusted, dict) else None
        if not isinstance(plans, list) or not plans:
            entry = risk.entry_price or (tick.ask if decision.side == "buy" else tick.bid)
            plans = [
                {
                    "entry_price": entry,
                    "volume": risk.volume,
                    "stop_loss": decision.stop_loss,
                    "take_profit": decision.take_profit,
                    "order_kind": risk.order_kind,
                }
            ]

        bodies = []
        for plan in plans:
            body = {
                "symbol": symbol,
                "side": decision.side,
                "volume": float(plan.get("volume", risk.volume)),
                "sl": round(float(plan["stop_loss"]), info.digits) if plan.get("stop_loss") is not None else None,
                "tp": round(float(plan["take_profit"]), info.digits) if plan.get("take_profit") is not None else None,
                "deviation": config.mt5_deviation,
                "magic": config.mt5_magic,
                #"comment": f"{config.app_name}-{strategy}-{uid}",
                "type_filling": config.mt5_type_filling,
            }
            if config.execution_mode == "pending_limit":
                body.update({"order_kind": "limit", "price": round(float(plan["entry_price"]), info.digits)})
            bodies.append(body)
        return bodies

    def _log(self, symbol: str, uid: int, strategy: str, result: ExecutionResult) -> None:
        self.ledger.log(EventType.TRADE, symbol=symbol, uid=uid, strategy=strategy, timeframe=config.timeframe, data=asdict(result))
