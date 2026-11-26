#!/bin/bash
# lab-agent.sh - allowlisted maintenance agent for the weekly lab report.
#
# Installed as /usr/local/sbin/lab-agent.sh (root:root 0755) on m920q, nas, pve
# and deus-vult. lab-report reaches it over plain sshd with a key that can run
# nothing else (authorized_keys forced command):
#
#   restrict,command="sudo -n /usr/local/sbin/lab-agent.sh ssh \"$SSH_ORIGINAL_COMMAND\"" ssh-ed25519 AAAA... lab-report
#
# and one sudoers line (visudo -f /etc/sudoers.d/lab-agent):
#   <user> ALL=(root) NOPASSWD: /usr/local/sbin/lab-agent.sh *
#
# Verbs (anything else is refused):
#   ping            -> "ok"
#   check           pending OS packages + container image updates (pulls images, changes nothing running)
#   backups         result/age of the systemd backup units in BACKUP_UNITS
#   apply           apply OS + container updates, then verify the host; never reboots; pinned packages held.
#                   Verify: every container running before is running again (and healthy), none
#                   crash-looping, no new failed systemd units, VERIFY_CMDS pass. Containers the
#                   update stopped (e.g. a Docker daemon restart) are started once before judging.
#   apply-detached  same, in a transient systemd unit, result posted to Discord
#                   (for the M920q, whose update may restart lab-report itself)
#
# Output is tab-separated lines so no jq/python is needed on the host:
#   os_manager <apt|dnf|none>   os_update <pkg> <new-version>   container_update <dir> <service> <image>
#   reboot_required <reason>    backup <unit> <result> <exit-status> <finished-epoch>
#   applied <what>              failed <what>     error <message>
#   verify_ok <what>            verify_fail <what>   healed <what>
set -uo pipefail
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

# ---- defaults, overridden by /etc/lab-agent.conf ---------------------------
COMPOSE_DIRS=""                       # "dir[:file1,file2] ..." empty = no container checks
SKIP_SERVICES="nut-server nut-exporter"  # never pulled or recreated (NUT stays pinned)
PIN_PACKAGES="nut nut-client nut-server nut-monitor"  # apt-mark hold / dnf --exclude
OS_UPDATES=auto                       # auto | none (UGOS: vendor-managed -> none)
BACKUP_UNITS=""                       # e.g. "homelab-backup.service"
DISCORD_WEBHOOK_URL=""                # only used by apply-detached
VERIFY_WAIT=240                       # s for containers to be running/healthy again after apply
VERIFY_SETTLE=60                      # s of watching restart counts (crash-loop detection)
VERIFY_CMDS=()                        # host-specific checks, each must exit 0, e.g. ("upsc cyberpower@192.168.0.172 ups.status")
[ -r /etc/lab-agent.conf ] && . /etc/lab-agent.conf

HOST=$(hostname -s)
TAB=$'\t'
out() { local IFS="$TAB"; echo "$*"; }

os_manager() {
  [ "$OS_UPDATES" = none ] && { echo none; return; }
  if command -v apt-get >/dev/null; then echo apt
  elif command -v dnf >/dev/null; then echo dnf
  else echo none; fi
}

is_skipped() { case " $SKIP_SERVICES " in *" $1 "*) return 0;; esac; return 1; }

# ---- OS packages -----------------------------------------------------------
os_check() {
  local m; m=$(os_manager); out os_manager "$m"
  case "$m" in
    apt)
      apt-get update -qq >/dev/null 2>&1 || out error "apt-get update failed"
      apt list --upgradable 2>/dev/null | awk -F'[/ ]' 'NR>1 && NF>2 {print "os_update\t"$1"\t"$3}'
      ;;
    dnf)
      dnf -q check-update --refresh 2>/dev/null \
        | awk 'NF==3 && $1 ~ /\./ && $1 !~ /^(Obsoleting|Security|Last)/ {print "os_update\t"$1"\t"$2}'
      ;;
  esac
  reboot_check
}

reboot_check() {
  [ -f /var/run/reboot-required ] && out reboot_required "reboot-required flag set"
  local newest
  newest=$(ls /boot/vmlinuz-* 2>/dev/null | sed 's|/boot/vmlinuz-||' | grep -v rescue | sort -V | tail -1)
  [ -n "$newest" ] && [ "$newest" != "$(uname -r)" ] && out reboot_required "running $(uname -r), newest installed $newest"
}

hold_pins() {
  local p
  for p in $PIN_PACKAGES; do
    dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" && apt-mark hold "$p" >/dev/null
  done
}

os_apply() {
  local m; m=$(os_manager)
  case "$m" in
    apt)
      hold_pins
      local verb="upgrade --with-new-pkgs"
      command -v pveversion >/dev/null && verb="full-upgrade"   # Proxmox's documented upgrade path
      apt-get update -qq >/dev/null 2>&1
      if DEBIAN_FRONTEND=noninteractive apt-get -y -q \
           -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold $verb >/tmp/lab-agent-apt.log 2>&1; then
        out applied "apt $verb ($(grep -c '^Setting up' /tmp/lab-agent-apt.log) packages set up)"
        apt-get -y -q autoremove >/dev/null 2>&1
      else
        out failed "apt $verb: $(tail -n 3 /tmp/lab-agent-apt.log | tr '\n\t' '  ')"
        return 1
      fi
      ;;
    dnf)
      local ex="" p
      for p in $PIN_PACKAGES; do ex="$ex --exclude=$p*"; done
      if dnf -y -q upgrade --refresh $ex >/tmp/lab-agent-dnf.log 2>&1; then
        out applied "dnf upgrade"
      else
        out failed "dnf upgrade: $(tail -n 3 /tmp/lab-agent-dnf.log | tr '\n\t' '  ')"
        return 1
      fi
      ;;
    none) ;;
  esac
}

# ---- containers ------------------------------------------------------------
# Pulls newer images for running, non-skipped services and prints
# "<dir>\t<service>\t<image>" for every container whose image is now stale.
container_scan() {
  local spec dir files f svcs s cid ref run cur missing
  local -a fl
  for spec in $COMPOSE_DIRS; do
    dir=${spec%%:*}; files=()
    if [ "$spec" != "$dir" ]; then
      IFS=, read -ra fl <<<"${spec#*:}"
      for f in "${fl[@]}"; do files+=(-f "$f"); done
    fi
    [ -d "$dir" ] || { out error "compose dir $dir missing"; continue; }
    missing=""
    for f in "${fl[@]:-}"; do [ -n "$f" ] && [ ! -f "$dir/$f" ] && missing="$missing $f"; done
    [ -n "$missing" ] && { out error "compose file(s) missing in $dir:$missing"; fl=(); continue; }
    svcs=()
    while read -r s; do
      [ -n "$s" ] && ! is_skipped "$s" && svcs+=("$s")
    done < <(cd "$dir" && docker compose "${files[@]}" ps --services --status running 2>/dev/null)
    [ ${#svcs[@]} -eq 0 ] && continue
    (cd "$dir" && docker compose "${files[@]}" pull -q --ignore-pull-failures "${svcs[@]}" >/dev/null 2>&1)
    for s in "${svcs[@]}"; do
      cid=$(cd "$dir" && docker compose "${files[@]}" ps -q "$s" 2>/dev/null | head -1)
      [ -n "$cid" ] || continue
      ref=$(docker inspect -f '{{.Config.Image}}' "$cid" 2>/dev/null)
      run=$(docker inspect -f '{{.Image}}' "$cid" 2>/dev/null)
      cur=$(docker image inspect -f '{{.Id}}' "$ref" 2>/dev/null)
      [ -n "$cur" ] && [ "$cur" != "$run" ] && printf '%s\t%s\t%s\n' "$spec" "$s" "$ref"
    done
  done
}

containers_check() {
  container_scan | while IFS="$TAB" read -r spec s ref; do
    if [ "$spec" = error ]; then out error "$s"; else out container_update "${spec%%:*}" "$s" "$ref"; fi
  done
}

containers_apply() {
  local rc=0 spec s ref dir files f
  declare -A todo=()
  while IFS="$TAB" read -r spec s ref; do
    if [ "$spec" = error ]; then out failed "$s"; rc=1; else todo[$spec]+="$s "; fi
  done < <(container_scan)
  for spec in "${!todo[@]}"; do
    dir=${spec%%:*}; files=()
    if [ "$spec" != "$dir" ]; then
      IFS=, read -ra fl <<<"${spec#*:}"
      for f in "${fl[@]}"; do files+=(-f "$f"); done
    fi
    # shellcheck disable=SC2086
    if (cd "$dir" && docker compose "${files[@]}" up -d --no-build --no-deps ${todo[$spec]} >/tmp/lab-agent-dc.log 2>&1); then
      out applied "containers in $dir: ${todo[$spec]% }"
    else
      out failed "containers in $dir: $(tail -n 2 /tmp/lab-agent-dc.log | tr '\n\t' '  ')"; rc=1
    fi
  done
  [ ${#todo[@]} -gt 0 ] && docker image prune -f >/dev/null 2>&1
  return $rc
}

# ---- backups ---------------------------------------------------------------
backups() {
  local u res st ts epoch
  for u in $BACKUP_UNITS; do
    res=$(systemctl show -p Result --value "$u" 2>/dev/null)
    st=$(systemctl show -p ExecMainStatus --value "$u" 2>/dev/null)
    ts=$(systemctl show -p ExecMainExitTimestamp --value "$u" 2>/dev/null)
    epoch=0; [ -n "$ts" ] && epoch=$(date -d "$ts" +%s 2>/dev/null || echo 0)
    out backup "$u" "${res:-unknown}" "${st:-?}" "$epoch"
  done
}

# ---- verify ----------------------------------------------------------------
snapshot() {   # state captured before any change
  BEFORE_CTRS=$(docker ps --format '{{.Names}}' 2>/dev/null | sort)
  BEFORE_FAILED=$(systemctl list-units --state=failed --plain --no-legend 2>/dev/null | awk '{print $1}' | sort)
}

ctr_state() {  # "<status> <health>" e.g. "running healthy", "running none", "exited none"
  docker inspect -f '{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$1" 2>/dev/null \
    || echo "missing none"
}

not_back() {   # containers from the snapshot that are not running+healthy yet
  local n st
  for n in $BEFORE_CTRS; do
    st=$(ctr_state "$n")
    case "$st" in "running healthy"|"running none") ;; *) echo "$n:${st// /,}";; esac
  done
}

wait_back() {  # wait up to $1 s; "running starting" counts as not back yet
  local end=$((SECONDS + $1)) left
  while :; do
    left=$(not_back)
    [ -z "$left" ] || [ $SECONDS -ge $end ] && { echo "$left"; return; }
    sleep 10
  done
}

verify() {
  local rc=0 left n c1 c2 looping="" newfail cmd
  if command -v docker >/dev/null && [ -n "$BEFORE_CTRS" ]; then
    docker info >/dev/null 2>&1 || { out verify_fail "docker daemon not answering"; return 1; }
    left=$(wait_back "$VERIFY_WAIT")
    # containers stopped outright (not unhealthy/starting) get one start: that restores the
    # state from before the update, it is not a new change
    for n in $(grep -E ':(exited|created),' <<<"$left" | cut -d: -f1); do
      docker start "$n" >/dev/null 2>&1 && out healed "started $n (stopped by the update)"
    done
    [ -n "$left" ] && left=$(wait_back 120)
    for n in $left; do out verify_fail "container ${n%%:*} ${n#*:}"; rc=1; done
    # crash loops: restart counts must hold still for VERIFY_SETTLE seconds
    c1=$(for n in $BEFORE_CTRS; do docker inspect -f '{{.Name}} {{.RestartCount}}' "$n" 2>/dev/null; done)
    sleep "$VERIFY_SETTLE"
    c2=$(for n in $BEFORE_CTRS; do docker inspect -f '{{.Name}} {{.RestartCount}}' "$n" 2>/dev/null; done)
    looping=$(diff <(echo "$c1") <(echo "$c2") | awk '/^>/ {sub("/","",$2); print $2}')
    for n in $looping; do out verify_fail "container $n restarting (crash loop)"; rc=1; done
    [ $rc -eq 0 ] && out verify_ok "$(wc -w <<<"$BEFORE_CTRS") containers running, none restarting"
  fi
  newfail=$(comm -13 <(echo "$BEFORE_FAILED") <(systemctl list-units --state=failed --plain --no-legend 2>/dev/null | awk '{print $1}' | sort) | grep . || true)
  for n in $newfail; do out verify_fail "systemd unit $n failed"; rc=1; done
  for cmd in "${VERIFY_CMDS[@]}"; do
    if timeout 30 bash -c "$cmd" >/dev/null 2>&1; then out verify_ok "$cmd"
    else out verify_fail "check failed: $cmd"; rc=1; fi
  done
  return $rc
}

# ---- apply -----------------------------------------------------------------
apply() {
  local notify=${1:-} log rc=0
  exec 9>/run/lab-agent.lock
  flock -n 9 || { out error "another apply is running"; return 1; }
  snapshot
  log=$( { os_apply || echo "__fail"; containers_apply || echo "__fail"; verify || echo "__fail"; reboot_check; } 2>&1 )
  grep -q '^__fail' <<<"$log" && rc=1
  log=$(grep -v '^__fail' <<<"$log")
  [ -z "$log" ] && log="applied nothing (already current)"
  echo "$log"
  if [ "$notify" = --notify ] && [ -n "$DISCORD_WEBHOOK_URL" ]; then
    local icon="✅"; [ $rc -ne 0 ] && icon="⚠️"
    local msg="$icon **Updates on $HOST**"$'\n'"$(sed 's/\t/ /g' <<<"$log")"
    msg=$(printf '%s' "$msg" | sed 's/\\/\\\\/g; s/"/\\"/g' | awk 'BEGIN{ORS="\\n"} {print}')
    curl -fsS -m 15 -H 'Content-Type: application/json' \
      -d "{\"username\":\"Lab Report\",\"content\":\"${msg:0:1900}\"}" "$DISCORD_WEBHOOK_URL" >/dev/null || true
  fi
  return $rc
}

# ---- dispatch --------------------------------------------------------------
if [ "${1:-}" = ssh ]; then set -- "${2:-}"; fi   # forced-command entry: one word, no extra args
verb=${1:-}
case "$verb" in
  ping)            echo ok ;;
  check)           os_check; containers_check ;;
  backups)         backups ;;
  apply)           apply "${2:-}" ;;
  apply-detached)
    unit="lab-agent-apply-$(date +%s)"
    systemd-run --quiet --collect --unit="$unit" /usr/local/sbin/lab-agent.sh apply --notify \
      && out applied "started $unit on $HOST (result will be posted to Discord)" \
      || { out failed "systemd-run on $HOST"; exit 1; } ;;
  *) out error "refused: '${verb}'"; exit 2 ;;
esac
