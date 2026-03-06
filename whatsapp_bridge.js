/**
 * whatsapp_bridge.js — NeoLife WhatsApp Bridge v2
 * Sesión de WhatsApp persistida en Redis (sobrevive reinicios y redeploys)
 *
 * npm install whatsapp-web.js qrcode-terminal express axios ioredis
 *
 * Env vars:
 *   PORT=3001
 *   PYTHON_API=https://neolife-api.onrender.com
 *   REDIS_URL=redis://...   (Render Redis URL, empieza con rediss:// en prod)
 *   ALLOWED_NUMBER=521XXXXXXXXXX  (opcional)
 *   PUPPETEER_EXECUTABLE_PATH=/usr/bin/chromium-browser
 */

const { Client, RemoteAuth } = require('whatsapp-web.js');
const qrcode = require('qrcode-terminal');
const express = require('express');
const axios   = require('axios');
const Redis   = require('ioredis');

const PORT       = process.env.PORT        || 3001;
const PYTHON_API = process.env.PYTHON_API  || 'http://localhost:8000';
const REDIS_URL  = process.env.REDIS_URL   || 'redis://localhost:6379';
const ALLOWED    = process.env.ALLOWED_NUMBER || '';

// ─────────────────────────────────────
// REDIS
// ─────────────────────────────────────
const redis = new Redis(REDIS_URL, {
    maxRetriesPerRequest: 3,
    retryStrategy: (t) => Math.min(t * 200, 3000),
    tls: REDIS_URL.startsWith('rediss://') ? { rejectUnauthorized: false } : undefined,
});
redis.on('connect', () => console.log('✅ Redis conectado'));
redis.on('error',   (e) => console.error('❌ Redis:', e.message));

// ─────────────────────────────────────
// REDIS STORE (interfaz que pide RemoteAuth)
// ─────────────────────────────────────
class RedisStore {
    constructor(client, prefix = 'wa') {
        this.r      = client;
        this.prefix = prefix;
    }
    key(session) { return `${this.prefix}:session:${session}`; }

    async save({ session }) {
        await this.r.set(this.key(session), '1', 'EX', 60 * 60 * 24 * 30);
        console.log('💾 Sesión WA guardada en Redis');
    }
    async extract({ session }) {
        const v = await this.r.get(this.key(session));
        return v ? { session } : null;
    }
    async sessionExists({ session }) {
        return (await this.r.exists(this.key(session))) === 1;
    }
    async delete({ session }) {
        await this.r.del(this.key(session));
        console.log('🗑️  Sesión WA eliminada de Redis');
    }
}

const store = new RedisStore(redis, 'neolife');

// ─────────────────────────────────────
// CLIENTE WHATSAPP
// ─────────────────────────────────────
let waReady  = false;
let currentQR = null;

const client = new Client({
    authStrategy: new RemoteAuth({
        clientId: 'neolife',
        store,
        backupSyncIntervalMs: 300_000,  // sync a Redis cada 5 min
    }),
    puppeteer: {
        headless: true,
        executablePath: process.env.PUPPETEER_EXECUTABLE_PATH || '/usr/bin/chromium-browser',
        args: [
            '--no-sandbox', '--disable-setuid-sandbox',
            '--disable-dev-shm-usage', '--disable-gpu',
            '--single-process', '--no-zygote',
        ],
    },
});

client.on('qr', (qr) => {
    currentQR = qr;
    console.log('\n📱 Escanea este QR con WhatsApp:');
    qrcode.generate(qr, { small: true });
    console.log('\n💡 O visita GET /qr para obtenerlo como texto\n');
});

client.on('remote_session_saved', () => console.log('💾 Sesión synced → Redis'));
client.on('authenticated',        () => console.log('🔐 WhatsApp autenticado'));

client.on('ready', () => {
    waReady   = true;
    currentQR = null;
    console.log('✅ WhatsApp listo');
});

client.on('auth_failure', (msg) => {
    console.error('❌ Auth fallida:', msg);
    waReady = false;
});

client.on('disconnected', (reason) => {
    console.warn('⚠️  Desconectado:', reason);
    waReady = false;
    setTimeout(() => client.initialize().catch(console.error), 10_000);
});

// ── Mensajes entrantes ──
client.on('message', async (msg) => {
    try {
        if (msg.from.endsWith('@g.us') || msg.fromMe) return;

        const numero = msg.from.replace(/@c\.us|@s\.whatsapp\.net/, '');
        if (ALLOWED && numero !== ALLOWED) return;

        let nombre = 'Cliente';
        try {
            const c = await msg.getContact();
            nombre  = c.pushname || c.name || 'Cliente';
        } catch (_) {}

        console.log(`📩 ${nombre} (${numero}): ${msg.body?.slice(0, 80)}`);

        const resp = await axios.post(
            `${PYTHON_API}/whatsapp/incoming`,
            { from: msg.from, body: msg.body, name: nombre },
            { timeout: 25_000 }
        );
        if (resp.data?.reply)
            console.log(`🤖 → ${nombre}: ${resp.data.reply.slice(0, 60)}…`);

    } catch (err) {
        console.error('❌ Error:', err.message);
        try { await client.sendMessage(msg.from, 'Disculpa, problema técnico. Intenta en un momento 🙏'); } catch (_) {}
    }
});

client.initialize().catch(console.error);

// ─────────────────────────────────────
// EXPRESS API
// ─────────────────────────────────────
const app = express();
app.use(express.json());

// Enviar mensaje (lo llama el bot Python)
app.post('/send', async (req, res) => {
    const { number, message } = req.body;
    if (!number || !message)   return res.status(400).json({ error: 'Faltan number/message' });
    if (!waReady)              return res.status(503).json({ error: 'WhatsApp no listo' });
    try {
        const id = number.includes('@') ? number : `${number}@c.us`;
        await client.sendMessage(id, message);
        res.json({ ok: true });
    } catch (e) { res.status(500).json({ error: e.message }); }
});

// Estado
app.get('/status', async (req, res) => {
    const saved = await store.sessionExists({ session: 'neolife' });
    res.json({
        whatsapp:      waReady ? 'CONNECTED' : 'DISCONNECTED',
        session_redis: saved ? 'SAVED' : 'NOT_FOUND',
        qr_pending:    !!currentQR,
        timestamp:     new Date().toISOString(),
    });
});

// QR como texto (para escanear desde Render logs o dashboard)
app.get('/qr', (req, res) => {
    if (!currentQR)
        return res.json({ ok: false, msg: 'Sin QR pendiente — WhatsApp ya conectado o aún iniciando' });
    res.json({ ok: true, qr: currentQR });
});

// Logout y limpiar Redis
app.post('/logout', async (req, res) => {
    try {
        await client.logout();
        await store.delete({ session: 'neolife' });
        waReady = false;
        res.json({ ok: true });
    } catch (e) { res.status(500).json({ error: e.message }); }
});

app.listen(PORT, () => {
    console.log(`\n🚀 WA Bridge → http://localhost:${PORT}`);
    console.log(`   Python API: ${PYTHON_API}`);
    console.log(`   Redis:      ${REDIS_URL.replace(/:\/\/[^@]+@/, '://***@')}\n`);
});
