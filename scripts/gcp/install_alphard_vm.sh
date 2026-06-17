#!/usr/bin/env bash
set -Eeuo pipefail

: "${ALPHARD_IMAGE:?ALPHARD_IMAGE is required}"
: "${ALPHARD_ENV_FILE:=/etc/alphard/.env.cloud}"
: "${ALPHARD_STATE_DIR:=/var/lib/alphard}"
: "${ALPHARD_CONTAINER_NAME:=alphard-run}"
: "${ALPHARD_DOCKER_MEMORY:=1536m}"
: "${ALPHARD_DOCKER_CPUS:=1.0}"

mkdir -p \
  "${ALPHARD_STATE_DIR}/data" \
  "${ALPHARD_STATE_DIR}/img_cache"

exec /usr/bin/docker run --rm \
  --name "${ALPHARD_CONTAINER_NAME}" \
  --network host \
  --memory "${ALPHARD_DOCKER_MEMORY}" \
  --cpus "${ALPHARD_DOCKER_CPUS}" \
  --stop-timeout 30 \
  --env-file "${ALPHARD_ENV_FILE}" \
  -e ENV_FILE=.env.cloud \
  -v "${ALPHARD_ENV_FILE}:/app/.env.cloud:ro" \
  -v "${ALPHARD_STATE_DIR}/data:/app/data" \
  -v "${ALPHARD_STATE_DIR}/img_cache:/app/img_cache" \
  "${ALPHARD_IMAGE}" \
  python app.py --once