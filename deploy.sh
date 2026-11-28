#!/bin/bash
# deploy.sh - update this host's stack from GitHub and apply it.
#
#   ./deploy.sh              pull + docker compose up -d --build (everything)
#   ./deploy.sh n8n nginx    pull + rebuild/recreate only those services
#   ./deploy.sh --check      show what a pull would bring in, change nothing
#
# Runs in the checkout it lives in (/opt/homelab/homelab-bundle on the M920q,
# /volume1/docker/homelab-bundle on the NAS). Which compose files apply comes from
# COMPOSE_FILE in that host's .env (docker-compose.m920q.yml:docker-compose.m920q.override.yml
# or docker-compose.nas.yml). On the NAS, run it with sudo (docker needs root there);
# git still runs as you, and if git isn't installed it runs in the alpine/git image.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

[ -f .env ] || { echo "no .env in $PWD"; exit 1; }
grep -q '^COMPOSE_FILE=' .env || { echo "COMPOSE_FILE is not set in .env (see .env.example)"; exit 1; }

as_user=()
[ -n "${SUDO_USER:-}" ] && as_user=(sudo -u "$SUDO_USER")
git_() {
  if command -v git >/dev/null; then
    "${as_user[@]}" git "$@"
  else
    docker run --rm -u "${SUDO_UID:-$(id -u)}:${SUDO_GID:-$(id -g)}" -e HOME=/tmp \
      -v "$PWD:/repo" -w /repo alpine/git -c safe.directory=/repo "$@"
  fi
}

# tracked files edited in place on this host would be overwritten or block the pull
if [ -n "$(git_ status --porcelain --untracked-files=no)" ]; then
  echo "Local changes to tracked files on $(hostname -s):"
  git_ status --short --untracked-files=no
  echo "Commit them from your laptop (or 'git checkout -- <file>' here to drop them), then rerun."
  exit 1
fi

git_ fetch -q origin main
before=$(git_ rev-parse HEAD); after=$(git_ rev-parse origin/main)
if [ "$before" = "$after" ]; then
  echo "Already at $(git_ log --oneline -1)"
else
  echo "Incoming:"; git_ --no-pager log --oneline "$before..$after"
  git_ --no-pager diff --stat "$before" "$after"
fi
[ "${1:-}" = --check ] && exit 0
[ "$before" = "$after" ] || git_ merge -q --ff-only origin/main

docker compose up -d --build "$@"
bad=$(docker compose ps -a --format '{{.Name}} {{.Status}}' | grep -vE ' Up ' | grep -v 'kuma-seed' || true)
if [ -n "$bad" ]; then echo "Not running:"; echo "$bad"; exit 1; fi
echo "Deployed $(git_ log --oneline -1) on $(hostname -s); all containers up."
