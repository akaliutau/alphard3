from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from core.models import Candle, SymbolInfo, Tick, SUCCESS_RETCODES
from utilities.settings import logger


class MT5APIError(RuntimeError):
    pass


@dataclass(frozen=True)
class MT5Config:
    base_url: str
    api_key: str
    timeout_seconds: float = 30.0
    max_retries: int = 3


class MT5Client:
    """Async client for the uploaded MT5 Wine Proxy API."""

    def __init__(self, cfg: MT5Config):
        self.cfg = cfg
        self.base_url = cfg.base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            headers={"X-API-Key": cfg.api_key},
            timeout=cfg.timeout_seconds,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(1, self.cfg.max_retries + 1):
            try:
                resp = await self._client.request(method, path, **kwargs)
                data = resp.json() if resp.content else {}
                if resp.status_code >= 400:
                    raise MT5APIError(f"{method} {path} -> HTTP {resp.status_code}: {data}")
                if isinstance(data, dict) and data.get("ok") is False:
                    raise MT5APIError(f"{method} {path} returned ok=false: {data}")
                return data
            except Exception as exc:
                last_error = exc
                if attempt >= self.cfg.max_retries:
                    break
                await asyncio.sleep(min(2 ** attempt, 10))
        raise MT5APIError(str(last_error))

    async def health(self) -> dict[str, Any]:
        # /health does not require API key but sending it is harmless.
        return await self._request("GET", "/health")

    async def ready(self) -> dict[str, Any]:
        return await self._request("GET", "/health/ready")

    async def status(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/status")

    async def account(self) -> dict[str, Any]:
        return await self._request("GET", "/v1/account")

    async def tick(self, symbol: str) -> tuple[Tick, SymbolInfo]:
        data = await self._request("GET", f"/v1/symbols/{symbol}/tick")
        tick_raw = data.get("tick") or {}
        info_raw = data.get("symbol_info") or {}
        tick = Tick(
            bid=float(tick_raw.get("bid") or 0.0),
            ask=float(tick_raw.get("ask") or 0.0),
            time=int(tick_raw["time"]) if tick_raw.get("time") is not None else None,
            time_msc=int(tick_raw["time_msc"]) if tick_raw.get("time_msc") is not None else None,
            raw=tick_raw,
        )
        info = SymbolInfo(
            name=info_raw.get("name") or symbol,
            digits=int(info_raw.get("digits") or 5),
            point=float(info_raw.get("point") or 0.00001),
            volume_min=float(info_raw.get("volume_min") or 0.01),
            volume_step=float(info_raw.get("volume_step") or 0.01),
            volume_max=float(info_raw["volume_max"]) if info_raw.get("volume_max") is not None else None,
            raw=info_raw,
        )
        if tick.bid <= 0 and tick.ask <= 0:
            raise MT5APIError(f"No usable bid/ask for {symbol}: {data}")
        return tick, info

    async def bars(self, symbol: str, timeframe: str, start: datetime, end: datetime) -> list[Candle]:
        params = {
            "symbol": symbol,
            "timeframe": timeframe,
            "start": _iso_z(start),
            "end": _iso_z(end),
        }
        data = await self._request("GET", "/v1/bars", params=params)
        bars = []
        for row in data.get("bars") or []:
            volume = row.get("tick_volume") if row.get("tick_volume") is not None else row.get("real_volume", 0)
            bars.append(
                Candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    time=int(row["time"]),
                    time_iso=row.get("time_iso") or datetime.fromtimestamp(int(row["time"]), timezone.utc).isoformat(),
                    open=float(row["open"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    close=float(row["close"]),
                    volume=float(volume or 0),
                    spread=float(row.get("spread") or 0),
                    real_volume=float(row.get("real_volume") or 0),
                )
            )
        return bars

    async def positions(self, symbol: str | None = None, ticket: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if ticket is not None:
            params["ticket"] = ticket
        elif symbol:
            params["symbol"] = symbol
        data = await self._request("GET", "/v1/positions", params=params)
        return list(data.get("positions") or [])

    async def orders(self, symbol: str | None = None, ticket: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {}
        if ticket is not None:
            params["ticket"] = ticket
        elif symbol:
            params["symbol"] = symbol
        data = await self._request("GET", "/v1/orders", params=params)
        return list(data.get("orders") or [])

    async def open_deal(self, body: dict[str, Any]) -> dict[str, Any]:
        data = await self._request("POST", "/v1/deals/open", json=_trade_payload(body))
        _assert_trade_ok(data, "open_deal")
        return data

    async def close_deal(self, body: dict[str, Any]) -> dict[str, Any]:
        data = await self._request("POST", "/v1/deals/close", json=_trade_payload(body))
        _assert_trade_ok(data, "close_deal")
        return data

    async def place_pending_order(self, body: dict[str, Any]) -> dict[str, Any]:
        data = await self._request("POST", "/v1/orders/pending", json=_trade_payload(body))
        _assert_trade_ok(data, "place_pending_order")
        return data

    async def modify_order(self, ticket: int, body: dict[str, Any]) -> dict[str, Any]:
        data = await self._request("POST", f"/v1/orders/{ticket}/modify", json=_trade_payload(body))
        _assert_trade_ok(data, "modify_order")
        return data

    async def cancel_order(self, ticket: int, comment: str | None = None) -> dict[str, Any]:
        data = await self._request("DELETE", f"/v1/orders/{ticket}")
        _assert_trade_ok(data, "cancel_order")
        return data


def _trade_payload(data: dict[str, Any]) -> dict[str, Any]:
    # A few brokers reject non-empty MT5 order comments.  Keep comments out of
    # all app-originated trade payloads even if a caller accidentally adds one.
    clean = dict(data)
    clean.pop("comment", None)
    return _drop_none(clean)


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in data.items() if v is not None}


def _iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _assert_trade_ok(payload: dict[str, Any], label: str) -> None:
    retcode = payload.get("retcode") or (payload.get("result") or {}).get("retcode")
    if retcode not in SUCCESS_RETCODES:
        raise MT5APIError(f"{label} retcode not success: {retcode}; payload={payload}")


