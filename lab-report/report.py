"""lab-report - weekly full lab status report, with approve-gated updates.

n8n (workflow "Lab - Weekly Report", Sunday 9 AM, after the 4 AM offsite
backup) calls POST /run. One run:

  1. Lab Up the on-demand tier through control-api, remembering what was off.
     Skipped for the X570 while gaming mode is on.
  2. Collect, all in parallel:
       hosts       Prometheus: CPU (now + 7 d avg), RAM (now + 7 d peak), disk, temps, uptime
       targets     Prometheus `up == 0`
       drives      Scrutiny (NAS SMART) + iDRAC Redfish storage (PERC controller, array, disks)
       UPS         NUT exporter: status, charge, load, runtime, on-battery events this week
       backups     systemd results of the backup units (via lab-agent)
       containers  Docker socket proxies on the M920q + NAS: stopped / unhealthy
       network     AdGuard primary + secondary, sentinel lockdown + last scan, Tailscale devices
       updates     lab-agent `check` on every host: apt/dnf packages + stale container images
  3. Put each on-demand box back the way it was (Lab Down, or just the one box).
  4. Post the report to Discord and return it as JSON. n8n asks Approve/Deny if
     there are updates and calls POST /apply on Approve.

POST /apply runs in the background: powers up only what needs it, updates one
host at a time (nas -> pve -> deus-vult), stops at the first failure, restores
power, then starts the M920q last as a detached systemd unit (its own update may
restart this container), which posts its own result. Never reboots anything.

Routes:  GET /health   GET /last (last report JSON)
         POST /run[?lab=0&post=0]  POST /apply   (both: Authorization: Bearer CONTROL_API_TOKEN)
"""
import asyncio
import datetime
import json
import logging
import os
import pathlib
import time
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("lab-report")

E = os.getenv
TZ = ZoneInfo(E("TZ", "America/Chicago"))
DATA = pathlib.Path(E("DATA_DIR", "/data"))
TOKEN = E("CONTROL_API_TOKEN", "")
WEBHOOK = E("DISCORD_WEBHOOK_URL", "")
CONTROL = E("CONTROL_API_URL", "http://control-api:5000").rstrip("/")
PROM = E("PROMETHEUS_URL", "http://prometheus:9090").rstrip("/")
SCRUTINY = E("SCRUTINY_URL", "http://192.168.0.172:8082").rstrip("/")
SENTINEL = E("SENTINEL_URL", "http://jarvis-sentinel:8000").rstrip("/")
IDRAC = E("IDRAC_HOST", "").rstrip("/")
IDRAC_AUTH = aiohttp.BasicAuth(E("IDRAC_RO_USER", ""), E("IDRAC_RO_PASS", ""))
IDRAC_SYS = E("IDRAC_SYSTEM_ID", "System.Embedded.1")
AG_AUTH = aiohttp.BasicAuth(E("ADGUARD_USER", ""), E("ADGUARD_PASS", ""))
AG = [("primary", E("ADGUARD_PRIMARY_URL", "http://adguard:80")),
      ("secondary", E("ADGUARD_SECONDARY_URL", "http://192.168.0.172:8086"))]
TS_ID, TS_SECRET = E("TS_OAUTH_CLIENT_ID", ""), E("TS_OAUTH_CLIENT_SECRET", "")
TS_API = "https://api.tailscale.com/api/v2"


def pairs(s):  # "a=x b=y" -> {"a": "x", "b": "y"}
    return dict(p.split("=", 1) for p in s.split() if "=" in p)


DOCKER = pairs(E("DOCKER_ENDPOINTS", "m920q=http://docker-proxy:2375 nas=http://192.168.0.172:2375"))
CONTAINER_IGNORE = set(E("CONTAINER_IGNORE", "kuma-seed").split())
# host -> user@ip for lab-agent; order here is the apply order (m920q is always forced last)
AGENTS = pairs(E("AGENTS", "nas=Havok@192.168.0.172 pve=root@192.168.0.10 "
                           "deus-vult=mitch@192.168.0.24 m920q=havok@192.168.0.91"))
ON_DEMAND = {"pve": "t430", "deus-vult": "x570"}   # agent host -> control-api box
TS_IGNORE = set(E("TS_IGNORE", "").split())        # tailnet devices allowed to be offline (e.g. laptops)
SETTLE = int(E("SCRAPE_SETTLE", "90"))             # s after Lab Up before reading Prometheus
DISK_WARN, RAM_WARN, TEMP_WARN = (float(E("DISK_WARN", "85")), float(E("RAM_WARN", "90")),
                                  float(E("TEMP_WARN", "80")))
BACKUP_MAX_AGE = {"m920q-backup.service": 26 * 3600, "homelab-backup.service": 8 * 86400}

session: aiohttp.ClientSession | None = None
busy = asyncio.Lock()
BG: set = set()


def T(s):
    return aiohttp.ClientTimeout(total=s)


def now():
    return datetime.datetime.now(TZ)


def ago(epoch):
    if not epoch:
        return "never"
    s = time.time() - epoch
    return f"{s / 3600:.0f} h ago" if s < 172800 else f"{s / 86400:.1f} d ago"


async def get_json(url, **kw):
    async with session.get(url, timeout=kw.pop("timeout", T(20)), ssl=False, **kw) as r:
        if r.status >= 400:
            raise RuntimeError(f"HTTP {r.status} from {url.split('?')[0]}")
        return await r.json(content_type=None)


async def post_json(url, body=None, auth=False, timeout=60):
    h = {"Authorization": f"Bearer {TOKEN}"} if auth else {}
    async with session.post(url, json=body or {}, headers=h, timeout=T(timeout), ssl=False) as r:
        txt = await r.text()
        try:
            return r.status, json.loads(txt)
        except ValueError:
            return r.status, {"text": txt[:300]}


async def discord(text):
    if not WEBHOOK:
        return
    chunks, cur = [], ""
    for line in text.split("\n"):           # split on lines so code/format never breaks mid-line
        if len(cur) + len(line) + 1 > 1900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    chunks.append(cur)
    for c in filter(str.strip, chunks):
        for _ in range(5):
            try:
                async with session.post(WEBHOOK, json={"username": "Lab Report", "content": c}, timeout=T(15)) as r:
                    if r.status == 429:
                        await asyncio.sleep(float((await r.json()).get("retry_after", 2)) + 0.5)
                        continue
                    break
            except Exception as e:
                log.warning("discord post failed: %s", e)
                await asyncio.sleep(2)


# ---------------------------------------------------------------- power (control-api)
async def power():
    s = await get_json(f"{CONTROL}/lab/status", timeout=T(60))
    try:
        gaming = (await get_json(f"{CONTROL}/x570/gaming/status")).get("gaming", False)
    except Exception:
        gaming = False
    return {"t430": s.get("t430") == "On", "x570": s.get("x570") == "Up",
            "running": bool((s.get("run") or {}).get("running")), "gaming": gaming}


async def wait_lab_idle(limit=1500):
    end = time.time() + limit
    while time.time() < end:
        if not (await power())["running"]:
            return True
        await asyncio.sleep(10)
    return False


async def lab(action):
    """POST /lab/up|down, retrying while another run holds the lock; wait for it to finish."""
    for _ in range(30):
        code, body = await post_json(f"{CONTROL}/lab/{action}", auth=(action == "down"))
        if code == 202:
            break
        if code == 409:                    # e.g. the daily briefing or Jarvis is mid-run
            await wait_lab_idle(600)
            continue
        raise RuntimeError(f"Lab {action}: HTTP {code} {body}")
    await asyncio.sleep(3)
    await wait_lab_idle()
    st = await get_json(f"{CONTROL}/lab/status", timeout=T(60))
    return (st.get("run") or {}).get("result", "?")


async def bring_up(want_t430, want_x570, notes):
    """Power on what is wanted and off; returns the prior state for restore()."""
    prior = await power()
    if prior["gaming"]:
        want_x570 = False
        notes.append("X570 in gaming mode: left alone, not checked")
    if (want_t430 and not prior["t430"]) or (want_x570 and not prior["x570"]):
        if not want_x570 and not prior["x570"]:
            # T430 only: Lab Up would also wake the X570, so power the T430 alone
            await post_json(f"{CONTROL}/api/idrac/power", {"action": "On"}, timeout=60)
            end = time.time() + 900
            while time.time() < end and not await pve_api_up():
                await asyncio.sleep(10)
            notes.append("T430 powered on for the check")
        else:
            res = await lab("up")
            notes.append(f"Lab Up: {res}")
    return prior


async def pve_api_up():
    try:
        st = await get_json(f"{CONTROL}/api/status", timeout=T(60))
        return bool(st.get("proxmox"))
    except Exception:
        return False


async def restore(prior, notes):
    cur = await power()
    t_off, x_off = prior["t430"] is False and cur["t430"], prior["x570"] is False and cur["x570"] and not prior["gaming"]
    if t_off and x_off:
        notes.append(f"Lab Down: {await lab('down')}")
        return
    if x_off:
        await session.post(f"{CONTROL}/x570/shutdown", timeout=T(40))
        notes.append("X570 shut down again")
    if t_off:
        # T430 alone: Proxmox shuts its guests down on an ACPI shutdown (pve-guests.service)
        await post_json(f"{CONTROL}/api/idrac/power", {"action": "GracefulShutdown"}, timeout=60)
        notes.append("T430 sent GracefulShutdown")


# ---------------------------------------------------------------- lab-agent over SSH
async def agent(host, verb, timeout=900):
    target = AGENTS[host]
    proc = await asyncio.create_subprocess_exec(
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=/root/.ssh/known_hosts", "-i", "/root/.ssh/id_ed25519", target, verb,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return [["error", f"timed out after {timeout}s"]], 124
    rows = [l.split("\t") for l in out.decode(errors="replace").splitlines() if l.strip()]
    if proc.returncode == 255:
        rows.append(["error", "ssh: " + err.decode(errors="replace").strip()[:160]])
    return rows, proc.returncode


# ---------------------------------------------------------------- collectors
async def prom(q):
    d = await get_json(f"{PROM}/api/v1/query", params={"query": q})
    return d["data"]["result"]


async def by_host(q):
    return {r["metric"].get("host", r["metric"].get("instance", "?")): float(r["value"][1]) for r in await prom(q)}


FS = 'fstype!~"tmpfs|overlay|squashfs|vfat|ramfs",mountpoint!~"/ugreen.*|/boot.*"'
CPU = '100 * (1 - avg by (host) (rate(node_cpu_seconds_total{mode="idle"}[5m])))'
RAM = '100 * (1 - avg by (host) (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes))'


async def c_hosts():
    cpu, cpu7, ram, ram7, temp, up = await asyncio.gather(
        by_host(CPU), by_host(f"avg_over_time(({CPU})[7d:15m])"),
        by_host(RAM), by_host(f"max_over_time(({RAM})[7d:15m])"),
        by_host("max by (host) (node_hwmon_temp_celsius < 120)"),  # unconnected headers read 127 C
        by_host("max by (host) (time() - node_boot_time_seconds)"))
    disks = await prom(f"100 * (1 - node_filesystem_avail_bytes{{{FS}}} / node_filesystem_size_bytes{{{FS}}})")
    hosts = {}
    for h in sorted(set(cpu) | set(ram)):
        hosts[h] = {"cpu": cpu.get(h), "cpu7": cpu7.get(h), "ram": ram.get(h), "ram7": ram7.get(h),
                    "temp": temp.get(h), "uptime_d": (up.get(h) or 0) / 86400, "disks": {}}
    for r in disks:
        h = r["metric"].get("host", "?")
        hosts.setdefault(h, {"disks": {}})["disks"][r["metric"].get("mountpoint", "?")] = float(r["value"][1])
    return hosts


async def c_targets():
    return [f'{r["metric"].get("job")} ({r["metric"].get("instance")})' for r in await prom("up == 0")]


async def c_ups():
    q = {"status": 'network_ups_tools_ups_status{flag="OL"}', "charge": "network_ups_tools_battery_charge",
         "load": "network_ups_tools_ups_load", "runtime": "network_ups_tools_battery_runtime",
         "ob_events": 'changes(network_ups_tools_ups_status{flag="OB"}[7d]) / 2',
         "ob_max": 'max_over_time(network_ups_tools_ups_status{flag="OB"}[7d])'}
    res = await asyncio.gather(*(prom(v) for v in q.values()))
    return {k: (float(r[0]["value"][1]) if r else None) for k, r in zip(q, res)}


async def c_scrutiny():
    d = (await get_json(f"{SCRUTINY}/api/summary"))["data"]["summary"]
    out = []
    for v in d.values():
        dev, smart = v.get("device", {}), v.get("smart") or {}
        out.append({"name": f'{dev.get("device_name")} {dev.get("model_name", "")}'.strip(),
                    "status": dev.get("device_status", 0), "temp": smart.get("temp"),
                    "hours": smart.get("power_on_hours")})
    return out


async def c_idrac():
    """PERC health via Redfish (read-only iDRAC user). Handles iDRAC8's old and new storage layouts."""
    def g(p):
        return get_json(f"{IDRAC}{p}", auth=IDRAC_AUTH, timeout=T(45))
    system = await g(f"/redfish/v1/Systems/{IDRAC_SYS}")
    out = {"rollup": (system.get("Status") or {}).get("HealthRollup"), "controllers": [], "problems": []}
    try:
        members = (await g(f"/redfish/v1/Systems/{IDRAC_SYS}/Storage")).get("Members", [])
    except RuntimeError:
        members = (await g(f"/redfish/v1/Systems/{IDRAC_SYS}/Storage/Controllers")).get("Members", [])
    for m in members:
        c = await g(m["@odata.id"])
        drives = c.get("Drives") or c.get("Devices") or []
        if not drives:
            continue                                        # AHCI/embedded controllers with nothing on them
        ctl = {"name": c.get("Name") or c.get("Id"), "health": (c.get("Status") or {}).get("Health"),
               "drives": 0, "volumes": []}
        for d in drives:
            if "@odata.id" not in d:
                continue
            dr = await g(d["@odata.id"])
            ctl["drives"] += 1
            h = (dr.get("Status") or {}).get("Health")
            life = dr.get("PredictedMediaLifeLeftPercent")
            if h not in (None, "OK") or dr.get("FailurePredicted") or (life is not None and life < 20):
                out["problems"].append(f'{dr.get("Name")}: health {h}, failure predicted {dr.get("FailurePredicted")}, life {life}%')
        vref = (c.get("Volumes") or {}).get("@odata.id")
        if vref:
            for v in (await g(vref)).get("Members", []):
                vol = await g(v["@odata.id"])
                vh = (vol.get("Status") or {}).get("Health")
                ctl["volumes"].append(f'{vol.get("Name") or vol.get("Id")} {vol.get("RAIDType") or vol.get("VolumeType") or ""} {vh}'.strip())
                if vh not in (None, "OK"):
                    out["problems"].append(f'volume {vol.get("Name")}: {vh}')
        if ctl["health"] not in (None, "OK"):
            out["problems"].append(f'controller {ctl["name"]}: {ctl["health"]}')
        out["controllers"].append(ctl)
    return out


async def c_containers():
    out = {}
    for host, url in DOCKER.items():
        try:
            cs = await get_json(f"{url}/containers/json?all=1")
        except Exception as e:
            out[host] = {"error": str(e)}
            continue
        bad = []
        for c in cs:
            name = (c.get("Names") or ["?"])[0].lstrip("/")
            if name in CONTAINER_IGNORE:
                continue
            if c.get("State") != "running" or "unhealthy" in c.get("Status", "") or "Restarting" in c.get("Status", ""):
                bad.append(f'{name} ({c.get("Status")})')
        out[host] = {"total": len(cs), "bad": bad}
    return out


async def c_adguard():
    out = {}
    for name, url in AG:
        try:
            st = await get_json(f"{url.rstrip('/')}/control/status", auth=AG_AUTH)
            stats = await get_json(f"{url.rstrip('/')}/control/stats", auth=AG_AUTH)
            q, b = stats.get("num_dns_queries", 0), stats.get("num_blocked_filtering", 0)
            out[name] = {"running": st.get("running"), "protection": st.get("protection_enabled"),
                         "version": st.get("version"), "queries": q, "blocked_pct": 100 * b / q if q else 0}
        except Exception as e:
            out[name] = {"error": str(e)}
    return out


async def c_sentinel():
    return await get_json(f"{SENTINEL}/status")


async def c_tailscale():
    if not (TS_ID and TS_SECRET):
        raise RuntimeError("TS_OAUTH_CLIENT_ID/SECRET not set")
    async with session.post(f"{TS_API}/oauth/token", data={"client_id": TS_ID, "client_secret": TS_SECRET},
                            timeout=T(15)) as r:
        tok = (await r.json())["access_token"]
    async with session.get(f"{TS_API}/tailnet/-/devices?fields=all", headers={"Authorization": f"Bearer {tok}"},
                           timeout=T(20)) as r:
        devs = (await r.json()).get("devices", [])
    out = {"total": len(devs), "offline": [], "expiring": [], "updates": []}
    utc = datetime.timezone.utc
    for d in devs:
        name = d.get("hostname") or d.get("name", "?").split(".")[0]
        seen = d.get("lastSeen")
        if seen and name not in TS_IGNORE and not d.get("connectedToControl", True):
            age = datetime.datetime.now(utc) - datetime.datetime.fromisoformat(seen.replace("Z", "+00:00"))
            if age.days >= 7:
                out["offline"].append(f"{name} ({age.days} d)")
        exp = d.get("expires")
        if exp and not d.get("keyExpiryDisabled") and not exp.startswith("0001"):
            left = datetime.datetime.fromisoformat(exp.replace("Z", "+00:00")) - datetime.datetime.now(utc)
            if left.days < 30:
                out["expiring"].append(f"{name} ({left.days} d)")
        if d.get("updateAvailable"):
            out["updates"].append(name)
    return out


async def c_agents(power_now):
    """check + backups on each reachable host."""
    out = {}

    async def one(h):
        box = ON_DEMAND.get(h)
        if box and not power_now.get(box):
            out[h] = {"skipped": "powered off"}
            return
        rows, rc = await agent(h, "check")
        brows, _ = await agent(h, "backups", timeout=60)
        r = {"manager": None, "os": [], "containers": [], "reboot": [], "errors": [], "backups": []}
        for row in rows + brows:
            k, v = row[0], row[1:]
            if k == "os_manager":
                r["manager"] = v[0]
            elif k == "os_update":
                r["os"].append(" ".join(v))
            elif k == "container_update":
                r["containers"].append(f"{v[1]} ({v[2]})")
            elif k == "reboot_required":
                r["reboot"].append(v[0])
            elif k == "backup":
                r["backups"].append({"unit": v[0], "result": v[1], "status": v[2], "epoch": int(v[3] or 0)})
            elif k == "error":
                r["errors"].append(v[0])
        out[h] = r
    await asyncio.gather(*(one(h) for h in AGENTS))
    return out


async def safe(name, coro, errors):
    try:
        return await coro
    except Exception as e:
        log.warning("%s failed: %s", name, e)
        errors.append(f"{name}: {e}")
        return None


# ---------------------------------------------------------------- report
def f0(x, unit=""):
    return "?" if x is None else f"{x:.0f}{unit}"


def build(r):
    attn, lines = [], []
    L = lines.append
    L(f"📋 **Weekly lab report** · {r['time']}")

    L("\n**Hosts**  (CPU now/7d avg · RAM now/7d peak · hottest sensor · uptime)")
    for h, v in (r["hosts"] or {}).items():
        if v.get("cpu") is None:
            continue
        L(f"`{h:<9}` CPU {f0(v['cpu'])}/{f0(v['cpu7'])}% · RAM {f0(v['ram'])}/{f0(v['ram7'])}% · "
          f"{f0(v['temp'], '°C')} · up {v['uptime_d']:.1f} d")
        if (v.get("ram7") or 0) >= RAM_WARN:
            attn.append(f"{h} RAM peaked at {v['ram7']:.0f}% this week")
        if (v.get("temp") or 0) >= TEMP_WARN:
            attn.append(f"{h} running hot: {v['temp']:.0f}°C")
        full = [f"{m} {p:.0f}%" for m, p in v["disks"].items() if p >= DISK_WARN]
        if full:
            attn.append(f"{h} disk: " + ", ".join(full))
    if r["targets"]:
        attn.append("Prometheus targets down: " + ", ".join(r["targets"]))

    L("\n**Storage**")
    for d in r["scrutiny"] or []:
        ok = d["status"] == 0
        L(f"{'✅' if ok else '❌'} {d['name']} · {f0(d['temp'], '°C')} · {f0(d['hours'])} h")
        if not ok:
            attn.append(f"NAS drive {d['name']} failing SMART/Scrutiny checks")
    if r["idrac"]:
        i = r["idrac"]
        for c in i["controllers"]:
            L(f"{'✅' if c['health'] in (None, 'OK') else '❌'} T430 {c['name']}: {c['drives']} disks · " + "; ".join(c["volumes"]))
        L(f"T430 iDRAC health rollup: {i['rollup']}")
        attn += [f"T430 {p}" for p in i["problems"]]
        if i["rollup"] not in (None, "OK"):
            attn.append(f"T430 iDRAC health rollup is {i['rollup']}")

    u = r["ups"]
    if u:
        on_line = u["status"] == 1
        L(f"\n**UPS** {'on line' if on_line else '⚠️ NOT on line'} · {f0(u['charge'], '%')} charge · "
          f"{f0(u['load'], '%')} load · {f0(u['runtime'] / 60 if u['runtime'] else None, ' min')} runtime · "
          f"{f0(u['ob_events'])} on-battery events this week")
        if not on_line:
            attn.append("UPS is not on line power")
        if (u["charge"] or 100) < 90 and on_line:
            attn.append(f"UPS charge only {u['charge']:.0f}% on line power (battery aging?)")

    L("\n**Backups**")
    for h, a in (r["agents"] or {}).items():
        for b in a.get("backups", []):
            age = time.time() - b["epoch"] if b["epoch"] else 1e12
            ok = b["result"] == "success" and age <= BACKUP_MAX_AGE.get(b["unit"], 8 * 86400)
            L(f"{'✅' if ok else '❌'} {h} {b['unit']}: {b['result']}, finished {ago(b['epoch'])}")
            if not ok:
                attn.append(f"backup {b['unit']} on {h}: {b['result']}, last {ago(b['epoch'])}")

    L("\n**Containers**")
    for h, c in (r["containers"] or {}).items():
        if "error" in c:
            L(f"⚠️ {h}: couldn't read ({c['error']})")
            continue
        L(f"{'✅' if not c['bad'] else '❌'} {h}: {c['total'] - len(c['bad'])}/{c['total']} healthy"
          + (" · " + ", ".join(c["bad"]) if c["bad"] else ""))
        if c["bad"]:
            attn.append(f"{h} containers: " + ", ".join(c["bad"]))

    L("\n**Network & security**")
    for name, a in (r["adguard"] or {}).items():
        if "error" in a:
            L(f"❌ AdGuard {name}: {a['error']}")
            attn.append(f"AdGuard {name} unreachable")
            continue
        ok = a["running"] and a["protection"]
        L(f"{'✅' if ok else '❌'} AdGuard {name} {a['version']}: {a['queries']:,} queries, {a['blocked_pct']:.1f}% blocked")
        if not ok:
            attn.append(f"AdGuard {name}: running={a['running']} protection={a['protection']}")
    s = r["sentinel"]
    if s:
        last = s.get("last_scan") or {}
        findings = last.get("findings") or []
        L(f"{'🔒' if s.get('locked') else '✅'} Sentinel: {s.get('mode')} · last scan {str(last.get('time', '?'))[:16]}"
          f" · {len(findings)} finding(s)")
        if s.get("locked"):
            attn.append(f"Lab is in LOCKDOWN since {s.get('since')} ({s.get('reason')})")
        attn += [f"sentinel: {x}" for x in findings[:5]]
    t = r["tailscale"]
    if t:
        L(f"Tailscale: {t['total']} devices" + (f" · client updates: {', '.join(t['updates'])}" if t["updates"] else ""))
        if t["offline"]:
            attn.append("Tailscale offline 7+ days: " + ", ".join(t["offline"]))
        if t["expiring"]:
            attn.append("Tailscale keys expiring <30 d: " + ", ".join(t["expiring"]))

    L("\n**Updates pending**")
    pending = {}
    for h, a in (r["agents"] or {}).items():
        if "skipped" in a:
            L(f"⏭️ {h}: {a['skipped']}")
            continue
        n_os, n_c = len(a["os"]), len(a["containers"])
        if n_os or n_c:
            pending[h] = {"os": n_os, "containers": a["containers"]}
        L(f"`{h:<9}` {a['manager'] or 'n/a'}: {n_os} pkg · containers: "
          + (", ".join(a["containers"]) if n_c else "current")
          + (f" · ♻️ reboot pending ({a['reboot'][0]})" + (", clears at next Lab Down/Up" if h in ON_DEMAND else "")
             if a["reboot"] else ""))
        attn += [f"{h} agent: {e}" for e in a["errors"]]

    if r["notes"]:
        L("\n_" + " · ".join(r["notes"]) + "_")
    if r["errors"]:
        L("⚠️ Couldn't check: " + "; ".join(r["errors"]))

    head = ["\n**Needs attention**"] + [f"• {a}" for a in attn] if attn else ["\n✅ **Nothing needs attention.**"]
    text = "\n".join(lines[:1] + head + lines[1:])
    upd = ", ".join(f"{h} ({v['os']} pkg, {len(v['containers'])} containers)" for h, v in pending.items())
    return text, attn, pending, upd


async def run_report(do_lab=True, post=True):
    t0 = time.time()
    notes, errors = [], []
    prior = None
    if do_lab:
        try:
            prior = await bring_up(True, True, notes)
            notes.append(f"waited {SETTLE}s for metrics")
            await asyncio.sleep(SETTLE)
        except Exception as e:
            errors.append(f"Lab Up: {e}")
    pnow = await safe("power", power(), errors) or {}
    keys = ["hosts", "targets", "ups", "scrutiny", "idrac", "containers", "adguard", "sentinel", "tailscale", "agents"]
    vals = await asyncio.gather(
        safe("hosts", c_hosts(), errors), safe("targets", c_targets(), errors), safe("ups", c_ups(), errors),
        safe("scrutiny", c_scrutiny(), errors),
        safe("idrac", c_idrac(), errors) if pnow.get("t430") else asyncio.sleep(0),
        safe("containers", c_containers(), errors), safe("adguard", c_adguard(), errors),
        safe("sentinel", c_sentinel(), errors), safe("tailscale", c_tailscale(), errors),
        safe("agents", c_agents(pnow), errors))
    r = dict(zip(keys, vals))
    if prior:
        try:
            await restore(prior, notes)
        except Exception as e:
            errors.append(f"restore power: {e}")
    notes.append(f"took {(time.time() - t0) / 60:.1f} min")
    r.update(time=now().strftime("%a %b %d %Y, %H:%M"), notes=notes, errors=errors)
    text, attn, pending, upd = build(r)
    result = {"time": now().isoformat(), "text": text, "attention": attn, "updates_pending": bool(pending),
              "pending": pending, "updates_text": upd, "raw": r}
    (DATA / "last.json").write_text(json.dumps(result, default=str, indent=1))
    if post:
        await discord(text)
    return result


async def run_apply():
    try:
        last = json.loads((DATA / "last.json").read_text())
    except (OSError, ValueError):
        await discord("⚠️ No report on file; run the weekly report first.")
        return
    hosts = [h for h in AGENTS if h in last.get("pending", {})]
    if not hosts:
        await discord("Nothing to update.")
        return
    notes = []
    remote = [h for h in hosts if h != "m920q"]
    await discord(f"🔧 Applying approved updates, one host at a time: {', '.join(remote + (['m920q'] if 'm920q' in hosts else []))}")
    prior = None
    try:
        if any(h in ON_DEMAND for h in remote):
            prior = await bring_up("pve" in remote, "deus-vult" in remote, notes)
            pnow = await power()
            if pnow["gaming"] and "deus-vult" in remote:
                remote.remove("deus-vult")
                await discord("⏭️ deus-vult skipped: gaming mode is on")
        for h in remote:
            box = ON_DEMAND.get(h)
            if box and not (await power()).get(box):
                await discord(f"⚠️ {h} did not come up; stopping here.")
                return
            rows, rc = await agent(h, "apply", timeout=3600)
            body = "\n".join(" ".join(r) for r in rows) or "(no output)"
            await discord(f"{'✅' if rc == 0 else '⚠️'} **Updates on {h}**\n{body[:1700]}")
            if rc != 0:
                await discord("Stopped after the first failure; remaining hosts untouched.")
                return
    finally:
        if prior:
            try:
                await restore(prior, notes)
            except Exception as e:
                notes.append(f"restore failed: {e}")
            if notes:
                await discord("_" + " · ".join(notes) + "_")
    if "m920q" in hosts:
        rows, rc = await agent("m920q", "apply-detached", timeout=60)
        await discord(f"{'🕐' if rc == 0 else '⚠️'} " + " ".join(rows[0][1:] if rows else ["m920q: no answer"]))


# ---------------------------------------------------------------- HTTP
def authorized(req):
    return bool(TOKEN) and req.headers.get("Authorization", "") == f"Bearer {TOKEN}"


async def h_run(req):
    if not authorized(req):
        return web.json_response({"error": "unauthorized"}, status=401)
    if busy.locked():
        return web.json_response({"error": "a report or apply is already running"}, status=409)
    async with busy:
        res = await run_report(req.query.get("lab", "1") != "0", req.query.get("post", "1") != "0")
    return web.json_response({k: v for k, v in res.items() if k != "raw"})


async def h_apply(req):
    if not authorized(req):
        return web.json_response({"error": "unauthorized"}, status=401)
    if busy.locked():
        return web.json_response({"error": "a report or apply is already running"}, status=409)

    async def job():
        async with busy:
            try:
                await run_apply()
            except Exception as e:
                log.exception("apply failed")
                await discord(f"⚠️ Update run crashed: {e}")
    t = asyncio.create_task(job())
    BG.add(t)
    t.add_done_callback(BG.discard)
    return web.json_response({"ok": True, "status": "applying; results go to Discord"}, status=202)


async def h_last(_req):
    try:
        d = json.loads((DATA / "last.json").read_text())
        d.pop("raw", None)
        return web.json_response(d)
    except (OSError, ValueError):
        return web.json_response({"error": "no report yet"}, status=404)


async def h_health(_req):
    return web.json_response({"ok": True, "busy": busy.locked()})


async def on_startup(_app):
    global session
    session = aiohttp.ClientSession()
    DATA.mkdir(parents=True, exist_ok=True)


async def on_cleanup(_app):
    await session.close()


app = web.Application()
app.router.add_get("/health", h_health)
app.router.add_get("/last", h_last)
app.router.add_post("/run", h_run)
app.router.add_post("/apply", h_apply)
app.on_startup.append(on_startup)
app.on_cleanup.append(on_cleanup)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8000)
