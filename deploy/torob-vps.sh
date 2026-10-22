#!/usr/bin/env bash
# Redeploy Torob to the VPS: ship the committed code (not the dataset), rebuild, swap the container, check /health.
#   bash deploy/torob-vps.sh            # uses the "wallex-iran" host from ~/.ssh/config
# The server's ufw LIMITs SSH to 6 new connections per 30 s, so this makes exactly two (scp + ssh).
# torob.db lives in ~/torob/data on the server and is not re-uploaded (100 MB over the VPN took ~20 min).
set -euo pipefail
HOST="${HOST:-wallex-iran}"
cd "$(dirname "$0")/.."
tmp="$(mktemp -d)"
git archive --format=tar.gz -o "$tmp/torob-src.tar.gz" HEAD
scp -q "$tmp/torob-src.tar.gz" "$HOST:torob/torob-src.tar.gz"
ssh -o BatchMode=yes "$HOST" 'set -e
cd ~/torob && rm -rf src && mkdir src && tar xzf torob-src.tar.gz -C src
docker build -q -t torob:latest src >/dev/null
docker rm -f torob >/dev/null 2>&1 || true
docker run -d --name torob --restart unless-stopped --memory 768m --memory-swap 768m --cpus 1 --pids-limit 256 \
  --log-opt max-size=20m --log-opt max-file=3 -p 172.20.0.1:3130:8000 -v /home/ubuntu/torob/data:/data \
  -e TRUST_PROXY=1 torob:latest >/dev/null
for i in $(seq 1 30); do curl -sf --max-time 3 http://172.20.0.1:3130/health && exit 0; sleep 1; done
echo "health check failed"; docker logs --tail 30 torob; exit 1'
echo
curl -s --max-time 20 https://torob.frontage.store/health; echo
rm -rf "$tmp"
