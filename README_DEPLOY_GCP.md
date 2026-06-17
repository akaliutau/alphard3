# Alphard ops-only Docker + GCE systemd timer deployment

This is deployment instructions

## Runtime shape

```text
GCE permanent VM
  systemd timer at :01/:16/:31/:46 or :01/:31
    -> alphard.service one-shot
      -> /usr/local/bin/alphard-run-once.sh
        -> flock /run/alphard.lock
        -> docker run --rm IMAGE --once
          -> python app.py --once
          -> SQLite/GCS/img_cache persisted under /var/lib/alphard
```

The scheduler lives on the host, not inside the container. 
The container is a fresh Python process each cycle, so leaked HTTP clients, matplotlib state, LiteLLM state, 
or MT5 proxy client state do not survive into the next basket.

## Recommended VM size

Use `e2-small` as the default. `e2-micro` is attractive for cost/free-tier tests, 
but 1 GB RAM is tight for pandas + matplotlib/mplfinance + LiteLLM + Google libraries, 
especially when multiple symbols are processed concurrently. `e2-small` gives 2 GB RAM and twice the sustained shared-core CPU share. 

Move to `e2-medium` if cycles regularly approach the 14-minute service timeout or if you add many symbols.

## First deployment

Prerequisites on your workstation:

```bash
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
```

From the Alphard repo root after copying these files in:

```bash
scripts/gcp/deploy_alphard_vm.sh
```

To use a hand-maintained environment file instead of generated env:

```bash
cp .env.cloud.example .env.cloud
# edit MT5_BASE_URL, MT5_API_KEY, PROJECT_ID, bucket, symbols, DRY_RUN
export ENV_CLOUD_FILE=$PWD/.env.cloud
export OVERWRITE_ENV=true
scripts/gcp/deploy_alphard_vm.sh
```

For 30-minute scheduling set `RUN_INTERVAL_MINUTES=30`


## MT5 proxy on another VM

Put the Alphard VM and MT5 proxy VM in the same VPC when possible. 
Give the Alphard VM the tag `alphard-runner` and the MT5 proxy VM the tag `mt5proxy`, then allow only Alphard to call the proxy port:

```bash
export PROJECT_ID=YOUR_PROJECT_ID
export NETWORK=default
export MT5_PROXY_TAG=mt5proxy
export MT5_PROXY_PORT=8000
scripts/gcp/allow_mt5proxy_firewall.sh
```

Then set:

```text
MT5_BASE_URL=http://MT5_PROXY_INTERNAL_IP:8000
```

## External web access

The deploy script creates a normal non-Spot Compute Engine VM with an external IP. The container runs with host networking, 
so outbound internet, Vertex AI, GCS, Artifact Registry, and the MT5 proxy URL use the VM network path. 

If you later remove the external IP for a private-only VM, add Cloud NAT or another egress path.

## Operations

Check the timer:

```bash
gcloud compute ssh alphard-runner --zone europe-west2-b --command 'systemctl list-timers alphard.timer --no-pager'
```

View recent run logs:

```bash
gcloud compute ssh alphard-runner --zone europe-west2-b --command 'sudo journalctl -u alphard.service -n 200 --no-pager'
```

Run one cycle manually:

```bash
gcloud compute ssh alphard-runner --zone europe-west2-b --command 'sudo systemctl start alphard.service && sudo journalctl -u alphard.service -n 120 --no-pager'
```

Edit cloud env on the VM:

```bash
gcloud compute ssh alphard-runner --zone europe-west2-b --command 'sudoedit /etc/alphard/.env.cloud'
```

Disable trading runner:

```bash
gcloud compute ssh alphard-runner --zone europe-west2-b --command 'sudo systemctl disable --now alphard.timer'
```

## Redeploy only image/config

Every deployment builds a new Artifact Registry image and updates `/etc/alphard/runner.env` with the new image tag. By default it preserves `/etc/alphard/.env.cloud`; set `OVERWRITE_ENV=true` to replace it from `ENV_CLOUD_FILE` or generated env.

```bash
export PROJECT_ID=YOUR_PROJECT_ID
export OVERWRITE_ENV=false
scripts/gcp/deploy_alphard_vm.sh
```
