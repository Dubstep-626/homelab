#!/bin/bash
# Nightly: export M920q volumes + stack config, push to NAS drop folder.
# NAS weekly homelab-backup.sh then ships it to Google Drive (encrypted).
#
# Covered: Kuma, Grafana, Scrypted, Diun, fail2ban, n8n, Jarvis (bot data,
# sentinel baseline), Open WebUI, NetBox (pg_dump + media/scripts/reports),
# and the homelab-bundle + netbox stack directories (compose files, .env).
# Not covered on purpose (re-downloadable or disposable): jarvis-models
# (Whisper/Kokoro weights), prometheus-data (metrics history), NetBox Redis,
# anonymous volumes. Ollama models live on the X570.
set -euo pipefail
PROJ=/opt/homelab/homelab-bundle
OUT=/var/backups/m920q
NAS=Havok@192.168.0.172
DEST=/volume1/docker/m920q-backup
KEY=/root/.ssh/id_ed25519_nas

# full volume names; output file = name without the "homelab-bundle_" prefix
VOLS="homelab-bundle_uptime-kuma-data homelab-bundle_grafana-data homelab-bundle_grafana-dashboards
      homelab-bundle_scrypted-data homelab-bundle_diun-data homelab-bundle_fail2ban-config
      homelab-bundle_n8n-data homelab-bundle_jarvis-bot-data homelab-bundle_sentinel-data
      homelab-bundle_open-webui-data
      netbox_netbox-media-files netbox_netbox-scripts-files netbox_netbox-reports-files"

# containers with SQLite/JSON state that must be quiet while copied
# (stopped together for well under a minute; started again even on failure)
STOP="uptime-kuma grafana scrypted n8n jarvis-bot jarvis-sentinel open-webui"
NETBOX_DB=netbox-postgres-1

rm -rf "$OUT"; mkdir -p "$OUT/volumes"; chmod 700 "$OUT"

# 1. NetBox database: consistent SQL dump while Postgres keeps running
docker exec "$NETBOX_DB" sh -c 'pg_dump -U "$POSTGRES_USER" --clean --if-exists "$POSTGRES_DB"' \
  | gzip > "$OUT/volumes/netbox-postgres.sql.gz"
zcat "$OUT/volumes/netbox-postgres.sql.gz" | tail -n 5 | grep 'PostgreSQL database dump complete' >/dev/null \
  || { echo "NetBox pg_dump incomplete" >&2; exit 1; }

# 2. Volumes, with stateful containers briefly stopped
running=$(for c in $STOP; do docker ps -q -f "name=^${c}$" | grep -q . && echo "$c"; done || true)
[ -n "$running" ] && docker stop -t 30 $running >/dev/null
trap '[ -n "$running" ] && docker start $running >/dev/null' EXIT
for v in $VOLS; do
  docker volume inspect "$v" >/dev/null 2>&1 || { echo "missing volume $v" >&2; exit 1; }
  excl=""
  # Open WebUI's cache/ is ~1 GB of re-downloadable embedding/Whisper models
  [ "$v" = homelab-bundle_open-webui-data ] && excl="--exclude=./cache"
  docker run --rm -v "$v":/v:ro -v "$OUT/volumes":/out alpine \
    tar czf "/out/${v#homelab-bundle_}.tar.gz" $excl -C /v .
done
[ -n "$running" ] && docker start $running >/dev/null
trap - EXIT

# 3. Stack directories (compose files, configs, .env for both projects)
tar czf "$OUT/stack.tar.gz" -C /opt/homelab --exclude='netbox/.git' homelab-bundle netbox

scp -O -q -i "$KEY" "$OUT"/stack.tar.gz "$OUT"/volumes/* "$NAS:$DEST/"
echo "m920q backup complete $(date -Is): $(ls "$OUT/volumes" | wc -l) volume files, $(du -sh "$OUT" | cut -f1)"
