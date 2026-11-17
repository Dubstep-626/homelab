# X570 (deus-vult) setup for Jarvis

Fedora 45, RTX 4070 SUPER 12 GB, RPM Fusion `akmod-nvidia`. LAN 192.168.0.24, user `mdubin`.

## Ollama

`/etc/systemd/system/ollama.service.d/override.conf`:

```ini
[Service]
Environment="OLLAMA_HOST=0.0.0.0:11434"
Environment="OLLAMA_KEEP_ALIVE=5m"
Environment="OLLAMA_FLASH_ATTENTION=1"
Environment="OLLAMA_KV_CACHE_TYPE=q8_0"
```

Flash attention plus the q8_0 KV cache roughly halves the context memory, so `qwen3:14b` (8k context) and Kokoro fit together at 100% GPU. Check with `ollama ps`.
Models: `qwen3:14b` (Jarvis), `llama3.1:8b`.

Firewall (Ollama only from the M920q, LAN and Tailscale):

```bash
sudo firewall-cmd --permanent --add-rich-rule='rule priority="-10" family="ipv4" source address="192.168.0.91" port port="11434" protocol="tcp" accept'
sudo firewall-cmd --permanent --add-rich-rule='rule priority="-9" family="ipv4" source address="100.83.195.113" port port="11434" protocol="tcp" accept'
sudo firewall-cmd --permanent --add-rich-rule='rule family="ipv4" port port="11434" protocol="tcp" reject'
sudo firewall-cmd --reload
```

## Kokoro TTS on the GPU

```bash
curl -s -L https://nvidia.github.io/libnvidia-container/stable/rpm/nvidia-container-toolkit.repo | sudo tee /etc/yum.repos.d/nvidia-container-toolkit.repo
sudo dnf install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker && sudo systemctl enable docker
docker run -d --name kokoro --restart unless-stopped --gpus all \
  -p 192.168.0.24:8880:8880 ghcr.io/remsky/kokoro-fastapi-gpu:latest
```

Kokoro holds about 1.5 GB of VRAM while running: `docker stop kokoro` before gaming. jarvis-voice falls back to the M920q's CPU Kokoro automatically when this one is down.

## Power control (from control-api on the M920q)

- Wake: WoL to `d4:5d:64:56:41:b1` (permanent ARP entry on the M920q, `host/m920q/homelab-x570-neigh.service`).
- Shutdown: Tailscale SSH → `sudo systemctl poweroff` (`/etc/sudoers.d/remote-poweroff`, NOPASSWD for that command only).
