from __future__ import annotations

import json
import re
import traceback
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from core.ledger import EventType, Ledger
from core.models import AdvisoryRecommendation, Decision, Symbol, SymbolInfo, Tick
from middleware.llm_middleware import LLMMetrics, ModelConfig, call_llm, coerce_to_simple_string
from utilities.ImageStorage import ImageStorage
from utilities.imagegenv2 import detect_chart_levels, generate_chart_image_v2
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

    async def analyze_recommendation(
        self,
        uid: int,
        candles: pd.DataFrame,
        positions: list[dict[str, Any]],
        orders: list[dict[str, Any]] | None = None,
        tick: Tick | None = None,
        symbol_info: SymbolInfo | None = None,
    ) -> AdvisoryRecommendation:
        """Return a structured, analysis-only recommendation.

        This method is the new live path. It renders two M1 scales:
        - global: approx. 26h / 1560 candles, 16:9 or 4:3, max/min highlighted.
        - detail: latest 2-3h / 120-180 candles, 1:1.
        """
        global_view = _slice_to_uid(candles, uid, window_size=config.global_analysis_bars)
        detail_view = _slice_to_uid(candles, uid, window_size=config.detailed_analysis_bars)
        if detail_view.empty:
            debug = {
                "stage": "slice_detail",
                "uid": uid,
                "input_rows": 0 if candles is None else len(candles),
                "requested_detail_bars": config.detailed_analysis_bars,
                "available_columns": [] if candles is None else list(candles.columns),
            }
            logger.error("%s uid=%s advisory ERROR error=%s debug=%s", self.symbol.name, uid, "target candle unavailable", _json_preview(debug))
            return AdvisoryRecommendation(status="ERROR", error="target candle unavailable", debug=debug)
        if global_view.empty:
            global_view = detail_view

        current = detail_view.iloc[-1]
        logger.info(
            "%s uid=%s advisory current time=%s C=%.5f detail_rows=%s global_rows=%s detail_start=%s global_start=%s global_low=%.5f global_high=%.5f",
            self.symbol.name,
            uid,
            current.get("time_iso", current.get("datetime", "")),
            float(current["close"]),
            len(detail_view),
            len(global_view),
            str(detail_view.iloc[0].get("time_iso", detail_view.iloc[0].get("datetime", ""))),
            str(global_view.iloc[0].get("time_iso", global_view.iloc[0].get("datetime", ""))),
            float(global_view["low"].min()),
            float(global_view["high"].max()),
        )

        global_chart_path = generate_chart_image_v2(
            global_view,
            target_uid=uid,
            window_size=config.global_analysis_bars,
            symbol=f"{self.symbol.name}_global",
            aspect_ratio=config.global_chart_aspect,
            profile="global",
            highlight_extrema=True,
            max_levels=24,
        )
        if global_chart_path is None:
            debug = {
                "stage": "render_global_chart",
                "global_rows": len(global_view),
                "global_window_bars": config.global_analysis_bars,
                "aspect_ratio": config.global_chart_aspect,
            }
            logger.error("%s uid=%s advisory ERROR error=%s debug=%s", self.symbol.name, uid, "global chart generation failed", _json_preview(debug))
            return AdvisoryRecommendation(status="ERROR", error="global chart generation failed", debug=debug)

        detail_chart_path = generate_chart_image_v2(
            detail_view,
            target_uid=uid,
            window_size=config.detailed_analysis_bars,
            symbol=f"{self.symbol.name}_detail",
            aspect_ratio=config.detailed_chart_aspect,
            profile="detail",
            highlight_extrema=False,
            max_levels=14,
        )
        if detail_chart_path is None:
            debug = {
                "stage": "render_detail_chart",
                "detail_rows": len(detail_view),
                "detail_window_bars": config.detailed_analysis_bars,
                "aspect_ratio": config.detailed_chart_aspect,
            }
            logger.error("%s uid=%s advisory ERROR error=%s debug=%s", self.symbol.name, uid, "detail chart generation failed", _json_preview(debug))
            return AdvisoryRecommendation(status="ERROR", error="detail chart generation failed", debug=debug)

        global_ref = self.image_storage.get_image_entry(global_chart_path, self.symbol.name, uid)
        detail_ref = self.image_storage.get_image_entry(detail_chart_path, self.symbol.name, uid)
        chart_artifacts = {"global": global_ref.ledger_ref, "detail": detail_ref.ledger_ref}
        visible_levels = {
            "global": detect_chart_levels(global_view, uid, config.global_analysis_bars, max_levels=24),
            "detail": detect_chart_levels(detail_view, uid, config.detailed_analysis_bars, max_levels=14),
        }
        self.ledger.log(
            EventType.CHART,
            symbol=self.symbol.name,
            uid=uid,
            strategy=self.name,
            timeframe=config.timeframe,
            data={
                "global": {
                    "chart_path": str(global_chart_path),
                    "bars": len(global_view),
                    "aspect_ratio": config.global_chart_aspect,
                    "highlight_extrema": True,
                    **global_ref.ledger_ref,
                },
                "detail": {
                    "chart_path": str(detail_chart_path),
                    "bars": len(detail_view),
                    "aspect_ratio": config.detailed_chart_aspect,
                    **detail_ref.ledger_ref,
                },
                "visible_levels": visible_levels,
            },
        )

        prompt = self.prompt_manager.compose_prompt(
            f"{self.name}.j2",
            pair_name=self.symbol.name,
            timeframe=config.timeframe,
            state=json.dumps(
                {
                    "positions": positions,
                    "orders": orders or [],
                    "tick": getattr(tick, "raw", None) or (asdict(tick) if tick else None),
                    "symbol_info": getattr(symbol_info, "raw", None) or (asdict(symbol_info) if symbol_info else None),
                },
                default=str,
            ),
            current_price=float(current["close"]),
            global_summary=_analysis_summary(global_view, label="global_26h"),
            detailed_summary=_analysis_summary(detail_view, label="latest_2_3h"),
            visible_levels=json.dumps(visible_levels, default=str),
            global_window_bars=len(global_view),
            detailed_window_bars=len(detail_view),
            global_chart_aspect=config.global_chart_aspect,
            detailed_chart_aspect=config.detailed_chart_aspect,
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "text",
                        "text": (
                            f"CHART 1 — GLOBAL CONTEXT: last {len(global_view)} M1 candles "
                            f"(~{len(global_view) / 60:.1f}h) ending at uid {uid}. "
                            "This chart is rendered wide and highlights the window max/min."
                        ),
                    },
                    global_ref.prompt_part,
                    {
                        "type": "text",
                        "text": (
                            f"CHART 2 — LATEST DYNAMICS: last {len(detail_view)} M1 candles "
                            f"(~{len(detail_view) / 60:.1f}h), rendered square for execution-level detail."
                        ),
                    },
                    detail_ref.prompt_part,
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
                    "debug": config.litellm_debug,
                    "log_prompt": config.litellm_log_prompt,
                    "log_response": config.litellm_log_response,
                    "response_preview_chars": config.litellm_response_preview_chars,
                    "drop_params": config.litellm_drop_params,
                    "suppress_pydantic_warnings": config.litellm_suppress_pydantic_warnings,
                },
                metrics=metrics,
            )
            recommendation = parse_recommendation_output(raw)
            if recommendation.status == "ERROR":
                recommendation.debug.setdefault("stage", "parse_recommendation")
                recommendation.debug.setdefault("llm_metrics", metrics.to_dict())
                recommendation.debug.setdefault("model", asdict(self.model_conf))
            if not recommendation.levels:
                recommendation.levels = visible_levels
        except Exception as exc:
            logger.exception("%s uid=%s LLM advisory analysis failed: %s", self.symbol.name, uid, exc)
            recommendation = AdvisoryRecommendation(
                status="ERROR",
                error=str(exc),
                raw_text=raw,
                debug=_exception_debug(
                    stage="llm_call_or_parse_exception",
                    exc=exc,
                    raw=raw,
                    metrics=metrics,
                    extra={"model": asdict(self.model_conf)},
                ),
            )

        recommendation.artifact = {"charts": chart_artifacts}
        recommendation_payload = asdict(recommendation)
        advice_artifact = {"provider": config.image_provider, "uploaded": False}
        try:
            advice_artifact = self.image_storage.put_json_entry(
                _advice_payload(
                    symbol=self.symbol.name,
                    uid=uid,
                    strategy=self.name,
                    current=current,
                    recommendation=recommendation_payload,
                    global_ref=global_ref.ledger_ref,
                    detail_ref=detail_ref.ledger_ref,
                    visible_levels=visible_levels,
                    positions=positions,
                    orders=orders or [],
                    tick=tick,
                    symbol_info=symbol_info,
                    metrics=metrics,
                    chart_windows={"global_bars": len(global_view), "detail_bars": len(detail_view)},
                ),
                symbol=self.symbol.name,
                uid=uid,
            ).ledger_ref
        except Exception as exc:
            logger.exception("%s uid=%s failed to upload advisory JSON: %s", self.symbol.name, uid, exc)
            advice_artifact = {"provider": config.image_provider, "uploaded": False, "error": str(exc)}
            self.ledger.log(
                EventType.ERROR,
                symbol=self.symbol.name,
                uid=uid,
                strategy=self.name,
                timeframe=config.timeframe,
                data={"stage": "upload_advisory_json", "error": str(exc)},
            )
        recommendation.artifact = {"advice": advice_artifact, "charts": chart_artifacts}
        if recommendation.status == "ERROR":
            recommendation.debug.setdefault("advice_artifact", advice_artifact)
            error_details = _advisory_error_details(recommendation, metrics=metrics, raw=raw)
            logger.error(
                "%s uid=%s advisory ERROR error=%s details=%s",
                self.symbol.name,
                uid,
                recommendation.error,
                _json_preview(error_details),
            )
            self.ledger.log(
                EventType.ERROR,
                symbol=self.symbol.name,
                uid=uid,
                strategy=self.name,
                timeframe=config.timeframe,
                data={"stage": "advisory_recommendation", **error_details},
            )

        self.ledger.log(
            EventType.LLM,
            symbol=self.symbol.name,
            uid=uid,
            strategy=self.name,
            timeframe=config.timeframe,
            data={
                "model": asdict(self.model_conf),
                "metrics": metrics.to_dict(),
                "raw": raw[:8000],
                "advice_artifact": advice_artifact,
                "current_candle": _candle_to_dict(current),
                "chart_windows": {"global_bars": len(global_view), "detail_bars": len(detail_view)},
                "parse": {
                    "status": recommendation.status,
                    "confidence": recommendation.confidence,
                    "has_action_plan": bool(recommendation.action_plan),
                    "error": recommendation.error,
                },
            },
        )
        self.ledger.log(
            EventType.RECOMMENDATION,
            symbol=self.symbol.name,
            uid=uid,
            strategy=self.name,
            timeframe=config.timeframe,
            data=asdict(recommendation),
        )
        if recommendation.status == "ERROR":
            logger.error(
                "%s %s recommendation=ERROR confidence=%.2f plan_legs=%s error=%s artifact=%s",
                self.symbol.name,
                uid,
                recommendation.confidence,
                len(recommendation.action_plan.get("order_plan", []) or []),
                recommendation.error,
                _json_preview(recommendation.artifact, limit=1000),
            )
        else:
            logger.info(
                "%s %s recommendation=%s confidence=%.2f plan_legs=%s",
                self.symbol.name,
                uid,
                recommendation.status,
                recommendation.confidence,
                len(recommendation.action_plan.get("order_plan", []) or []),
            )
        return recommendation

    async def analyze(self, uid: int, candles: pd.DataFrame, positions: list[dict[str, Any]]) -> Decision:
        """Legacy decision path kept for tests and backward compatibility.

        The live application no longer calls this method. It is retained so the
        old parser/risk/execution utility tests and offline experiments continue
        to work without forcing callers to migrate immediately.
        """
        trigger_view = _slice_to_uid(candles, uid, window_size=config.chart_window_bars)
        if trigger_view.empty:
            logger.error("%s uid=%s no candles available after slicing to target", self.symbol.name, uid)
            return Decision(status="ERROR", error="target candle unavailable")

        context_view = _slice_to_uid(candles, uid, window_size=config.chart_context_window_bars)
        if context_view.empty:
            context_view = trigger_view

        current = trigger_view.iloc[-1]
        trigger_chart_path = generate_chart_image_v2(
            trigger_view,
            target_uid=uid,
            window_size=config.chart_window_bars,
            symbol=f"{self.symbol.name}_trigger",
        )
        if trigger_chart_path is None:
            return Decision(status="ERROR", error="trigger chart generation failed")

        context_chart_path = generate_chart_image_v2(
            context_view,
            target_uid=uid,
            window_size=config.chart_context_window_bars,
            symbol=f"{self.symbol.name}_context",
        )
        if context_chart_path is None:
            return Decision(status="ERROR", error="context chart generation failed")

        trigger_ref = self.image_storage.get_image_entry(trigger_chart_path, self.symbol.name, uid)
        context_ref = self.image_storage.get_image_entry(context_chart_path, self.symbol.name, uid)
        self.ledger.log(
            EventType.CHART,
            symbol=self.symbol.name,
            uid=uid,
            strategy=self.name,
            timeframe=config.timeframe,
            data={
                "trigger": {"chart_path": str(trigger_chart_path), "bars": len(trigger_view), **trigger_ref.ledger_ref},
                "context": {"chart_path": str(context_chart_path), "bars": len(context_view), **context_ref.ledger_ref},
            },
        )

        prompt = self.prompt_manager.compose_prompt(
            f"{self.name}.j2",
            pair_name=self.symbol.name,
            timeframe=config.timeframe,
            state=json.dumps({"positions": positions}, default=str),
            market_summary=_market_summary(trigger_view),
            context_summary=_context_summary(context_view, trigger_view),
            trigger_window_bars=len(trigger_view),
            context_window_bars=len(context_view),
            execution_mode=config.execution_mode,
            pullback_ratio=config.pullback_ratio,
            min_reward_risk_ratio=config.min_reward_risk_ratio,
            max_chase_progress=config.max_chase_progress,
        )
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "text", "text": f"CHART 1 — trigger evidence window: last {len(trigger_view)} candles ending at uid {uid}."},
                    trigger_ref.prompt_part,
                    {"type": "text", "text": f"CHART 2 — wider context window: last {len(context_view)} candles ending at the same candle."},
                    context_ref.prompt_part,
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
                    "debug": config.litellm_debug,
                    "log_prompt": config.litellm_log_prompt,
                    "log_response": config.litellm_log_response,
                    "response_preview_chars": config.litellm_response_preview_chars,
                    "drop_params": config.litellm_drop_params,
                    "suppress_pydantic_warnings": config.litellm_suppress_pydantic_warnings,
                },
                metrics=metrics,
            )
            decision = parse_strategy_output(raw)
        except Exception as exc:
            logger.exception("%s uid=%s LLM analysis failed: %s", self.symbol.name, uid, exc)
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
                "current_candle": _candle_to_dict(current),
                "chart_windows": {"trigger_bars": len(trigger_view), "context_bars": len(context_view)},
                "parse": {
                    "status": decision.status,
                    "confidence": decision.confidence,
                    "allocation": decision.allocation,
                    "has_sl": decision.stop_loss is not None,
                    "has_tp": decision.take_profit is not None,
                    "evidence": decision.evidence,
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
        return decision


def _advice_payload(
    *,
    symbol: str,
    uid: int,
    strategy: str,
    current: pd.Series,
    recommendation: dict[str, Any],
    global_ref: dict[str, Any],
    detail_ref: dict[str, Any],
    visible_levels: dict[str, Any],
    positions: list[dict[str, Any]],
    orders: list[dict[str, Any]],
    tick: Tick | None,
    symbol_info: SymbolInfo | None,
    metrics: LLMMetrics,
    chart_windows: dict[str, int],
) -> dict[str, Any]:
    return {
        "schema_version": "alphard.advisory_recommendation.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "symbol": symbol,
        "timeframe": config.timeframe,
        "uid": uid,
        "strategy": strategy,
        "model_config": {
            "name": config.model_name,
            "model": config.model_id,
            "max_tokens": config.litellm_max_tokens,
            "vertex_location": config.vertex_location,
            "vertex_project": config.vertex_project,
        },
        "current_candle": _candle_to_dict(current),
        "chart_windows": chart_windows,
        "chart_refs": {
            "global": global_ref,
            "detail": detail_ref,
        },
        "visible_levels": visible_levels,
        "market_state": {
            "positions": positions,
            "orders": orders,
            "tick": getattr(tick, "raw", None) or (asdict(tick) if tick else None),
            "symbol_info": getattr(symbol_info, "raw", None) or (asdict(symbol_info) if symbol_info else None),
        },
        "llm_metrics": metrics.to_dict(),
        "recommendation": recommendation,
    }


def parse_recommendation_output(text: str) -> AdvisoryRecommendation:
    clean = text.strip().replace("```json", "").replace("```", "").strip()
    obj, parse_debug = _extract_json_with_debug(clean, original_text=text)
    if obj is None:
        return AdvisoryRecommendation(
            status="ERROR",
            error="empty or invalid advisory JSON",
            raw_text=text,
            debug=parse_debug,
        )

    action_plan = obj.get("action_plan") if isinstance(obj.get("action_plan"), dict) else {}
    status_source = (
        obj.get("recommendation")
        or obj.get("decision")
        or obj.get("status")
        or action_plan.get("recommendation")
        or action_plan.get("action")
        or "ERROR"
    )
    status = str(status_source).upper()
    parse_debug.update(
        {
            "stage": "parse_recommendation",
            "top_level_keys": sorted(str(k) for k in obj.keys()),
            "action_plan_keys": sorted(str(k) for k in action_plan.keys()),
            "status_candidate": status,
            "status_source_type": type(status_source).__name__,
        }
    )
    if status not in {"BUY", "SELL", "HOLD"}:
        return AdvisoryRecommendation(
            status="ERROR",
            error=f"invalid recommendation {status}",
            raw_text=text,
            debug=parse_debug,
        )

    confidence = _clamp_float(obj.get("confidence", action_plan.get("confidence", 0.0)), 0.0, 1.0)
    normalised_plan = _normalise_action_plan(action_plan, status)
    levels = obj.get("levels") if isinstance(obj.get("levels"), dict) else {}
    risk_notes = obj.get("risk_notes") if isinstance(obj.get("risk_notes"), list) else []

    return AdvisoryRecommendation(
        status=status,  # type: ignore[arg-type]
        confidence=confidence,
        market_classification=obj.get("market_classification") if isinstance(obj.get("market_classification"), dict) else {},
        latest_dynamics=obj.get("latest_dynamics") if isinstance(obj.get("latest_dynamics"), dict) else {},
        action_plan=normalised_plan,
        levels=levels,
        risk_notes=[str(x) for x in risk_notes],
        raw_text="NA",
    )


def parse_strategy_output(text: str) -> Decision:
    clean = text.strip().replace("```json", "").replace("```", "").strip()
    obj = _extract_json(clean)
    if obj is not None:
        status = str(obj.get("decision") or obj.get("status") or "ERROR").upper()
        if status not in {"BUY", "SELL", "HOLD"}:
            return Decision(status="ERROR", error=f"invalid decision {status}", raw_text=text)
        order_kind = str(obj.get("order_kind") or "market").lower()
        if order_kind not in {"market", "limit", "stop", "stop_limit"}:
            order_kind = "market"
        return Decision(
            status=status,  # type: ignore[arg-type]
            allocation=_clamp_float(obj.get("allocation", 0.0), -1.0, 1.0),
            confidence=_clamp_float(obj.get("confidence", abs(float(obj.get("allocation", 0.0) or 0.0))), 0.0, 1.0),
            stop_loss=_float_or_none(obj.get("stop_loss")),
            take_profit=_float_or_none(obj.get("take_profit")),
            entry_price=_float_or_none(obj.get("entry_price")),
            order_kind=order_kind,  # type: ignore[arg-type]
            rationale=str(obj.get("rationale") or obj.get("analysis") or ""),
            levels=obj.get("levels") if isinstance(obj.get("levels"), dict) else {},
            evidence=_normalise_evidence(obj),
            raw_text="NA",
        )

    lines = [x.strip() for x in clean.splitlines() if x.strip()]
    if not lines:
        return Decision(status="ERROR", error="empty LLM response", raw_text="")
    tokens = lines[0].split()
    command = tokens[0].upper()
    if command == "HOLD":
        return Decision(status="HOLD", rationale=" ".join(lines[1:]), raw_text="")
    if command not in {"BUY", "SELL"}:
        return Decision(status="ERROR", error=f"invalid command {command}", raw_text="")
    nums = []
    for token in tokens[1:]:
        try:
            nums.append(float(token.replace(",", "").replace("$", "")))
        except ValueError:
            pass
    if len(nums) < 3:
        return Decision(status="ERROR", error="expected allocation, stop_loss, take_profit", raw_text="")
    allocation = abs(nums[0]) if command == "BUY" else -abs(nums[0])
    return Decision(
        status=command,  # type: ignore[arg-type]
        allocation=_clamp_float(allocation, -1.0, 1.0),
        confidence=min(abs(allocation), 1.0),
        stop_loss=nums[1],
        take_profit=nums[2],
        order_kind="market",
        rationale=" ".join(lines[1:]),
        raw_text="",
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


def _extract_json_with_debug(text: str, *, original_text: str) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    debug: dict[str, Any] = {
        "stage": "extract_json",
        "raw_chars": len(original_text or ""),
        "clean_chars": len(text or ""),
        "raw_preview": _text_preview(original_text),
        "clean_preview": _text_preview(text),
    }
    if not text:
        debug["json_found"] = False
        debug["json_error"] = "empty response"
        return None, debug

    try:
        parsed = json.loads(text)
        debug["json_found"] = isinstance(parsed, dict)
        debug["json_mode"] = "direct"
        if not isinstance(parsed, dict):
            debug["json_error"] = f"top-level JSON is {type(parsed).__name__}, expected object"
            return None, debug
        return parsed, debug
    except Exception as exc:
        debug["direct_json_error"] = f"{exc.__class__.__name__}: {exc}"

    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        debug["json_found"] = False
        debug["json_error"] = "no JSON object delimiters found in response"
        return None, debug

    candidate = match.group(0)
    debug.update(
        {
            "json_found": True,
            "json_mode": "regex_object",
            "json_start": match.start(),
            "json_end": match.end(),
            "json_candidate_chars": len(candidate),
            "json_candidate_preview": _text_preview(candidate),
        }
    )
    try:
        parsed = json.loads(candidate)
        if not isinstance(parsed, dict):
            debug["json_error"] = f"extracted JSON is {type(parsed).__name__}, expected object"
            return None, debug
        return parsed, debug
    except Exception as exc:
        debug["json_error"] = f"{exc.__class__.__name__}: {exc}"
        return None, debug


def _exception_debug(
    *,
    stage: str,
    exc: Exception,
    raw: str,
    metrics: LLMMetrics,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    debug: dict[str, Any] = {
        "stage": stage,
        "error_type": exc.__class__.__name__,
        "error": str(exc),
        "traceback_tail": "".join(traceback.format_exception(exc)[-5:])[-4000:],
        "raw_chars": len(raw or ""),
        "raw_preview": _text_preview(raw),
        "llm_metrics": metrics.to_dict(),
    }
    if extra:
        debug.update(extra)
    return debug


def _advisory_error_details(recommendation: AdvisoryRecommendation, *, metrics: LLMMetrics, raw: str) -> dict[str, Any]:
    return {
        "status": recommendation.status,
        "error": recommendation.error,
        "debug": recommendation.debug,
        "raw_chars": len(raw or recommendation.raw_text or ""),
        "raw_preview": _text_preview(raw or recommendation.raw_text),
        "llm_metrics": metrics.to_dict(),
        "artifact": recommendation.artifact,
    }


def _text_preview(value: Any, limit: int | None = None) -> str:
    text = "" if value is None else str(value)
    max_chars = int(limit if limit is not None else config.advisory_error_preview_chars)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"...<truncated {len(text) - max_chars} chars>"


def _json_preview(value: Any, limit: int | None = None) -> str:
    max_chars = int(limit if limit is not None else config.advisory_error_preview_chars)
    text = json.dumps(value, default=str, ensure_ascii=False, sort_keys=True)
    # Keep journal lines readable; the full nested payload is still written to SQLite/GCS.
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"...<truncated {len(text) - max_chars} chars>"


def _normalise_action_plan(action_plan: dict[str, Any], status: str) -> dict[str, Any]:
    plan = dict(action_plan)
    plan["recommendation"] = status
    raw_legs = plan.get("order_plan") if isinstance(plan.get("order_plan"), list) else []
    legs: list[dict[str, Any]] = []
    for i, raw_leg in enumerate(raw_legs, start=1):
        if not isinstance(raw_leg, dict):
            continue
        leg = dict(raw_leg)
        if "leg_id" not in leg:
            leg["leg_id"] = f"L{i}"
        if "allocation_pct" in leg:
            leg["allocation_pct"] = _clamp_float(leg["allocation_pct"], 0.0, 100.0)
        elif "allocation_fraction" in leg:
            leg["allocation_pct"] = _clamp_float(float(leg.get("allocation_fraction") or 0.0) * 100.0, 0.0, 100.0)
        else:
            leg["allocation_pct"] = 0.0
        if "side" in leg:
            leg["side"] = str(leg["side"]).lower()
        legs.append(leg)
    plan["order_plan"] = legs
    computed_total = round(sum(float(x.get("allocation_pct", 0.0) or 0.0) for x in legs), 4)
    declared_total = _float_or_none(plan.get("total_allocation_pct"))
    plan["total_allocation_pct"] = _clamp_float(declared_total if declared_total is not None else computed_total, 0.0, 100.0)
    return plan


def _normalise_evidence(obj: dict[str, Any]) -> dict[str, Any]:
    evidence = obj.get("evidence") if isinstance(obj.get("evidence"), dict) else {}
    out = dict(evidence)
    for key in (
        "setup",
        "confirmed_rebounce",
        "too_late_to_chase",
        "rebound_level",
        "target_level",
        "drawdown_risk",
        "why_not_late",
    ):
        if key in obj and key not in out:
            out[key] = obj[key]
    return out


def _market_summary(df: pd.DataFrame) -> str:
    tail = df.tail(8)
    rows = []
    for _, r in tail.iterrows():
        rows.append(
            f"{r.get('time_iso', '')}: O={r.open:.5g} H={r.high:.5g} L={r.low:.5g} C={r.close:.5g} V={r.volume:.0f}"
        )
    return "\n".join(rows)


def _analysis_summary(df: pd.DataFrame, *, label: str) -> str:
    if df.empty:
        return f"{label}: no rows"
    close = pd.to_numeric(df["close"], errors="coerce")
    high = pd.to_numeric(df["high"], errors="coerce")
    low = pd.to_numeric(df["low"], errors="coerce")
    volume = pd.to_numeric(df["volume"], errors="coerce")
    first_close = float(close.iloc[0])
    last_close = float(close.iloc[-1])
    net_change = last_close - first_close
    net_change_pct = (net_change / first_close * 100.0) if first_close else 0.0
    range_low = float(low.min())
    range_high = float(high.max())
    range_width = max(range_high - range_low, 1e-12)
    range_position = (last_close - range_low) / range_width
    max_idx = int(high.idxmax())
    min_idx = int(low.idxmin())
    tail = _market_summary(df.tail(8))
    return "\n".join(
        [
            f"{label}_rows={len(df)}",
            f"{label}_start={df.iloc[0].get('time_iso', df.iloc[0].get('datetime', ''))}",
            f"{label}_end={df.iloc[-1].get('time_iso', df.iloc[-1].get('datetime', ''))}",
            f"{label}_first_close={first_close:.5g} {label}_last_close={last_close:.5g}",
            f"{label}_net_change={net_change:.5g} ({net_change_pct:.3f}%)",
            f"{label}_low={range_low:.5g} {label}_high={range_high:.5g} {label}_range_position={range_position:.3f}",
            f"{label}_max_at={df.loc[max_idx].get('time_iso', '')} price={float(high.loc[max_idx]):.5g}",
            f"{label}_min_at={df.loc[min_idx].get('time_iso', '')} price={float(low.loc[min_idx]):.5g}",
            f"{label}_avg_volume={float(volume.mean()):.1f}",
            f"{label}_last_8_candles:\n{tail}",
        ]
    )


def _context_summary(context: pd.DataFrame, trigger: pd.DataFrame) -> str:
    if context.empty:
        return "No wider context rows available."
    return "\n".join(
        [
            f"context_rows={len(context)} trigger_rows={len(trigger)}",
            f"context_start={context.iloc[0].get('time_iso', context.iloc[0].get('datetime', ''))}",
            f"context_end={context.iloc[-1].get('time_iso', context.iloc[-1].get('datetime', ''))}",
            f"context_low={float(context['low'].min()):.5g} context_high={float(context['high'].max()):.5g}",
            f"trigger_low={float(trigger['low'].min()):.5g} trigger_high={float(trigger['high'].max()):.5g}",
        ]
    )


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


def _slice_to_uid(df: pd.DataFrame, uid: int, window_size: int) -> pd.DataFrame:
    if df is None or df.empty or "uid" not in df.columns:
        return pd.DataFrame()
    work = df.sort_values("uid").copy()
    matches = work.index[work["uid"].astype(int) == int(uid)].tolist()
    if not matches:
        return pd.DataFrame()
    target_idx = matches[-1]
    pos = work.index.get_loc(target_idx)
    if isinstance(pos, slice):
        pos = pos.stop - 1
    elif hasattr(pos, "__len__") and not isinstance(pos, int):
        pos = int(pos[-1])
    start = max(0, int(pos) - window_size + 1)
    return work.iloc[start:int(pos) + 1].reset_index(drop=True)


def _candle_to_dict(r: pd.Series) -> dict[str, Any]:
    return {
        "time_iso": str(r.get("time_iso", r.get("datetime", ""))),
        "open": float(r.get("open", 0.0)),
        "high": float(r.get("high", 0.0)),
        "low": float(r.get("low", 0.0)),
        "close": float(r.get("close", 0.0)),
        "volume": float(r.get("volume", 0.0)),
        "uid": int(r.get("uid", 0)),
    }
