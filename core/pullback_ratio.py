from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Side = Literal["buy", "sell"]

PULLBACK_STRATEGY_NAMES = frozenset({
    "pullback_ratio_strategy",
    "levels_pullback_ratio_strategy",
    "pullback_ratio_strategy_trend",
    "pullback_ratio_strategy_v2",
})


@dataclass(frozen=True)
class PullbackLimitPlan:
    """Deterministic pending-limit entry derived from current quote and SL.

    The VLM/model chooses direction, structural stop, and target.  This planner
    converts that into a non-chasing limit entry:

    BUY:  limit = bid - (bid - stop_loss) * ratio
    SELL: limit = ask + (stop_loss - ask) * ratio

    The result is also constrained so it remains:
    - on the correct side of the current quote,
    - inside SL/TP geometry,
    - at least a broker-safe gap away from the current quote.
    """

    side: Side
    reference_price: float
    stop_loss: float
    ratio: float
    min_gap: float
    tick_size: float
    digits: int
    split_leg: int = 1

    @property
    def entry_price(self) -> float | None:
        if self.side == "buy":
            if self.stop_loss >= self.reference_price:
                return None
            raw = self.reference_price - (self.reference_price - self.stop_loss) * self.ratio
            constrained = min(raw, self.reference_price - self.min_gap)
            constrained = max(constrained, self.stop_loss + self.tick_size)
            return align_price(constrained, self.tick_size, self.digits, direction="down")

        if self.stop_loss <= self.reference_price:
            return None
        raw = self.reference_price + (self.stop_loss - self.reference_price) * self.ratio
        constrained = max(raw, self.reference_price + self.min_gap)
        constrained = min(constrained, self.stop_loss - self.tick_size)
        return align_price(constrained, self.tick_size, self.digits, direction="up")


def is_pullback_ratio_strategy(strategy_name: str) -> bool:
    return strategy_name in PULLBACK_STRATEGY_NAMES


def pullback_ratio_for_leg(base_ratio: float, split_leg: int = 1) -> float:
    base = clamp(float(base_ratio), 0.05, 0.95)
    if split_leg <= 1:
        return base
    # Second leg goes deeper toward SL; useful for split orders without clustering entries.
    return clamp(base + (1.0 - base) * 0.5, base, 0.95)


def build_pullback_limit_plan(
    *,
    side: Side | None,
    bid: float,
    ask: float,
    stop_loss: float,
    ratio: float,
    min_gap: float,
    tick_size: float,
    digits: int,
    split_leg: int = 1,
) -> PullbackLimitPlan | None:
    if side not in {"buy", "sell"}:
        return None
    reference_price = bid if side == "buy" else ask
    return PullbackLimitPlan(
        side=side,
        reference_price=float(reference_price),
        stop_loss=float(stop_loss),
        ratio=pullback_ratio_for_leg(ratio, split_leg),
        min_gap=float(min_gap),
        tick_size=max(float(tick_size), 1e-12),
        digits=int(digits),
        split_leg=split_leg,
    )


def align_price(price: float, tick_size: float, digits: int, *, direction: Literal["up", "down", "nearest"] = "nearest") -> float:
    import math

    tick_size = max(float(tick_size), 1e-12)
    if direction == "up":
        steps = math.ceil((price - 1e-12) / tick_size)
    elif direction == "down":
        steps = math.floor((price + 1e-12) / tick_size)
    else:
        steps = round(price / tick_size)
    return round(steps * tick_size, digits)


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


