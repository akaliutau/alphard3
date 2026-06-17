#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
cd "$REPO_ROOT"

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"
INSTANCE_NAME="${INSTANCE_NAME:-alphard-runner}"
MACHINE_TYPE="${MACHINE_TYPE:-e2-small}"
AR_REPO="${AR_REPO:-alphard}"
IMAGE_NAME="${IMAGE_NAME:-alphard}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"
SERVICE_ACCOUNT_NAME="${SA_NAME:-alphard-trader-sa}"
STATE_DISK="${STATE_DISK:-alphard-state}"
STATE_DISK_SIZE="${STATE_DISK_SIZE:-30GB}"
BOOT_DISK_SIZE="${BOOT_DISK_SIZE:-10GB}"
RUN_INTERVAL_MINUTES="${RUN_INTERVAL_MINUTES:-15}"
RUN_NOW="${RUN_NOW:-false}"
OVERWRITE_ENV="${OVERWRITE_ENV:-false}"
DRY_RUN="${DRY_RUN:-true}"
SYMBOLS="${SYMBOLS:-EURUSD,USDJPY}"
MT5_TIMEFRAME="${MT5_TIMEFRAME:-M15}"
GCS_BUCKET_NAME="${GCS_BUCKET_NAME:-charts-${PROJECT_ID}}"
ENV_CLOUD_FILE="${ENV_CLOUD_FILE:-}"
ALPHARD_DOCKER_MEMORY="${ALPHARD_DOCKER_MEMORY:-1536m}"
ALPHARD_DOCKER_CPUS="${ALPHARD_DOCKER_CPUS:-1.0}"
CREATE_MT5_FIREWALL="${CREATE_MT5_FIREWALL:-false}"
MT5_PROXY_TAG="${MT5_PROXY_TAG:-mt5proxy}"
MT5_PROXY_PORT="${MT5_PROXY_PORT:-8000}"
NETWORK="${NETWORK:-default}"

require_file() {
  local path="$1"
  if [[ ! -f "$path" ]]; then
    echo "Missing required file: $path" >&2
    echo "Run this from the Alphard repo root after copying the ops bundle into the repo root:" >&2
    echo "  cp -a alphard_ops_bundle/{Dockerfile,.dockerignore,.env.cloud.example,ops,scripts} ." >&2
    exit 2
  fi
}

if [[ -z "$PROJECT_ID" ]]; then
  echo "Set PROJECT_ID or run: gcloud config set project YOUR_PROJECT" >&2
  exit 2
fi

require_file Dockerfile
require_file requirements.txt
require_file app.py
require_file ops/bin/alphard-run-once.sh
require_file ops/systemd/alphard.service
require_file ops/systemd/alphard-m15.timer
require_file ops/systemd/alphard-m30.timer
require_file scripts/gcp/install_alphard_vm.sh
require_file scripts/gcp/allow_mt5proxy_firewall.sh

ARTIFACT_REGISTRY_HOST="${REGION}-docker.pkg.dev"
IMAGE_URI="${ARTIFACT_REGISTRY_HOST}/${PROJECT_ID}/${AR_REPO}/${IMAGE_NAME}:${IMAGE_TAG}"
SA_EMAIL="${SERVICE_ACCOUNT_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
TIMER_MODE="m15"
if [[ "$RUN_INTERVAL_MINUTES" == "30" ]]; then
  TIMER_MODE="m30"
elif [[ "$RUN_INTERVAL_MINUTES" != "15" ]]; then
  echo "RUN_INTERVAL_MINUTES must be 15 or 30 for the supplied systemd timers." >&2
  exit 2
fi

gcloud config set project "$PROJECT_ID" >/dev/null

if ! gcloud artifacts repositories describe "$AR_REPO" --location "$REGION" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$AR_REPO" \
    --repository-format=docker \
    --location="$REGION" \
    --description="Alphard Docker images"
fi

gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" \
  --project "$PROJECT_ID" \
  --location "$REGION" \
  --member "serviceAccount:${SA_EMAIL}" \
  --role "roles/artifactregistry.reader"

gcloud builds submit --tag "$IMAGE_URI" .

if ! gcloud compute disks describe "$STATE_DISK" --zone "$ZONE" >/dev/null 2>&1; then
  gcloud compute disks create "$STATE_DISK" \
    --zone "$ZONE" \
    --size "$STATE_DISK_SIZE" \
    --type pd-balanced
fi

if ! gcloud compute instances describe "$INSTANCE_NAME" --zone "$ZONE" >/dev/null 2>&1; then
  gcloud compute instances create "$INSTANCE_NAME" \
    --zone "$ZONE" \
    --machine-type "$MACHINE_TYPE" \
    --image-family debian-12 \
    --image-project debian-cloud \
    --boot-disk-size "$BOOT_DISK_SIZE" \
    --service-account "$SA_EMAIL" \
    --scopes cloud-platform \
    --tags alphard-runner \
    --metadata enable-oslogin=TRUE \
    --disk "name=${STATE_DISK},device-name=alphard-state,mode=rw,boot=no,auto-delete=no"
else
  echo "VM $INSTANCE_NAME already exists; updating files/services on it."
fi

if [[ "$CREATE_MT5_FIREWALL" == "true" ]]; then
  "$REPO_ROOT/scripts/gcp/allow_mt5proxy_firewall.sh"
fi

TMPDIR="$(mktemp -d)"
ARCHIVE="$(mktemp -t alphard-ops.XXXXXX.tgz)"
trap 'rm -rf "$TMPDIR" "$ARCHIVE"' EXIT
mkdir -p "$TMPDIR/ops/bin" "$TMPDIR/ops/systemd" "$TMPDIR/scripts/gcp" "$TMPDIR/etc/alphard"

install -m 0755 ops/bin/alphard-run-once.sh "$TMPDIR/ops/bin/alphard-run-once.sh"
install -m 0644 ops/systemd/alphard.service "$TMPDIR/ops/systemd/alphard.service"
install -m 0644 ops/systemd/alphard-m15.timer "$TMPDIR/ops/systemd/alphard-m15.timer"
install -m 0644 ops/systemd/alphard-m30.timer "$TMPDIR/ops/systemd/alphard-m30.timer"
install -m 0755 scripts/gcp/install_alphard_vm.sh "$TMPDIR/scripts/gcp/install_alphard_vm.sh"

cat > "$TMPDIR/etc/alphard/runner.env" <<EOF2
ALPHARD_IMAGE=${IMAGE_URI}
ALPHARD_ARTIFACT_REGISTRY_HOST=${ARTIFACT_REGISTRY_HOST}
ALPHARD_ENV_FILE=/etc/alphard/.env.cloud
ALPHARD_STATE_DIR=/var/lib/alphard
ALPHARD_CONTAINER_NAME=alphard-run
ALPHARD_DOCKER_MEMORY=${ALPHARD_DOCKER_MEMORY}
ALPHARD_DOCKER_CPUS=${ALPHARD_DOCKER_CPUS}
ALPHARD_DIAGNOSTICS_ONLY=${ALPHARD_DIAGNOSTICS_ONLY:-true}
EOF2

cp "$ENV_CLOUD_FILE" "$TMPDIR/etc/alphard/.env.cloud"
chmod 0600 "$TMPDIR/etc/alphard/.env.cloud"

cat > "$TMPDIR/install.env" <<EOF2
TIMER_MODE=${TIMER_MODE}
OVERWRITE_ENV=${OVERWRITE_ENV}
RUN_NOW=${RUN_NOW}
EOF2

# Create the archive outside TMPDIR. Creating it inside the directory being
# archived can make tar fail or upload an incomplete payload.
tar -C "$TMPDIR" -czf "$ARCHIVE" .

# Fail locally before touching the VM if the payload is incomplete.
if ! tar -tzf "$ARCHIVE" | grep -Eq '(^|\./)scripts/gcp/install_alphard_vm\.sh$'; then
  echo "Internal packaging error: archive does not contain scripts/gcp/install_alphard_vm.sh" >&2
  echo "Archive contents:" >&2
  tar -tzf "$ARCHIVE" >&2
  exit 2
fi

ARCHIVE_SIZE="$(wc -c < "$ARCHIVE")"
echo "Prepared VM payload: $ARCHIVE_SIZE bytes"
tar -tzf "$ARCHIVE" | sed 's#^#  #'

gcloud compute scp "$ARCHIVE" "${INSTANCE_NAME}:/tmp/alphard-ops.tgz" --zone "$ZONE"

REMOTE_INSTALL_CMD="$(cat <<'EOF'
set -Eeuo pipefail

sudo rm -rf /tmp/alphard-ops
sudo mkdir -p /tmp/alphard-ops
sudo tar -xzf /tmp/alphard-ops.tgz -C /tmp/alphard-ops

echo "Remote payload:"
sudo find /tmp/alphard-ops -maxdepth 4 -type f | sort

sudo test -f /tmp/alphard-ops/scripts/gcp/install_alphard_vm.sh
sudo bash /tmp/alphard-ops/scripts/gcp/install_alphard_vm.sh

echo "Post-install checks:"
sudo test -x /opt/alphard/bin/alphard-run-once.sh
sudo test -f /etc/alphard/runner.env
sudo test -f /etc/alphard/.env.cloud
sudo test -f /etc/systemd/system/alphard.service

sudo systemctl daemon-reload
systemctl cat alphard.service | grep -F '/opt/alphard/bin/alphard-run-once.sh'
systemctl list-timers 'alphard*' --no-pager

echo "Installed /opt/alphard:"
sudo find /opt/alphard -maxdepth 3 -type f -o -type l | sort
EOF
)"

gcloud compute ssh "$INSTANCE_NAME" --zone "$ZONE" --command "$REMOTE_INSTALL_CMD"

cat <<EOF2

Deployed Alphard runner.
Image:       ${IMAGE_URI}
VM:          ${INSTANCE_NAME} (${MACHINE_TYPE}) in ${ZONE}
Timer:       ${TIMER_MODE}
State:       ${STATE_DISK} mounted at /var/lib/alphard
Env file:    /etc/alphard/.env.cloud
Logs:        gcloud compute ssh ${INSTANCE_NAME} --zone ${ZONE} --command 'sudo journalctl -u alphard.service -n 200 --no-pager'
Timer check: gcloud compute ssh ${INSTANCE_NAME} --zone ${ZONE} --command 'systemctl list-timers "alphard*" --no-pager'

EOF2
