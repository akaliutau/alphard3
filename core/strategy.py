from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any

import pandas as pd

from core.ledger import EventType, Ledger
from core.models import Decision, Symbol
from middleware.llm_middleware import LLMMetrics, ModelConfig, call_llm, coerce_to_simple_string
from utilities.ImageStorage import ImageStorage
from utilities.imagegenv2 import generate_chart_image_v2
from utilities.prompt_manager import PromptManager
from utilities.settings import config, logger


class Strategy:
    def __init__(self, name: str, symbol: Symbol, model_conf: ModelConfig, ledger: Ledger):
        self.name = name
        self.symbol = symbol
        self.model_conf = model_conf
        self.ledger = ledger
        self.prompt_manager = PromptManager()
        self.image_storage = ImageStorage(
            provider=config.image_provider,
            bucket_name=config.gcs_bucket_name,
            public_read=config.gcs_public_read,
        )

    async def analyze(self, uid: int, candles: pd.DataFrame, positions: list[dict[str, Any]]) -> Decision:
        chart_path = generate_chart_image_v2(candles, target_uid=uid, window_size=config.chart_window_bars, symbol=self.symbol.name)
        if chart_path is None:
            return Decision(status="ERROR", error="chart generation failed")

        image_ref = self.image_storage.get_image_entry(chart_path, self.symbol.name, uid)
        self.ledger.log(
            EventType.CHART,
            symbol=self.symbol.name,
            uid=uid,
            strategy=self.name,
            timeframe=config.timeframe,
            data={"chart_path": str(chart_path), **image_ref.ledger_ref},
        )

        market_summary = _market_summary(candles)
        prompt = self.prompt_manager.compose_prompt(
            f"{self.name}.j2",
            pair_name=self.symbol.name,
            timeframe=config.timeframe,
            state=json.dumps({"positions": positions}, default=str),
            market_summary=market_summary,
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    image_ref.prompt_part,
                ],
            }
        ]

        metrics = LLMMetrics()
        raw = ""
        try:
            raw = await call_llm(
                messages=messages,
                config=self.model_conf,
                transformer=coerce_to_simple_string,
                metadata={
                    "vertex_location": config.vertex_location,
                    "vertex_project": config.vertex_project,
                    "timeout": config.litellm_timeout_seconds,
                },
                metrics=metrics,
            )
            decision = parse_strategy_output(raw)
        except Exception as exc:
            decision = Decision(status="ERROR", error=str(exc), raw_text=raw)

        self.ledger.log(
            EventType.LLM,
            symbol=self.symbol.name,
            uid=uid,
            strategy=self.name,
            timeframe=config.timeframe,
            data={
                "model": asdict(self.model_conf),
                "metrics": metrics.to_dict(),
                "raw": raw[:4000],
                "parse": {
                    "status": decision.status,
                    "confidence": decision.confidence,
                    "allocation": decision.allocation,
                    "has_sl": decision.stop_loss is not None,
                    "has_tp": decision.take_profit is not None,
                    "error": decision.error,
                },
            },
        )
        self.ledger.log(
            EventType.DECISION,
            symbol=self.symbol.name,
            uid=uid,
            strategy=self.name,
            timeframe=config.timeframe,
            data=asdict(decision),
        )
        logger.info("%s %s decision=%s confidence=%.2f", self.symbol.name, uid, decision.status, decision.confidence)
        return decision


def parse_strategy_output(text: str) -> Decision:
    clean = text.strip().replace("```json", "").replace("```", "").strip()
    obj = _extract_json(clean)
    if obj is not None:
        status = str(obj.get("decision") or obj.get("status") or "ERROR").upper()
        if status not in {"BUY", "SELL", "HOLD"}:
            return Decision(status="ERROR", error=f"invalid decision {status}", raw_text=text)
        return Decision(
            status=status,  # type: ignore[arg-type]
            allocation=_clamp_float(obj.get("allocation", 0.0), -1.0, 1.0),
            confidence=_clamp_float(obj.get("confidence", abs(float(obj.get("allocation", 0.0) or 0.0))), 0.0, 1.0),
            stop_loss=_float_or_none(obj.get("stop_loss")),
            take_profit=_float_or_none(obj.get("take_profit")),
            entry_price=_float_or_none(obj.get("entry_price")),
            order_kind=str(obj.get("order_kind") or "limit").lower(),  # type: ignore[arg-type]
            rationale=str(obj.get("rationale") or obj.get("analysis") or ""),
            levels={},
            raw_text=text,
        )

    # Backward-compatible parser for the old plain text format: BUY 0.5 SL TP\nreason
    lines = [x.strip() for x in clean.splitlines() if x.strip()]
    if not lines:
        return Decision(status="ERROR", error="empty LLM response", raw_text=text)
    tokens = lines[0].split()
    command = tokens[0].upper()
    if command == "HOLD":
        return Decision(status="HOLD", rationale=" ".join(lines[1:]), raw_text=text)
    if command not in {"BUY", "SELL"}:
        return Decision(status="ERROR", error=f"invalid command {command}", raw_text=text)
    nums = []
    for token in tokens[1:]:
        try:
            nums.append(float(token.replace(",", "").replace("$", "")))
        except ValueError:
            pass
    if len(nums) < 3:
        return Decision(status="ERROR", error="expected allocation, stop_loss, take_profit", raw_text=text)
    allocation = abs(nums[0]) if command == "BUY" else -abs(nums[0])
    return Decision(
        status=command,  # type: ignore[arg-type]
        allocation=_clamp_float(allocation, -1.0, 1.0),
        confidence=min(abs(allocation), 1.0),
        stop_loss=nums[1],
        take_profit=nums[2],
        rationale=" ".join(lines[1:]),
        raw_text=text,
    )


def _extract_json(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _market_summary(df: pd.DataFrame) -> str:
    tail = df.tail(8)
    rows = []
    for _, r in tail.iterrows():
        rows.append(
            f"{r.get('time_iso', '')}: O={r.open:.5g} H={r.high:.5g} L={r.low:.5g} C={r.close:.5g} V={r.volume:.0f}"
        )
    return "\n".join(rows)


def _float_or_none(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except Exception:
        return None


def _clamp_float(value: Any, lo: float, hi: float) -> float:
    try:
        v = float(value)
    except Exception:
        v = 0.0
    return max(lo, min(hi, v))
