# Weekly lab report

Sunday 9 AM (after the 4 AM offsite backup) n8n calls `lab-report`, which:

1. Lab Ups the on-demand tier (skips the X570 while gaming mode is on).
2. Checks hosts (CPU/RAM/disk/temps/uptime), Prometheus targets, NAS SMART (Scrutiny), the T430's PERC
   (iDRAC Redfish, read-only user), UPS, backup units, containers on the M920q and NAS, AdGuard primary +
   secondary, sentinel, Tailscale devices, and pending apt/dnf packages + stale container images.
3. Puts each box back the way it found it, posts the report to Discord, and returns it to n8n.
4. If anything is pending, n8n posts Approve/Deny. Approve -> `POST /apply`: one host at a time
   (nas -> pve -> deus-vult), stop at the first failure, restore power, then the M920q last as a detached
   systemd unit that posts its own result. Never reboots. NUT is skipped/held.

`lab-agent.sh` is the only thing lab-report can run on a host: a forced-command SSH key that accepts
exactly `ping | check | backups | apply | apply-detached`.

## Setup

### 1. Key (M920q, in /opt/homelab/homelab-bundle)
```bash
mkdir -p lab-report/ssh && chmod 700 lab-report/ssh
ssh-keygen -t ed25519 -N '' -C lab-report -f lab-report/ssh/id_ed25519
cat lab-report/ssh/id_ed25519.pub     # used in step 2
```

### 2. Each host: m920q (havok), nas (Havok), pve (root), deus-vult (your user)
```bash
sudo install -m 0755 host/lab-agent.sh /usr/local/sbin/lab-agent.sh
sudo install -m 0600 host/lab-agent.conf.example /etc/lab-agent.conf
sudo nano /etc/lab-agent.conf           # keep the block for this host, delete the rest
```
Non-root users (m920q, nas, deus-vult):
```bash
echo "$USER ALL=(root) NOPASSWD: /usr/local/sbin/lab-agent.sh *" | sudo tee /etc/sudoers.d/lab-agent
sudo chmod 0440 /etc/sudoers.d/lab-agent && sudo visudo -c
# ~/.ssh/authorized_keys, one line:
restrict,command="sudo -n /usr/local/sbin/lab-agent.sh ssh \"$SSH_ORIGINAL_COMMAND\"" ssh-ed25519 AAAA... lab-report
```
pve (root, no sudo), in `/root/.ssh/authorized_keys`:
```
restrict,command="/usr/local/sbin/lab-agent.sh ssh \"$SSH_ORIGINAL_COMMAND\"" ssh-ed25519 AAAA... lab-report
```
deus-vult only: plain sshd must be on (control-api uses Tailscale SSH, which ignores authorized_keys):
`sudo systemctl enable --now sshd`. lab-report connects to the LAN IP, not the Tailscale IP.

Local sanity check on each host: `sudo /usr/local/sbin/lab-agent.sh check`.

### 3. Deploy
```bash
docker compose -f docker-compose.m920q.yml -f docker-compose.m920q.override.yml up -d --build lab-report
for h in Havok@192.168.0.172 root@192.168.0.10 mitch@192.168.0.24 havok@192.168.0.91; do
  docker exec lab-report ssh -o StrictHostKeyChecking=accept-new $h ping; done     # expect "ok" x4
```
Fix the X570 username in `LAB_AGENTS` (.env) if it isn't `mitch`.

### 4. Dry run (no Lab Up, no Discord post)
```bash
docker exec lab-report python -c "import urllib.request as u,os,json;r=u.Request('http://localhost:8000/run?lab=0&post=0',method='POST',headers={'Authorization':'Bearer '+os.environ['CONTROL_API_TOKEN']});print(json.load(u.urlopen(r,timeout=1800))['text'])"
```
`check` pulls newer images on the M920q/NAS (nothing running changes), so the first run is slow.

### 5. n8n
Import `n8n/lab-weekly-report.json`, paste the control token into both HTTP nodes, pick the same Discord
credential as "Jarvis - Actions" on the two Discord nodes, activate.

## Notes
- `GET http://lab-report:8000/last` returns the last report (for Jarvis later).
- Watchtower (monitor-only) and Diun now overlap with this; either can go once you trust the report.
- On Fedora, a kernel update with the NVIDIA akmod builds on the next boot; the first Lab Up after it is slower.
