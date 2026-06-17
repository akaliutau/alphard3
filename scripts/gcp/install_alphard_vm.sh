#!/usr/bin/env bash
set -Eeuo pipefail

PAYLOAD_DIR="${PAYLOAD_DIR:-/tmp/alphard-ops}"
INSTALL_ENV="$PAYLOAD_DIR/install.env"
if [[ -r "$INSTALL_ENV" ]]; then
  # shellcheck disable=SC1090
  source "$INSTALL_ENV"
fi

TIMER_MODE="${TIMER_MODE:-m15}"
OVERWRITE_ENV="${OVERWRITE_ENV:-false}"
RUN_NOW="${RUN_NOW:-false}"
STATE_DEVICE="${STATE_DEVICE:-/dev/disk/by-id/google-alphard-state}"
STATE_MOUNT="${STATE_MOUNT:-/var/lib/alphard}"
APP_UID="${APP_UID:-10001}"
APP_GID="${APP_GID:-0}"

if [[ "$EUID" -ne 0 ]]; then
  echo "Run as root, e.g. sudo $0" >&2
  exit 1
fi

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  docker.io \
  util-linux
systemctl enable --now docker

mkdir -p "$STATE_MOUNT"
if [[ -e "$STATE_DEVICE" ]]; then
  if ! blkid "$STATE_DEVICE" >/dev/null 2>&1; then
    mkfs.ext4 -F "$STATE_DEVICE"
  fi
  if ! grep -q "$STATE_DEVICE" /etc/fstab; then
    echo "$STATE_DEVICE $STATE_MOUNT ext4 defaults,nofail 0 2" >> /etc/fstab
  fi
  mount "$STATE_MOUNT" >/dev/null 2>&1 || mount -a
else
  echo "State disk $STATE_DEVICE not found; using $STATE_MOUNT on the boot disk." >&2
fi

mkdir -p "$STATE_MOUNT/data" "$STATE_MOUNT/img_cache" /etc/alphard
chown -R "$APP_UID:$APP_GID" "$STATE_MOUNT"
chmod -R g=u "$STATE_MOUNT"

install -m 0755 "$PAYLOAD_DIR/ops/bin/alphard-run-once.sh" /usr/local/bin/alphard-run-once.sh
install -m 0644 "$PAYLOAD_DIR/ops/systemd/alphard.service" /etc/systemd/system/alphard.service
case "$TIMER_MODE" in
  m15|15) install -m 0644 "$PAYLOAD_DIR/ops/systemd/alphard-m15.timer" /etc/systemd/system/alphard.timer ;;
  m30|30) install -m 0644 "$PAYLOAD_DIR/ops/systemd/alphard-m30.timer" /etc/systemd/system/alphard.timer ;;
  *) echo "Unknown TIMER_MODE=$TIMER_MODE; expected m15 or m30" >&2; exit 2 ;;
esac

install -m 0644 "$PAYLOAD_DIR/etc/alphard/runner.env" /etc/alphard/runner.env
if [[ ! -f /etc/alphard/.env.cloud || "$OVERWRITE_ENV" == "true" ]]; then
  install -m 0600 "$PAYLOAD_DIR/etc/alphard/.env.cloud" /etc/alphard/.env.cloud
else
  echo "Preserving existing /etc/alphard/.env.cloud. Set OVERWRITE_ENV=true to replace it."
fi

# Configure Docker to pull Artifact Registry images using the VM service account.
# Debian GCE images normally include gcloud. If not, install the Google Cloud CLI first.
if command -v gcloud >/dev/null 2>&1; then
  # shellcheck disable=SC1091
  source /etc/alphard/runner.env
  if [[ -n "${ALPHARD_ARTIFACT_REGISTRY_HOST:-}" ]]; then
    gcloud auth configure-docker "$ALPHARD_ARTIFACT_REGISTRY_HOST" --quiet
  fi
else
  echo "gcloud is not installed; Docker pulls from Artifact Registry may fail." >&2
fi

systemctl daemon-reload
systemctl disable --now alphard.timer >/dev/null 2>&1 || true
systemctl enable --now alphard.timer

if [[ "$RUN_NOW" == "true" ]]; then
  systemctl start alphard.service || journalctl -u alphard.service -n 120 --no-pager
fi

systemctl list-timers alphard.timer --no-pager
