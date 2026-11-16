#!/bin/sh
# Install to /usr/local/sbin/tailscale-exit-gro.sh (chmod 755). Needs: apt install ethtool
# Tailscale's recommended UDP GRO settings for Linux exit nodes / subnet routers.
DEV=$(ip -o route get 8.8.8.8 | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}')
[ -n "$DEV" ] && ethtool -K "$DEV" rx-udp-gro-forwarding on rx-gro-list off
