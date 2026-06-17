from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import traceback
import warnings
from contextlib import contextmanager
from dataclasses import dataclass, asdict, field
from time import perf_counter_ns
from typing import Any, Callable, Coroutine, Optional, Protocol, TypeVar

T_Coerced = TypeVar("T_Coerced")
logger = logging.getLogger("alphard.llm")

warnings.filterwarnings(
    "ignore",
    message=r"Pydantic serializer warnings:.*",
    category=UserWarning,
)
warnings.filterwarnings(
    "ignore",
    message=r".*PydanticSerializationUnexpectedValue.*",
    category=UserWarning,
)

@dataclass(frozen=True)
class ModelConfig:
    name: str
    model: str
    temperature: float = 0.2
    max_retries: int = 3
    max_tokens: int = 4096

    def get_provider(self) -> str:
        if "/" not in self.model:
            raise ValueError("model must be provider-qualified, e.g. vertex_ai/gemini-2.5-flash")
        return self.model.split("/", 1)[0].lower()


@dataclass
class LLMMetrics:
    latency_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    retries: int = 0
    attempts: int = 0
    error: str | None = None
    error_type: str | None = None
    traceback_tail: str | None = None

    # Debug/observability fields. Keep these primitive so SQLite JSON logging never
    # serializes the LiteLLM/Pydantic response object itself.
    provider: str | None = None
    model: str | None = None
    vertex_project: str | None = None
    vertex_location: str | None = None
    timeout_seconds: float | None = None
    request_summary: dict[str, Any] = field(default_factory=dict)
    response_summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def coerce_to_json(content: Any) -> dict[str, Any]:
    text = str(content).strip()
    text = text.replace("```json", "").replace("```", "").strip()
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        text = match.group(0)
    return json.loads(text)


def coerce_to_simple_string(content: Any) -> str:
    return str(content).strip()


class GenericLLMCallable(Protocol):
    def __call__(
        self,
        messages: list[Any],
        config: ModelConfig,
        transformer: Callable[[Any], T_Coerced],
        metadata: dict[str, Any] | None,
        metrics: Optional[LLMMetrics] = None,
    ) -> Coroutine[Any, Any, T_Coerced]:
        ...


async def call_llm(
    messages: list[Any],
    config: ModelConfig,
    transformer: Callable[[Any], T_Coerced],
    metadata: dict[str, Any] | None = None,
    metrics: Optional[LLMMetrics] = None,
) -> T_Coerced:
    """Call LiteLLM and return transformed content.

    Important reliability rule: do not serialize/store the full LiteLLM response.
    Some LiteLLM versions return Pydantic objects that emit serializer warnings when
    model_dump()/dict serialization touches nested choices/messages. We only extract
    primitive fields needed for metrics and debugging.
    """
    metadata = metadata or {}
    metrics = metrics or LLMMetrics()
    params = _build_litellm_params(config, metadata)
    debug = bool(metadata.get("debug"))
    suppress_pydantic_warnings = bool(metadata.get("suppress_pydantic_warnings", True))
    preview_chars = int(metadata.get("response_preview_chars") or 600)

    metrics.provider = config.get_provider()
    metrics.model = config.model
    metrics.vertex_project = params.get("vertex_project")
    metrics.vertex_location = params.get("vertex_location")
    metrics.timeout_seconds = params.get("timeout")
    metrics.request_summary = _summarize_messages(messages)

    if debug:
        logger.info(
            "LLM start provider=%s model=%s vertex_project=%s vertex_location=%s timeout=%s attempts=%s request=%s",
            metrics.provider,
            metrics.model,
            metrics.vertex_project,
            metrics.vertex_location,
            metrics.timeout_seconds,
            config.max_retries,
            json.dumps(metrics.request_summary, sort_keys=True),
        )
        if metadata.get("log_prompt"):
            logger.debug("LLM prompt text preview: %s", _prompt_text_preview(messages, 4000))

    last_err: Exception | None = None

    for attempt in range(1, config.max_retries + 1):
        metrics.attempts += 1
        try:
            start = perf_counter_ns()
            from litellm import acompletion  # imported lazily so parser/risk tests do not require LiteLLM

            with _litellm_warning_scope(suppress_pydantic_warnings):
                resp = await acompletion(model=config.model, messages=messages, stream=False, **params)

            metrics.latency_ms += (perf_counter_ns() - start) // 1_000_000
            _capture_usage(resp, metrics)
            content = _extract_content(resp)
            metrics.response_summary = _summarize_response(resp, content, preview_chars)

            if debug:
                logger.info(
                    "LLM ok attempt=%s latency_ms=%s usage=%s finish_reason=%s response_chars=%s",
                    attempt,
                    metrics.latency_ms,
                    {
                        "prompt_tokens": metrics.prompt_tokens,
                        "completion_tokens": metrics.completion_tokens,
                        "total_tokens": metrics.total_tokens,
                    },
                    metrics.response_summary.get("finish_reason"),
                    metrics.response_summary.get("content_chars"),
                )
                if metadata.get("log_response"):
                    logger.debug("LLM response preview: %s", metrics.response_summary.get("content_preview"))

            return transformer(content)
        except Exception as exc:
            last_err = exc
            metrics.error = str(exc)
            metrics.error_type = exc.__class__.__name__
            metrics.traceback_tail = "".join(traceback.format_exception(exc)[-4:])[-3000:]
            logger.warning(
                "LLM attempt failed attempt=%s/%s error_type=%s error=%s",
                attempt,
                config.max_retries,
                metrics.error_type,
                metrics.error,
            )
            if debug:
                logger.debug("LLM traceback tail:\n%s", metrics.traceback_tail)
            if attempt >= config.max_retries:
                break
            metrics.retries += 1
            await asyncio.sleep(min(2 ** attempt, 20))

    raise RuntimeError(f"LLM call failed after {config.max_retries} attempts: {last_err}")


def _build_litellm_params(config: ModelConfig, metadata: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        # Safer across providers/models: LiteLLM drops unsupported OpenAI-style
        # params instead of failing the call.
        "drop_params": bool(metadata.get("drop_params", True)),
    }
    if metadata.get("timeout"):
        params["timeout"] = metadata["timeout"]
    if metadata.get("vertex_location"):
        params["vertex_location"] = metadata["vertex_location"]
    if metadata.get("vertex_project"):
        params["vertex_project"] = metadata["vertex_project"]
    if isinstance(metadata.get("litellm_params"), dict):
        params.update(metadata["litellm_params"])
    return params


def _extract_content(resp: Any) -> Any:
    try:
        return resp.choices[0].message.content
    except Exception:
        return resp["choices"][0]["message"]["content"]


def _capture_usage(resp: Any, metrics: LLMMetrics) -> None:
    usage = getattr(resp, "usage", None) or (resp.get("usage") if isinstance(resp, dict) else None)
    if not usage:
        return
    get = usage.get if isinstance(usage, dict) else lambda k, default=0: getattr(usage, k, default)
    metrics.prompt_tokens += int(get("prompt_tokens", 0) or 0)
    metrics.completion_tokens += int(get("completion_tokens", 0) or 0)
    metrics.total_tokens += int(get("total_tokens", metrics.prompt_tokens + metrics.completion_tokens) or 0)


def _summarize_messages(messages: list[Any]) -> dict[str, Any]:
    text_chars = 0
    image_count = 0
    image_refs: list[dict[str, Any]] = []
    role_counts: dict[str, int] = {}

    for msg in messages:
        role = _get(msg, "role", "unknown")
        role_counts[role] = role_counts.get(role, 0) + 1
        content = _get(msg, "content", "")
        parts = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
        for part in parts:
            part_type = _get(part, "type", "")
            if part_type == "text":
                text_chars += len(str(_get(part, "text", "")))
            elif part_type == "image_url":
                image_count += 1
                image_url = _get(part, "image_url", {})
                url = _get(image_url, "url", "") if isinstance(image_url, dict) else ""
                image_refs.append(_summarize_image_url(url))

    return {
        "messages": len(messages),
        "roles": role_counts,
        "text_chars": text_chars,
        "text_sha256": _prompt_text_hash(messages),
        "image_count": image_count,
        "images": image_refs,
    }


def _summarize_image_url(url: str) -> dict[str, Any]:
    if url.startswith("data:"):
        header, _, payload = url.partition(",")
        return {
            "kind": "data_url",
            "mime": header[5:].split(";", 1)[0],
            "base64_chars": len(payload),
            "sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16],
        }
    if url.startswith("gs://"):
        return {"kind": "gcs_uri", "uri": url}
    if url.startswith("http"):
        return {"kind": "http_url", "url": url}
    return {"kind": "unknown", "chars": len(url)}


def _summarize_response(resp: Any, content: Any, preview_chars: int) -> dict[str, Any]:
    text = "" if content is None else str(content)
    return {
        "response_type": type(resp).__name__,
        "id": _get(resp, "id"),
        "model": _get(resp, "model"),
        "finish_reason": _extract_finish_reason(resp),
        "content_chars": len(text),
        "content_preview": text[:preview_chars],
    }


def _extract_finish_reason(resp: Any) -> str | None:
    try:
        return resp.choices[0].finish_reason
    except Exception:
        try:
            return resp["choices"][0].get("finish_reason")
        except Exception:
            return None


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _prompt_text_hash(messages: list[Any]) -> str:
    return hashlib.sha256(_prompt_text_preview(messages, None).encode("utf-8")).hexdigest()[:16]


def _prompt_text_preview(messages: list[Any], limit: int | None) -> str:
    chunks: list[str] = []
    for msg in messages:
        content = _get(msg, "content", "")
        parts = content if isinstance(content, list) else [{"type": "text", "text": str(content)}]
        for part in parts:
            if _get(part, "type", "") == "text":
                chunks.append(str(_get(part, "text", "")))
    text = "\n\n".join(chunks)
    return text if limit is None else text[:limit]


@contextmanager
def _litellm_warning_scope(suppress: bool):
    if not suppress:
        yield
        return
    with warnings.catch_warnings():
        # LiteLLM/Pydantic warning seen with successful Vertex Gemini calls:
        # "Pydantic serializer warnings: PydanticSerializationUnexpectedValue..."
        # It is noisy but not actionable for our app because we never serialize the
        # full ModelResponse. Keep it scoped to the actual LiteLLM call.
        warnings.filterwarnings(
            "ignore",
            message=r"Pydantic serializer warnings:.*",
            category=UserWarning,
        )
        warnings.filterwarnings(
            "ignore",
            message=r".*PydanticSerializationUnexpectedValue.*",
            category=UserWarning,
        )
        yield
