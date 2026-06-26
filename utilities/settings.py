from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent

# ENV_FILE lets you switch without editing code:
#   ENV_FILE=.env.local python app.py
#   ENV_FILE=.env.cloud python app.py
def _load_dotenv_if_readable(path: Path) -> None:
    if path.exists() and path.is_file() and os.access(path, os.R_OK):
        load_dotenv(path, override=False)

env_file = os.getenv("ENV_FILE")

# Local/dev mode:
#   ENV_FILE=.env.local python app.py
#
# Cloud/container mode:
#   Docker injects env vars with --env-file.
#   Set ENV_FILE=/dev/null or unset it, so Python does not try to read the secret file.
if env_file and env_file != "/dev/null":
    env_path = Path(env_file)
    if not env_path.is_absolute():
        env_path = ROOT_DIR / env_path
    _load_dotenv_if_readable(env_path)

_load_dotenv_if_readable(ROOT_DIR / ".env")

# LiteLLM reads this process env var when it is imported lazily by middleware/llm_middleware.py.
# Keep it quiet by default; turn on with LITELLM_LOG=DEBUG or LITELLM_DEBUG=true in .env.local.
os.environ.setdefault("LITELLM_LOG", os.getenv("LITELLM_LOG_LEVEL", "ERROR"))

TEMPLATES_DIR = ROOT_DIR / "templates"
DATA_DIR = ROOT_DIR / os.getenv("DATA_DIR", "data")
IMAGE_CACHE_DIR = ROOT_DIR / os.getenv("IMAGE_CACHE_DIR", "img_cache")
DATA_DIR.mkdir(parents=True, exist_ok=True)
IMAGE_CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw in (None, "") else int(raw)


def _float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw in (None, "") else float(raw)


def _list(name: str, default: str) -> list[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass(frozen=True)
class AppConfig:
    app_name: str = os.getenv("APP_NAME", "alphard")
    env: Literal["local", "cloud"] = os.getenv("APP_ENV", "local")  # type: ignore[assignment]
    dry_run: bool = _bool("DRY_RUN", True)
    # Advisory-only is the production default for this refactor. The app may still
    # query MT5 market/account state, but it never places or cancels orders.
    advisory_only: bool = _bool("ADVISORY_ONLY", True)
    run_interval_minutes: int = _int("RUN_INTERVAL_MINUTES", 15)
    candle_close_delay_seconds: int = _int("CANDLE_CLOSE_DELAY_SECONDS", 30)
    symbols: tuple[str, ...] = tuple(_list("SYMBOLS", "EURUSD"))
    timeframe: str = os.getenv("MT5_TIMEFRAME", "M15")
    candle_warmup_bars: int = _int("CANDLE_WARMUP_BARS", 1800)
    # Detailed chart: the latest execution dynamics. Default is 180 M1 candles (~3h).
    detailed_analysis_bars: int = _int("DETAILED_ANALYSIS_BARS", 180)
    # Global chart: approx. 26 hours of M1 candles. 1560 = 26 * 60.
    global_analysis_bars: int = _int("GLOBAL_ANALYSIS_BARS", 1560)
    # Legacy names kept for compatibility with tests/scripts and old utilities.
    chart_window_bars: int = _int("CHART_WINDOW_BARS", _int("DETAILED_ANALYSIS_BARS", 180))
    chart_context_window_bars: int = _int("CHART_CONTEXT_WINDOW_BARS", _int("GLOBAL_ANALYSIS_BARS", 1560))
    global_chart_aspect: str = os.getenv("GLOBAL_CHART_ASPECT", "16:9")
    detailed_chart_aspect: str = os.getenv("DETAILED_CHART_ASPECT", "1:1")
    sqlite_path: Path = ROOT_DIR / os.getenv("SQLITE_PATH", "data/alphard.sqlite3")

    mt5_base_url: str = os.getenv("MT5_BASE_URL", os.getenv("BASE_URL", "http://127.0.0.1:8000"))
    mt5_api_key: str = os.getenv("MT5_API_KEY", os.getenv("API_KEY", "dev-api-key"))
    mt5_timeout_seconds: float = _float("MT5_TIMEOUT_SECONDS", 30.0)
    mt5_magic: int = _int("MT5_MAGIC", 424242)
    mt5_type_filling: str = os.getenv("MT5_TYPE_FILLING", "AUTO")
    mt5_deviation: int = _int("MT5_DEVIATION", 30)

    strategy_name: str = os.getenv("STRATEGY_NAME", "levels_strategy")
    advisory_strategy_name: str = os.getenv("ADVISORY_STRATEGY_NAME", "advisory_two_scale_strategy")
    model_name: str = os.getenv("MODEL_NAME", "gemini-2.5-flash")
    model_id: str = os.getenv("MODEL_ID", "vertex_ai/gemini-2.5-flash")
    vertex_location: str = os.getenv("VERTEX_LOCATION", "global")
    vertex_project: str | None = os.getenv("VERTEX_PROJECT") or os.getenv("GOOGLE_CLOUD_PROJECT")
    litellm_timeout_seconds: int = _int("LITELLM_TIMEOUT_SECONDS", 120)
    # Advisory JSON is larger than the legacy trade command. 4096 tokens can cut
    # the response mid-object, which makes the recommendation parse as ERROR.
    litellm_max_tokens: int = _int("LITELLM_MAX_TOKENS", 8192)
    litellm_debug: bool = _bool("LITELLM_DEBUG", False)
    litellm_log_prompt: bool = _bool("LITELLM_LOG_PROMPT", False)
    litellm_log_response: bool = _bool("LITELLM_LOG_RESPONSE", False)
    litellm_response_preview_chars: int = _int("LITELLM_RESPONSE_PREVIEW_CHARS", 600)
    # Always include this much raw model/output context in ERROR logs and diagnostics.
    # This is separate from LITELLM_LOG_RESPONSE, so parse failures are visible even
    # when normal response logging remains disabled.
    advisory_error_preview_chars: int = _int("ADVISORY_ERROR_PREVIEW_CHARS", 4000)
    litellm_drop_params: bool = _bool("LITELLM_DROP_PARAMS", True)
    litellm_suppress_pydantic_warnings: bool = _bool("LITELLM_SUPPRESS_PYDANTIC_WARNINGS", True)

    image_provider: Literal["local", "gcs"] = os.getenv("IMAGE_PROVIDER", "local")  # type: ignore[assignment]
    gcs_bucket_name: str | None = os.getenv("GCS_BUCKET_NAME") or os.getenv("BUCKET_NAME")
    gcs_public_read: bool = _bool("GCS_PUBLIC_READ", False)
    advisory_latest_pointer_blob: str = os.getenv("ADVISORY_LATEST_POINTER_BLOB", "advice/latest.json")
    advisory_slot_manifest_name: str = os.getenv("ADVISORY_SLOT_MANIFEST_NAME", "manifest.json")

    execution_mode: Literal["pending_limit", "market"] = os.getenv("EXECUTION_MODE", "pending_limit")  # type: ignore[assignment]
    base_volume: float = _float("BASE_VOLUME", 0.01)
    max_volume: float = _float("MAX_VOLUME", 1.00)
    min_confidence: float = _float("MIN_CONFIDENCE", 0.60)
    max_allocation: float = _float("MAX_ALLOCATION", 1.00)
    max_active_positions: int = _int("MAX_ACTIVE_POSITIONS", 1)
    # Pending entry is intentionally placed very close to the current quote so M1 noise can fill it.
    # Broker stops_level can still force a wider gap.
    entry_noise_points: int = max(0, min(_int("ENTRY_NOISE_POINTS", 5), 10))
    entry_pullback_points: int = _int("ENTRY_PULLBACK_POINTS", 30)  # legacy; no longer used for pending entry placement
    pullback_ratio: float = _float("PULLBACK_RATIO", 0.60)
    split_second_entry_multiplier: int = _int("SPLIT_SECOND_ENTRY_MULTIPLIER", 8)
    split_order_ratio: float = _float("SPLIT_ORDER_RATIO", 0.40)
    split_partial_tp_ratio: float = _float("SPLIT_PARTIAL_TP_RATIO", 0.60)
    min_stop_distance_points: int = _int("MIN_STOP_DISTANCE_POINTS", 20)
    min_reward_risk_ratio: float = _float("MIN_REWARD_RISK_RATIO", 1.05)
    # VLM decisions are actionable only after the move has visibly rebounded/
    # reflected already. The deterministic risk layer also rejects model-declared
    # chase setups once too much of the path from rebound to TP is consumed.
    require_evidence_confirmation: bool = _bool("REQUIRE_EVIDENCE_CONFIRMATION", True)
    max_chase_progress: float = _float("MAX_CHASE_PROGRESS", 0.70)
    split_order_enabled: bool = _bool("SPLIT_ORDER_ENABLED", True)
    symbol_base_volume: dict[str, float] = field(default_factory=lambda:
    {"EURUSD":0.1,"USDJPY":0.1,"USDCAD":0.1,"USDCHF":0.1, "EURCHF":0.1,"XAUUSD":1.0,"EURGBP":0.1,"GBPUSD":0.05})
    cancel_stale_pending_orders: bool = _bool("CANCEL_STALE_PENDING_ORDERS", False)


config = AppConfig()


def _setup_logger() -> logging.Logger:
    logger = logging.getLogger("alphard")
    if logger.handlers:
        return logger
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(level)
    return logger


logger = _setup_logger()


