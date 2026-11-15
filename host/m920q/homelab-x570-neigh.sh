#!/bin/sh
# Install to /usr/local/sbin/homelab-x570-neigh.sh (chmod 755)
# Permanent ARP entry so unicast WoL from the control-api container reaches the X570 while it's off.
X570_IP="${X570_IP:-192.168.0.24}"
X570_MAC="${X570_MAC:-d4:5d:64:56:41:b1}"
DEV=$(ip -o route get 192.168.0.1 | awk '{for(i=1;i<=NF;i++) if($i=="dev") print $(i+1)}')
case "$1" in
  stop) ip neigh del "$X570_IP" dev "$DEV" 2>/dev/null || true ;;
  *)    ip neigh replace "$X570_IP" lladdr "$X570_MAC" nud permanent dev "$DEV" ;;
esac
