// Jarvis voice: joins the owner's voice channel, listens (DAVE-capable @discordjs/voice),
// transcribes with jarvis-speech, asks jarvis-bot, and speaks the reply with Piper.
const { Client, GatewayIntentBits, Events } = require('discord.js');
const {
  joinVoiceChannel, getVoiceConnection, EndBehaviorType, createAudioPlayer,
  createAudioResource, StreamType, AudioPlayerStatus, VoiceConnectionStatus,
  entersState, generateDependencyReport,
} = require('@discordjs/voice');
const prism = require('prism-media');
const { Readable } = require('node:stream');
const { spawn } = require('node:child_process');

const TOKEN = process.env.DISCORD_BOT_TOKEN;
const OWNER = process.env.JARVIS_OWNER_ID;
const GUILD = process.env.JARVIS_GUILD_ID;
const BOT = process.env.BOT_URL || 'http://jarvis-bot:8099';
const SPEECH = process.env.SPEECH_URL || 'http://jarvis-speech:8000';
const WAKE = (process.env.WAKE_WORD || 'jarvis').toLowerCase();
const REQUIRE_WAKE = (process.env.REQUIRE_WAKE_WORD || 'true') === 'true';
const SILENCE_MS = parseInt(process.env.SILENCE_MS || '1200', 10);
const TTS_ENGINE = (process.env.TTS_ENGINE || 'kokoro').toLowerCase();
const KOKORO_URLS = (process.env.KOKORO_URLS || process.env.KOKORO_URL || 'http://kokoro:8880').split(',').map((u) => u.trim()).filter(Boolean);
const KOKORO_VOICE = process.env.KOKORO_VOICE || 'bm_george';
const KOKORO_SPEED = parseFloat(process.env.KOKORO_SPEED || '1.0');
const MIN_RMS = parseInt(process.env.MIN_RMS || '350', 10);
const MIC_FILTER = process.env.MIC_FILTER || 'highpass=f=100,lowpass=f=7000,afftdn=nf=-25';

const log = (...a) => console.log(new Date().toISOString(), ...a);
console.log(generateDependencyReport());

const client = new Client({ intents: [GatewayIntentBits.Guilds, GatewayIntentBits.GuildVoiceStates] });
const player = createAudioPlayer();
player.on('error', (e) => log('player error:', e.message));

let busy = false;
let armedUntil = 0;

// ---------- audio helpers ----------
function pcmToWav(pcm, rate = 48000, channels = 2) {
  const h = Buffer.alloc(44);
  h.write('RIFF', 0); h.writeUInt32LE(36 + pcm.length, 4); h.write('WAVE', 8);
  h.write('fmt ', 12); h.writeUInt32LE(16, 16); h.writeUInt16LE(1, 20); h.writeUInt16LE(channels, 22);
  h.writeUInt32LE(rate, 24); h.writeUInt32LE(rate * channels * 2, 28); h.writeUInt16LE(channels * 2, 32);
  h.writeUInt16LE(16, 34); h.write('data', 36); h.writeUInt32LE(pcm.length, 40);
  return Buffer.concat([h, pcm]);
}

function forSpeech(text) {
  return text
    .replace(/```[\s\S]*?```/g, ' ')
    .replace(/[*_`#>~|]/g, '')
    .replace(/https?:\/\/\S+/g, 'a link')
    .replace(/\s+/g, ' ')
    .trim();
}

function ffmpeg(args, input) {
  return new Promise((resolve, reject) => {
    const p = spawn('ffmpeg', ['-hide_banner', '-loglevel', 'error', ...args]);
    const out = []; let err = '';
    p.stdout.on('data', (c) => out.push(c));
    p.stderr.on('data', (c) => { err += c; });
    p.on('error', reject);
    p.on('close', (code) => (code === 0 ? resolve(Buffer.concat(out)) : reject(new Error(`ffmpeg ${code}: ${err.slice(-300)}`))));
    p.stdin.on('error', () => {});
    p.stdin.end(input);
  });
}

function rms(pcm) {
  let sum = 0; const n = pcm.length >> 1;
  for (let i = 0; i < n; i++) { const v = pcm.readInt16LE(i * 2); sum += v * v; }
  return n ? Math.sqrt(sum / n) : 0;
}

// Mic audio -> speech-band filter + noise reduction -> 16 kHz mono WAV for Whisper
function cleanMic(pcm) {
  return ffmpeg(['-f', 's16le', '-ar', '48000', '-ac', '2', '-i', 'pipe:0',
    '-af', MIC_FILTER, '-ar', '16000', '-ac', '1', '-f', 'wav', 'pipe:1'], pcm);
}

const probeCache = new Map();
async function kokoroUp(url) {
  const c = probeCache.get(url);
  if (c && Date.now() - c.at < 30_000) return c.ok;
  let ok = false;
  try { ok = (await fetch(`${url}/v1/audio/voices`, { signal: AbortSignal.timeout(1500) })).ok; } catch {}
  probeCache.set(url, { ok, at: Date.now() });
  return ok;
}

async function synth(text) {
  if (TTS_ENGINE === 'kokoro') {
    for (const url of KOKORO_URLS) {
      if (!(await kokoroUp(url))) continue;
      try {
        const r = await fetch(`${url}/v1/audio/speech`, {
          method: 'POST', headers: { 'content-type': 'application/json' },
          body: JSON.stringify({ model: 'kokoro', input: text, voice: KOKORO_VOICE, response_format: 'wav', speed: KOKORO_SPEED }),
          signal: AbortSignal.timeout(60_000),
        });
        if (!r.ok) throw new Error(`HTTP ${r.status}: ${(await r.text()).slice(0, 200)}`);
        return Buffer.from(await r.arrayBuffer());
      } catch (e) {
        probeCache.delete(url);
        log(`kokoro ${url} failed: ${e.message}`);
      }
    }
    log('no kokoro endpoint available, falling back to piper');
  }
  const r = await fetch(`${SPEECH}/tts`, {
    method: 'POST', headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ text }), signal: AbortSignal.timeout(90_000),
  });
  if (!r.ok) throw new Error(`tts HTTP ${r.status}`);
  return Buffer.from(await r.arrayBuffer());
}

async function say(text, lap = {}) {
  const ts = Date.now();
  const clean = forSpeech(text);
  if (!clean) return;
  const wav = await synth(clean);
  // Encode the whole reply to Ogg/Opus up front with ffmpeg's native libopus,
  // so playback never waits on a real-time JS encoder (that was the stutter).
  const ogg = await ffmpeg(['-i', 'pipe:0', '-ar', '48000', '-ac', '2', '-c:a', 'libopus', '-b:a', '96k', '-f', 'ogg', 'pipe:1'], wav);
  lap.tts = Date.now() - ts;
  player.play(createAudioResource(Readable.from([ogg]), { inputType: StreamType.OggOpus }));
  await entersState(player, AudioPlayerStatus.Playing, 10_000).catch(() => {});
  await entersState(player, AudioPlayerStatus.Idle, 180_000).catch(() => {});
}

// Returns the command text, '' if only the wake word was said, or null to ignore.
function extractCommand(text) {
  const t = text.trim();
  if (!t) return null;
  const re = new RegExp(`^(?:.*?\\b)?(?:hey|ok|okay|yo)?[\\s,]*${WAKE}\\b[\\s,.!?:-]*`, 'i');
  if (re.test(t)) return t.replace(re, '').trim();
  if (!REQUIRE_WAKE || Date.now() < armedUntil) return t;
  return null;
}

// ---------- the conversation loop ----------
async function handleUtterance(pcm) {
  if (busy) return;
  if (pcm.length < 48000 * 2 * 2 * 0.4) return; // under 0.4 s: a cough or a click
  const level = rms(pcm);
  if (level < MIN_RMS) { log(`ignored quiet audio (rms ${level.toFixed(0)} < ${MIN_RMS})`); return; }
  busy = true;
  const t0 = Date.now(); const lap = {};
  try {
    let wav;
    try { wav = await cleanMic(pcm); } catch (e) { log('mic filter failed, sending raw:', e.message); wav = pcmToWav(pcm); }
    const sttRes = await fetch(`${SPEECH}/stt`, {
      method: 'POST', headers: { 'content-type': 'audio/wav' },
      body: wav, signal: AbortSignal.timeout(60_000),
    });
    if (!sttRes.ok) throw new Error(`stt HTTP ${sttRes.status}: ${(await sttRes.text()).slice(0, 200)}`);
    const { text = '' } = await sttRes.json();
    lap.stt = Date.now() - t0;
    log('heard:', JSON.stringify(text));
    const cmd = extractCommand(text);
    if (cmd === null) return;
    if (cmd === '' || /^[.!?]*$/.test(cmd)) {
      armedUntil = Date.now() + 10_000;
      await say('Yes?');
      return;
    }
    armedUntil = 0;
    const brain = await fetch(`${BOT}/brain`, { signal: AbortSignal.timeout(10_000) })
      .then((r) => r.json()).catch(() => ({ up: false }));
    if (!brain.up) await say('One moment, I need to wake up the X570.');
    const res = await fetch(`${BOT}/ask`, {
      method: 'POST', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ text: cmd }), signal: AbortSignal.timeout(290_000),
    });
    const { reply } = await res.json();
    lap.llm = Date.now() - t0 - lap.stt;
    log('reply:', JSON.stringify(reply));
    const t1 = Date.now();
    await say(reply || "I didn't get an answer.", lap);
    log(`timing: stt ${lap.stt}ms | llm ${lap.llm}ms | tts ${lap.tts}ms | total-to-voice ${lap.stt + lap.llm + lap.tts}ms (+${SILENCE_MS}ms silence wait)`);
  } catch (e) {
    log('utterance failed:', e.message);
    await say('Sorry, something went wrong on my end.').catch(() => {});
  } finally {
    busy = false;
  }
}

function listen(connection) {
  const receiver = connection.receiver;
  receiver.speaking.on('start', (userId) => {
    log(`speaking start: ${userId}${userId === OWNER ? ' (owner)' : ''} busy=${busy} sub=${receiver.subscriptions.has(userId)}`);
    if (userId !== OWNER || busy || receiver.subscriptions.has(userId)) return;
    const opus = receiver.subscribe(userId, { end: { behavior: EndBehaviorType.AfterSilence, duration: SILENCE_MS } });
    const decoder = new prism.opus.Decoder({ rate: 48000, channels: 2, frameSize: 960 });
    const chunks = [];
    opus.on('error', (e) => log('receive error:', e.message));
    decoder.on('error', (e) => log('decode error:', e.message));
    decoder.on('data', (c) => chunks.push(c));
    decoder.on('end', () => { const pcm = Buffer.concat(chunks); log(`utterance: ${(pcm.length / 192000).toFixed(1)} s, rms ${rms(pcm).toFixed(0)}`); handleUtterance(pcm); });
    opus.pipe(decoder);
  });
}

// ---------- follow the owner between voice channels ----------
async function join(channel) {
  const existing = getVoiceConnection(channel.guild.id);
  if (existing && existing.joinConfig.channelId === channel.id) return;
  if (existing) existing.destroy();
  log(`joining ${channel.name}`);
  const connection = joinVoiceChannel({
    channelId: channel.id, guildId: channel.guild.id,
    adapterCreator: channel.guild.voiceAdapterCreator, selfDeaf: false, selfMute: false,
  });
  connection.on('error', (e) => log('connection error:', e.message));
  connection.on(VoiceConnectionStatus.Disconnected, async () => {
    try {
      await Promise.race([
        entersState(connection, VoiceConnectionStatus.Signalling, 5_000),
        entersState(connection, VoiceConnectionStatus.Connecting, 5_000),
      ]);
    } catch {
      connection.destroy();
    }
  });
  try {
    await entersState(connection, VoiceConnectionStatus.Ready, 30_000);
  } catch (e) {
    log('voice connection never became ready:', e.message);
    connection.destroy();
    return;
  }
  connection.subscribe(player);
  listen(connection);
  log('ready and listening');
}

function leave(guildId) {
  const c = getVoiceConnection(guildId);
  if (c) { log('owner left voice; leaving'); c.destroy(); }
}

client.on(Events.VoiceStateUpdate, (oldState, newState) => {
  if (newState.id !== OWNER) return;
  if (newState.channelId && newState.channelId !== oldState.channelId) join(newState.channel);
  else if (!newState.channelId && oldState.channelId) leave(oldState.guild.id);
});

client.once(Events.ClientReady, async () => {
  log(`voice logged in as ${client.user.tag}; following owner ${OWNER}`);
  const guild = client.guilds.cache.get(GUILD);
  const vs = guild && guild.voiceStates.cache.get(OWNER);
  if (vs && vs.channel) join(vs.channel);
});

require('node:http').createServer((req, res) => {
  const conn = GUILD ? getVoiceConnection(GUILD) : null;
  const ok = client.isReady();
  res.writeHead(ok ? 200 : 503, { 'content-type': 'application/json' });
  res.end(JSON.stringify({ discord: ok, in_voice: !!conn, voice_state: conn ? conn.state.status : null, busy }));
}).listen(8098);

client.login(TOKEN);
