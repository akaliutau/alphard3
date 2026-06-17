from __future__ import annotations

import math
from dataclasses import asdict
from typing import Any

from core.models import Decision, RiskResult, SymbolInfo, Tick
from utilities.settings import config


class RiskEngine:
    """Deterministic chain of responsibility. AI proposes; this decides whether it is executable."""

    def validate(
        self,
        decision: Decision,
        tick: Tick,
        symbol_info: SymbolInfo,
        positions: list[dict[str, Any]],
        orders: list[dict[str, Any]],
    ) -> RiskResult:
        checks = [
            self._check_not_error,
            self._check_not_hold,
            self._check_confidence,
            self._check_position_cap,
            self._check_prices,
        ]
        ctx = {
            "decision": decision,
            "tick": tick,
            "info": symbol_info,
            "positions": positions,
            "orders": orders,
        }
        for check in checks:
            result = check(ctx)
            if result is not None:
                return result

        side = decision.side
        assert side is not None
        entry = self._entry_price(decision, tick, symbol_info)
        volume = self._volume(decision, symbol_info)
        if volume <= 0:
            return RiskResult(False, "computed volume is zero")
        return RiskResult(
            approved=True,
            reason="approved",
            volume=volume,
            entry_price=entry,
            order_kind=decision.order_kind,
            adjusted={"decision": asdict(decision), "tick": tick.raw, "symbol_info": symbol_info.raw},
        )

    def _check_not_error(self, ctx: dict[str, Any]) -> RiskResult | None:
        d: Decision = ctx["decision"]
        if d.status == "ERROR":
            return RiskResult(False, f"LLM decision error: {d.error}")
        return None

    def _check_not_hold(self, ctx: dict[str, Any]) -> RiskResult | None:
        d: Decision = ctx["decision"]
        if d.status == "HOLD":
            return RiskResult(False, "decision is HOLD")
        return None

    def _check_confidence(self, ctx: dict[str, Any]) -> RiskResult | None:
        d: Decision = ctx["decision"]
        if d.confidence < config.min_confidence:
            return RiskResult(False, f"confidence {d.confidence:.2f} < minimum {config.min_confidence:.2f}")
        if abs(d.allocation) <= 0:
            return RiskResult(False, "allocation is zero")
        return None

    def _check_position_cap(self, ctx: dict[str, Any]) -> RiskResult | None:
        positions = ctx["positions"]
        if len(positions) >= config.max_active_positions:
            return RiskResult(False, f"active position cap reached: {len(positions)} >= {config.max_active_positions}")
        return None

    def _check_prices(self, ctx: dict[str, Any]) -> RiskResult | None:
        d: Decision = ctx["decision"]
        tick: Tick = ctx["tick"]
        info: SymbolInfo = ctx["info"]
        if d.stop_loss is None or d.take_profit is None:
            return RiskResult(False, "missing stop_loss or take_profit")
        ref = tick.ask if d.side == "buy" else tick.bid
        min_dist = config.min_stop_distance_points * info.point
        if d.side == "buy":
            if not (d.stop_loss < ref < d.take_profit):
                return RiskResult(False, f"BUY requires stop_loss < price < take_profit; sl={d.stop_loss}, price={ref}, tp={d.take_profit}")
            if abs(ref - d.stop_loss) < min_dist:
                return RiskResult(False, "BUY stop_loss too close to current price")
        elif d.side == "sell":
            if not (d.take_profit < ref < d.stop_loss):
                return RiskResult(False, f"SELL requires take_profit < price < stop_loss; tp={d.take_profit}, price={ref}, sl={d.stop_loss}")
            if abs(d.stop_loss - ref) < min_dist:
                return RiskResult(False, "SELL stop_loss too close to current price")
        else:
            return RiskResult(False, "decision has no side")
        return None

    def _check_entry_price(self, d: Decision, tick: Tick, info: SymbolInfo, entry: float) -> RiskResult | None:
        if config.execution_mode != "pending_limit":
            return None

        min_dist = config.min_stop_distance_points * info.point

        if d.side == "buy":
            if not (entry < tick.bid):
                return RiskResult(False, f"BUY_LIMIT entry must be below current bid; entry={entry}, bid={tick.bid}")
            if not (float(d.stop_loss) < entry < float(d.take_profit)):
                return RiskResult(False,
                                  f"BUY_LIMIT requires stop_loss < entry < take_profit; sl={d.stop_loss}, entry={entry}, tp={d.take_profit}")
            if abs(entry - float(d.stop_loss)) < min_dist:
                return RiskResult(False, "BUY_LIMIT stop_loss too close to entry")

        elif d.side == "sell":
            if not (entry > tick.ask):
                return RiskResult(False, f"SELL_LIMIT entry must be above current ask; entry={entry}, ask={tick.ask}")
            if not (float(d.take_profit) < entry < float(d.stop_loss)):
                return RiskResult(False,
                                  f"SELL_LIMIT requires take_profit < entry < stop_loss; tp={d.take_profit}, entry={entry}, sl={d.stop_loss}")
            if abs(float(d.stop_loss) - entry) < min_dist:
                return RiskResult(False, "SELL_LIMIT stop_loss too close to entry")

        return None

    def _pending_limit_gap(self, info: SymbolInfo) -> float:
        raw_stops_level = info.raw.get("trade_stops_level") or info.raw.get("stops_level") or 0
        try:
            broker_stop_points = int(raw_stops_level)
        except Exception:
            broker_stop_points = 0

        points = max(config.entry_pullback_points, broker_stop_points + 1, 1)
        return points * info.point

    def _entry_price(self, d: Decision, tick: Tick, info: SymbolInfo) -> float:
        if config.execution_mode == "market":
            return round(tick.ask if d.side == "buy" else tick.bid, info.digits)

        gap = self._pending_limit_gap(info)
        requested = round(float(d.entry_price),
                          info.digits) if d.entry_price is not None and d.entry_price > 0 else None

        if d.side == "buy":
            safe_price = round(tick.bid - gap, info.digits)
            if requested is None or requested >= tick.bid:
                return safe_price
            return round(min(requested, safe_price), info.digits)

        safe_price = round(tick.ask + gap, info.digits)
        if requested is None or requested <= tick.ask:
            return safe_price
        return round(max(requested, safe_price), info.digits)

    def _volume(self, d: Decision, info: SymbolInfo) -> float:
        requested = config.base_volume * min(abs(d.allocation), config.max_allocation)
        requested = min(requested, config.max_volume)
        requested = max(requested, info.volume_min)
        if info.volume_max is not None:
            requested = min(requested, info.volume_max)
        step = info.volume_step or info.volume_min or 0.01
        steps = math.floor((requested + 1e-12) / step)
        return round(max(info.volume_min, steps * step), 8)
