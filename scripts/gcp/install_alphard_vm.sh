#!/usr/bin/env bash
set -Eeuo pipefail

PAYLOAD_DIR="${PAYLOAD_DIR:-/tmp/alphard-ops}"

if [[ -f "${PAYLOAD_DIR}/install.env" ]]; then
  # shellcheck disable=SC1090
  source "${PAYLOAD_DIR}/install.env"
fi

TIMER_MODE="${TIMER_MODE:-m10}"
OVERWRITE_ENV="${OVERWRITE_ENV:-false}"
RUN_NOW="${RUN_NOW:-false}"

apt-get update
apt-get install -y ca-certificates curl gnupg docker.io util-linux

systemctl enable --now docker

install -d -m 0755 /opt/alphard/bin
install -d -m 0755 /etc/alphard
install -d -m 0755 /var/lib/alphard/data
install -d -m 0755 /var/lib/alphard/img_cache

install -m 0755 "${PAYLOAD_DIR}/ops/bin/alphard-run-once.sh" \
  /opt/alphard/bin/alphard-run-once.sh

ln -sf /opt/alphard/bin/alphard-run-once.sh \
  /usr/local/bin/alphard-run-once.sh

install -m 0644 "${PAYLOAD_DIR}/ops/systemd/alphard.service" \
  /etc/systemd/system/alphard.service

install -m 0644 "${PAYLOAD_DIR}/ops/systemd/alphard-m5.timer" \
  /etc/systemd/system/alphard-m5.timer

install -m 0644 "${PAYLOAD_DIR}/ops/systemd/alphard-m10.timer" \
  /etc/systemd/system/alphard-m10.timer

install -m 0644 "${PAYLOAD_DIR}/ops/systemd/alphard-m15.timer" \
  /etc/systemd/system/alphard-m15.timer

install -m 0644 "${PAYLOAD_DIR}/ops/systemd/alphard-m30.timer" \
  /etc/systemd/system/alphard-m30.timer

install -m 0644 "${PAYLOAD_DIR}/etc/alphard/runner.env" \
  /etc/alphard/runner.env

if [[ ! -f /etc/alphard/.env.cloud || "$OVERWRITE_ENV" == "true" ]]; then
  install -m 0600 "${PAYLOAD_DIR}/etc/alphard/.env.cloud" \
    /etc/alphard/.env.cloud
fi


# shellcheck disable=SC1091
source /etc/alphard/runner.env

: "${ALPHARD_ARTIFACT_REGISTRY_HOST:?ALPHARD_ARTIFACT_REGISTRY_HOST is required}"

if ! command -v gcloud >/dev/null 2>&1; then
  apt-get update
  apt-get install -y apt-transport-https ca-certificates gnupg curl

  curl -fsSL https://packages.cloud.google.com/apt/doc/apt-key.gpg \
    | gpg --dearmor \
    | tee /usr/share/keyrings/cloud.google.gpg >/dev/null

  echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" \
    | tee /etc/apt/sources.list.d/google-cloud-sdk.list >/dev/null

  apt-get update
  apt-get install -y google-cloud-cli
fi

gcloud auth configure-docker "${ALPHARD_ARTIFACT_REGISTRY_HOST}" --quiet

# Validate runner config after copying it.
grep -q '^ALPHARD_IMAGE=' /etc/alphard/runner.env
grep -q '^ALPHARD_CONTAINER_NAME=' /etc/alphard/runner.env

systemctl daemon-reload

systemctl disable --now alphard-m15.timer alphard-m30.timer alphard-m5.timer alphard-m10.timer >/dev/null 2>&1 || true

case "$TIMER_MODE" in
  m5)
    systemctl enable --now alphard-m5.timer
    ;;
  m10)
    systemctl enable --now alphard-m10.timer
    ;;
  m15)
    systemctl enable --now alphard-m15.timer
    ;;
  m30)
    systemctl enable --now alphard-m30.timer
    ;;
  *)
    echo "Unsupported TIMER_MODE=$TIMER_MODE; expected m15 or m30" >&2
    exit 2
    ;;
esac

test -x /opt/alphard/bin/alphard-run-once.sh
test -f /etc/systemd/system/alphard.service
test -f /etc/alphard/runner.env
test -f /etc/alphard/.env.cloud

if [[ "$RUN_NOW" == "true" ]]; then
  systemctl start alphard.service
fi
