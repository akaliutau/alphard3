from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

Side = Literal["buy", "sell"]
DecisionStatus = Literal["BUY", "SELL", "HOLD", "ERROR"]
OrderKind = Literal["limit", "stop", "stop_limit"]

SUCCESS_RETCODES = {10008, 10009, 10010}


@dataclass(frozen=True)
class Symbol:
    name: str
    alias: str | None = None


@dataclass(frozen=True)
class Candle:
    symbol: str
    timeframe: str
    time: int                  # API/MT5 bar open time, seconds since epoch UTC
    time_iso: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    spread: float = 0.0
    real_volume: float = 0.0

    @property
    def ts_ms(self) -> int:
        return int(self.time) * 1000


@dataclass
class SymbolInfo:
    name: str
    digits: int = 5
    point: float = 0.00001
    volume_min: float = 0.01
    volume_step: float = 0.01
    volume_max: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Tick:
    bid: float
    ask: float
    # MetaQuotes/MT5 server-clock timestamp. We use this as broker "now" for candle baskets.
    time: int | None = None
    time_msc: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass
class Decision:
    status: DecisionStatus
    allocation: float = 0.0
    confidence: float = 0.0
    stop_loss: float | None = None
    take_profit: float | None = None
    entry_price: float | None = None
    order_kind: OrderKind = "limit"
    rationale: str = ""
    levels: dict[str, Any] = field(default_factory=dict)
    raw_text: str = ""
    error: str | None = None

    @property
    def side(self) -> Side | None:
        if self.status == "BUY":
            return "buy"
        if self.status == "SELL":
            return "sell"
        return None


@dataclass
class RiskResult:
    approved: bool
    reason: str
    volume: float = 0.0
    entry_price: float | None = None
    order_kind: OrderKind = "limit"
    adjusted: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExecutionResult:
    attempted: bool
    dry_run: bool
    request: dict[str, Any] = field(default_factory=dict)
    response: dict[str, Any] = field(default_factory=dict)
    ok: bool = False
    retcode: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class ModelRunMeta:
    symbol: str
    uid: int
    strategy: str
    model: str
    chart_uri: str | None
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
