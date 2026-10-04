#!/bin/sh
# Keep dnsmasq published on the tailnet address (it can start before tailscale0 exists at boot)
TSIP=<TAILSCALE_IP>
PROJ=/volume1/docker/homelab-bundle
while true; do
  if ip -4 -o addr show 2>/dev/null | grep -q "$TSIP/"; then
    if ! docker port dnsmasq 2>/dev/null | grep -q "$TSIP:53"; then
      logger -t homelab-dns "dnsmasq not published on $TSIP:53, recreating"
      docker compose --project-directory "$PROJ" up -d --force-recreate dnsmasq >/dev/null 2>&1
      sleep 30
    fi
  fi
  sleep 20
done
