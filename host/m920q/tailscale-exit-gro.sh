#!/bin/sh
# Install to /usr/local/sbin/tailscale-exit-gro.sh (chmod 755). Needs: apt install ethtool
# Tailscale's recommended UDP GRO settings for Linux exit nodes / subnet routers.
# At boot network-online.target can be reached before DHCP has a route; wait up to 90 s for one.
for i in $(seq 45); do ip -o route get 8.8.8.8 >/dev/null 2>&1 && break; sleep 2; done
DEV=$(ip -o route get 8.8.8.8 | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}')
[ -n "$DEV" ] && ethtool -K "$DEV" rx-udp-gro-forwarding on rx-gro-list off
