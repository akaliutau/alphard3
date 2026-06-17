from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, asdict
from time import perf_counter_ns
from typing import Any, Callable, Coroutine, Optional, Protocol, TypeVar

T_Coerced = TypeVar("T_Coerced")
os.environ.setdefault("LITELLM_LOG", "ERROR")


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
    metrics = metrics or LLMMetrics()
    params = _build_litellm_params(config, metadata or {})
    last_err: Exception | None = None

    for attempt in range(1, config.max_retries + 1):
        metrics.attempts += 1
        try:
            start = perf_counter_ns()
            from litellm import acompletion  # imported lazily so parser/risk tests do not require LiteLLM
            resp = await acompletion(model=config.model, messages=messages, stream=False, **params)
            metrics.latency_ms += (perf_counter_ns() - start) // 1_000_000
            _capture_usage(resp, metrics)
            content = _extract_content(resp)
            return transformer(content)
        except Exception as exc:
            last_err = exc
            metrics.error = str(exc)
            if attempt >= config.max_retries:
                break
            metrics.retries += 1
            await asyncio.sleep(min(2 ** attempt, 20))

    raise RuntimeError(f"LLM call failed after {config.max_retries} attempts: {last_err}")


def _build_litellm_params(config: ModelConfig, metadata: dict[str, Any]) -> dict[str, Any]:
    params: dict[str, Any] = {
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
    }
    if metadata.get("timeout"):
        params["timeout"] = metadata["timeout"]
    if metadata.get("vertex_location"):
        params["vertex_location"] = metadata["vertex_location"]
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
