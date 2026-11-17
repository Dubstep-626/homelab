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
| control-api | M920q | Flask; X570 wake/shutdown, iDRAC power, Proxmox VMs |

## Phases

1. **Local LLM**: Ollama on the X570, Open WebUI on the M920q.
2. **Knowledge base**: NetBox models every device, port, cable and IP. Jarvis reads it with a read-only API token.
3. **Daily briefing**: n8n wakes the X570 at 6:56, gathers Prometheus + NetBox facts, has qwen3 write a briefing, posts it to Discord, and shuts the X570 back down only if it was off before. On-demand machines (NetBox tag `on-demand`) being off is reported as normal. A Kuma push monitor alerts if a morning run is missed.
4. **Approval-gated actions**: anything that changes state goes through n8n's Discord *send and wait for approval*. Allowed actions are an allowlist (`wake_x570`, `shutdown_x570`, `power_on_t430`, `shutdown_t430`).
5. **Conversational interface**: text in `#jarvis` (owner only), then voice. Wake word "Jarvis"; replies are spoken and echoed to `#jarvis`.

## Guardrails

- Jarvis never acts directly. The LLM can only call `request_action`, which posts Approve/Deny buttons. Nothing runs until the owner clicks Approve.
- Only the owner's Discord user ID is listened to (text and voice).
- Tokens: NetBox token is read-only; the bot token, n8n encryption key and control-api token live in `.env` (never committed).
- control-api: X570 wake/shutdown and T430 `On`/`GracefulShutdown` need no token (reachable only from the LAN/tailnet); `ForceOff`/restarts still require the bearer token.

## Why a Node container for voice

Discord has required DAVE end-to-end encryption on all voice calls since March 2026. Released Pycord can *send* DAVE audio but cannot *receive* it, so speech recognition is impossible with Pycord. `@discordjs/voice` 0.19+ with `@snazzah/davey` can receive. The Node container handles only audio and forwards text to the Python bot's `/ask`, so the tools, history and approvals stay in one place.

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
