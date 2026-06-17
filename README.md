# Alphard MT5 refactor

The version of Alphard uses the MT5 Wine Proxy API from `api.md`.

## Runtime workflow

1. The daemon wakes every `RUN_INTERVAL_MINUTES` minutes, normally 15 or 30.
2. It computes the latest fully closed candle from the current UTC timestamp.
3. It checks SQLite for cached candles and requests only missing `/v1/bars` from the MT5 proxy.
4. It stores fresh OHLCV in `candles` and all operational events in `event_log`.
5. It renders one `imagegenv2.py` state chart per symbol.
6. It sends the chart to Gemini via LiteLLM, either as local base64 or a GCS URL/URI.
7. It stores raw LLM output, token usage, retry count, and latency.
8. It parses a strict JSON decision.
9. A deterministic risk engine validates confidence, SL/TP direction, stop distance, volume, and active position caps.
10. If approved, it optionally cancels old strategy pending orders and calls the MT5 proxy. `DRY_RUN=true` records the request without trading.
11. It sleeps until the next 15/30-minute boundary.

## Local run

```bash
cd alphard_mt5_refactor
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env.local

```bash
ENV_FILE=.env.local  python -m tests.smoke_mt5_api  --symbol EURUSD
ENV_FILE=.env.local python app.py --once
ENV_FILE=.env.local python app.py
```

## Cloud run on a stateful VM

```bash
source scripts/set_env.sh
scripts/deploy_infra.sh
```

The script creates a service account, grants Vertex AI and Storage permissions, creates a GCS bucket, and optionally creates a light Compute Engine VM with a persistent disk mounted for state.

Copy the project to `/opt/alphard/app`, copy `.env.cloud.example` to `/opt/alphard/app/.env.cloud`, then run:

```bash
cd /opt/alphard/app
ENV_FILE=.env.cloud python app.py
```

For production, set `DRY_RUN=false` and make sure the MT5 proxy has `TRADING_ENABLED=true` only after smoke tests pass.

## Metrics

```bash
ENV_FILE=.env.local python scripts/show_metrics.py --limit 20
```

## Key files

- `core/mt5_api.py` — async MT5 proxy client.
- `core/candle_cache.py` — SQLite OHLCV cache and incremental sync.
- `core/strategy.py` — chart generation, LiteLLM call, output parsing.
- `core/risk.py` — deterministic sanity/risk chain.
- `core/execution.py` — dry run, pending limit, market execution, pending order cleanup.
- `core/ledger.py` — candle and event ledger.
- `scripts/deploy_infra.sh` — GCP infra for Vertex, GCS, and stateful VM.
