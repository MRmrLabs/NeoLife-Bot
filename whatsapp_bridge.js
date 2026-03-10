/**
 * whatsapp_bridge.js — NeoLife WhatsApp Bridge v2.1
 * Sesión de WhatsApp persistida en Redis (sobrevive reinicios y redeploys)
 * + endpoints de chat para el CRM dashboard
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
// IN-MEMORY CHAT STORE
// Guarda los últimos 100 mensajes por número.
// Se pierde al reiniciar, pero el historial real está en la DB Python.
// ─────────────────────────────────────
const MAX_MSGS_PER_CHAT = 100;
const chatStore = new Map();   // numero → { name, messages: [{...}] }

function storeMessage({ numero, name, body, fromMe, timestamp }) {
    if (!chatStore.has(numero)) {
        chatStore.set(numero, { name: name || 'Cliente', messages: [] });
    }
    const chat = chatStore.get(numero);
    if (name && name !== 'Cliente') chat.name = name;
    chat.messages.push({
        id:        `${Date.now()}-${Math.random().toString(36).slice(2,7)}`,
        body:      body || '',
        fromMe:    !!fromMe,
        timestamp: timestamp || Date.now(),
    });
    // Limitar a MAX_MSGS_PER_CHAT
    if (chat.messages.length > MAX_MSGS_PER_CHAT)
        chat.messages = chat.messages.slice(-MAX_MSGS_PER_CHAT);
}

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
let waReady   = false;
let currentQR = null;

const client = new Client({
    authStrategy: new RemoteAuth({
        clientId: 'neolife',
        store,
        backupSyncIntervalMs: 300_000,
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

        // Guardar en store local para el CRM
        storeMessage({
            numero,
            name:      nombre,
            body:      msg.body,
            fromMe:    false,
            timestamp: msg.timestamp ? msg.timestamp * 1000 : Date.now(),
        });

        const resp = await axios.post(
            `${PYTHON_API}/whatsapp/incoming`,
            { from: msg.from, body: msg.body, name: nombre },
            { timeout: 25_000 }
        );

        // Guardar también la respuesta del bot en el store
        if (resp.data?.reply) {
            console.log(`🤖 → ${nombre}: ${resp.data.reply.slice(0, 60)}…`);
            storeMessage({
                numero,
                name:      nombre,
                body:      resp.data.reply,
                fromMe:    true,
                timestamp: Date.now(),
            });
        }

    } catch (err) {
        console.error('❌ Error:', err.message);
        try { await client.sendMessage(msg.from, 'Disculpa, problema técnico. Intenta en un momento 🙏'); } catch (_) {}
    }
});

// Capturar también mensajes enviados desde el teléfono (fromMe)
client.on('message_create', async (msg) => {
    try {
        if (!msg.fromMe || msg.to.endsWith('@g.us')) return;
        const numero = msg.to.replace(/@c\.us|@s\.whatsapp\.net/, '');
        storeMessage({
            numero,
            name:      null,
            body:      msg.body,
            fromMe:    true,
            timestamp: msg.timestamp ? msg.timestamp * 1000 : Date.now(),
        });
    } catch (_) {}
});

client.initialize().catch(console.error);

// ─────────────────────────────────────
// EXPRESS API
// ─────────────────────────────────────
const app = express();
app.use(express.json());

// CORS — permite que el dashboard Python sirva llamadas al bridge
app.use((req, res, next) => {
    res.header('Access-Control-Allow-Origin', '*');
    res.header('Access-Control-Allow-Headers', 'Content-Type');
    next();
});

// ── Enviar mensaje (lo llama el bot Python o el CRM manual)
app.post('/send', async (req, res) => {
    const { number, message } = req.body;
    if (!number || !message)   return res.status(400).json({ error: 'Faltan number/message' });
    if (!waReady)              return res.status(503).json({ error: 'WhatsApp no listo' });
    try {
        const id = number.includes('@') ? number : `${number}@c.us`;
        await client.sendMessage(id, message);

        // Guardar en store como mensaje saliente
        storeMessage({
            numero:    number.replace(/@c\.us|@s\.whatsapp\.net/, ''),
            name:      null,
            body:      message,
            fromMe:    true,
            timestamp: Date.now(),
        });

        res.json({ ok: true });
    } catch (e) { res.status(500).json({ error: e.message }); }
});

// ── Estado
app.get('/status', async (req, res) => {
    const saved = await store.sessionExists({ session: 'neolife' });
    res.json({
        whatsapp:      waReady ? 'CONNECTED' : 'DISCONNECTED',
        session_redis: saved ? 'SAVED' : 'NOT_FOUND',
        qr_pending:    !!currentQR,
        timestamp:     new Date().toISOString(),
    });
});

// ── QR como texto
app.get('/qr', (req, res) => {
    if (!currentQR)
        return res.json({ ok: false, msg: 'Sin QR pendiente — WhatsApp ya conectado o aún iniciando' });
    res.json({ ok: true, qr: currentQR });
});

// ── Lista de chats con último mensaje (para el sidebar del CRM)
app.get('/chats', (req, res) => {
    const chats = [];
    for (const [numero, data] of chatStore.entries()) {
        const msgs = data.messages;
        const last = msgs[msgs.length - 1];
        chats.push({
            numero,
            name:          data.name,
            lastMessage:   last?.body || '',
            lastTimestamp: last?.timestamp || 0,
            lastFromMe:    last?.fromMe || false,
            unread:        msgs.filter(m => !m.fromMe && m.timestamp > (data.lastSeen || 0)).length,
        });
    }
    // Ordenar por más reciente primero
    chats.sort((a, b) => b.lastTimestamp - a.lastTimestamp);
    res.json({ ok: true, chats });
});

// ── Mensajes de un chat específico
app.get('/chats/:numero/messages', (req, res) => {
    const { numero } = req.params;
    const data = chatStore.get(numero);
    if (!data) return res.json({ ok: true, messages: [], name: 'Cliente' });

    // Marcar como visto
    data.lastSeen = Date.now();

    res.json({ ok: true, name: data.name, messages: data.messages });
});

// ── Marcar chat como visto
app.post('/chats/:numero/seen', (req, res) => {
    const { numero } = req.params;
    if (chatStore.has(numero)) chatStore.get(numero).lastSeen = Date.now();
    res.json({ ok: true });
});

// ── Logout y limpiar Redis
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