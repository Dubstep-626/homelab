#!/bin/bash
# Nightly: export M920q volumes + stack config, push to the NAS drop folder.
# The NAS's weekly homelab-backup.sh then ships it to Google Drive (rclone crypt).
# Prometheus is excluded on purpose: ~4 GB of re-collectable metrics.
set -euo pipefail
PROJ=/opt/homelab/homelab-bundle
OUT=/var/backups/m920q
NAS=Havok@192.168.0.172
DEST=/volume1/docker/m920q-backup
KEY=/root/.ssh/id_ed25519_nas
VOLS="uptime-kuma-data grafana-data grafana-dashboards scrypted-data diun-data fail2ban-config"
STOP="uptime-kuma grafana scrypted"   # stopped briefly so their databases export consistently

rm -rf "$OUT"; mkdir -p "$OUT/volumes"; chmod 700 "$OUT"
cd "$PROJ"
docker compose stop $STOP
trap 'cd "$PROJ" && docker compose start $STOP' EXIT
for v in $VOLS; do
  docker run --rm -v "homelab-bundle_$v":/v:ro -v "$OUT/volumes":/out alpine \
    tar czf "/out/$v.tar.gz" -C /v .
done
docker compose start $STOP
trap - EXIT

tar czf "$OUT/stack.tar.gz" -C /opt/homelab homelab-bundle
scp -O -q -i "$KEY" "$OUT"/stack.tar.gz "$OUT"/volumes/*.tar.gz "$NAS:$DEST/"
echo "m920q backup complete $(date -Is)"
