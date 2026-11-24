"""Lab Up / Lab Down - sequenced power control for the on-demand tier.

Scope: the T430 (Proxmox guests first, then the host) and the X570.
The always-on tier (M920q, NAS) is never touched; NUT handles a full power loss.

Routes (blueprint, url_prefix /lab):
  GET  /lab/                page with Up / Down / dry-run buttons and a live log
  GET  /lab/status          JSON for Homepage + Jarvis: state, t430, x570, last run log
  POST /lab/up[?dry_run=1]  no token (non-destructive, same rule as T430 On / X570 wake)
  POST /lab/down[?dry_run=1]  bearer token required, except for a dry run

Lab Down:  X570 poweroff (Tailscale SSH) -> graceful shutdown of every running
           Proxmox VM/LXC (wait up to VM_STOP_WAIT) -> T430 GracefulShutdown via
           Redfish (poll PowerState until Off, up to HOST_OFF_WAIT).
Lab Up:    WoL the X570 + Redfish On for the T430 -> wait for the Proxmox API.
           Guests flagged onboot are started by Proxmox itself; nothing else is
           started, so Lab Up never brings up a VM you left off on purpose.

Runs in a background thread (a full Lab Down takes minutes, longer than the
gunicorn worker timeout). Progress lives in a JSON file under /tmp so every
gunicorn worker serves the same status; an O_EXCL lock file stops two runs
from overlapping (a lock older than LOCK_STALE is treated as abandoned).

Register in app.py (after the Proxmox/iDRAC helpers are defined):
    from lab_control import make_bp as make_lab_bp
    app.register_blueprint(make_lab_bp(
        require_token=require_token, proxmox_get=proxmox_get, proxmox_post=proxmox_post,
        idrac_get=idrac_get, idrac_reset=idrac_reset, idrac_system_id=IDRAC_SYSTEM_ID))
"""
import json
import os
import subprocess
import threading
import time
from datetime import datetime

import requests
from flask import Blueprint, jsonify, request, render_template_string

from x570_control import SSH_OPTS, TS_IP, USER, fedora_up, send_wol, set_gaming

STATE_FILE = "/tmp/lab_run.json"
LOCK_FILE = "/tmp/lab_run.lock"
LOCK_STALE = 20 * 60          # s
VM_STOP_WAIT = 180            # s, all guests together
HOST_OFF_WAIT = 360           # s, T430 GracefulShutdown -> PowerState Off
PVE_UP_WAIT = 900             # s, T430 power on -> Proxmox API answering (POST is slow)
X570_UP_WAIT = 240            # s, WoL -> Fedora reachable
POLL = 10                     # s
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")  # optional: post a summary of real runs


def _now():
    return datetime.now().strftime("%H:%M:%S")


def _read_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _write_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE_FILE)


def _take_lock():
    try:
        if time.time() - os.path.getmtime(LOCK_FILE) > LOCK_STALE:
            os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass
    try:
        os.close(os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
        return True
    except FileExistsError:
        return False


def _drop_lock():
    try:
        os.remove(LOCK_FILE)
    except FileNotFoundError:
        pass


def make_bp(*, require_token, proxmox_get, proxmox_post, idrac_get, idrac_reset,
            idrac_system_id, proxmox_node=None):
    node = proxmox_node or os.environ.get("PROXMOX_NODE", "pve")
    bp = Blueprint("lab", __name__, url_prefix="/lab")

    # ---------------------------------------------------------------- probes
    def t430_power(tries=3):
        for i in range(tries):
            try:
                return idrac_get(f"/redfish/v1/Systems/{idrac_system_id}").get("PowerState", "Unknown")
            except Exception:
                if i < tries - 1:
                    time.sleep(3)
        return "Unknown"

    def send_reset(reset_type, log):
        """iDRAC8 often accepts a Reset but answers late. A timeout is not a failure:
        log it and let the caller's power-state polling decide."""
        try:
            idrac_reset(reset_type)
        except requests.exceptions.Timeout:
            log(f"T430: iDRAC slow to answer {reset_type} - assuming accepted, watching power state")

    def pve_up():
        try:
            proxmox_get("/version")
            return True
        except Exception:
            return False

    def running_guests():
        guests = []
        for kind in ("qemu", "lxc"):
            for g in proxmox_get(f"/nodes/{node}/{kind}") or []:
                if g.get("status") == "running":
                    guests.append((kind, str(g["vmid"]), g.get("name") or str(g["vmid"])))
        return guests

    # ---------------------------------------------------------------- runner
    def run(action, dry, st):

        def log(msg):
            st["log"].append(f"{_now()}  {msg}")
            _write_state(st)

        def wait(check, timeout, what):
            end = time.time() + timeout
            while time.time() < end:
                if check():
                    return True
                time.sleep(POLL)
            log(f"WARN: timed out after {timeout}s waiting for {what}")
            return False

        problems = 0
        try:
            log(f"Lab {action.upper()}{' (DRY RUN - nothing will change)' if dry else ''}")
            if action == "down":
                problems += _down(log, wait, dry)
            else:
                problems += _up(log, wait, dry)
            st["result"] = "ok" if not problems else f"done with {problems} warning(s)"
        except Exception as e:  # never leave the lock held or the state stuck
            st["result"] = f"error: {e}"
            log(f"ERROR: {e}")
        finally:
            st["running"] = False
            st["finished"] = _now()
            log(f"Finished: {st['result']}")
            _drop_lock()
            if DISCORD_WEBHOOK_URL and not dry:
                icon = "\u2705" if st["result"] == "ok" else "\u26a0\ufe0f"
                body = "\n".join(st["log"][-12:])
                try:
                    requests.post(DISCORD_WEBHOOK_URL, timeout=10, json={
                        "username": "Lab Power",
                        "content": f"{icon} **Lab {action.title()}**: {st['result']}\n```{body[:1800]}```"})
                except Exception:
                    pass

    def _down(log, wait, dry):
        warn = 0
        # 1. X570
        if fedora_up():
            log("X570: up in Fedora -> poweroff over Tailscale SSH")
            if not dry:
                set_gaming(False)
                try:
                    r = subprocess.run(["ssh", *SSH_OPTS, f"{USER}@{TS_IP}",
                                        "sudo", "/usr/bin/systemctl", "poweroff"],
                                       capture_output=True, text=True, timeout=20)
                    if r.returncode not in (0, 255):
                        log(f"WARN: X570 poweroff failed: {r.stderr.strip()[:150]}"); warn += 1
                except subprocess.TimeoutExpired:
                    pass  # link dropped as it powered off
                if not wait(lambda: not fedora_up(), 90, "X570 to go offline"):
                    warn += 1
                else:
                    log("X570: off")
        else:
            log("X570: already off (or not in Fedora) - skipped")

        # 2-3. T430
        power = t430_power()
        if power == "Off":
            log("T430: already off - skipped")
            return warn
        log(f"T430: power state {power}")

        if pve_up():
            guests = running_guests()
            if guests:
                for kind, vmid, name in guests:
                    log(f"Proxmox: shutdown {kind} {vmid} ({name})")
                    if not dry:
                        proxmox_post(f"/nodes/{node}/{kind}/{vmid}/status/shutdown")
                if not dry:
                    if wait(lambda: not running_guests(), VM_STOP_WAIT, "guests to stop"):
                        log("Proxmox: all guests stopped")
                    else:
                        left = ", ".join(f"{v}({n})" for _, v, n in running_guests())
                        log(f"WARN: still running: {left} - host shutdown will stop them")
                        warn += 1
            else:
                log("Proxmox: no running guests")
        else:
            log("WARN: Proxmox API not answering - host shutdown will handle any guests")
            warn += 1

        log("T430: Redfish GracefulShutdown")
        if not dry:
            send_reset("GracefulShutdown", log)
            if wait(lambda: t430_power() == "Off", HOST_OFF_WAIT, "T430 PowerState Off"):
                log("T430: off")
            else:
                warn += 1
        return warn

    def _up(log, wait, dry):
        warn = 0
        x_up = fedora_up()
        power = t430_power()

        if x_up:
            log("X570: already up - skipped")
        else:
            log("X570: sending Wake-on-LAN")
            if not dry:
                set_gaming(False)
                send_wol()

        if power == "On":
            log("T430: already on")
        else:
            log(f"T430: power state {power} -> Redfish On")
            if not dry:
                send_reset("On", log)

        if dry:
            return warn
        if not x_up:
            if wait(fedora_up, X570_UP_WAIT, "X570 to reach Fedora"):
                log("X570: up")
            else:
                log("X570: not in Fedora - possible boot hang (needs reset) or WoL not armed")
                warn += 1
        if wait(pve_up, PVE_UP_WAIT, "Proxmox API"):
            log("Proxmox: API up - onboot guests start on their own")
            try:
                g = running_guests()
                log("Proxmox: running: " + (", ".join(n for _, _, n in g) if g else "none yet"))
            except Exception:
                pass
        else:
            warn += 1
        return warn

    def start(action):
        dry = request.args.get("dry_run") in ("1", "true", "yes")
        if action == "down" and not dry and not require_token():
            return jsonify(error="unauthorized"), 401
        if not _take_lock():
            return jsonify(error="a lab run is already in progress", status=_read_state()), 409
        st = {"action": action, "dry_run": dry, "running": True, "result": None,
              "started": _now(), "finished": None, "log": []}
        _write_state(st)  # before the thread starts, so /status never shows the previous run
        threading.Thread(target=run, args=(action, dry, st), daemon=True).start()
        return jsonify(ok=True, action=action, dry_run=dry, status_url="/lab/status"), 202

    # ---------------------------------------------------------------- routes
    @bp.post("/up")
    def lab_up():
        return start("up")

    @bp.post("/down")
    def lab_down():
        return start("down")

    @bp.get("/status")
    def lab_status():
        st = _read_state()
        lock_live = os.path.exists(LOCK_FILE) and time.time() - os.path.getmtime(LOCK_FILE) <= LOCK_STALE
        if st.get("running") and not lock_live:
            st["running"], st["result"] = False, "interrupted (control-api restarted mid-run)"
        if st.get("running"):
            state = f"Lab {st['action'].title()} running"
        elif st:
            state = f"Last: {st['action'].title()}{' (dry)' if st.get('dry_run') else ''} {st.get('result')} at {st.get('finished')}"
        else:
            state = "Idle"
        return jsonify(state=state, t430=t430_power(),
                       x570="Up" if fedora_up() else "Off", run=st)

    @bp.get("/")
    def lab_page():
        return render_template_string(PAGE)

    return bp


PAGE = """<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Lab power</title>
<style>
 body{font-family:system-ui,sans-serif;background:#111827;color:#e5e7eb;display:flex;justify-content:center;padding:2rem}
 .card{background:#1f2937;border-radius:12px;padding:1.5rem;max-width:480px;width:100%}
 h1{font-size:1.2rem;margin:0 0 .5rem}.s{margin:.25rem 0;font-weight:600}
 button{font-size:1rem;padding:.7rem 1.1rem;border:0;border-radius:8px;margin:.25rem .25rem .25rem 0;cursor:pointer;color:#fff}
 .up{background:#059669}.down{background:#dc2626}.dry{background:#374151}
 pre{background:#0b1220;border-radius:8px;padding:.75rem;font-size:.8rem;white-space:pre-wrap;min-height:6rem;max-height:22rem;overflow:auto}
 .hint{color:#9ca3af;font-size:.85rem}
</style></head><body><div class="card">
<h1>&#9889; Lab power &middot; on-demand tier</h1>
<div class="s" id="state">loading...</div>
<div class="hint" id="hosts"></div>
<p>
 <button class="up" onclick="go('up',false)">Lab Up</button>
 <button class="down" onclick="go('down',false)">Lab Down</button>
 <button class="dry" onclick="go('up',true)">Dry run: Up</button>
 <button class="dry" onclick="go('down',true)">Dry run: Down</button>
</p>
<pre id="log"></pre>
<p class="hint">Down: X570 off, Proxmox guests shut down cleanly, then the T430. The M920q and NAS stay up.
Up: wakes the X570 and powers on the T430; guests marked "start at boot" come up on their own.</p>
</div><script>
let token=null;
async function go(action,dry){
  const h={};
  if(action==='down'&&!dry){
    if(!confirm('Shut down the X570, all Proxmox guests and the T430?'))return;
    token=token||prompt('Control token:'); if(!token)return;
    h['Authorization']='Bearer '+token;
  }
  const r=await fetch('/lab/'+action+(dry?'?dry_run=1':''),{method:'POST',headers:h});
  const d=await r.json();
  if(!r.ok){ if(r.status===401)token=null; alert(d.error||'failed'); }
  poll();
}
async function poll(){
  try{
    const d=await (await fetch('/lab/status')).json();
    document.getElementById('state').textContent=d.state;
    document.getElementById('hosts').textContent='T430: '+d.t430+'  ·  X570: '+d.x570;
    document.getElementById('log').textContent=((d.run&&d.run.log)||[]).join('\\n');
  }catch(e){}
}
poll(); setInterval(poll,4000);
</script></body></html>"""
