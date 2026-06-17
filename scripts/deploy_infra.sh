#!/usr/bin/env bash
set -euo pipefail

: "${PROJECT_ID:?Set PROJECT_ID, e.g. source scripts/set_env.sh}"
: "${REGION:=us-central1}"
: "${ZONE:=us-central1-a}"
: "${SA_NAME:=alphard-trader-sa}"
: "${KEY_FILE_NAME:=alphard-trader-sa-key.json}"
: "${SA_DISPLAY_NAME:=Alphard Trader Service Account}"
: "${BUCKET_NAME:=charts-${PROJECT_ID}}"
: "${VM_NAME:=alphard-vm}"
: "${VM_MACHINE_TYPE:=e2-micro}"
: "${VM_DISK_SIZE:=20GB}"
: "${CREATE_VM:=true}"
: "${GCS_PUBLIC_READ:=false}"

SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "Setting active project to ${PROJECT_ID}"
gcloud config set project "${PROJECT_ID}"

echo "Enabling APIs"
gcloud services enable \
  compute.googleapis.com \
  aiplatform.googleapis.com \
  storage.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  iam.googleapis.com

if ! gcloud iam service-accounts describe "${SA_EMAIL}" >/dev/null 2>&1; then
  echo "Creating service account ${SA_EMAIL}"
  gcloud iam service-accounts create "${SA_NAME}" --display-name="${SA_DISPLAY_NAME}"
else
  echo "Service account exists: ${SA_EMAIL}"
fi

echo "Waiting to changes update"
sleep 10

# ==========================================
# 3. Create & Save the API Key (JSON Key)
# ==========================================
if [ -f "$KEY_FILE_NAME" ]; then
    echo "✅ API Key already exists locally at ${KEY_FILE_NAME}. Skipping creation to prevent key rotation."
else
    echo "Generating and saving Service Account JSON key to ${KEY_FILE_NAME}..."
    gcloud iam service-accounts keys create $KEY_FILE_NAME \
        --iam-account=$SA_EMAIL

    # Secure the key file locally
    chmod 600 $KEY_FILE_NAME
    echo "✅ API Key successfully saved and secured: $KEY_FILE_NAME"
fi


echo "Granting Vertex AI access"
gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/aiplatform.user" \
  --quiet >/dev/null


if ! gcloud storage buckets describe "gs://${BUCKET_NAME}" >/dev/null 2>&1; then
  echo "Creating bucket gs://${BUCKET_NAME}"
  gcloud storage buckets create "gs://${BUCKET_NAME}" \
    --location="${REGION}" \
    --uniform-bucket-level-access
else
  echo "Bucket exists: gs://${BUCKET_NAME}"
fi

echo "Granting bucket object admin to service account"
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET_NAME}" \
  --member="serviceAccount:${SA_EMAIL}" \
  --role="roles/storage.objectAdmin" \
  --quiet >/dev/null

if [[ "${GCS_PUBLIC_READ}" == "true" ]]; then
  echo "Making chart objects publicly readable because GCS_PUBLIC_READ=true"
  gcloud storage buckets add-iam-policy-binding "gs://${BUCKET_NAME}" \
    --member="allUsers" \
    --role="roles/storage.objectViewer" \
    --quiet >/dev/null
else
  echo "Keeping bucket private. Vertex/Gemini should access gs:// via the VM service account."
fi

if [[ "${CREATE_VM}" == "true" ]]; then
  if ! gcloud compute instances describe "${VM_NAME}" --zone="${ZONE}" >/dev/null 2>&1; then
    echo "Creating stateful light VM ${VM_NAME}"
    gcloud compute instances create "${VM_NAME}" \
      --zone="${ZONE}" \
      --machine-type="${VM_MACHINE_TYPE}" \
      --service-account="${SA_EMAIL}" \
      --scopes="https://www.googleapis.com/auth/cloud-platform" \
      --boot-disk-size="${VM_DISK_SIZE}" \
      --image-family="debian-12" \
      --image-project="debian-cloud" \
      --metadata=startup-script='#!/usr/bin/env bash
set -eux
apt-get update
apt-get install -y python3-venv python3-pip git sqlite3
mkdir -p /opt/alphard/app /opt/alphard/state/data /opt/alphard/state/img_cache
chown -R $USER:$USER /opt/alphard || true
'
  else
    echo "VM exists: ${VM_NAME}"
  fi
fi

cat <<EOF

Infra ready.

Use these app settings in .env.cloud:
APP_ENV=cloud
IMAGE_PROVIDER=gcs
GCS_BUCKET_NAME=${BUCKET_NAME}
GCS_PUBLIC_READ=${GCS_PUBLIC_READ}
SQLITE_PATH=/opt/alphard/state/alphard.sqlite3
DATA_DIR=/opt/alphard/state/data
IMAGE_CACHE_DIR=/opt/alphard/state/img_cache

EOF
