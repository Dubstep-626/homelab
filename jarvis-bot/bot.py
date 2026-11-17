"""Jarvis - Discord text bot for the homelab (Phase 5a).

Listens in #jarvis (and on @mentions) for messages from the owner only, thinks with
qwen3 on the X570 via Ollama tool calling, reads the lab through Prometheus,
NetBox and control-api, and routes anything that changes state through the n8n
approval workflow (Approve/Deny buttons in Discord). It never acts directly.
"""
import asyncio
import json
import logging
import os
import re
from collections import defaultdict, deque

import aiohttp
import discord
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("jarvis")

TOKEN = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID = int(os.environ["JARVIS_CHANNEL_ID"])
OWNER_ID = int(os.environ["JARVIS_OWNER_ID"])
NB_TOKEN = os.environ["NETBOX_TOKEN"]
OLLAMA = os.getenv("OLLAMA_URL", "http://192.168.0.24:11434")
MODEL = os.getenv("OLLAMA_MODEL", "qwen3:14b")
PROM = os.getenv("PROMETHEUS_URL", "http://prometheus:9090")
NETBOX = os.getenv("NETBOX_URL", "http://netbox:8080")
CONTROL = os.getenv("CONTROL_API_URL", "http://control-api:5000")
ACTION_HOOK = os.getenv("N8N_ACTION_WEBHOOK", "http://n8n:5678/webhook/jarvis-action")

ACTIONS = {
    "wake_x570": "Wake the X570 (deus-vult)",
    "shutdown_x570": "Shut down the X570 (deus-vult)",
    "power_on_t430": "Power on the T430 (pve) via iDRAC",
    "shutdown_t430": "Gracefully shut down the T430 (pve) via iDRAC",
}

SYSTEM = """You are Jarvis, the AI that helps Mitch run his homelab. You talk to him in Discord.
Be concise and direct: a few sentences unless he asks for detail. Dry wit is fine.

Lab facts:
- M920q (192.168.0.91): Docker host running AdGuard, NetBox, n8n, Prometheus, Grafana, Homepage, control-api, and you.
- X570 "deus-vult" (192.168.0.24): GPU workstation that runs your model through Ollama. Powered on on demand.
- T430 "pve" (192.168.0.10): Proxmox server, on demand, power managed through iDRAC.
- DXP2800 (192.168.0.172): UGREEN NAS. UDR7 (192.168.0.1): router.

Rules:
- For anything about current state (up/down, disk, power, devices, cabling), call a tool. Never guess numbers or devices.
- NetBox is the source of truth for inventory and cabling.
- You cannot change anything directly. To power a machine on or off, call request_action: it posts an Approve/Deny prompt in Discord. Then tell Mitch the request is waiting for his approval; never claim it is done.
- Machines tagged on-demand being off is normal, not a problem.
- If a tool returns an error, say so plainly."""

TOOLS = [
    {"type": "function", "function": {
        "name": "get_lab_status",
        "description": "Current health of the lab: which monitored hosts are up or down, disk usage per host, and power state of the X570 and T430.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "get_device_info",
        "description": "Look up a device in NetBox: role, model, status, IP, tags, and its interfaces with what each one is cabled to.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string", "description": "Device name or part of it, e.g. pve, m920q, flex-25g, dxp2800"}},
            "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "query_prometheus",
        "description": "Run a read-only PromQL instant query for metrics not covered by get_lab_status (CPU, memory, temperatures, uptime, network).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string", "description": "PromQL expression"}},
            "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "request_action",
        "description": "Ask Mitch to approve a state-changing action. Posts Approve/Deny buttons in Discord; nothing happens unless he approves.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string", "enum": list(ACTIONS)},
            "reason": {"type": "string", "description": "One short sentence on why"}},
            "required": ["action"]}}},
]

history: dict[int, deque] = defaultdict(lambda: deque(maxlen=12))
session: aiohttp.ClientSession | None = None


def T(seconds: float) -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=seconds)


async def http_json(method: str, url: str, **kw):
    kw.setdefault("timeout", T(20))
    async with session.request(method, url, **kw) as r:
        text = await r.text()
        try:
            data = json.loads(text)
        except ValueError:
            data = {"raw": text[:300]}
        if r.status >= 400:
            data = {"http_status": r.status, "body": data}
        return data


async def prom(query: str):
    d = await http_json("GET", f"{PROM}/api/v1/query", params={"query": query})
    if "data" not in d:
        raise RuntimeError(f"prometheus: {d}")
    return d["data"].get("result", [])


async def netbox(path: str, params=None):
    return await http_json("GET", f"{NETBOX}/api/{path}", params=params,
                           headers={"Authorization": f"Bearer {NB_TOKEN}"})


async def tool_lab_status(_args):
    nb = await netbox("dcim/devices/", {"limit": 200})
    devs = nb.get("results", [])
    names = {}
    for d in devs:
        ip = (d.get("primary_ip4") or {}).get("address")
        if ip:
            names[ip.split("/")[0]] = d["name"]
    on_demand = {d["name"] for d in devs if any(t.get("slug") == "on-demand" for t in d.get("tags", []))}
    name = lambda inst: names.get(str(inst).split(":")[0], inst)

    targets = []
    for r in await prom("up"):
        host = name(r["metric"].get("instance"))
        targets.append({"host": host, "job": r["metric"].get("job"),
                        "up": r["value"][1] == "1", "on_demand": host in on_demand})

    flt = 'fstype!~"tmpfs|overlay|squashfs|vfat|ramfs",mountpoint!~"/ugreen.*|/boot.*"'
    disks = {}
    for r in await prom(f"100 * (1 - node_filesystem_avail_bytes{{{flt}}} / node_filesystem_size_bytes{{{flt}}})"):
        disks[(name(r["metric"].get("instance")), r["metric"].get("mountpoint"))] = round(float(r["value"][1]))
    disk_list = [{"host": h, "mount": m, "used_pct": u}
                 for (h, m), u in sorted(disks.items(), key=lambda kv: -kv[1])[:12]]

    try:
        power = await http_json("GET", f"{CONTROL}/api/status")
    except Exception as e:
        power = {"error": str(e)}
    return {"targets": targets, "disks": disk_list,
            "power": {"x570": power.get("x570"), "t430_idrac": power.get("idrac"), "errors": power.get("errors")}}


async def tool_device_info(args):
    q = str(args.get("name", "")).strip()
    found = await netbox("dcim/devices/", {"q": q, "limit": 5})
    out = []
    for d in found.get("results", []):
        ifs = await netbox("dcim/interfaces/", {"device_id": d["id"], "limit": 100})
        interfaces = []
        for i in ifs.get("results", []):
            peers = []
            for p in i.get("link_peers") or []:
                peers.append(f'{(p.get("device") or {}).get("name", "?")} {p.get("name", "")}'.strip())
            interfaces.append({"name": i.get("name"), "type": (i.get("type") or {}).get("label"),
                               "description": i.get("description"), "cabled_to": peers})
        out.append({
            "name": d.get("name"),
            "role": (d.get("role") or {}).get("name"),
            "model": (d.get("device_type") or {}).get("model"),
            "status": (d.get("status") or {}).get("label"),
            "primary_ip": (d.get("primary_ip4") or {}).get("address"),
            "platform": (d.get("platform") or {}).get("name"),
            "tags": [t.get("name") for t in d.get("tags", [])],
            "description": d.get("description"),
            "comments": (d.get("comments") or "")[:300],
            "interfaces": interfaces,
        })
    return out or {"error": f"no NetBox device matching '{q}'"}


async def tool_promql(args):
    rows = await prom(str(args.get("query", "")))
    return [{"metric": r.get("metric"), "value": (r.get("value") or [None, None])[1]} for r in rows[:25]]


async def tool_request_action(args):
    action = args.get("action")
    if action not in ACTIONS:
        return {"error": f"unknown action {action}; allowed: {list(ACTIONS)}"}
    res = await http_json("POST", ACTION_HOOK, json={"action": action, "reason": args.get("reason", "")})
    if isinstance(res, dict) and res.get("http_status"):
        return {"error": f"n8n webhook failed: {res}"}
    return {"status": f"Approval for '{ACTIONS[action]}' posted in Discord. Nothing happens until Mitch clicks Approve."}


TOOL_FUNCS = {
    "get_lab_status": tool_lab_status,
    "get_device_info": tool_device_info,
    "query_prometheus": tool_promql,
    "request_action": tool_request_action,
}


async def run_tool(name, args):
    fn = TOOL_FUNCS.get(name)
    if not fn:
        return {"error": f"no such tool {name}"}
    try:
        return await fn(args)
    except Exception as e:
        log.exception("tool %s failed", name)
        return {"error": f"{type(e).__name__}: {e}"}


async def ollama_up() -> bool:
    try:
        async with session.get(f"{OLLAMA}/api/tags", timeout=T(3)) as r:
            return r.status == 200
    except Exception:
        return False


async def wake_and_wait(max_wait: int = 150) -> bool:
    try:
        async with session.post(f"{CONTROL}/x570/wake", timeout=T(10)) as r:
            await r.read()
    except Exception as e:
        log.warning("wake failed: %s", e)
    for _ in range(max_wait // 5):
        await asyncio.sleep(5)
        if await ollama_up():
            return True
    return False


async def ensure_brain(channel) -> bool:
    if await ollama_up():
        return True
    await channel.send("My brain (the X570) is off. Waking it, give me a minute or two.")
    if await wake_and_wait():
        return True
    await channel.send("The X570 didn't come up in time. Check on it, or ask me again in a bit.")
    return False


async def ask(channel_id: int, user_text: str, extra_system: str = "") -> str:
    hist = history[channel_id]
    system = SYSTEM + ("\n\n" + extra_system if extra_system else "")
    msgs = [{"role": "system", "content": system}, *hist, {"role": "user", "content": user_text}]
    for _ in range(6):
        payload = {"model": MODEL, "messages": msgs, "tools": TOOLS, "stream": False,
                   "think": False, "options": {"num_ctx": 8192}}
        async with session.post(f"{OLLAMA}/api/chat", json=payload, timeout=T(300)) as r:
            r.raise_for_status()
            data = await r.json()
        msg = data.get("message", {})
        calls = msg.get("tool_calls") or []
        if not calls:
            reply = re.sub(r"<think>.*?</think>", "", msg.get("content", ""), flags=re.S).strip() or "(no reply)"
            hist.append({"role": "user", "content": user_text})
            hist.append({"role": "assistant", "content": reply})
            return reply
        msgs.append(msg)
        for c in calls:
            fn = c.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            log.info("tool call: %s %s", fn.get("name"), args)
            result = await run_tool(fn.get("name"), args)
            msgs.append({"role": "tool", "tool_name": fn.get("name"),
                         "content": json.dumps(result, default=str)[:6000]})
    return "I went in circles calling tools. Try rephrasing that."


# ---------------------------------------------------------------------------
# Local API for the voice container (homelab network only, no published port)
# ---------------------------------------------------------------------------
VOICE_NOTE = ("This message came by voice and your reply will be spoken aloud. Answer in one to three "
              "short sentences of plain speech: no markdown, no lists, no emoji, no URLs. Say numbers naturally.")
voice_lock = asyncio.Lock()
api_started = False


async def api_brain(_req):
    return web.json_response({"up": await ollama_up()})


async def api_ask(req):
    data = await req.json()
    text = str(data.get("text", "")).strip()
    if not text:
        return web.json_response({"reply": ""})
    async with voice_lock:
        if not await ollama_up() and not await wake_and_wait():
            return web.json_response({"reply": "The X570 did not come up, so I can't think right now."})
        try:
            reply = await ask(CHANNEL_ID, text, extra_system=VOICE_NOTE)
        except Exception as e:
            log.exception("voice ask failed")
            reply = f"Something broke on my end: {type(e).__name__}."
    ch = client.get_channel(CHANNEL_ID)
    if ch and os.getenv("VOICE_ECHO", "1") == "1":
        try:
            await ch.send(f"🎙️ **You:** {text}\n**Jarvis:** {reply}"[:1990])
        except Exception:
            log.warning("could not echo voice exchange to channel")
    return web.json_response({"reply": reply})


async def start_api():
    global api_started
    if api_started:
        return
    app = web.Application()
    app.router.add_get("/brain", api_brain)
    app.router.add_post("/ask", api_ask)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 8099).start()
    api_started = True
    log.info("voice API listening on :8099")


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    global session
    if session is None:
        session = aiohttp.ClientSession()
    await start_api()
    log.info("Logged in as %s; listening in channel %s for owner %s", client.user, CHANNEL_ID, OWNER_ID)


@client.event
async def on_message(m: discord.Message):
    if m.author.bot or m.author.id != OWNER_ID:
        return
    if m.channel.id != CHANNEL_ID and client.user not in m.mentions:
        return
    text = m.content.replace(f"<@{client.user.id}>", "").replace(f"<@!{client.user.id}>", "").strip()
    if not text:
        return
    if text.lower() in {"reset", "forget", "new topic"}:
        history[m.channel.id].clear()
        await m.reply("Context cleared.")
        return
    if not await ensure_brain(m.channel):
        return
    async with m.channel.typing():
        try:
            reply = await ask(m.channel.id, text)
        except Exception as e:
            log.exception("ask failed")
            reply = f"Something broke on my end: {type(e).__name__}: {e}"
    for i in range(0, len(reply), 1900):
        chunk = reply[i:i + 1900]
        if i == 0:
            await m.reply(chunk)
        else:
            await m.channel.send(chunk)


client.run(TOKEN)
