# Deploying

The stack folders are git checkouts of this repo:

| Host  | Folder                          | `COMPOSE_FILE` in `.env`                                        |
|-------|---------------------------------|-----------------------------------------------------------------|
| M920q | `/opt/homelab/homelab-bundle`   | `docker-compose.m920q.yml:docker-compose.m920q.override.yml`    |
| NAS   | `/volume1/docker/homelab-bundle`| `docker-compose.nas.yml`                                        |

Change things on the laptop, commit, push, then on the host:

```bash
./deploy.sh --check        # what would come in
./deploy.sh                # pull + docker compose up -d --build
./deploy.sh n8n            # only rebuild/recreate n8n
```
(NAS: `sudo ./deploy.sh` - docker needs root there; git still runs as you, or in the
`alpine/git` image if UGOS has no git.)

Never edit tracked files on a host: `deploy.sh` refuses to run while a tracked file has local
changes, so nothing gets silently overwritten. Host-level files under `host/` (systemd units,
scripts in /usr/local/sbin, /etc configs) are still installed by hand from the checkout.

Not in git (see `.gitignore`): `.env`, SSH keys (`control-api/ssh`, `lab-report/ssh`), certs,
logs, data folders (`adguard/`, `tailscale/state/`, `nut-server/flag/`), Homepage's template files.

## Converting a folder that predates git (done once per host)
```bash
cd <folder>
tar czf ../homelab-bundle-pre-git.tgz --exclude=./adguard/work .   # rollback copy
git init -q -b main && git remote add origin https://github.com/Dubstep-626/homelab
git fetch -q origin main && git reset -q origin/main && git branch -q -u origin/main
git status --short          # review: ' M' = live differs from repo, ' D' = in repo not here, '??' = untracked
```
Resolve every ` M` before going on (the repo should win only once it has the live change),
then `git checkout -- .`, add `COMPOSE_FILE` to `.env`, check `docker compose up -d --dry-run`
recreates nothing, and remove the old per-host `docker-compose.yml` copies.
