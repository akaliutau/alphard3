from __future__ import annotations

import argparse
from pathlib import Path

from core.ledger import Ledger
from utilities.settings import config


def main() -> int:
    parser = argparse.ArgumentParser(description="Show recent Alphard LiteLLM metrics stored in SQLite")
    parser.add_argument("--db", default=str(config.sqlite_path))
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()

    ledger = Ledger(Path(args.db))
    for item in ledger.recent_llm_metrics(args.limit):
        payload = item["payload"]
        metrics = payload.get("metrics", {})
        model = (payload.get("model") or {}).get("model")
        response = metrics.get("response_summary", {}) or {}
        request = metrics.get("request_summary", {}) or {}
        parse = payload.get("parse", {}) or {}
        print(
            f"{item['ts']} {item['symbol']} uid={item['uid']} strategy={item['strategy']} "
            f"model={model} vertex_project={metrics.get('vertex_project')} vertex_location={metrics.get('vertex_location')} "
            f"latency_ms={metrics.get('latency_ms')} prompt={metrics.get('prompt_tokens')} "
            f"completion={metrics.get('completion_tokens')} total={metrics.get('total_tokens')} "
            f"attempts={metrics.get('attempts')} retries={metrics.get('retries')} "
            f"finish={response.get('finish_reason')} chars={response.get('content_chars')} "
            f"prompt_chars={request.get('text_chars')} images={request.get('image_count')} "
            f"decision={parse.get('status')} conf={parse.get('confidence')} error={metrics.get('error')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


