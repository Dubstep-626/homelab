"""Jarvis speech service: speech-to-text (faster-whisper) and text-to-speech (Piper), CPU only.

POST /stt   body = WAV audio            -> {"text": "..."}
POST /tts   json = {"text": "..."}      -> audio/wav
GET  /health                            -> {"ready": bool}

Models download on first start into /models (a named volume), then load from disk.
"""
import asyncio
import logging
import os
import pathlib
import subprocess
import tempfile
import wave

import numpy as np

import aiohttp
from aiohttp import web
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("speech")

MODELS = pathlib.Path("/models")
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small.en")
PIPER_VOICE = os.getenv("PIPER_VOICE", "en_GB-alan-medium")
# Bias recognition toward lab vocabulary.
HINT = os.getenv("WHISPER_HINT", "Jarvis, Proxmox, T430, X570, deus-vult, NetBox, NAS, DXP2800, M920q, iDRAC, AdGuard, n8n, Ollama.")

state = {"whisper": None, "voice": None}


def piper_url(voice: str, ext: str) -> str:
    lang_region, name, quality = voice.split("-")
    lang = lang_region.split("_")[0]
    return (f"https://huggingface.co/rhasspy/piper-voices/resolve/main/"
            f"{lang}/{lang_region}/{name}/{quality}/{voice}{ext}")


async def fetch_voice() -> pathlib.Path:
    d = MODELS / "piper"
    d.mkdir(parents=True, exist_ok=True)
    onnx = d / f"{PIPER_VOICE}.onnx"
    async with aiohttp.ClientSession() as s:
        for ext in (".onnx", ".onnx.json"):
            dest = d / f"{PIPER_VOICE}{ext}"
            if dest.exists() and dest.stat().st_size > 0:
                continue
            log.info("downloading piper voice %s%s", PIPER_VOICE, ext)
            async with s.get(piper_url(PIPER_VOICE, ext), timeout=aiohttp.ClientTimeout(total=600)) as r:
                r.raise_for_status()
                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as f:
                    async for chunk in r.content.iter_chunked(1 << 16):
                        f.write(chunk)
                tmp.rename(dest)
    return onnx


def load_whisper() -> WhisperModel:
    log.info("loading whisper %s (downloads on first run)", WHISPER_MODEL)
    return WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8",
                        download_root=str(MODELS / "whisper"))


def load_wav_16k(path: str) -> np.ndarray:
    """Decode our own 16-bit PCM WAV to 16 kHz mono float32 (no PyAV needed)."""
    with wave.open(path, "rb") as w:
        ch, rate = w.getnchannels(), w.getframerate()
        raw = w.readframes(w.getnframes())
    a = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        a = a.reshape(-1, ch).mean(axis=1)
    if rate == 48000:
        n = len(a) // 3 * 3
        a = a[:n].reshape(-1, 3).mean(axis=1)
    elif rate != 16000:
        idx = np.linspace(0, len(a) - 1, int(len(a) * 16000 / rate))
        a = np.interp(idx, np.arange(len(a)), a)
    return a.astype(np.float32)


def transcribe(path: str) -> str:
    segs, _info = state["whisper"].transcribe(load_wav_16k(path), language="en", beam_size=1,
                                              vad_filter=True, initial_prompt=HINT)
    return " ".join(s.text.strip() for s in segs).strip()


def synthesize(text: str) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        out = f.name
    try:
        subprocess.run(["piper", "-m", str(state["voice"]), "-f", out],
                       input=text.encode(), check=True, capture_output=True, timeout=90)
        with open(out, "rb") as f:
            return f.read()
    finally:
        try:
            os.unlink(out)
        except OSError:
            pass


async def health(_req):
    return web.json_response({"ready": bool(state["whisper"] and state["voice"])})


async def stt(req):
    if not state["whisper"]:
        return web.json_response({"error": "whisper still loading"}, status=503)
    body = await req.read()
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(body)
        path = f.name
    try:
        text = await asyncio.get_running_loop().run_in_executor(None, transcribe, path)
    finally:
        os.unlink(path)
    log.info("stt: %r", text)
    return web.json_response({"text": text})


async def tts(req):
    if not state["voice"]:
        return web.json_response({"error": "piper voice still loading"}, status=503)
    text = str((await req.json()).get("text", "")).strip()
    if not text:
        return web.json_response({"error": "empty text"}, status=400)
    try:
        wav = await asyncio.get_running_loop().run_in_executor(None, synthesize, text[:1500])
    except subprocess.CalledProcessError as e:
        log.error("piper failed: %s", e.stderr.decode(errors="replace")[-500:])
        return web.json_response({"error": "piper failed"}, status=500)
    return web.Response(body=wav, content_type="audio/wav")


async def on_startup(_app):
    async def load():
        try:
            state["voice"] = await fetch_voice()
            log.info("piper voice ready: %s", state["voice"])
            state["whisper"] = await asyncio.get_running_loop().run_in_executor(None, load_whisper)
            log.info("whisper ready")
        except Exception:
            log.exception("model load failed")
    asyncio.create_task(load())


app = web.Application(client_max_size=50 * 1024 * 1024)
app.router.add_get("/health", health)
app.router.add_post("/stt", stt)
app.router.add_post("/tts", tts)
app.on_startup.append(on_startup)

if __name__ == "__main__":
    web.run_app(app, host="0.0.0.0", port=8000)
