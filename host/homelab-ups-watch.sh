#!/bin/sh
FLAG=/volume1/docker/homelab-bundle/nut-server/flag/monitor
echo 0 > "$FLAG"   # clear any stale flag at start
while true; do
  if [ "$(cat "$FLAG" 2>/dev/null)" = "1" ]; then
    echo 0 > "$FLAG"   # reset first so a stale 1 can never cause a boot loop
    logger -t homelab-ups "UPS critical flag seen"
    if [ "${DRY_RUN:-1}" = "1" ]; then
      logger -t homelab-ups "DRY RUN: would power off now"
    else
      logger -t homelab-ups "waiting ${SHUTDOWN_DELAY:-45}s for pve, then powering off NAS"
      sleep "${SHUTDOWN_DELAY:-45}"
      systemctl poweroff
    fi
    sleep 60
  fi
  sleep 5
done
