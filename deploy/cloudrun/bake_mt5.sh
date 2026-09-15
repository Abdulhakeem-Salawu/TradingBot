#!/usr/bin/env bash
# Cloud Run job "signal-bot-bake" (image wine-base): install MetaTrader 5 and
# Windows Python into the Wine prefix, then upload the prefix to BAKE_URI
# (gs://bucket/build/mt5-prefix.tar.gz) for Dockerfile.mt5. See Dockerfile.wine
# for why this runs on Cloud Run and not in Cloud Build.
set -euo pipefail
: "${BAKE_URI:?set BAKE_URI=gs://bucket/build/mt5-prefix.tar.gz}"

echo "kernel $(uname -r), $(nproc) cpus, $(awk '/MemTotal/{print int($2/1048576)}' /proc/meminfo) GiB"
bash /opt/install_mt5.sh

echo; echo "== $(date -u +%H:%M:%S) pack the prefix"
tar -C "$(dirname "$WINEPREFIX")" -cf - "$(basename "$WINEPREFIX")" | pigz -6 > /tmp/mt5-prefix.tar.gz
ls -la /tmp/mt5-prefix.tar.gz

echo; echo "== $(date -u +%H:%M:%S) upload to $BAKE_URI"
path="${BAKE_URI#gs://}"
bucket="${path%%/*}"
object="${path#*/}"
token=$(curl -fsS -H "Metadata-Flavor: Google" \
  "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" |
  sed -E 's/.*"access_token" *: *"([^"]+)".*/\1/')
# -T streams the file (no copy in memory); the JSON API wants POST.
curl -fsS -X POST -T /tmp/mt5-prefix.tar.gz -o /dev/null \
  -H "Authorization: Bearer $token" -H "Content-Type: application/gzip" \
  "https://storage.googleapis.com/upload/storage/v1/b/$bucket/o?uploadType=media&name=${object//\//%2F}"
echo "== $(date -u +%H:%M:%S) done"
