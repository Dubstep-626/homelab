#!/bin/bash
set -eu
PROJ=/volume1/docker/homelab-bundle
STAGEROOT=/volume1/backup-staging
KEEP=3
STAMP=$(date +%Y%m%d-%H%M)
STAGE=$STAGEROOT/$STAMP
NAME=homelab-$STAMP.tar
RC="docker run --rm -v $PROJ/rclone:/config/rclone -v $STAGEROOT:/data rclone/rclone"
log() { logger -t homelab-backup "$*"; echo "$*"; }
cleanup() { rm -rf "$STAGE" "$STAGEROOT/$NAME"; }
trap 'log "BACKUP FAILED (line $LINENO)"' ERR
trap cleanup EXIT
mkdir -p "$STAGE/volumes" "$STAGE/host"

log "start $STAMP"
tar czf "$STAGE/stack.tar.gz" --exclude=homelab-bundle/rclone -C /volume1/docker homelab-bundle m920q-backup
cp /usr/local/sbin/homelab-*.sh /etc/systemd/system/homelab-*.service "$STAGE/host/"
for v in $(docker volume ls -q); do
  case "$v" in *prometheus-data|*netdata-cache|*netdata-lib|*scrutiny-influxdb) continue;; esac
  [ ${#v} -eq 64 ] && continue
  docker run --rm --entrypoint tar -v "$v":/v:ro -v "$STAGE/volumes":/out rclone/rclone czf "/out/$v.tar.gz" -C /v .
done
tar cf "$STAGEROOT/$NAME" -C "$STAGE" .
log "archive $NAME ($(du -h "$STAGEROOT/$NAME" | cut -f1)), uploading"
$RC copyto "/data/$NAME" "gdrive-crypt:$NAME" --drive-use-trash=false
$RC lsf gdrive-crypt: | grep -qx "$NAME"
log "uploaded $NAME"
OLD=$($RC lsf gdrive-crypt: | grep -E '^homelab-[0-9]{8}-[0-9]{4}\.tar$' | sort | head -n -$KEEP || true)
for f in $OLD; do $RC deletefile "gdrive-crypt:$f" --drive-use-trash=false; log "deleted old $f"; done
log "done"
[ -r /etc/homelab-backup.env ] && . /etc/homelab-backup.env
if [ -n "${KUMA_PUSH_URL:-}" ]; then curl -fsS -m 10 "$KUMA_PUSH_URL" >/dev/null && log "pinged kuma" || log "kuma ping failed"; fi
