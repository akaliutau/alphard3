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
        print(
            f"{item['ts']} {item['symbol']} uid={item['uid']} strategy={item['strategy']} "
            f"model={model} latency_ms={metrics.get('latency_ms')} "
            f"prompt={metrics.get('prompt_tokens')} completion={metrics.get('completion_tokens')} "
            f"total={metrics.get('total_tokens')} retries={metrics.get('retries')} error={metrics.get('error')}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
