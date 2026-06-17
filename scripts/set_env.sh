#!/usr/bin/env bash
# Usage: source scripts/set_env.sh
export PROJECT_ID="alphard3"
export REGION="us-central1"
export ZONE="us-central1-a"
export SA_NAME="alphard-trader-sa"
export SA_DISPLAY_NAME="Alphard Trader Service Account"
export BUCKET_NAME="charts-${PROJECT_ID}"
export VM_NAME="alphard-vm"
export VM_MACHINE_TYPE="e2-small"
export VM_DISK_SIZE="30GB"
export CREATE_VM="false"
export GCS_PUBLIC_READ="false"

export MACHINE_TYPE=e2-small
export INSTANCE_NAME=alphard-runner

# Use the MT5 proxy VM's INTERNAL IP if both VMs are in the same VPC.
export MT5_BASE_URL=http://10.0.0.10:8000
export MT5_API_KEY=dev-api-key

# Start safe. Switch DRY_RUN=false only after smoke tests.
export DRY_RUN=true
export RUN_INTERVAL_MINUTES=15
export RUN_NOW=true
