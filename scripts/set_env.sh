#!/usr/bin/env bash
# Usage: source scripts/set_env.sh
export PROJECT_ID="alphard3"
export REGION="us-central1"
export ZONE="us-central1-a"
export SA_NAME="alphard-trader-sa"
export SA_DISPLAY_NAME="Alphard Trader Service Account"
export BUCKET_NAME="alphard-charts-${PROJECT_ID}"
export VM_NAME="alphard-vm"
export VM_MACHINE_TYPE="e2-micro"
export VM_DISK_SIZE="20GB"
export CREATE_VM="false"
export GCS_PUBLIC_READ="false"
