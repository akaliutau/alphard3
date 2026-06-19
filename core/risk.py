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
        sl_adjustment = self._auto_correct_stop_loss(decision, symbol_info, entry)

        price_result = self._check_prices(ctx)
        if price_result is not None:
            return price_result
        entry_result = self._check_entry_price(decision, tick, symbol_info, entry)
        if entry_result is not None:
            return entry_result

        volume = self._volume(decision, symbol_info)
        if volume <= 0:
            return RiskResult(False, "computed volume is zero")
        adjusted = {"decision": asdict(decision), "tick": tick.raw, "symbol_info": symbol_info.raw}
        if sl_adjustment:
            adjusted["sl_adjustment"] = sl_adjustment
        order_plan = self._order_plan(decision, tick, symbol_info, entry, volume)
        if len(order_plan) > 1:
            adjusted["orders"] = order_plan
        return RiskResult(
            approved=True,
            reason="approved",
            volume=volume,
            entry_price=entry,
            order_kind=decision.order_kind,
            adjusted=adjusted,
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

    def _pending_limit_gap(self, info: SymbolInfo, multiplier: int = 1) -> float:
        raw_stops_level = info.raw.get("trade_stops_level") or info.raw.get("stops_level") or 0
        try:
            broker_stop_points = int(raw_stops_level)
        except Exception:
            broker_stop_points = 0

        noise_points = max(0, min(int(config.entry_noise_points), 5))
        # A limit order still needs to be on the correct side of the spread; use at least one point.
        points = max(noise_points * max(1, multiplier), broker_stop_points + 1, 1)
        return points * info.point

    def _entry_price(self, d: Decision, tick: Tick, info: SymbolInfo) -> float:
        if config.execution_mode == "market":
            return round(tick.ask if d.side == "buy" else tick.bid, info.digits)

        return self._entry_price_for_gap(d, tick, info, multiplier=1)

    def _entry_price_for_gap(self, d: Decision, tick: Tick, info: SymbolInfo, multiplier: int = 1) -> float:
        gap = self._pending_limit_gap(info, multiplier=multiplier)
        if d.side == "buy":
            return round(tick.bid - gap, info.digits)
        return round(tick.ask + gap, info.digits)

    def _auto_correct_stop_loss(self, d: Decision, info: SymbolInfo, entry: float) -> dict[str, Any]:
        if d.side is None or d.stop_loss is None or d.take_profit is None:
            return {}

        min_dist = config.min_stop_distance_points * info.point
        original = float(d.stop_loss)
        tp = float(d.take_profit)
        buffer = max(self._pending_limit_gap(info), info.point)

        if d.side == "buy":
            if entry - original >= min_dist:
                return {}
            level = self._nearest_level(d.levels, below=entry - min_dist, prefer="support")
            reward = max(tp - entry, min_dist)
            fallback = entry - max(min_dist, reward * 0.5)
            corrected = min((level - buffer) if level is not None else fallback, entry - min_dist)
        else:
            if original - entry >= min_dist:
                return {}
            level = self._nearest_level(d.levels, above=entry + min_dist, prefer="resistance")
            reward = max(entry - tp, min_dist)
            fallback = entry + max(min_dist, reward * 0.5)
            corrected = max((level + buffer) if level is not None else fallback, entry + min_dist)

        d.stop_loss = round(corrected, info.digits)
        return {"from": original, "to": d.stop_loss, "entry": entry, "min_distance_points": config.min_stop_distance_points}

    def _nearest_level(
        self,
        levels: dict[str, Any],
        *,
        below: float | None = None,
        above: float | None = None,
        prefer: str,
    ) -> float | None:
        values = self._level_values(levels, prefer) or self._level_values(levels, "")
        if below is not None:
            candidates = [x for x in values if x <= below]
            return max(candidates) if candidates else None
        if above is not None:
            candidates = [x for x in values if x >= above]
            return min(candidates) if candidates else None
        return None

    def _level_values(self, value: Any, prefer: str) -> list[float]:
        out: list[float] = []
        if isinstance(value, dict):
            for key, item in value.items():
                key_l = str(key).lower()
                if prefer and prefer not in key_l:
                    continue
                out.extend(self._level_values(item, ""))
            return out
        if isinstance(value, (list, tuple, set)):
            for item in value:
                out.extend(self._level_values(item, ""))
            return out
        try:
            out.append(float(value))
        except Exception:
            pass
        return out

    def _order_plan(self, d: Decision, tick: Tick, info: SymbolInfo, entry: float, volume: float) -> list[dict[str, Any]]:
        base = {
            "entry_price": entry,
            "volume": volume,
            "stop_loss": float(d.stop_loss),
            "take_profit": float(d.take_profit),
            "order_kind": d.order_kind,
        }
        if not config.split_order_enabled or config.execution_mode != "pending_limit":
            return [base]

        first_volume = self._normalize_volume(volume / 2.0, info, enforce_min=False)
        second_volume = self._normalize_volume(volume - first_volume, info, enforce_min=False)
        if first_volume < info.volume_min or second_volume < info.volume_min:
            return [base]

        second_entry = self._entry_price_for_gap(d, tick, info, multiplier=2)
        if d.side == "buy":
            if not (float(d.stop_loss) < second_entry < float(d.take_profit)):
                return [base]
            close_tp = round(entry + (float(d.take_profit) - entry) * 0.5, info.digits)
            if not (entry < close_tp < float(d.take_profit)):
                return [base]
        else:
            if not (float(d.take_profit) < second_entry < float(d.stop_loss)):
                return [base]
            close_tp = round(entry - (entry - float(d.take_profit)) * 0.5, info.digits)
            if not (float(d.take_profit) < close_tp < entry):
                return [base]

        return [
            {**base, "volume": first_volume, "take_profit": close_tp},
            {**base, "entry_price": second_entry, "volume": second_volume},
        ]

    def _volume(self, d: Decision, info: SymbolInfo) -> float:
        name = info.name.upper()
        print(f"_volume {name}")
        symbol_base_volume = next(
            (volume for symbol, volume in config.symbol_base_volume.items() if name.startswith(symbol.upper())),
            config.base_volume,
        )
        print(f"_volume {symbol_base_volume}")
        requested = symbol_base_volume * min(abs(d.allocation), config.max_allocation)
        requested = min(requested, config.max_volume)
        requested = max(requested, info.volume_min)
        normv = self._normalize_volume(requested, info)
        print(f"normv {normv}")
        return normv

    def _normalize_volume(self, requested: float, info: SymbolInfo, enforce_min: bool = True) -> float:
        if info.volume_max is not None:
            requested = min(requested, info.volume_max)
        step = info.volume_step or info.volume_min or 0.01
        steps = math.floor((requested + 1e-12) / step)
        volume = round(steps * step, 8)
        if enforce_min:
            volume = max(info.volume_min, volume)
        return round(volume, 8)
