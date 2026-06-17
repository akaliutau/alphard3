#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
REGION="${REGION:-us-central1}"
ZONE="${ZONE:-us-central1-a}"
INSTANCE_NAME="${INSTANCE_NAME:-alphard-runner}"
MACHINE_TYPE="${MACHINE_TYPE:-e2-small}"
AR_REPO="${AR_REPO:-alphard}"
IMAGE_NAME="${IMAGE_NAME:-alphard}"
IMAGE_TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)}"
SERVICE_ACCOUNT_NAME="${SERVICE_ACCOUNT_NAME:-alphard-runner}"
STATE_DISK="${STATE_DISK:-alphard-state}"
STATE_DISK_SIZE="${STATE_DISK_SIZE:-20GB}"
BOOT_DISK_SIZE="${BOOT_DISK_SIZE:-20GB}"
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

if [[ -z "$PROJECT_ID" ]]; then
  echo "Set PROJECT_ID or run: gcloud config set project YOUR_PROJECT" >&2
  exit 2
fi

if [[ ! -f Dockerfile || ! -f requirements.txt || ! -f app.py ]]; then
  echo "Run this script from the Alphard repo root after adding the Dockerfile/ops bundle." >&2
  exit 2
fi

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

gcloud services enable \
  compute.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  aiplatform.googleapis.com \
  storage.googleapis.com \
  iam.googleapis.com

if ! gcloud artifacts repositories describe "$AR_REPO" --location "$REGION" >/dev/null 2>&1; then
  gcloud artifacts repositories create "$AR_REPO" \
    --repository-format=docker \
    --location="$REGION" \
    --description="Alphard Docker images"
fi

gcloud builds submit --tag "$IMAGE_URI" .

if ! gcloud iam service-accounts describe "$SA_EMAIL" >/dev/null 2>&1; then
  gcloud iam service-accounts create "$SERVICE_ACCOUNT_NAME" \
    --display-name="Alphard VM runner"
fi

for role in roles/aiplatform.user roles/logging.logWriter roles/artifactregistry.reader; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="$role" \
    --condition=None >/dev/null
done

if ! gcloud storage buckets describe "gs://${GCS_BUCKET_NAME}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${GCS_BUCKET_NAME}" \
    --project="$PROJECT_ID" \
    --location="$REGION" \
    --uniform-bucket-level-access
fi
# Bucket-scoped write/read for chart objects.
gcloud storage buckets add-iam-policy-binding "gs://${GCS_BUCKET_NAME}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.objectAdmin" >/dev/null

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
  PROJECT_ID="$PROJECT_ID" NETWORK="$NETWORK" MT5_PROXY_TAG="$MT5_PROXY_TAG" MT5_PROXY_PORT="$MT5_PROXY_PORT" "$PWD/scripts/gcp/allow_mt5proxy_firewall.sh"
fi

TMPDIR="$(mktemp -d)"
trap 'rm -rf "$TMPDIR"' EXIT
mkdir -p "$TMPDIR/ops/bin" "$TMPDIR/ops/systemd" "$TMPDIR/scripts/gcp" "$TMPDIR/etc/alphard"
cp ops/bin/alphard-run-once.sh "$TMPDIR/ops/bin/"
cp ops/systemd/alphard.service ops/systemd/alphard-m15.timer ops/systemd/alphard-m30.timer "$TMPDIR/ops/systemd/"
cp scripts/gcp/install_alphard_vm.sh "$TMPDIR/scripts/gcp/"

cat > "$TMPDIR/etc/alphard/runner.env" <<EOF
ALPHARD_IMAGE=${IMAGE_URI}
ALPHARD_ARTIFACT_REGISTRY_HOST=${ARTIFACT_REGISTRY_HOST}
ALPHARD_ENV_FILE=/etc/alphard/.env.cloud
ALPHARD_STATE_DIR=/var/lib/alphard
ALPHARD_CONTAINER_NAME=alphard-run
ALPHARD_DOCKER_MEMORY=${ALPHARD_DOCKER_MEMORY}
ALPHARD_DOCKER_CPUS=${ALPHARD_DOCKER_CPUS}
EOF

cp "$ENV_CLOUD_FILE" "$TMPDIR/etc/alphard/.env.cloud"
chmod 0600 "$TMPDIR/etc/alphard/.env.cloud"

cat > "$TMPDIR/install.env" <<EOF
TIMER_MODE=${TIMER_MODE}
OVERWRITE_ENV=${OVERWRITE_ENV}
RUN_NOW=${RUN_NOW}
EOF

tar -C "$TMPDIR" -czf "$TMPDIR/alphard-ops.tgz" .

gcloud compute scp "$TMPDIR/alphard-ops.tgz" "${INSTANCE_NAME}:/tmp/alphard-ops.tgz" --zone "$ZONE"
gcloud compute ssh "$INSTANCE_NAME" --zone "$ZONE" --command \
  "sudo rm -rf /tmp/alphard-ops && sudo mkdir -p /tmp/alphard-ops && sudo tar -xzf /tmp/alphard-ops.tgz -C /tmp/alphard-ops && sudo bash /tmp/alphard-ops/scripts/gcp/install_alphard_vm.sh"

cat <<EOF

Deployed Alphard runner.
Image:       ${IMAGE_URI}
VM:          ${INSTANCE_NAME} (${MACHINE_TYPE}) in ${ZONE}
Timer:       ${TIMER_MODE}
State:       ${STATE_DISK} mounted at /var/lib/alphard
Env file:    /etc/alphard/.env.cloud
Logs:        gcloud compute ssh ${INSTANCE_NAME} --zone ${ZONE} --command 'sudo journalctl -u alphard.service -n 200 --no-pager'
Timer check: gcloud compute ssh ${INSTANCE_NAME} --zone ${ZONE} --command 'systemctl list-timers alphard.timer --no-pager'

EOF
