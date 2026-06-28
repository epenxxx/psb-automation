const { default: makeWASocket, useMultiFileAuthState, DisconnectReason, downloadMediaMessage } = require('@whiskeysockets/baileys');
const express = require('express');
const qrcode = require('qrcode');
const axios = require('axios');
const pino = require('pino');

const app = express();
app.use(express.json({ limit: '50mb' }));

const PORT = process.env.PORT || 8000;
const WEBHOOK_URL = process.env.WEBHOOK_URL || 'http://psb-app:5000/webhook';
const API_TOKEN = process.env.API_TOKEN || 'zylvemedia';
const SESSION_DIR = process.env.SESSION_DIR || './session_auth';

let sock, qrBase64 = null, status = 'disconnected';

async function initWhatsApp() {
    const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
    sock = makeWASocket({ auth: state, printQRInTerminal: true, logger: pino({ level: 'silent' }) });
    sock.ev.on('creds.update', saveCreds);

    sock.ev.on('connection.update', async (update) => {
        const { connection, lastDisconnect, qr } = update;
        if (qr) { qrBase64 = await qrcode.toDataURL(qr); status = 'scan_qr'; }
        if (connection === 'close') {
            status = 'disconnected'; qrBase64 = null;
            if ((lastDisconnect?.error)?.output?.statusCode !== DisconnectReason.loggedOut) initWhatsApp();
        } else if (connection === 'open') { status = 'connected'; qrBase64 = null; }
    });

    sock.ev.on('messages.upsert', async ({ messages, type }) => {
        if (type !== 'notify') return;
        for (const m of messages) {
            if (!m.message || m.key.fromMe) continue;
            let body = '', msgType = 'chat', mediaBase64 = null, lat = null, lng = null;
            const content = m.message;

            if (content.conversation) body = content.conversation;
            else if (content.extendedTextMessage) body = content.extendedTextMessage.text;
            else if (content.imageMessage) {
                msgType = 'image'; body = content.imageMessage.caption || '';
                try {
                    const buf = await downloadMediaMessage(m, 'buffer', {}, { logger: pino({ level: 'silent' }) });
                    mediaBase64 = buf.toString('base64');
                } catch(e) {}
            } else if (content.locationMessage) {
                msgType = 'location';
                lat = content.locationMessage.degreesLatitude; lng = content.locationMessage.degreesLongitude;
            }

            try { await axios.post(WEBHOOK_URL, { from: m.key.remoteJid, body, type: msgType, lat, lng, media_base64: mediaBase64 }); } 
            catch (err) {}
        }
    });
}

app.post('/send-message', async (req, res) => {
    if (req.headers.authorization !== API_TOKEN) return res.status(401).json({ error: 'Unauthorized' });
    const { target, message } = req.body;
    let formatted = target.includes('@') ? target : `${target}@s.whatsapp.net`;
    try { await sock.sendMessage(formatted, { text: message }); res.json({ status: 'success' }); } 
    catch (e) { res.status(500).json({ error: e.message }); }
});

app.get('/qr-status', (req, res) => res.json({ status, qr: qrBase64 }));
app.post('/restart', (req, res) => { res.json({ status: 'restarting' }); setTimeout(() => process.exit(1), 500); });

initWhatsApp();
app.listen(PORT);
