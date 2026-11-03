# Restore runbook

Rebuild either host's stack from the encrypted weekly backup in Google Drive. The NAS archive also carries the M920q's latest nightly export in `m920q-backup/`.

## What you need (none of this is in the backup)

- rclone on any machine
- The two rclone crypt passwords (password and salt), from the password manager
- Google login, plus the OAuth client ID and secret used for the `gdrive` remote (scope `drive.file`)
- Tailscale admin access for a fresh auth key

`drive.file` only sees files created by the same client ID. Replace the client and the old backups become invisible, so keep the client ID and secret safe.

## 1. Rebuild rclone config

1. `rclone config`: create remote `gdrive` (type drive, scope `drive.file`, your client ID and secret), authorize in a browser.
2. Create remote `gdrive-crypt` (type crypt) over the same Drive folder with both passwords. Wrong passwords show garbage filenames instead of an error.
3. Check: `rclone ls gdrive-crypt:` shows `homelab-YYYYMMDD-HHMM.tar` files (newest 3 kept).

## 2. Fetch and unpack the newest archive

```bash
mkdir -p ~/restore && cd ~/restore
rclone copy gdrive-crypt:homelab-YYYYMMDD-HHMM.tar . -P
tar -xf homelab-*.tar
ls            # stack.tar.gz  volumes/  host/
```

Archive layout: `stack.tar.gz` (the stack directory without `rclone/`), `volumes/<name>.tar.gz` (one per Docker volume), `host/` (scripts and `.service` files).

## 3. Rebuild the stack

1. Extract: `sudo tar -xzf stack.tar.gz -C /volume1/docker`
2. Recreate `.env` from the password manager if it isn't in the extract (`.env.example` lists every variable; use a fresh Tailscale auth key).
3. Create each volume and restore its data:
   ```bash
   docker volume create homelab-bundle_uptime-kuma-data
   docker run --rm -v homelab-bundle_uptime-kuma-data:/v -v "$PWD/volumes":/in alpine \
     tar xzf /in/homelab-bundle_uptime-kuma-data.tar.gz -C /v
   ```
   Repeat per file in `volumes/`.
4. Validate and start: `docker compose --project-directory /volume1/docker/homelab-bundle config -q && docker compose up -d`

## 4. Restore host-level pieces

```bash
sudo cp host/homelab-*.sh /usr/local/sbin/ && sudo chmod +x /usr/local/sbin/homelab-*.sh
sudo cp host/homelab-*.service host/homelab-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now homelab-backup.timer homelab-ups-watch.service homelab-dns-guard.service
```

Recreate `/etc/homelab-backup.env` (chmod 600) with `KUMA_PUSH_URL` from Uptime Kuma's push monitor. Rejoin Tailscale and disable key expiry for the node.

## 5. Verify

- Uptime Kuma: all monitors green; send a Discord test notification.
- Homepage tiles live; Prometheus targets up (the T430 job is down whenever that host is off, which is expected).
- Run `sudo systemctl start homelab-backup.service` once and confirm the push heartbeat arrives.

## Rebuilding the M920q

The NAS archive's `stack.tar.gz` contains `m920q-backup/`, which holds the M920q's own `stack.tar.gz` plus one tarball per volume (Prometheus is not backed up; it starts empty).

1. Fresh Debian 13, Docker from Docker's apt repo, Tailscale (disable key expiry), then `sudo mkdir -p /opt/homelab /var/log/nginx`.
2. `sudo tar -xzf stack.tar.gz -C /opt/homelab && sudo chown -R $USER: /opt/homelab/homelab-bundle`
3. Restore each volume the same way as step 3 above, using `homelab-bundle_<name>` as the volume name.
4. `cd /opt/homelab/homelab-bundle && docker compose up -d`
5. Re-create `host/m920q/`: `nut-client` with `upsmon.conf.example`, the backup script and timer, and the `/root/.ssh/id_ed25519_nas` key (add its public key to the NAS user's `authorized_keys`).
6. If the M920q's tailnet IP changed, update `address=/lab.lan/...` in the NAS's `dnsmasq/dnsmasq.conf` and recreate dnsmasq.
