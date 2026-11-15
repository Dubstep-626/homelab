#!/usr/bin/env bash
# X570 (deus-vult, Fedora) - remote power + metrics. Run as the login user.
set -euo pipefail
# Tailscale SSH: control-api's poweroff authenticates by tailnet identity (no keys)
sudo tailscale set --ssh
# Allow exactly one passwordless command for remote shutdown
echo "$USER ALL=(root) NOPASSWD: /usr/bin/systemctl poweroff" | sudo tee /etc/sudoers.d/remote-poweroff >/dev/null
sudo chmod 440 /etc/sudoers.d/remote-poweroff
sudo visudo -c
# node-exporter (no Fedora package) - container, same as the M920q
sudo systemctl enable --now docker
sudo docker run -d --name node-exporter --restart unless-stopped \
  --net host --pid host -v /:/host:ro,rslave \
  prom/node-exporter:latest --path.rootfs=/host
sleep 3
curl -s localhost:9100/metrics | grep -q node_boot_time_seconds && echo "node-exporter OK on :9100"
echo "X570_TS_IP=$(tailscale ip -4)"
