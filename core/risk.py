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

        adjustments = self._auto_correct_trade_geometry(decision, symbol_info, entry)

        price_result = self._check_prices(ctx)
        if price_result is not None:
            return price_result
        entry_result = self._check_entry_price(decision, tick, symbol_info, entry)
        if entry_result is not None:
            return entry_result
        reward_risk_result = self._check_reward_risk(decision, entry)
        if reward_risk_result is not None:
            return reward_risk_result

        volume, sizing = self._volume(decision, symbol_info)
        if volume <= 0:
            return RiskResult(False, "computed volume is zero")
        adjusted = {"decision": asdict(decision), "tick": tick.raw, "symbol_info": symbol_info.raw, "sizing": sizing}
        if adjustments:
            adjusted["auto_adjustments"] = adjustments
            if "stop_loss" in adjustments:
                adjusted["sl_adjustment"] = adjustments["stop_loss"]
            if "take_profit" in adjustments:
                adjusted["tp_adjustment"] = adjustments["take_profit"]
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
        if d.status == "BUY" and d.allocation < 0:
            return RiskResult(False, "BUY allocation must be positive")
        if d.status == "SELL" and d.allocation > 0:
            return RiskResult(False, "SELL allocation must be negative")
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
        min_dist = self._protection_distance(info)
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

        min_dist = self._protection_distance(info)

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

    def _check_reward_risk(self, d: Decision, entry: float) -> RiskResult | None:
        if d.stop_loss is None or d.take_profit is None:
            return RiskResult(False, "missing stop_loss or take_profit")

        sl = float(d.stop_loss)
        tp = float(d.take_profit)
        if d.side == "buy":
            risk = entry - sl
            reward = tp - entry
        elif d.side == "sell":
            risk = sl - entry
            reward = entry - tp
        else:
            return RiskResult(False, "decision has no side")

        if risk <= 0 or reward <= 0:
            return RiskResult(False, "invalid reward/risk geometry")

        ratio = reward / risk
        if ratio < config.min_reward_risk_ratio:
            return RiskResult(False, f"reward/risk {ratio:.2f} < minimum {config.min_reward_risk_ratio:.2f}")
        return None

    def _pending_limit_gap(self, info: SymbolInfo, multiplier: int = 1) -> float:
        noise_points = max(0, min(int(config.entry_noise_points), 5))
        # A limit order still needs to be on the correct side of the spread,
        # and MT5 also requires it to satisfy the broker stops level.
        points = max(noise_points * max(1, multiplier), self._broker_stop_points(info) + 2, 2)
        return points * info.point

    def _protection_distance(self, info: SymbolInfo) -> float:
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

    def _tick_size(self, info: SymbolInfo) -> float:
        raw = info.raw.get("trade_tick_size") or info.raw.get("tick_size") or info.point
        try:
            tick_size = float(raw)
        except Exception:
            tick_size = info.point
        return tick_size if tick_size > 0 else info.point

    def _entry_price(self, d: Decision, tick: Tick, info: SymbolInfo) -> float:
        if config.execution_mode == "market":
            return round(tick.ask if d.side == "buy" else tick.bid, info.digits)

        if self._uses_pullback_ratio_entry() and d.stop_loss is not None:
            return self._entry_price_for_pullback(d, tick, info, split_leg=1)

        return self._entry_price_for_gap(d, tick, info, multiplier=1)

    def _entry_price_for_gap(self, d: Decision, tick: Tick, info: SymbolInfo, multiplier: int = 1) -> float:
        gap = self._pending_limit_gap(info, multiplier=multiplier)
        if d.side == "buy":
            return round(tick.bid - gap, info.digits)
        return round(tick.ask + gap, info.digits)

    def _entry_price_for_pullback(self, d: Decision, tick: Tick, info: SymbolInfo, split_leg: int = 1) -> float:
        if d.stop_loss is None:
            return self._entry_price_for_gap(d, tick, info, multiplier=split_leg)

        ratio = self._pullback_ratio(split_leg)
        min_gap = self._pending_limit_gap(info, multiplier=max(1, split_leg))
        sl = float(d.stop_loss)

        if d.side == "buy":
            ref = tick.bid
            if sl >= ref:
                return self._entry_price_for_gap(d, tick, info, multiplier=split_leg)
            entry = ref - (ref - sl) * ratio
            entry = min(entry, ref - min_gap)
            entry = max(entry, sl + info.point)
        else:
            ref = tick.ask
            if sl <= ref:
                return self._entry_price_for_gap(d, tick, info, multiplier=split_leg)
            entry = ref + (sl - ref) * ratio
            entry = max(entry, ref + min_gap)
            entry = min(entry, sl - info.point)

        return round(entry, info.digits)

    def _pullback_ratio(self, split_leg: int = 1) -> float:
        base = self._clamp(float(config.pullback_ratio), 0.05, 0.95)
        if split_leg <= 1:
            return base
        # Put the second split order deeper toward SL so the two entries are not clustered.
        return self._clamp(base + (1.0 - base) * 0.5, base, 0.95)

    def _uses_pullback_ratio_entry(self) -> bool:
        return config.strategy_name in {"pullback_ratio_strategy", "levels_pullback_ratio_strategy"}

    def _auto_correct_trade_geometry(self, d: Decision, info: SymbolInfo, entry: float) -> dict[str, Any]:
        """Repair deterministic, broker-checkable geometry before rejecting.

        The VLM often gets direction right but places SL/TP a few points too close
        for the broker or a little short of the configured reward/risk floor.  Those
        are mechanical issues, so fix them deterministically instead of converting
        an otherwise tradable signal into a HOLD/reject.
        """
        if d.side is None or d.stop_loss is None or d.take_profit is None:
            return {}

        adjustments: dict[str, Any] = {}
        # Use the larger of app policy and broker stops level. Add slack so
        # rounding and broker-side comparisons do not turn an exactly-on-
        # threshold value back into "too close".
        min_effective = self._protection_distance(info) + max(self._tick_size(info), self._pending_limit_gap(info) * 0.05)
        min_rr = max(float(config.min_reward_risk_ratio), 0.0)

        original_sl = float(d.stop_loss)
        original_tp = float(d.take_profit)
        sl = original_sl
        tp = original_tp

        if d.side == "buy":
            # SL must be below entry by at least the broker/min configured distance.
            if entry - sl < min_effective:
                level = self._nearest_level(d.levels, below=entry - min_effective, prefer="support")
                fallback = entry - min_effective
                sl = min((level - info.point) if level is not None else fallback, fallback)

            risk = max(entry - sl, min_effective)
            min_tp = entry + risk * min_rr + info.point
            if tp - entry < risk * min_rr:
                tp = min_tp

        else:
            # SL must be above entry by at least the broker/min configured distance.
            if sl - entry < min_effective:
                level = self._nearest_level(d.levels, above=entry + min_effective, prefer="resistance")
                fallback = entry + min_effective
                sl = max((level + info.point) if level is not None else fallback, fallback)

            risk = max(sl - entry, min_effective)
            max_tp = entry - risk * min_rr - info.point
            if entry - tp < risk * min_rr:
                tp = max_tp

        sl = round(sl, info.digits)
        tp = round(tp, info.digits)

        if sl != original_sl:
            d.stop_loss = sl
            adjustments["stop_loss"] = {
                "from": original_sl,
                "to": sl,
                "entry": entry,
                "min_distance_points": round(min_effective / info.point, 2),
            }
        if tp != original_tp:
            d.take_profit = tp
            adjustments["take_profit"] = {
                "from": original_tp,
                "to": tp,
                "entry": entry,
                "min_reward_risk_ratio": config.min_reward_risk_ratio,
            }
        return adjustments

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
            "full_take_profit": float(d.take_profit),
            "tp_ratio": 1.0,
            "order_kind": d.order_kind,
        }
        if not config.split_order_enabled or config.execution_mode != "pending_limit":
            return [base]

        runner_ratio = self._clamp(float(config.split_order_ratio), 0.0, 1.0)
        partial_tp_ratio = self._clamp(float(config.split_partial_tp_ratio), 0.0, 1.0)
        runner_volume = self._normalize_volume(volume * runner_ratio, info, enforce_min=False)
        partial_volume = self._normalize_volume(volume - runner_volume, info, enforce_min=False)
        if runner_volume < info.volume_min or partial_volume < info.volume_min:
            return [base]

        partial_tp = self._scaled_take_profit(d.side, entry, float(d.take_profit), partial_tp_ratio, info)
        if d.side == "buy":
            if not (entry < partial_tp < float(d.take_profit)):
                return [base]
        else:
            if not (float(d.take_profit) < partial_tp < entry):
                return [base]

        return [
            {**base, "volume": runner_volume, "split_role": "runner"},
            {
                **base,
                "volume": partial_volume,
                "take_profit": partial_tp,
                "tp_ratio": partial_tp_ratio,
                "split_role": "partial",
            },
        ]

    def _scaled_take_profit(self, side: str | None, entry: float, take_profit: float, ratio: float, info: SymbolInfo) -> float:
        if side == "buy":
            return round(entry + (take_profit - entry) * ratio, info.digits)
        return round(entry - (entry - take_profit) * ratio, info.digits)

    def _volume(self, d: Decision, info: SymbolInfo) -> tuple[float, dict[str, Any]]:
        min_volume = info.volume_min or 0.0
        volume_cap = self._symbol_volume_cap(info)
        allocation_weight = self._allocation_weight(d)
        confidence_weight = self._confidence_weight(d.confidence)

        # Confidence should be the main size driver, while allocation remains a
        # soft modifier. This avoids the old behavior where large symbol base
        # volumes immediately hit MAX_VOLUME and every approved trade used the
        # same lot size.
        conviction_weight = confidence_weight * (0.5 + 0.5 * allocation_weight)
        requested = min_volume + max(volume_cap - min_volume, 0.0) * conviction_weight
        volume = self._normalize_volume(requested, info)
        sizing = {
            "min_volume": min_volume,
            "volume_cap": volume_cap,
            "allocation_weight": round(allocation_weight, 4),
            "confidence_weight": round(confidence_weight, 4),
            "conviction_weight": round(conviction_weight, 4),
            "requested_volume": round(requested, 8),
            "normalized_volume": volume,
        }
        return volume, sizing

    def _symbol_volume_cap(self, info: SymbolInfo) -> float:
        name = info.name.upper()
        symbol_cap = next(
            (volume for symbol, volume in config.symbol_base_volume.items() if name.startswith(symbol.upper())),
            config.base_volume,
        )
        cap = min(float(symbol_cap), config.max_volume)
        if info.volume_max is not None:
            cap = min(cap, info.volume_max)
        return max(info.volume_min or 0.0, cap)

    def _allocation_weight(self, d: Decision) -> float:
        if config.max_allocation <= 0:
            return 0.0
        return self._clamp(abs(float(d.allocation)) / config.max_allocation, 0.0, 1.0)

    def _confidence_weight(self, confidence: float) -> float:
        denominator = max(1.0 - config.min_confidence, 1e-9)
        return self._clamp((float(confidence) - config.min_confidence) / denominator, 0.0, 1.0)

    def _clamp(self, value: float, lower: float, upper: float) -> float:
        return max(lower, min(upper, value))

    def _normalize_volume(self, requested: float, info: SymbolInfo, enforce_min: bool = True) -> float:
        if info.volume_max is not None:
            requested = min(requested, info.volume_max)
        step = info.volume_step or info.volume_min or 0.01
        steps = math.floor((requested + 1e-12) / step)
        volume = round(steps * step, 8)
        if enforce_min:
            volume = max(info.volume_min, volume)
        return round(volume, 8)
