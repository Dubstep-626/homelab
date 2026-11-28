# HTTPS for the lab

Every lab site answers on `https://<name>.lab.mitchthedubs.com` (port 443 on the M920q) with one
wildcard Let's Encrypt certificate. The old `http://<name>.lab.lan:8080` addresses keep working.

- **No public exposure.** Ownership is proven with a DNS-01 TXT record that certbot creates and
  removes at Cloudflare; no ports are opened and Cloudflare holds no records for lab hosts.
  Names resolve only through AdGuard (tailnet DNS): `*.lab.mitchthedubs.com` -> the M920q.
- **Renewal.** The `certbot` container runs `certbot renew` every 12 h; nginx reloads every 6 h.
- **Certificate Transparency.** `*.lab.mitchthedubs.com` appears in public CT logs; the wildcard
  keeps individual host names out of them.
- Files (not in git): `certbot/cloudflare.ini` (API token, chmod 600), `certbot/letsencrypt/`.

## Cloudflare token
Cloudflare dashboard -> My Profile -> API Tokens -> Create Token -> **Edit zone DNS** template ->
Zone Resources: *Include / Specific zone / mitchthedubs.com* -> (optional) Client IP Address
Filtering: your home public IP -> Create. On the M920q:
```bash
cd /opt/homelab/homelab-bundle && mkdir -p certbot && umask 077
printf 'dns_cloudflare_api_token = %s\n' 'PASTE_TOKEN' > certbot/cloudflare.ini
```

## First certificate
```bash
docker compose run --rm --entrypoint certbot certbot certonly --dns-cloudflare \
  --dns-cloudflare-credentials /cloudflare.ini --dns-cloudflare-propagation-seconds 30 \
  -d lab.mitchthedubs.com -d '*.lab.mitchthedubs.com' \
  --email YOU@example.com --agree-tos --no-eff-email -n
```
Then `./deploy.sh` (nginx needs the certificate to exist before it starts with the new config).

## Apps that need to know their new name (host-side settings)
- Homepage: add `home.lab.mitchthedubs.com` to `HOMEPAGE_ALLOWED_HOSTS` in `.env`.
- NetBox: add `https://netbox.lab.mitchthedubs.com` to `CSRF_TRUSTED_ORIGINS` in
  `/opt/homelab/netbox/env/netbox.env`, then restart NetBox.
- n8n and Grafana: set in the compose files (`N8N_HOST`/`WEBHOOK_URL`, `GF_SERVER_ROOT_URL`).
  n8n now uses secure cookies, so log in through the https address.
