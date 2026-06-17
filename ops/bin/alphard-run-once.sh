#!/usr/bin/env bash
set -Eeuo pipefail

RUNNER_ENV="${RUNNER_ENV:-/etc/alphard/runner.env}"
if [[ ! -r "$RUNNER_ENV" ]]; then
  echo "Missing runner env: $RUNNER_ENV" >&2
  exit 2
fi
# shellcheck disable=SC1090
source "$RUNNER_ENV"

: "${ALPHARD_IMAGE:?Set ALPHARD_IMAGE in /etc/alphard/runner.env}"
: "${ALPHARD_ENV_FILE:=/etc/alphard/.env.cloud}"
: "${ALPHARD_STATE_DIR:=/var/lib/alphard}"
: "${ALPHARD_CONTAINER_NAME:=alphard-run}"
: "${ALPHARD_DOCKER_MEMORY:=1536m}"
: "${ALPHARD_DOCKER_CPUS:=1.0}"

mkdir -p "$ALPHARD_STATE_DIR/data" "$ALPHARD_STATE_DIR/img_cache"

exec 9>/run/alphard.lock
if ! flock -n 9; then
  echo "Another Alphard run is active; skipping this slot."
  exit 0
fi

if docker ps --format '{{.Names}}' | grep -qx "$ALPHARD_CONTAINER_NAME"; then
  echo "Container $ALPHARD_CONTAINER_NAME is still running; skipping this slot."
  exit 0
fi
# Remove a stale exited container with the same fixed name, if any.
docker rm "$ALPHARD_CONTAINER_NAME" >/dev/null 2>&1 || true

# Pull on every run so a deploy can update only runner.env/image tag and wait for the next slot.
docker pull "$ALPHARD_IMAGE"

exec docker run \
  --rm \
  --name "$ALPHARD_CONTAINER_NAME" \
  --network host \
  --env-file "$ALPHARD_ENV_FILE" \
  -e ENV_FILE=.env.cloud \
  -e MPLCONFIGDIR=/tmp/matplotlib \
  --memory "$ALPHARD_DOCKER_MEMORY" \
  --cpus "$ALPHARD_DOCKER_CPUS" \
  --stop-timeout 30 \
  --log-driver journald \
  -v "$ALPHARD_STATE_DIR/data:/app/data" \
  -v "$ALPHARD_STATE_DIR/img_cache:/app/img_cache" \
  "$ALPHARD_IMAGE" --once
