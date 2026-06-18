#!/usr/bin/env bash
set -Eeuo pipefail

RUNNER_ENV="${RUNNER_ENV:-/etc/alphard/runner.env}"
source "$RUNNER_ENV"

: "${ALPHARD_IMAGE:?Set ALPHARD_IMAGE}"
: "${ALPHARD_ENV_FILE:=/etc/alphard/.env.cloud}"
: "${ALPHARD_STATE_DIR:=/var/lib/alphard}"
: "${ALPHARD_CONTAINER_NAME:=alphard-run}"
: "${ALPHARD_DOCKER_MEMORY:=1536m}"
: "${ALPHARD_DOCKER_CPUS:=1.0}"
: "${ALPHARD_DIAGNOSTICS_ONLY:=false}"

mkdir -p "$ALPHARD_STATE_DIR/data" "$ALPHARD_STATE_DIR/img_cache"
chmod 0777 "$ALPHARD_STATE_DIR/data" "$ALPHARD_STATE_DIR/img_cache"

docker rm -f "$ALPHARD_CONTAINER_NAME" >/dev/null 2>&1 || true
docker pull "$ALPHARD_IMAGE"

if [[ "$ALPHARD_DIAGNOSTICS_ONLY" == "true" ]]; then
  echo "Running Alphard diagnostics only; app.py will NOT start; network disabled."

  exec docker run --rm \
    --name "$ALPHARD_CONTAINER_NAME" \
    --network none \
    --memory "$ALPHARD_DOCKER_MEMORY" \
    --cpus "$ALPHARD_DOCKER_CPUS" \
    --entrypoint /bin/sh \
    -v "$ALPHARD_STATE_DIR/data:/app/data" \
    -v "$ALPHARD_STATE_DIR/img_cache:/app/img_cache" \
    "$ALPHARD_IMAGE" \
    -lc '
      echo "=== diagnostics ==="
      date -u
      echo "=== memory ==="
      free -h || cat /proc/meminfo
      echo "=== disk ==="
      df -h /app /app/data /app/img_cache || true
      echo "=== safe env ==="
      env | sort | sed -E "s/(KEY|TOKEN|SECRET|PASSWORD|CREDENTIALS)=.*/\1=REDACTED/g"
      echo "=== imports ==="
      python - <<PY
import sqlite3, pandas, numpy, matplotlib, httpx
print("imports ok")
PY
      echo "diagnostics complete"
    '
  exit 0
fi

exec docker run --rm \
  --name "$ALPHARD_CONTAINER_NAME" \
  --network host \
  --env-file "$ALPHARD_ENV_FILE" \
  -e ENV_FILE=/dev/null \
  -e MPLCONFIGDIR=/tmp/matplotlib \
  --memory "$ALPHARD_DOCKER_MEMORY" \
  --cpus "$ALPHARD_DOCKER_CPUS" \
  --stop-timeout 30 \
  -v "$ALPHARD_STATE_DIR/data:/app/data" \
  -v "$ALPHARD_STATE_DIR/img_cache:/app/img_cache" \
  "$ALPHARD_IMAGE" \
  --once