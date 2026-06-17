#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
NETWORK="${NETWORK:-default}"
RULE_NAME="${RULE_NAME:-allow-mt5proxy-from-alphard}"
ALPHARD_TAG="${ALPHARD_TAG:-alphard-runner}"
MT5_PROXY_TAG="${MT5_PROXY_TAG:-mt5proxy}"
MT5_PROXY_PORT="${MT5_PROXY_PORT:-8000}"

if [[ -z "$PROJECT_ID" ]]; then
  echo "Set PROJECT_ID or run: gcloud config set project YOUR_PROJECT" >&2
  exit 2
fi

gcloud config set project "$PROJECT_ID" >/dev/null

if gcloud compute firewall-rules describe "$RULE_NAME" >/dev/null 2>&1; then
  echo "Firewall rule $RULE_NAME already exists."
else
  gcloud compute firewall-rules create "$RULE_NAME" \
    --network "$NETWORK" \
    --direction INGRESS \
    --action ALLOW \
    --rules "tcp:${MT5_PROXY_PORT}" \
    --source-tags "$ALPHARD_TAG" \
    --target-tags "$MT5_PROXY_TAG" \
    --description "Allow Alphard runner VMs to reach MT5 proxy over the VPC internal IP"
fi

cat <<EOF
Make sure the MT5 proxy VM has network tag: ${MT5_PROXY_TAG}
Use the proxy VM's internal IP in Alphard: MT5_BASE_URL=http://INTERNAL_IP:${MT5_PROXY_PORT}
EOF
