"""
control-api
-----------
Small Flask service that gives a single control surface for:
  - Proxmox VE: list VMs/LXCs, start/stop/shutdown
  - iDRAC (Redfish): read power state + temps, power on/off/restart the T430
  - X570 (deus-vult): Wake-on-LAN + Tailscale-SSH shutdown at /x570/ (no token;
    see x570_control.py)

This is intentionally minimal — no database, no session auth. It relies on
network-level access control (Tailscale ACLs / not exposed to the internet)
plus a shared bearer token for anything that changes state (POST routes).

Env vars (see .env / docker-compose.yml):
  CONTROL_API_TOKEN      shared secret required as `Authorization: Bearer <token>`
                         on every POST route

  PROXMOX_HOST           e.g. https://192.168.1.50:8006
  PROXMOX_NODE           e.g. pve  (the node name inside Proxmox, not hostname)
  PROXMOX_TOKEN_ID       e.g. automation@pve!homepage
  PROXMOX_TOKEN_SECRET   the API token secret (UUID) shown once at creation

  IDRAC_HOST             e.g. https://192.168.1.60
  IDRAC_USER             iDRAC local user (root by default, change this)
  IDRAC_PASS             iDRAC password
  IDRAC_SYSTEM_ID        usually "System.Embedded.1" — safe to leave default

  X570_MAC / X570_LAN_IP / X570_TS_IP / X570_USER   see x570_control.py
"""

import os
import requests
import urllib3

from flask import Flask, jsonify, request, render_template

from x570_control import bp as x570_bp, fedora_up as x570_fedora_up

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)
app.register_blueprint(x570_bp)  # /x570/ - token-free by design (tailnet-only)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
CONTROL_API_TOKEN = os.environ.get("CONTROL_API_TOKEN", "")

PROXMOX_HOST = os.environ.get("PROXMOX_HOST", "").rstrip("/")
PROXMOX_NODE = os.environ.get("PROXMOX_NODE", "pve")
PROXMOX_TOKEN_ID = os.environ.get("PROXMOX_TOKEN_ID", "")
PROXMOX_TOKEN_SECRET = os.environ.get("PROXMOX_TOKEN_SECRET", "")

IDRAC_HOST = os.environ.get("IDRAC_HOST", "").rstrip("/")
IDRAC_USER = os.environ.get("IDRAC_USER", "root")
IDRAC_PASS = os.environ.get("IDRAC_PASS", "")
IDRAC_SYSTEM_ID = os.environ.get("IDRAC_SYSTEM_ID", "System.Embedded.1")

REQUEST_TIMEOUT = 8  # seconds — fail fast rather than hang the page
# iDRAC8 can take 10-20 s to answer, especially Reset actions; 8 s caused false failures
IDRAC_TIMEOUT = int(os.environ.get("IDRAC_TIMEOUT", "30"))


def require_token():
    """Simple bearer-token guard for state-changing routes."""
    if not CONTROL_API_TOKEN:
        # If no token is configured, refuse to allow control actions at all —
        # safer default than silently running unauthenticated.
        return False
    auth = request.headers.get("Authorization", "")
    return auth == f"Bearer {CONTROL_API_TOKEN}"


# ---------------------------------------------------------------------------
# Proxmox helpers
# ---------------------------------------------------------------------------
def proxmox_headers():
    return {"Authorization": f"PVEAPIToken={PROXMOX_TOKEN_ID}={PROXMOX_TOKEN_SECRET}"}


def proxmox_get(path):
    url = f"{PROXMOX_HOST}/api2/json{path}"
    r = requests.get(url, headers=proxmox_headers(), verify=False, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json().get("data")


def proxmox_post(path):
    url = f"{PROXMOX_HOST}/api2/json{path}"
    r = requests.post(url, headers=proxmox_headers(), verify=False, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return r.json().get("data")


# ---------------------------------------------------------------------------
# iDRAC / Redfish helpers
# ---------------------------------------------------------------------------
def idrac_auth():
    return (IDRAC_USER, IDRAC_PASS)


def idrac_get(path):
    url = f"{IDRAC_HOST}{path}"
    r = requests.get(url, auth=idrac_auth(), verify=False, timeout=IDRAC_TIMEOUT)
    r.raise_for_status()
    return r.json()


def idrac_reset(reset_type):
    """
    reset_type one of: On, ForceOff, GracefulShutdown, GracefulRestart,
    ForceRestart, PushPowerButton
    """
    url = f"{IDRAC_HOST}/redfish/v1/Systems/{IDRAC_SYSTEM_ID}/Actions/ComputerSystem.Reset"
    r = requests.post(
        url,
        json={"ResetType": reset_type},
        auth=idrac_auth(),
        verify=False,
        timeout=IDRAC_TIMEOUT,
        headers={"Content-Type": "application/json"},
    )
    r.raise_for_status()
    return {"ok": True, "action": reset_type}


# ---------------------------------------------------------------------------
# Routes — pages
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return render_template("index.html")


# ---------------------------------------------------------------------------
# Routes — status (read-only, no token required)
# ---------------------------------------------------------------------------
@app.route("/api/status")
def status():
    result = {"proxmox": None, "idrac": None, "x570": None, "errors": []}

    if PROXMOX_HOST and PROXMOX_TOKEN_ID:
        try:
            vms = proxmox_get(f"/nodes/{PROXMOX_NODE}/qemu")
            lxcs = proxmox_get(f"/nodes/{PROXMOX_NODE}/lxc")
            result["proxmox"] = {"vms": vms, "lxcs": lxcs}
        except Exception as e:
            result["errors"].append(f"proxmox: {e}")

    if IDRAC_HOST and IDRAC_PASS:
        try:
            sys_info = idrac_get(f"/redfish/v1/Systems/{IDRAC_SYSTEM_ID}")
            result["idrac"] = {
                "power_state": sys_info.get("PowerState"),
                "health": sys_info.get("Status", {}).get("Health"),
            }
        except Exception as e:
            result["errors"].append(f"idrac: {e}")

    try:
        result["x570"] = {"fedora_up": x570_fedora_up()}
    except Exception as e:
        result["errors"].append(f"x570: {e}")

    return jsonify(result)


# ---------------------------------------------------------------------------
# Routes — actions (token required)
# ---------------------------------------------------------------------------
@app.route("/api/idrac/power", methods=["POST"])
def idrac_power():
    action = (request.json or {}).get("action")
    # Power-on needs no token (by request); off/restart actions still do.
    if action not in {"On", "GracefulShutdown"} and not require_token():
        return jsonify({"error": "unauthorized"}), 401
    allowed = {"On", "ForceOff", "GracefulShutdown", "GracefulRestart", "ForceRestart"}
    if action not in allowed:
        return jsonify({"error": f"action must be one of {sorted(allowed)}"}), 400

    try:
        return jsonify(idrac_reset(action))
    except Exception as e:
        return jsonify({"error": str(e)}), 502


@app.route("/api/proxmox/vm/<vmid>/<action>", methods=["POST"])
def proxmox_vm_action(vmid, action):
    if not require_token():
        return jsonify({"error": "unauthorized"}), 401

    allowed = {"start", "stop", "shutdown", "reboot"}
    if action not in allowed:
        return jsonify({"error": f"action must be one of {sorted(allowed)}"}), 400

    try:
        data = proxmox_post(f"/nodes/{PROXMOX_NODE}/qemu/{vmid}/status/{action}")
        return jsonify({"ok": True, "task": data})
    except Exception as e:
        return jsonify({"error": str(e)}), 502


# ---------------------------------------------------------------------------
# Lab Up / Lab Down (sequenced on-demand tier power) - see lab_control.py
# ---------------------------------------------------------------------------
from lab_control import make_bp as make_lab_bp  # noqa: E402

app.register_blueprint(make_lab_bp(
    require_token=require_token,
    proxmox_get=proxmox_get,
    proxmox_post=proxmox_post,
    idrac_get=idrac_get,
    idrac_reset=idrac_reset,
    idrac_system_id=IDRAC_SYSTEM_ID,
    proxmox_node=PROXMOX_NODE,
))


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
