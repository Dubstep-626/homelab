"""X570 (deus-vult) power control - drop-in blueprint for control-api.

No token on these routes (by request). Access control = "you are on the tailnet",
same trust boundary as the rest of the nginx vhosts.

Wake : WoL magic packet. control-api stays on the `homelab` bridge network, so a
       LAN broadcast can't leave the container. Instead the packet is sent UNICAST
       to the X570's reserved LAN IP; a permanent ARP entry on the M920q host
       (host/m920q/homelab-x570-neigh.service) lets that frame reach the NIC even
       while the PC is off. A broadcast copy is also sent (harmless if dropped).
Stop : Tailscale SSH -> `sudo systemctl poweroff`. No SSH keys: the M920q's tailnet
       identity authenticates. Outbound from the bridge is NATed out tailscale0.

Register in app.py:
    from x570_control import bp as x570_bp
    app.register_blueprint(x570_bp)
If app.py has a global @app.before_request token check, exempt this blueprint:
    if request.blueprint == "x570": return None
"""
import os
import socket
import subprocess

from flask import Blueprint, jsonify, render_template_string

bp = Blueprint("x570", __name__, url_prefix="/x570")

MAC = os.getenv("X570_MAC", "d4:5d:64:56:41:b1")       # Intel I211 port (cabled)
LAN_IP = os.getenv("X570_LAN_IP", "192.168.0.X")        # UniFi DHCP reservation
BROADCAST = os.getenv("X570_BROADCAST", "192.168.0.255")
TS_IP = os.getenv("X570_TS_IP", "100.x.y.z")            # tailscale ip -4 (on deus-vult)
USER = os.getenv("X570_USER", "mdubin")

SSH_OPTS = ["-o", "BatchMode=yes",
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=5"]


def _magic_packet(mac: str) -> bytes:
    return b"\xff" * 6 + bytes.fromhex(mac.replace(":", "").replace("-", "")) * 16


def send_wol() -> None:
    pkt = _magic_packet(MAC)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        for target in (LAN_IP, BROADCAST):
            for port in (9, 7):
                try:
                    s.sendto(pkt, (target, port))
                except OSError:
                    pass


def fedora_up(timeout: float = 1.5) -> bool:
    """Fedora is up on the tailnet when Tailscale SSH answers on :22."""
    try:
        socket.create_connection((TS_IP, 22), timeout=timeout).close()
        return True
    except OSError:
        return False


PAGE = """<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1"><title>X570 power</title>
<style>
 body{font-family:system-ui,sans-serif;background:#111827;color:#e5e7eb;display:flex;justify-content:center;padding:2rem}
 .card{background:#1f2937;border-radius:12px;padding:1.5rem;max-width:360px;width:100%}
 h1{font-size:1.2rem;margin:0 0 .5rem}.s{margin:.5rem 0 1.25rem;font-weight:600}
 .on{color:#34d399}.off{color:#9ca3af}form{display:inline}
 button{font-size:1rem;padding:.7rem 1.2rem;border:0;border-radius:8px;margin:.25rem;cursor:pointer}
 .wake{background:#2563eb;color:#fff}.stop{background:#dc2626;color:#fff}.msg{margin-top:1rem;color:#fbbf24}
 a{color:#93c5fd}
</style></head><body><div class="card">
<h1>X570 &middot; deus-vult</h1>
<div class="s {{ 'on' if up else 'off' }}">{{ 'Online (Fedora)' if up else 'Offline / not in Fedora' }}</div>
<form method="post" action="{{ url_for('x570.wake') }}"><button class="wake">Wake</button></form>
<form method="post" action="{{ url_for('x570.shutdown') }}" onsubmit="return confirm('Shut down the X570?')">
<button class="stop">Shut down</button></form>
{% if msg %}<div class="msg">{{ msg }}</div>{% endif %}
<p><a href="{{ url_for('x570.page') }}">refresh</a></p>
</div></body></html>"""


@bp.get("/")
def page():
    return render_template_string(PAGE, up=fedora_up(), msg=None)


@bp.get("/status")
def status():
    return jsonify(host="deus-vult", fedora_up=fedora_up())


@bp.post("/wake")
def wake():
    set_gaming(False)
    send_wol()
    return render_template_string(PAGE, up=fedora_up(),
                                  msg="Magic packet sent - give it ~90 s, then refresh.")


@bp.post("/shutdown")
def shutdown():
    set_gaming(False)
    if not fedora_up():
        return render_template_string(PAGE, up=False,
                                      msg="Not reachable in Fedora - nothing to shut down.")
    try:
        r = subprocess.run(["ssh", *SSH_OPTS, f"{USER}@{TS_IP}",
                            "sudo", "/usr/bin/systemctl", "poweroff"],
                           capture_output=True, text=True, timeout=20)
        ok = r.returncode in (0, 255)   # 255 = link dropped as it powered off
        msg = "Shutdown sent." if ok else f"Shutdown failed: {r.stderr.strip()[:200]}"
    except subprocess.TimeoutExpired:
        msg = "Shutdown sent (connection timed out as it powered off)."
    return render_template_string(PAGE, up=False, msg=msg)


# ---------------------------------------------------------------------------
# Gaming mode: free the X570's GPU (stop Kokoro, unload Ollama models) and tell
# Jarvis to stay off the X570. State is a flag file shared by all gunicorn
# workers; it resets on wake/shutdown and whenever control-api restarts.
# Kokoro runs with --restart always, so a reboot also brings it back.
# ---------------------------------------------------------------------------
GAMING_FLAG = "/tmp/x570_gaming"

GAMING_ON_CMD = ("docker stop kokoro >/dev/null 2>&1; "
                 "for m in $(ollama ps 2>/dev/null | awk 'NR>1{print $1}'); do ollama stop \"$m\"; done; echo ok")
GAMING_OFF_CMD = "docker start kokoro >/dev/null 2>&1 && echo ok"


def gaming_on_flag() -> bool:
    return os.path.exists(GAMING_FLAG)


def set_gaming(on: bool) -> None:
    if on:
        open(GAMING_FLAG, "w").close()
    else:
        try:
            os.remove(GAMING_FLAG)
        except FileNotFoundError:
            pass


def _x570_ssh(cmd: str):
    try:
        r = subprocess.run(["ssh", *SSH_OPTS, f"{USER}@{TS_IP}", cmd],
                           capture_output=True, text=True, timeout=30)
        return r.returncode == 0, (r.stdout + r.stderr).strip()[:200]
    except subprocess.TimeoutExpired:
        return False, "timed out"


GAMING_PAGE = """<!doctype html><html><head>
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Gaming mode</title>
<style>
 body{font-family:system-ui,sans-serif;background:#111827;color:#e5e7eb;display:flex;justify-content:center;padding:2rem}
 .card{background:#1f2937;border-radius:12px;padding:1.5rem;max-width:360px;width:100%}
 h1{font-size:1.2rem;margin:0 0 .5rem}.s{margin:.5rem 0 1.25rem;font-weight:600}
 .on{color:#f472b6}.off{color:#34d399}.na{color:#9ca3af}form{display:inline}
 button{font-size:1rem;padding:.7rem 1.2rem;border:0;border-radius:8px;margin:.25rem;cursor:pointer;color:#fff}
 .go{background:#db2777}.back{background:#059669}.msg{margin-top:1rem;color:#fbbf24}.hint{color:#9ca3af;font-size:.9rem}
 a{color:#93c5fd}
</style></head><body><div class="card">
<h1>&#127918; Gaming mode &middot; X570</h1>
{% if not up %}<div class="s na">X570 is off</div>
{% elif gaming %}<div class="s on">ON &mdash; GPU free, Jarvis is off the X570</div>
<form method="post" action="{{ url_for('x570.gaming_off') }}"><button class="back">Turn off (bring Jarvis back)</button></form>
{% else %}<div class="s off">OFF &mdash; Jarvis is using the X570</div>
<form method="post" action="{{ url_for('x570.gaming_on') }}"><button class="go">Turn on gaming mode</button></form>
{% endif %}
<p class="hint">On: stops Kokoro and unloads Ollama models (about 11.5 GB VRAM freed). Jarvis falls back to the M920q voice and won't load models until you turn it off.</p>
{% if msg %}<div class="msg">{{ msg }}</div>{% endif %}
<p><a href="{{ url_for('x570.gaming_page') }}">refresh</a> &middot; <a href="{{ url_for('x570.page') }}">X570 power</a></p>
</div></body></html>"""


@bp.get("/gaming")
def gaming_page():
    return render_template_string(GAMING_PAGE, up=fedora_up(), gaming=gaming_on_flag(), msg=None)


@bp.get("/gaming/status")
def gaming_status():
    on = gaming_on_flag()
    return jsonify(gaming=on, mode="ON" if on else "OFF")


@bp.post("/gaming/on")
def gaming_on():
    if not fedora_up():
        return render_template_string(GAMING_PAGE, up=False, gaming=False, msg="X570 is off.")
    ok, out = _x570_ssh(GAMING_ON_CMD)
    if ok:
        set_gaming(True)
    msg = "Gaming mode on. GPU is free." if ok else f"Failed: {out}"
    return render_template_string(GAMING_PAGE, up=True, gaming=gaming_on_flag(), msg=msg)


@bp.post("/gaming/off")
def gaming_off():
    set_gaming(False)
    msg = "Gaming mode off."
    if fedora_up():
        ok, out = _x570_ssh(GAMING_OFF_CMD)
        msg = "Gaming mode off. Kokoro is starting; Jarvis is back." if ok else f"Flag cleared, but Kokoro didn't start: {out}"
    return render_template_string(GAMING_PAGE, up=fedora_up(), gaming=False, msg=msg)
