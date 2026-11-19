"""jarvis-sentinel: lab lockdown switch + "anything out of place?" scanner.

Lockdown ON  (no token needed - protective):
  1. Tailscale: save the current policy file, validate and apply lockdown-policy.hujson
  2. AdGuard:   enable the [lockdown] threat blocklists + Safe Browsing
  3. Run a full lab scan and post the report to Discord
Lockdown OFF (needs the control-api token - relaxing security):
  restore the saved Tailscale policy exactly, disable the [lockdown] lists,
  put Safe Browsing back the way it was.

Scan (read-only), compared against a baseline that is created on the first run:
  - network clients on UniFi (via unpoller metrics in Prometheus)  -> new MACs
  - Tailscale machines (API)                                        -> new machines, updates
  - open TCP ports on lab hosts from NetBox (nmap connect scan)     -> new open ports
  - running containers (cAdvisor metrics)                           -> new containers
  - Prometheus targets down that aren't tagged on-demand in NetBox
  - AdGuard: Safe Browsing hits and top blocked domains (24 h)
A nightly scan runs at SCAN_HOUR and only posts to Discord if it finds something.
"""
import asyncio
import datetime
import json
import logging
import os
import pathlib
import re
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sentinel")

TZ = ZoneInfo(os.getenv("TZ", "America/Chicago"))
DATA = pathlib.Path("/data")
POLICY_FILE = pathlib.Path(os.getenv("LOCKDOWN_POLICY", "/app/lockdown-policy.hujson"))
MARKER = "LOCKDOWN POLICY"

TOKEN = os.getenv("CONTROL_API_TOKEN", "")
TS_ID = os.getenv("TS_OAUTH_CLIENT_ID", "")
TS_SECRET = os.getenv("TS_OAUTH_CLIENT_SECRET", "")
TS_API = "https://api.tailscale.com/api/v2"
AG_URL = os.getenv("ADGUARD_URL", "http://adguard:80").rstrip("/")
AG_AUTH = aiohttp.BasicAuth(os.getenv("ADGUARD_USERNAME", ""), os.getenv("ADGUARD_PASSWORD", ""))
PROM = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
NETBOX = os.getenv("NETBOX_URL", "http://netbox:8080")
NB_TOKEN = os.getenv("NETBOX_TOKEN", "")
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "")
SCAN_HOUR = int(os.getenv("SCAN_HOUR", "3"))

LOCKDOWN_LISTS = [
    ("[lockdown] HaGeZi Threat Intelligence (medium)",
     "https://cdn.jsdelivr.net/gh/hagezi/dns-blocklists@latest/adblock/tif.medium.txt"),
    ("[lockdown] Phishing filter",
     "https://malware-filter.gitlab.io/malware-filter/phishing-filter-agh.txt"),
    ("[lockdown] Dandelion Sprout anti-malware",
     "https://github.com/DandelionSprout/adfilt/raw/master/Alternate%20versions%20Anti-Malware%20List/AntiMalwareDomains.txt"),
]

session: aiohttp.ClientSession | None = None
busy = asyncio.Lock()
BG_TASKS: set = set()


def bg(coro):
    t = asyncio.create_task(coro)
    BG_TASKS.add(t)
    t.add_done_callback(BG_TASKS.discard)
    return t


# ---------------------------------------------------------------- utilities
def now():
    return datetime.datetime.now(TZ)


def load(name, default):
    p = DATA / name
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, ValueError):
        return default


def save(name, obj):
    DATA.mkdir(parents=True, exist_ok=True)
    tmp = DATA / (name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    tmp.replace(DATA / name)


def T(s):
    return aiohttp.ClientTimeout(total=s)


async def discord(text: str):
    if not WEBHOOK:
        return
    for i in range(0, len(text), 1900):
        payload = {"username": "Jarvis Sentinel", "content": text[i:i + 1900]}
        for attempt in range(5):
            try:
                async with session.post(WEBHOOK, json=payload, timeout=T(15)) as r:
                    if r.status == 429:  # Discord rate limit: wait as told, then retry
                        try:
                            wait = float((await r.json()).get("retry_after", 2))
                        except Exception:
                            wait = 2.0
                        await asyncio.sleep(wait + 0.5)
                        continue
                    if r.status >= 400:
                        log.warning("discord post failed: HTTP %s %s", r.status, (await r.text())[:200])
                    else:
                        log.info("discord posted (%s): %s", r.status, payload["content"][:60].replace("\n", " | "))
                    break
            except Exception as e:
                log.warning("discord post failed: %s", e)
                await asyncio.sleep(2)


def authorized(req, form=None) -> bool:
    if not TOKEN:
        return False
    hdr = req.headers.get("Authorization", "")
    if hdr == f"Bearer {TOKEN}":
        return True
    return bool(form) and form.get("token", "") == TOKEN


# ---------------------------------------------------------------- Tailscale
async def ts_token() -> str:
    if not (TS_ID and TS_SECRET):
        raise RuntimeError("Tailscale OAuth client not configured")
    async with session.post(f"{TS_API}/oauth/token", data={"client_id": TS_ID, "client_secret": TS_SECRET},
                            timeout=T(15)) as r:
        d = await r.json()
        if r.status != 200:
            raise RuntimeError(f"Tailscale OAuth failed: {d}")
        return d["access_token"]


async def ts_get_policy(tok) -> str:
    async with session.get(f"{TS_API}/tailnet/-/acl", headers={"Authorization": f"Bearer {tok}", "Accept": "application/hujson"},
                           timeout=T(20)) as r:
        if r.status != 200:
            raise RuntimeError(f"read policy failed: HTTP {r.status} {await r.text()}")
        return await r.text()


async def ts_set_policy(tok, text: str):
    h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/hujson"}
    async with session.post(f"{TS_API}/tailnet/-/acl/validate", data=text.encode(), headers=h, timeout=T(30)) as r:
        body = await r.text()
        if r.status != 200 or (body.strip() not in ("", "{}") and '"message"' in body):
            raise RuntimeError(f"Tailscale rejected the policy: {body[:400]}")
    async with session.post(f"{TS_API}/tailnet/-/acl", data=text.encode(), headers=h, timeout=T(30)) as r:
        if r.status != 200:
            raise RuntimeError(f"apply policy failed: HTTP {r.status} {(await r.text())[:400]}")


async def ts_devices(tok):
    async with session.get(f"{TS_API}/tailnet/-/devices", headers={"Authorization": f"Bearer {tok}"}, timeout=T(20)) as r:
        if r.status != 200:
            raise RuntimeError(f"device list failed: HTTP {r.status}")
        return (await r.json()).get("devices", [])


# ---------------------------------------------------------------- AdGuard
async def ag(method, path, body=None):
    kw = {"json": body if body is not None else {}}
    async with session.request(method, f"{AG_URL}/control/{path}", auth=AG_AUTH, timeout=T(180), **kw) as r:
        text = await r.text()
        if r.status >= 400:
            raise RuntimeError(f"AdGuard {path}: HTTP {r.status} {text[:200]}")
        try:
            return json.loads(text) if text else {}
        except ValueError:
            return {}


async def ag_lockdown(enable: bool, state: dict):
    status = await ag("GET", "filtering/status")
    have = {f["url"]: f for f in status.get("filters") or []}
    for name, url in LOCKDOWN_LISTS:
        if url in have:
            await ag("POST", "filtering/set_url", {"url": url, "whitelist": False,
                                                   "data": {"name": name, "url": url, "enabled": enable}})
        elif enable:
            await ag("POST", "filtering/add_url", {"name": name, "url": url, "whitelist": False})
    if enable:
        sb = await ag("GET", "safebrowsing/status")
        state["safebrowsing_before"] = bool(sb.get("enabled"))
        await ag("POST", "safebrowsing/enable")
    elif not state.get("safebrowsing_before", False):
        await ag("POST", "safebrowsing/disable")


# ---------------------------------------------------------------- lockdown
async def lockdown_on(by: str, reason: str = "") -> dict:
    async with busy:
        state = load("state.json", {})
        if state.get("locked"):
            return {"ok": True, "already": True, "message": "Lockdown is already on."}
        steps, errors = [], []
        try:
            tok = await ts_token()
            current = await ts_get_policy(tok)
            if MARKER not in current:
                (DATA / "acl-normal.hujson").write_text(current)
            await ts_set_policy(tok, POLICY_FILE.read_text())
            steps.append("Tailscale lockdown policy applied (normal policy saved)")
        except Exception as e:
            errors.append(f"Tailscale: {e}")
        try:
            await ag_lockdown(True, state)
            steps.append("AdGuard threat blocklists + Safe Browsing on")
        except Exception as e:
            errors.append(f"AdGuard: {e}")
        state.update(locked=True, since=now().isoformat(timespec="seconds"), by=by, reason=reason)
        save("state.json", state)
        msg = f"🔒 **Lockdown ON** (by {by}{': ' + reason if reason else ''})\n" + "\n".join(f"✅ {s}" for s in steps)
        if errors:
            msg += "\n" + "\n".join(f"⚠️ {e}" for e in errors)
        await discord(msg + "\nRunning a full lab scan now…")
        bg(scan_and_report(always_post=True))
        return {"ok": not errors, "steps": steps, "errors": errors}


async def lockdown_off(by: str) -> dict:
    async with busy:
        state = load("state.json", {})
        steps, errors = [], []
        saved = DATA / "acl-normal.hujson"
        try:
            if not saved.exists():
                raise RuntimeError("no saved normal policy found, leaving Tailscale untouched")
            tok = await ts_token()
            await ts_set_policy(tok, saved.read_text())
            steps.append("normal Tailscale policy restored")
        except Exception as e:
            errors.append(f"Tailscale: {e}")
        try:
            await ag_lockdown(False, state)
            steps.append("AdGuard lockdown lists off, Safe Browsing restored")
        except Exception as e:
            errors.append(f"AdGuard: {e}")
        if not errors:
            state.update(locked=False, unlocked=now().isoformat(timespec="seconds"), unlocked_by=by)
        save("state.json", state)
        msg = f"🔓 **Lockdown OFF** (by {by})\n" + "\n".join(f"✅ {s}" for s in steps)
        if errors:
            msg += "\n" + "\n".join(f"⚠️ {e}" for e in errors) + "\nLockdown is still marked ON until this succeeds."
        await discord(msg)
        return {"ok": not errors, "steps": steps, "errors": errors}


# ---------------------------------------------------------------- scan
async def prom(q):
    async with session.get(f"{PROM}/api/v1/query", params={"query": q}, timeout=T(20)) as r:
        return (await r.json()).get("data", {}).get("result", [])


async def netbox_devices():
    async with session.get(f"{NETBOX}/api/dcim/devices/", params={"limit": 300},
                           headers={"Authorization": f"Bearer {NB_TOKEN}"}, timeout=T(20)) as r:
        return (await r.json()).get("results", [])


async def nmap(ips):
    if not ips:
        return {}
    p = await asyncio.create_subprocess_exec(
        "nmap", "-sT", "-Pn", "-T4", "--top-ports", "300", "--open", "--host-timeout", "90s", "-oG", "-", *ips,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await p.communicate()
    result = {}
    for line in out.decode(errors="replace").splitlines():
        m = re.match(r"Host: (\S+).*Ports: (.*)", line)
        if m:
            ports = sorted(int(x.split("/")[0]) for x in m.group(2).split(", ") if "/open/" in x)
            result[m.group(1)] = ports
    return result


async def scan() -> dict:
    base = load("baseline.json", {})
    seeded, findings, info, errors = [], [], [], []

    def compare(key, current: dict, label: str, fmt):
        known = base.get(key)
        if known is None:
            base[key] = sorted(current)
            seeded.append(f"{label}: {len(current)}")
            return
        for k in sorted(set(current) - set(known)):
            findings.append(fmt(k, current[k]))

    devs = []
    try:
        devs = await netbox_devices()
    except Exception as e:
        errors.append(f"NetBox: {e}")
    ip_name = {(d.get("primary_ip4") or {}).get("address", "").split("/")[0]: d["name"] for d in devs if d.get("primary_ip4")}
    on_demand = {d["name"] for d in devs if any(t.get("slug") == "on-demand" for t in d.get("tags", []))}

    # 1. network clients seen by UniFi
    try:
        rows = await prom('group by (mac, name, hostname, ip, oui, wired) ({__name__=~"unpoller_client_(uptime_seconds|receive_bytes_total|rssi_db)"})')
        clients = {}
        for r in rows:
            m = r["metric"]
            mac = (m.get("mac") or "").lower()
            if mac:
                clients[mac] = {"name": m.get("name") or m.get("hostname") or "?", "ip": m.get("ip", "?"),
                                "vendor": m.get("oui", ""), "wired": m.get("wired", "")}
        if clients:
            compare("clients", clients, "network clients",
                    lambda k, v: f"🆕 New network device: **{v['name']}** {v['ip']} ({k}{', ' + v['vendor'] if v['vendor'] else ''})")
            info.append(f"{len(clients)} devices on the network")
        else:
            errors.append("no UniFi client data in Prometheus (is unpoller exporting clients?)")
    except Exception as e:
        errors.append(f"UniFi clients: {e}")

    # 2. Tailscale machines
    try:
        tdevs = await ts_devices(await ts_token())
        current = {d["id"]: d for d in tdevs}
        compare("tailscale", current, "Tailscale machines",
                lambda k, d: f"🆕 New Tailscale machine: **{d.get('hostname')}** ({', '.join(d.get('addresses', [])[:1])})")
        for d in tdevs:
            if not d.get("authorized", True):
                findings.append(f"⚠️ Tailscale machine waiting for approval: {d.get('hostname')}")
        upd = [d.get("hostname") for d in tdevs if d.get("updateAvailable")]
        if upd:
            info.append("Tailscale updates available: " + ", ".join(upd))
    except Exception as e:
        errors.append(f"Tailscale: {e}")

    # 3. open ports on lab hosts
    try:
        ports = await nmap(sorted(ip_name))
        known = base.get("ports")
        if known is None:
            base["ports"] = ports
            seeded.append(f"open ports on {len(ports)} hosts")
        else:
            for ip, plist in ports.items():
                if not plist:
                    continue
                if ip not in known:
                    known[ip] = plist
                    continue
                new = sorted(set(plist) - set(known[ip]))
                if new:
                    findings.append(f"🚪 New open port(s) on **{ip_name.get(ip, ip)}** ({ip}): {', '.join(map(str, new))}")
        info.append(f"port-scanned {sum(1 for v in ports.values() if v)} reachable lab hosts")
    except Exception as e:
        errors.append(f"port scan: {e}")

    # 4. containers
    try:
        rows = await prom('count by (name, instance) (container_last_seen{name!=""} > time() - 300)')
        cont = {f"{r['metric'].get('instance', '?').split(':')[0]}/{r['metric']['name']}": 1 for r in rows}
        compare("containers", cont, "running containers", lambda k, v: f"📦 New container running: **{k}**")
        info.append(f"{len(cont)} containers running")
    except Exception as e:
        errors.append(f"containers: {e}")

    # 5. monitored hosts down (not on-demand)
    try:
        for r in await prom("up == 0"):
            inst = r["metric"].get("instance", "?")
            name = ip_name.get(inst.split(":")[0], inst)
            if name not in on_demand:
                findings.append(f"🔻 Monitoring target down: **{name}** [{r['metric'].get('job')}]")
    except Exception as e:
        errors.append(f"Prometheus: {e}")

    # 6. AdGuard
    try:
        st = await ag("GET", "stats")
        sb = st.get("num_replaced_safebrowsing", 0)
        if sb:
            findings.append(f"🛡️ AdGuard Safe Browsing blocked {sb} malicious lookup(s) in the stats window")
        top = [list(d)[0] for d in (st.get("top_blocked_domains") or [])[:3]]
        info.append(f"DNS: {st.get('num_dns_queries', 0)} queries, {st.get('num_blocked_filtering', 0)} blocked"
                    + (f" (top: {', '.join(top)})" if top else ""))
    except Exception as e:
        errors.append(f"AdGuard: {e}")

    save("baseline.json", base)
    report = {"time": now().isoformat(timespec="seconds"), "findings": findings, "info": info,
              "errors": errors, "baseline_created": seeded}
    save("last_scan.json", report)
    return report


def format_report(r: dict) -> str:
    lines = [f"🔎 **Lab scan** {r['time'][:16].replace('T', ' ')}"]
    if r["baseline_created"]:
        lines.append("First run: baseline recorded (" + "; ".join(r["baseline_created"]) + "). Future scans flag anything new.")
    lines += r["findings"] or ["✅ Nothing out of place."]
    if r["info"]:
        lines.append("ℹ️ " + " · ".join(r["info"]))
    if r["errors"]:
        lines.append("⚠️ Couldn't check: " + "; ".join(r["errors"]))
    return "\n".join(lines)


async def scan_and_report(always_post=False):
    try:
        r = await scan()
    except Exception as e:
        log.exception("scan failed")
        await discord(f"⚠️ Lab scan failed: {e}")
        return {"error": str(e)}
    if always_post or r["findings"] or r["errors"]:
        await discord(format_report(r))
    return r


async def accept_baseline():
    (DATA / "baseline.json").unlink(missing_ok=True)
    r = await scan()
    return r


async def nightly():
    while True:
        n = now()
        nxt = n.replace(hour=SCAN_HOUR, minute=15, second=0, microsecond=0)
        if nxt <= n:
            nxt += datetime.timedelta(days=1)
        await asyncio.sleep((nxt - n).total_seconds())
        log.info("nightly scan")
        await scan_and_report(always_post=False)


# ---------------------------------------------------------------- web
PAGE = """<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lockdown</title><style>
body{font-family:system-ui,sans-serif;background:#111827;color:#e5e7eb;display:flex;justify-content:center;padding:2rem}
.card{background:#1f2937;border-radius:12px;padding:1.5rem;max-width:520px;width:100%}
h1{font-size:1.2rem;margin:0 0 .5rem}.s{margin:.5rem 0 1rem;font-weight:700}.on{color:#f87171}.off{color:#34d399}
button{font-size:1rem;padding:.7rem 1.1rem;border:0;border-radius:8px;margin:.25rem 0;cursor:pointer;color:#fff}
.lock{background:#dc2626}.unlock{background:#059669}.scan{background:#2563eb}.acc{background:#6b7280}
input{padding:.6rem;border-radius:8px;border:1px solid #374151;background:#111827;color:#e5e7eb;width:12rem}
pre{white-space:pre-wrap;background:#111827;padding:.8rem;border-radius:8px;font-size:.85rem}.msg{color:#fbbf24}
a{color:#93c5fd}form{margin:.4rem 0}
</style></head><body><div class="card"><h1>&#128737;&#65039; Lab lockdown</h1>
<div class="s {cls}">{status}</div>
{msg}
<form method="post" action="/lockdown/on"><button class="lock">Lock down now</button></form>
<form method="post" action="/lockdown/off"><input type="password" name="token" placeholder="control-api token">
<button class="unlock">Unlock</button></form>
<form method="post" action="/scan"><button class="scan">Run lab scan</button></form>
<form method="post" action="/baseline/accept"><input type="password" name="token" placeholder="control-api token">
<button class="acc">Accept current state as normal</button></form>
<h1 style="margin-top:1rem">Last scan</h1><pre>{report}</pre>
<p><a href="/">refresh</a></p></div></body></html>"""


def html(e):
    return str(e).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def page(msg=""):
    st = load("state.json", {})
    last = load("last_scan.json", None)
    status = (f"LOCKED DOWN since {st.get('since', '?')[:16].replace('T', ' ')} (by {st.get('by', '?')})"
              if st.get("locked") else "Normal")
    body = PAGE.replace("{cls}", "on" if st.get("locked") else "off").replace("{status}", html(status)) \
        .replace("{msg}", f'<div class="msg">{html(msg)}</div>' if msg else "") \
        .replace("{report}", html(format_report(last)) if last else "No scan yet.")
    return web.Response(text=body, content_type="text/html")


def wants_html(req):
    # Browsers list text/html first; programs (n8n, Jarvis) get JSON, and so does anything sending a token header.
    return req.headers.get("Accept", "").startswith("text/html") and "Authorization" not in req.headers


async def h_index(req):
    return page()


async def h_status(req):
    st = load("state.json", {})
    return web.json_response({"locked": bool(st.get("locked")), "mode": "LOCKED" if st.get("locked") else "NORMAL",
                              "since": st.get("since"), "by": st.get("by"), "reason": st.get("reason"),
                              "last_scan": load("last_scan.json", None)})


async def h_on(req):
    data = {}
    if req.content_type.startswith("application/x-www-form"):
        data = await req.post()
    elif req.can_read_body:
        try:
            data = await req.json()
        except Exception:
            data = {}
    r = await lockdown_on(by=str(data.get("by") or "dashboard"), reason=str(data.get("reason") or ""))
    if wants_html(req):
        return page("Lockdown ON. " + ("; ".join(r.get("errors", [])) or "Scan running, report goes to Discord."))
    return web.json_response(r)


async def h_off(req):
    form = await req.post() if req.content_type.startswith("application/x-www-form") else {}
    if not authorized(req, form):
        h = req.headers.get("Authorization", "")
        log.warning("unlock rejected: auth header %s, starts with %r, length %d (expected %d)",
                    "present" if h else "MISSING", h[:7], len(h), len("Bearer ") + len(TOKEN))
        if wants_html(req):
            return page("Unlock needs the control-api token.")
        return web.json_response({"error": "unauthorized"}, status=401)
    r = await lockdown_off(by="n8n approval" if req.headers.get("Authorization") else "dashboard")
    if wants_html(req):
        return page("Lockdown OFF." if r["ok"] else "Unlock had errors: " + "; ".join(r["errors"]))
    return web.json_response(r)


async def h_scan(req):
    if busy.locked():
        return page("Busy, try again in a moment.") if wants_html(req) else web.json_response({"error": "busy"}, status=409)
    r = await scan_and_report(always_post=wants_html(req))
    return page("Scan finished.") if wants_html(req) else web.json_response(r)


async def h_accept(req):
    form = await req.post() if req.content_type.startswith("application/x-www-form") else {}
    if not authorized(req, form):
        return page("Accepting a new baseline needs the control-api token.") if wants_html(req) else \
            web.json_response({"error": "unauthorized"}, status=401)
    await accept_baseline()
    return page("Current state accepted as the new normal.") if wants_html(req) else web.json_response({"ok": True})


async def on_startup(app):
    global session
    session = aiohttp.ClientSession()
    bg(nightly())
    log.info("sentinel ready; lockdown=%s", load("state.json", {}).get("locked", False))


app = web.Application()
app.router.add_get("/", h_index)
app.router.add_get("/status", h_status)
app.router.add_post("/lockdown/on", h_on)
app.router.add_post("/lockdown/off", h_off)
app.router.add_post("/scan", h_scan)
app.router.add_post("/baseline/accept", h_accept)
app.on_startup.append(on_startup)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8000)
