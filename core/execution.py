from __future__ import annotations

import math
from dataclasses import asdict
from typing import Any

from core.ledger import EventType, Ledger
from core.models import Decision, ExecutionResult, RiskResult, SymbolInfo, Tick
from core.pullback_ratio import build_pullback_limit_plan, is_pullback_ratio_strategy, pullback_ratio_for_leg
from core.mt5_api import MT5Client
from utilities.settings import config, logger


class ExecutionEngine:
    def __init__(self, api: MT5Client, ledger: Ledger):
        self.api = api
        self.ledger = ledger

    async def cleanup_pending_orders(self, symbol: str) -> list[dict[str, Any]]:
        if not config.cancel_stale_pending_orders:
            return []

        try:
            orders = await self.api.orders(symbol=symbol)
        except Exception as exc:
            logger.exception("%s pending order cleanup: could not list orders; continuing to execution", symbol)
            return [{"stage": "list_orders", "ok": False, "error": str(exc)}]

        removed = []
        for order in orders:
            if not (order.get("magic") == config.mt5_magic or str(order.get("comment", "")).startswith(config.app_name)):
                continue
            ticket = int(order["ticket"])
            try:
                response = await self.api.cancel_order(ticket)
                removed.append({"ticket": ticket, "ok": True, "response": response})
            except Exception as exc:
                logger.exception("%s pending order cleanup: cancel failed ticket=%s; continuing to execution", symbol, ticket)
                removed.append({"ticket": ticket, "ok": False, "error": str(exc)})
        if removed:
            logger.info("%s pending order cleanup results=%s", symbol, removed)
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
        mode = self._execution_mode(decision)
        exec_tick = tick
        exec_info = info
        if not config.dry_run:
            try:
                exec_tick, exec_info = await self.api.tick(symbol)
                logger.info(
                    "%s uid=%s refreshed tick before %s execution bid=%s ask=%s",
                    symbol,
                    uid,
                    mode,
                    exec_tick.bid,
                    exec_tick.ask,
                )
            except Exception:
                # Fall back to the analysis-time tick, but make the risk visible.
                logger.exception("%s uid=%s could not refresh tick before %s execution; using analysis tick", symbol, uid, mode)
        try:
            bodies = self._request_bodies(symbol, decision, risk, exec_tick, exec_info, mode=mode)
            request_payload = {"orders": bodies} if len(bodies) > 1 else bodies[0]
        except Exception as exc:
            logger.exception("%s uid=%s failed to build MT5 request", symbol, uid)
            result = ExecutionResult(attempted=False, dry_run=config.dry_run, error=f"failed to build MT5 request: {exc}")
            self._log(symbol, uid, strategy, result)
            return result

        if config.dry_run:
            result = ExecutionResult(attempted=True, dry_run=True, request=request_payload, ok=True)
            self._log(symbol, uid, strategy, result)
            logger.info("DRY_RUN: would execute %s uid=%s request=%s", symbol, uid, request_payload)
            return result

        responses = []
        logger.info("%s uid=%s MT5 execution start mode=%s orders=%s", symbol, uid, mode, len(bodies))
        try:
            for index, body in enumerate(bodies, start=1):
                logger.info("%s uid=%s MT5 request %s/%s body=%s", symbol, uid, index, len(bodies), body)
                if mode == "market":
                    response = await self.api.open_deal(body)
                else:
                    response = await self.api.place_pending_order(body)
                logger.info("%s uid=%s MT5 response %s/%s response=%s", symbol, uid, index, len(bodies), response)
                responses.append(response)
            first_response = responses[0] if responses else {}
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
            logger.exception("%s uid=%s MT5 execution failed after %s/%s responses", symbol, uid, len(responses), len(bodies))
            result = ExecutionResult(
                attempted=True,
                dry_run=False,
                request=request_payload,
                response={"orders": responses} if responses else {},
                ok=False,
                error=str(exc),
            )

        self._log(symbol, uid, strategy, result)
        logger.info(
            "%s uid=%s trade result attempted=%s ok=%s retcode=%s error=%s",
            symbol,
            uid,
            result.attempted,
            result.ok,
            result.retcode,
            result.error,
        )
        return result

    def _request_bodies(
        self,
        symbol: str,
        decision: Decision,
        risk: RiskResult,
        tick: Tick,
        info: SymbolInfo,
        *,
        mode: str | None = None,
    ) -> list[dict[str, Any]]:
        mode = mode or self._execution_mode(decision)
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
        for index, plan in enumerate(plans, start=1):
            sl = round(float(plan["stop_loss"]), info.digits) if plan.get("stop_loss") is not None else None
            tp = round(float(plan["take_profit"]), info.digits) if plan.get("take_profit") is not None else None
            full_tp = round(float(plan.get("full_take_profit", plan.get("take_profit"))), info.digits) if plan.get("take_profit") is not None else None
            tp_ratio = self._clamp(float(plan.get("tp_ratio", 1.0)), 0.0, 1.0)
            price = round(float(plan["entry_price"]), info.digits) if plan.get("entry_price") is not None else None

            if mode == "pending_limit":
                # Re-price just-in-time from the fresh tick. The LLM/risk tick can
                # be several seconds old, and a stale sell/buy limit price easily
                # becomes MT5 retcode 10015 Invalid price. Split legs intentionally
                # share the same entry and SL; only volume and TP differ.
                price = self._pending_limit_price(
                    decision.side,
                    tick,
                    info,
                    multiplier=1,
                    stop_loss=sl,
                    split_leg=1,
                )
                sl, full_tp = self._repair_protection(decision.side, price, sl, full_tp, info)
                if tp_ratio < 1.0 and full_tp is not None:
                    tp = self._scaled_take_profit(decision.side, price, full_tp, tp_ratio, info)
                    tp = self._ensure_take_profit_distance(decision.side, price, tp, info)
                else:
                    tp = full_tp
                tp = self._spread_adjusted_tp(decision.side, price, tp, tick, info)
                tp = self._ensure_take_profit_distance(decision.side, price, tp, info)

            if mode == "market":
                price = tick.ask if decision.side == "buy" else tick.bid
                sl, full_tp = self._repair_protection(decision.side, price, sl, full_tp, info)
                tp = self._ensure_take_profit_distance(decision.side, price, full_tp, info)

            body = {
                "symbol": symbol,
                "side": decision.side,
                "volume": float(plan.get("volume", risk.volume)),
                "sl": sl,
                "tp": tp,
                "deviation": config.mt5_deviation,
                "magic": config.mt5_magic,
                "type_filling": config.mt5_type_filling,
            }
            # Intentionally do not send a comment field. Some brokers reject
            # non-empty comments, and the upstream MT5 proxy may add its own
            # default comment if that proxy is not patched separately.
            if mode == "pending_limit":
                body.update({"order_kind": "limit", "price": price})
            bodies.append(body)
        return bodies

    def _execution_mode(self, decision: Decision) -> str:
        if decision.order_kind == "market":
            return "market"
        return config.execution_mode

    def _pending_limit_price(
        self,
        side: str | None,
        tick: Tick,
        info: SymbolInfo,
        multiplier: int = 1,
        stop_loss: float | None = None,
        split_leg: int = 1,
    ) -> float:
        if self._uses_pullback_ratio_entry() and stop_loss is not None:
            price = self._pullback_limit_price(side, tick, info, float(stop_loss), split_leg=split_leg)
            if price is not None:
                return price

        gap = self._pending_limit_gap(info, multiplier=multiplier)
        if side == "buy":
            return self._align_price(tick.bid - gap, info, direction="down")
        return self._align_price(tick.ask + gap, info, direction="up")

    def _pullback_limit_price(
        self,
        side: str | None,
        tick: Tick,
        info: SymbolInfo,
        stop_loss: float,
        split_leg: int = 1,
    ) -> float | None:
        plan = build_pullback_limit_plan(
            side=side,
            bid=tick.bid,
            ask=tick.ask,
            stop_loss=float(stop_loss),
            ratio=float(config.pullback_ratio),
            min_gap=self._pending_limit_gap(info, multiplier=max(1, split_leg)),
            tick_size=self._tick_size(info),
            digits=info.digits,
            split_leg=split_leg,
        )
        return None if plan is None else plan.entry_price

    def _pullback_ratio(self, split_leg: int = 1) -> float:
        return pullback_ratio_for_leg(float(config.pullback_ratio), split_leg)

    def _uses_pullback_ratio_entry(self) -> bool:
        return is_pullback_ratio_strategy(config.strategy_name)

    def _pending_limit_gap(self, info: SymbolInfo, multiplier: int = 1) -> float:
        # Distance between current quote and pending entry. MT5 validates this
        # against the broker stops level before the order is accepted.
        noise_points = max(0, min(int(config.entry_noise_points), 10))
        points = max(noise_points * max(1, multiplier), self._broker_stop_points(info) + 2, 2)
        return points * info.point

    def _protection_distance(self, info: SymbolInfo) -> float:
        # Distance between pending entry and SL/TP. This must use the larger of
        # app policy and broker stop-level policy. The previous code used only
        # MIN_STOP_DISTANCE_POINTS here, so a broker requiring more than 20
        # points could reject an otherwise well-shaped pending order as
        # retcode=10016 Invalid stops.
        points = max(int(config.min_stop_distance_points), self._broker_stop_points(info) + 2, 1)
        return points * info.point + self._tick_size(info)

    def _broker_stop_points(self, info: SymbolInfo) -> int:
        values = []
        for key in ("trade_stops_level", "stops_level", "trade_freeze_level", "freeze_level"):
            raw = info.raw.get(key)
            if raw is None:
                continue
            try:
                values.append(int(raw))
            except Exception:
                continue
        return max(values, default=0)

    def _repair_protection(
        self,
        side: str | None,
        entry: float,
        stop_loss: float | None,
        take_profit: float | None,
        info: SymbolInfo,
    ) -> tuple[float | None, float | None]:
        if stop_loss is None or take_profit is None:
            return stop_loss, take_profit

        min_dist = self._protection_distance(info)
        min_rr = max(float(config.min_reward_risk_ratio), 0.0)
        sl = float(stop_loss)
        tp = float(take_profit)

        if side == "buy":
            if entry - sl < min_dist:
                sl = entry - min_dist
            risk = max(entry - sl, min_dist)
            if tp - entry < risk * min_rr:
                tp = entry + risk * min_rr + self._tick_size(info)
            sl = self._align_price(sl, info, direction="down")
            tp = self._align_price(tp, info, direction="up")
        else:
            if sl - entry < min_dist:
                sl = entry + min_dist
            risk = max(sl - entry, min_dist)
            if entry - tp < risk * min_rr:
                tp = entry - risk * min_rr - self._tick_size(info)
            sl = self._align_price(sl, info, direction="up")
            tp = self._align_price(tp, info, direction="down")

        return sl, tp

    def _spread_adjusted_tp(
        self,
        side: str | None,
        entry: float,
        take_profit: float | None,
        tick: Tick,
        info: SymbolInfo,
    ) -> float | None:
        if take_profit is None:
            return None

        spread_gap = max(tick.ask - tick.bid, 0.0) + 2 * self._tick_size(info)
        if side == "buy":
            adjusted = max(entry + self._tick_size(info), float(take_profit) - spread_gap)
            return self._align_price(adjusted, info, direction="down")

        adjusted = min(entry - self._tick_size(info), float(take_profit) + spread_gap)
        return self._align_price(adjusted, info, direction="up")

    def _scaled_take_profit(
        self,
        side: str | None,
        entry: float,
        take_profit: float,
        ratio: float,
        info: SymbolInfo,
    ) -> float:
        ratio = self._clamp(float(ratio), 0.0, 1.0)
        if side == "buy":
            target = entry + (float(take_profit) - entry) * ratio
            return self._align_price(target, info, direction="down")

        target = entry - (entry - float(take_profit)) * ratio
        return self._align_price(target, info, direction="up")

    def _ensure_take_profit_distance(
        self,
        side: str | None,
        entry: float,
        take_profit: float | None,
        info: SymbolInfo,
    ) -> float | None:
        if take_profit is None:
            return None

        min_gap = self._protection_distance(info)
        if side == "buy":
            return self._align_price(max(float(take_profit), entry + min_gap), info, direction="up")

        return self._align_price(min(float(take_profit), entry - min_gap), info, direction="down")

    def _clamp(self, value: float, lower: float, upper: float) -> float:
        return max(lower, min(upper, value))

    def _align_price(self, price: float, info: SymbolInfo, *, direction: str) -> float:
        tick_size = self._tick_size(info)
        if direction == "up":
            steps = math.ceil((price - 1e-12) / tick_size)
        elif direction == "down":
            steps = math.floor((price + 1e-12) / tick_size)
        else:
            steps = round(price / tick_size)
        return round(steps * tick_size, info.digits)

    def _tick_size(self, info: SymbolInfo) -> float:
        raw = info.raw.get("trade_tick_size") or info.raw.get("tick_size") or info.point
        try:
            tick_size = float(raw)
        except Exception:
            tick_size = info.point
        return tick_size if tick_size > 0 else info.point

    def _log(self, symbol: str, uid: int, strategy: str, result: ExecutionResult) -> None:
        self.ledger.log(EventType.TRADE, symbol=symbol, uid=uid, strategy=strategy, timeframe=config.timeframe, data=asdict(result))


