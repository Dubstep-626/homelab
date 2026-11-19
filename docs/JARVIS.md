# Jarvis: a semi-autonomous assistant for the homelab

Jarvis answers questions about the lab, posts a daily briefing, and takes actions only after explicit approval. You can talk to it by text in a Discord channel or by voice in a Discord voice channel. Everything runs locally: the LLM, speech recognition and speech synthesis are all on lab hardware, with no cloud AI APIs.

## Architecture

```
Discord ──text──▶ jarvis-bot (Python, Pycord) ──tools──▶ Prometheus · NetBox · control-api
   │                     ▲   │
   │                     │   └──"request_action"──▶ n8n "Jarvis - Actions" ──Approve/Deny in Discord──▶ control-api
   │                /ask │
   └──voice──▶ jarvis-voice (Node, @discordjs/voice, DAVE E2EE)
                  │ STT: jarvis-speech (faster-whisper small.en, CPU, M920q)
                  │ TTS: Kokoro (GPU on X570) → Kokoro (CPU on M920q) → Piper (fallback)
                  ▼
           Ollama qwen3:14b on the X570 (RTX 4070 SUPER), woken on demand via WoL
```

| Component | Host | Role |
|---|---|---|
| Ollama (`qwen3:14b`) | X570 `deus-vult` | The brain; tool calling. Woken by Wake-on-LAN when needed |
| Open WebUI | M920q (`ai.lab.lan`) | Browser chat with the local models |
| NetBox | M920q (`netbox.lab.lan`) | Source of truth: devices, interfaces, cabling, IPs, `on-demand` tag |
| n8n | M920q (`n8n.lab.lan`) | Daily briefing and approval-gated actions |
| jarvis-bot | M920q | Discord text bot + local `/ask` API (`:8099`, Docker network only) |
| jarvis-voice | M920q | Voice channel client; health on `:8098` |
| jarvis-speech | M920q | `/stt` (faster-whisper) and `/tts` (Piper) |
| Kokoro | X570 (GPU) + M920q (CPU) | Natural TTS, voice `bm_george` |
| control-api | M920q | Flask; X570 wake/shutdown, iDRAC power, Proxmox VMs, Gaming mode |
| SearXNG | M920q | Private metasearch for Jarvis's `web_search` (Docker network only) |
| jarvis-sentinel | M920q (`lockdown.lab.lan`) | Lockdown switch + lab scanner; nightly scan at 3:15 |
| docker-proxy | M920q | Read-only Docker API (GET only) on an internal network shared only with jarvis-bot, for log access |
| jarvis-files | NAS (`192.168.0.172:8095`, token) | Read-only search over Volume 1 (SQLite FTS5); nightly reindex at 2:00 |

## Phases

1. **Local LLM**: Ollama on the X570, Open WebUI on the M920q.
2. **Knowledge base**: NetBox models every device, port, cable and IP. Jarvis reads it with a read-only API token.
3. **Daily briefing**: n8n wakes the X570 at 6:56, gathers Prometheus + NetBox facts, has qwen3 write a briefing, posts it to Discord, and shuts the X570 back down only if it was off before. On-demand machines (NetBox tag `on-demand`) being off is reported as normal. A Kuma push monitor alerts if a morning run is missed.
4. **Approval-gated actions**: anything that changes state goes through n8n's Discord *send and wait for approval*. Allowed actions are an allowlist (`wake_x570`, `shutdown_x570`, `power_on_t430`, `shutdown_t430`).
5. **Conversational interface**: text in `#jarvis` (owner only), then voice. Wake word "Jarvis"; replies are spoken and echoed to `#jarvis`.

## Guardrails

- Jarvis never acts directly. The LLM can only call `request_action`, which posts Approve/Deny buttons. Nothing runs until the owner clicks Approve.
- Only the owner's Discord user ID is listened to (text and voice).
- Tokens: NetBox token is read-only; the bot token, n8n encryption key, control-api token (random 32-byte hex), Tailscale OAuth client and webhook URL live in `.env` (never committed).
- control-api: X570 wake/shutdown and T430 `On`/`GracefulShutdown` need no token (reachable only from the LAN/tailnet); `ForceOff`/restarts still require the bearer token.

## Why a Node container for voice

Discord has required DAVE end-to-end encryption on all voice calls since March 2026. Released Pycord can *send* DAVE audio but cannot *receive* it, so speech recognition is impossible with Pycord. `@discordjs/voice` 0.19+ with `@snazzah/davey` can receive. The Node container handles only audio and forwards text to the Python bot's `/ask`, so the tools, history and approvals stay in one place.

## Internet access (read-only)

Tools: `get_weather` (Open-Meteo, no key; defaults to home), `get_headlines` (ProPublica + The Conversation RSS, set in `NEWS_FEEDS`), `web_search` (self-hosted SearXNG), `fetch_page`.
Safety:
- Everything from the web is wrapped as untrusted. Jarvis is told never to follow instructions in it or request actions because of it.
- `fetch_page` only reaches public IPs: no RFC1918, CGNAT/Tailscale, loopback, `.lan`/`.local`/`.ts.net`, or non-80/443 ports. Every redirect is re-checked. 2 MB cap, 5,000 characters returned.
- Actions still require an Approve click, whatever any web page says.

## Personal data access

- **Reminders:** stored in the bot's volume (`/data/reminders.json`). At the time, Jarvis @mentions Mitch in `#jarvis`, which arrives as a push notification. Apple Reminders has no third-party API, so Discord is the delivery channel.
- **iCloud Calendar:** read any calendar and add events over CalDAV, using an Apple app-specific password (`ICLOUD_USER`, `ICLOUD_APP_PASSWORD`, optional `ICLOUD_DEFAULT_CALENDAR`). No edit or delete tool exists.
- **Lab logs:** `list_containers` and `get_container_logs` go through `tecnativa/docker-socket-proxy` (`CONTAINERS=1`, `POST=0`). It's on an `internal` network that only jarvis-bot shares. Log lines are scrubbed of tokens, webhooks, Discord tokens and `key=`/`password=` values before the model sees them.
- **NAS files:** `search_files`, `find_files` and `read_file` call jarvis-files, which runs on the NAS. `/volume1` is mounted read-only. Excluded from indexing and reading: `docker/`, every `@` system folder, recycle bins, `.env*`, `.kdbx`, keys and certs, SSH folders, `*secrets*` and SQLite databases. Text is extracted from documents only (txt/md/pdf/docx/csv/…); media files are indexed by name. The service is capped at 1 CPU and 768 MB.
- File contents and the answers built from them are posted in Discord, so they pass through Discord's servers.

## Lockdown and scanning (jarvis-sentinel)

**Lockdown ON** (dashboard, Homepage tile, or "Jarvis, lock down"; no token, since it's protective):
- Saves the current Tailscale policy and applies `jarvis-sentinel/lockdown-policy.hujson` through the API (OAuth client: Policy File write, Devices Core read). Tailscale validates the policy first.
- Under lockdown: phone and ThinkPad keep full access, lab servers keep talking to each other, every device keeps DNS, the work laptop (`pos`) gets SSH and dashboards on the M920q only, and `tag:portable` is cut off.
- AdGuard turns on the `[lockdown]` lists (HaGeZi TIF medium, phishing filter, Dandelion Sprout anti-malware) plus Safe Browsing. The NAS secondary follows via adguardhome-sync.
- Runs a full scan and posts the report to Discord.

**Lockdown OFF** needs the control-api token. That's the dashboard token box, or n8n after an Approve click (`lockdown_off` in *Jarvis - Actions*, which sends `Authorization: Bearer <token>`). It restores the saved policy exactly and returns AdGuard to its prior state.

**Scan**, compared against a baseline recorded on the first run (reset it with "Accept current state as normal"):
- new UniFi clients (unpoller metrics)
- new Tailscale machines, or machines waiting for approval
- new open ports on NetBox hosts (nmap connect scan)
- new containers (cAdvisor)
- down targets that aren't tagged `on-demand`
- AdGuard Safe Browsing hits

The nightly scan only posts if it finds something.

Lessons: AdGuard's API rejects body-less POSTs, so send `{}`. Programs that send `Accept: */*` with `text/html` in it must still get JSON. Keep references to background asyncio tasks. Respect Discord 429 `retry_after`. In n8n 2.x, edits must be **saved** before **Publish**, which only promotes the last saved version.

## Latency (end of speech → start of reply)

Measured after tuning: STT ≈1.4 s, LLM ≈1.2 s (more with a tool call), TTS ≈1.1 s, plus a 0.6 s silence window: about 4.3 s in total.
What got it there: Kokoro on the X570's GPU instead of the M920q's CPU (4.1 s → 1.1 s), Ollama flash attention + q8_0 KV cache so qwen3 and Kokoro both fit in 12 GB VRAM at 100% GPU, and pre-encoding replies to Ogg/Opus with ffmpeg (no real-time JS encoder, so playback doesn't stutter).

## Rebuild

1. M920q: copy `docker-compose.m920q.yml` → `/opt/homelab/homelab-bundle/docker-compose.yml` and `docker-compose.m920q.override.yml` → `docker-compose.override.yml`; fill `.env` from `.env.example`.
2. NetBox: clone `netbox-docker` (release branch) to `/opt/homelab/netbox`, add `netbox/docker-compose.override.yml` from this repo (joins the `homelab-bundle_homelab` network), set `CSRF_TRUSTED_ORIGINS=http://netbox.lab.lan:8080`.
3. X570: see `host/x570/JARVIS-X570.md` (Ollama overrides, firewall, NVIDIA container toolkit, Kokoro GPU).
4. Discord: create a bot (Message Content intent; Connect/Speak permissions; private bot needs the Install Link set to *None*), put the token in `.env` and in n8n as a *Discord Bot API* credential.
5. n8n: import the files in `n8n/`, attach credentials (NetBox *Header Auth* `Authorization: Bearer <token>`, Discord Bot API), paste the Discord webhook URL into the briefing's last Discord node, and **Publish** each workflow. Saving alone only stores a draft.
6. `docker compose up -d --build`, then add the Kuma monitors listed below.

## Monitoring

Uptime Kuma: `jarvis-bot` `http://jarvis-bot:8099/brain`, `jarvis-voice` `http://jarvis-voice:8098/` (keyword `"discord":true`), `jarvis-speech` `http://jarvis-speech:8000/health` (keyword `"ready": true`), Kokoro `http://kokoro:8880/v1/audio/voices`, n8n `http://n8n:5678/healthz`, and a 25 h **push** monitor hit by the last node of the daily briefing. Ollama and the X570 Kokoro are deliberately not monitored (on-demand host).

## Lessons learned

- n8n 2.x: workflows don't run on schedule until **Published**, and edits need a re-publish.
- n8n IF v2 boolean "is false" mis-evaluated; `{{ x === false }}` + "is true" is reliable.
- NetBox CSV import doesn't set primary IPs; assign IPs to interfaces with `is_primary=true`.
- `source .env` breaks on `KEY= value` (space after `=`) and echoes values in errors. Read single keys with `grep '^KEY=' .env | cut -d= -f2-`.
- New PyAV removed an argument faster-whisper still passes; the speech service decodes its own WAV with numpy instead.
