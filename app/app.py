import os, re, shutil, openpyxl, glob, requests, calendar, base64, sqlite3
from functools import wraps
from flask import Flask, request, jsonify, render_template, session, redirect, url_for
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime
from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFont

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "zylve-secret-default")

WA_API_TOKEN = os.getenv("WA_API_TOKEN", "zylvemedia")
URL_GW_BOT = "http://wa-bot:8000"
URL_GW_PELANGGAN = "http://wa-pelanggan:8000"

NAS_PATH = '/mnt/nas/share/bot_pelanggan/'
TEMPLATE_MASTER = os.path.join(NAS_PATH, 'template-import-pelanggan.xlsx')
KTP_PATH = os.path.join(NAS_PATH, 'foto_ktp/')
RUMAH_PATH = os.path.join(NAS_PATH, 'foto_rumah/')
EXCEL_PATH = os.path.join(NAS_PATH, 'data_excel/')
PDF_PATH = os.path.join(NAS_PATH, 'data_pdf/')
DB_PATH = os.path.join(NAS_PATH, 'psb_system.db')

for path in [KTP_PATH, RUMAH_PATH, EXCEL_PATH, PDF_PATH]:
    os.makedirs(path, exist_ok=True)

user_data = {}

QUESTIONS = [
    ('nama_pelanggan*', 'Nama Pelanggan *'),
    ('nik*', 'No KTP / NIK (Pastikan 16 Digit)'),
    ('no_hp*', 'No. WhatsApp (Hanya Angka, Contoh: 08123456xxx)'),
    ('alamat*', 'Alamat Lengkap'),
    ('tgl_aktif*', 'Tanggal Aktif Pemasangan (Format: DD/MM/YYYY, Contoh: 15/01/2026)'),
    ('redaman', 'Nilai Redaman FO (Contoh: -22 atau -24)'),
    ('tgl_jatuh_tempo', 'Angka Tanggal Jatuh Tempo Saja (Input: 1 sampai 31, Contoh: 15)'),
    ('biaya_paket', 'Biaya Paket Bulanan (Hanya Angka, Contoh: 150000)')
]

def clean_phone(num):
    cleaned = re.sub(r'\D', '', str(num))
    if cleaned.startswith('0'): cleaned = '62' + cleaned[1:]
    return cleaned

# ================= DATABASE STERIL HELPER =================
def init_db():
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS admins 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE, password TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings 
                 (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS whitelist 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT UNIQUE, lid TEXT, name TEXT)''')
    
    try: c.execute("ALTER TABLE whitelist ADD COLUMN lid TEXT")
    except sqlite3.OperationalError: pass 

    # Cuma bikin 1 Admin Web default, TIDAK ADA SEEDING NOMOR WA APAPUN!
    c.execute("SELECT COUNT(*) FROM admins")
    if c.fetchone()[0] == 0:
        c.execute("INSERT INTO admins (username, password) VALUES (?, ?)", ("admin", generate_password_hash("zylve123")))

    c.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('mode', 'dual')")
    conn.commit(); conn.close()

init_db()

def get_setting(key):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE key=?", (key,))
    row = c.fetchone(); conn.close()
    return row[0] if row else 'dual'

def set_setting(key, val):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, val))
    conn.commit(); conn.close()

def get_whitelisted_user(sender_id):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT name FROM whitelist WHERE phone=? OR lid=?", (sender_id, sender_id))
    res = c.fetchone(); conn.close()
    return res[0] if res else None

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'admin_user' not in session: return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated

# ================= CORE LOGIC FUNCTIONS =================
def hitung_biaya_prorata(tgl_aktif_str, tgl_jt_day_str, biaya_paket_str):
    try:
        tgl_aktif = datetime.strptime(tgl_aktif_str, '%Y-%m-%d')
        day_jt, biaya = int(tgl_jt_day_str), int(biaya_paket_str)
        tahun, bulan = tgl_aktif.year, tgl_aktif.month
        max_day = calendar.monthrange(tahun, bulan)[1]
        tgl_jt_target = datetime(tahun, bulan, min(day_jt, max_day))
        if tgl_jt_target <= tgl_aktif:
            bulan += 1
            if bulan > 12: bulan, tahun = 1, tahun + 1
            tgl_jt_target = datetime(tahun, bulan, min(day_jt, calendar.monthrange(tahun, bulan)[1]))
        jumlah_hari = (tgl_jt_target - tgl_aktif).days
        return int((biaya / 30) * jumlah_hari), jumlah_hari
    except Exception: return 0, 0

def beri_watermark_foto(image_path, lat, lon):
    try:
        img = Image.open(image_path); draw = ImageDraw.Draw(img)
        waktu = datetime.now().strftime('%d/%m/%Y | %H:%M:%S')
        teks = f"📍 LOKASI : {lat}, {lon}\n🕒 WAKTU  : {waktu} WIB\n🔒 ARSIP  : NAS SERVER AUTOMATION"
        w, h = img.size; fsize = max(18, int(h * 0.025))
        try: font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", fsize)
        except IOError: font = ImageFont.load_default()
        tbox = draw.textbbox((0, 0), teks, font=font)
        tw, th = tbox[2] - tbox[0], tbox[3] - tbox[1]
        x, y = 30, h - th - 60
        draw.rectangle([x - 15, y - 15, x + tw + 25, y + th + 25], fill="black")
        draw.text((x, y), teks, fill="white", font=font)
        img.save(image_path)
    except Exception as e: print(f"❌ Watermark Error: {e}")

def send_wa_bot(target, text):
    try: requests.post(f"{URL_GW_BOT}/send-message", json={'target': target, 'message': text}, headers={'Authorization': WA_API_TOKEN}, timeout=5)
    except Exception as e: print(f"❌ Outbound Bot Error: {e}")

def kirim_wa_terima_kasih(no_hp, nama, tgl_jt, biaya_paket, tgl_aktif_str):
    no_hp = clean_phone(no_hp)
    biaya_prorata, jumlah_hari = hitung_biaya_prorata(tgl_aktif_str, tgl_jt, biaya_paket)
    try:
        prorata_format = f"{biaya_prorata:,}".replace(",", ".")
        normal_format = f"{int(biaya_paket):,}".replace(",", ".")
    except Exception: prorata_format, normal_format = biaya_paket, biaya_paket
        
    pesan_teks = (
        f"Halo Bapak/Ibu *{nama}*,\n\nTerima kasih telah memilih layanan internet kami. Sambungan Baru Anda saat ini telah *AKTIF*.\n\n"
        f"📌 *Informasi Pembayaran Pelanggan*:\n• Nama: {nama}\n• Jatuh Tempo: Tiap Tanggal *{tgl_jt}*\n"
        f"• *Tagihan Bulan Ke-2 (Prorata {jumlah_hari} Hari)*: *Rp {prorata_format}*\n• Tagihan Bulan Ke-3 Normal: Rp {normal_format}\n\n"
        f"Link Pembayaran : https://ibb.co.com/zTQNjd4c . Mohon kirim bukti pembayaran ke nomer ini sebelum tanggal jatuh tempo. Terima kasih! 🙏"
    )
    target_gw = URL_GW_PELANGGAN if get_setting('mode') == 'dual' else URL_GW_BOT
    try:
        r = requests.post(f"{target_gw}/send-message", json={'target': no_hp, 'message': pesan_teks}, headers={'Authorization': WA_API_TOKEN}, timeout=5)
        return r.status_code == 200, prorata_format, jumlah_hari
    except Exception: return False, prorata_format, jumlah_hari

def simpan_data_keseluruhan(chat_id):
    user_input = user_data[chat_id]['data']
    nama_asli = user_input.get('nama_pelanggan*', 'Tanpa_Nama')
    nama_file_interal = nama_asli.replace(" ", "_").replace("/", "_")
    lokasi_excel = os.path.join(EXCEL_PATH, f"{nama_file_interal}.xlsx")
    lokasi_pdf = os.path.join(PDF_PATH, f"BA_{nama_file_interal}.pdf")
    lat_val, lon_val = user_input.get('lat*', ''), user_input.get('lon*', '')
    maps_link = f"http://maps.google.com/?q={lat_val},{lon_val}" if lat_val else "-"
    tgl_jt, redam = user_input.get('tgl_jatuh_tempo', '-'), user_input.get('redaman', '-')

    try:
        shutil.copy(TEMPLATE_MASTER, lokasi_excel)
        wb = openpyxl.load_workbook(lokasi_excel); ws = wb.active
        ws.cell(row=2, column=1).value = nama_asli; ws.cell(row=2, column=4).value = user_input.get('no_hp*', '')
        ws.cell(row=2, column=5).value = user_input.get('alamat*', ''); ws.cell(row=2, column=6).value = lat_val
        ws.cell(row=2, column=7).value = lon_val; ws.cell(row=2, column=8).value = 'Non-PPN'
        ws.cell(row=2, column=9).value = user_input.get('nik*', ''); ws.cell(row=2, column=12).value = user_input.get('tgl_aktif*', '')
        ws.cell(row=2, column=22).value = f"Tgl Jatuh Tempo: {tgl_jt} | Redaman: {redam} dBm | Maps: {maps_link}"
        wb.save(lokasi_excel); wb.close()
    except Exception as e: print(f"❌ Excel Error: {e}")

    try:
        pdf = FPDF(); pdf.add_page(); pdf.set_font("Helvetica", 'B', 16)
        pdf.cell(0, 10, "BERITA ACARA PASANG BARU (BA-PSB)", ln=1, align='C'); pdf.set_font("Helvetica", size=10)
        pdf.cell(0, 5, f"NAS Server Log: {datetime.now().strftime('%d/%m/%Y %H:%M')}", ln=1, align='C')
        pdf.ln(5); pdf.line(10, 28, 200, 28)
        pdf.set_font("Helvetica", 'B', 12); pdf.cell(0, 10, "DATA PELANGGAN", ln=1); pdf.set_font("Helvetica", size=11)
        pdf.cell(50, 8, "Nama Pelanggan"); pdf.cell(0, 8, f": {nama_asli}", ln=1)
        pdf.cell(50, 8, "No. NIK / KTP"); pdf.cell(0, 8, f": {user_input.get('nik*', '')}", ln=1)
        pdf.cell(50, 8, "No. WhatsApp"); pdf.cell(0, 8, f": {user_input.get('no_hp*', '')}", ln=1)
        pdf.cell(50, 8, "Alamat"); pdf.cell(0, 8, f": {user_input.get('alamat*', '')}", ln=1)
        tgl_aktif_pdf = user_input.get('tgl_aktif*', '-')
        if tgl_aktif_pdf and '-' in tgl_aktif_pdf: tgl_aktif_pdf = datetime.strptime(tgl_aktif_pdf, '%Y-%m-%d').strftime('%d/%m/%Y')
        pdf.cell(50, 8, "Tanggal Aktif"); pdf.cell(0, 8, f": {tgl_aktif_pdf}", ln=1)
        pdf.cell(50, 8, "Tanggal Jatuh Tempo"); pdf.cell(0, 8, f": Tiap Tanggal {tgl_jt}", ln=1)
        pdf.ln(5); pdf.set_font("Helvetica", 'B', 12); pdf.cell(0, 10, "DATA TEKNIS", ln=1); pdf.set_font("Helvetica", size=11)
        pdf.cell(50, 8, "Hasil Redaman FO"); pdf.cell(0, 8, f": {redam} dBm", ln=1)
        pdf.cell(50, 8, "Link Maps"); pdf.cell(0, 8, f": {maps_link}", ln=1)
        pdf.ln(15); pdf.cell(95, 8, "Teknisi Lapangan,", align='C'); pdf.cell(95, 8, "Pelanggan,", align='C', ln=1)
        pdf.ln(15); pdf.cell(95, 8, "( ____________________ )", align='C'); pdf.cell(95, 8, f"( {nama_asli} )", align='C', ln=1)
        pdf.add_page(); pdf.set_font("Helvetica", 'B', 14); pdf.cell(0, 10, "LAMPIRAN DOKUMENTASI", ln=1, align='C'); pdf.ln(10)
        if 'final_ktp_path' in user_data[chat_id] and os.path.exists(user_data[chat_id]['final_ktp_path']):
            pdf.image(user_data[chat_id]['final_ktp_path'], x=15, y=pdf.get_y()+5, w=110); pdf.set_y(pdf.get_y() + 80)
        if 'final_rumah_path' in user_data[chat_id] and os.path.exists(user_data[chat_id]['final_rumah_path']):
            pdf.image(user_data[chat_id]['final_rumah_path'], x=15, y=pdf.get_y()+5, w=110)
        pdf.output(lokasi_pdf)
    except Exception as e: print(f"❌ PDF Error: {e}")

    wa_ok, prorata_print, hari_print = kirim_wa_terima_kasih(user_input.get('no_hp*', ''), nama_asli, tgl_jt, user_input.get('biaya_paket', '0'), user_input.get('tgl_aktif*', ''))
    send_wa_bot(chat_id, f"✅ *PROSES PSB BERHASIL!*\n\nPelanggan *{nama_asli}* sukses didata.\n• File Excel & PDF aman di NAS.\n• Invoice WA: {'Terkirim' if wa_ok else 'Gagal'}")
    del user_data[chat_id]

# ================= WEB ROUTES =================
@app.route('/')
def root_redirect(): return redirect(url_for('dashboard'))

@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'POST':
        user, pwd = request.form['username'], request.form['password']
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        c.execute("SELECT password FROM admins WHERE username=?", (user,))
        row = c.fetchone(); conn.close()
        if row and check_password_hash(row[0], pwd):
            session['admin_user'] = user; return redirect(url_for('dashboard'))
        return render_template('login.html', error="Username / Password Salah!")
    return render_template('login.html')

@app.route('/register', methods=['GET', 'POST'])
def register_page():
    if request.method == 'POST':
        user, pwd, token = request.form['username'], request.form['password'], request.form['token']
        if token != WA_API_TOKEN: return render_template('register.html', error="Master Token Salah!")
        conn = sqlite3.connect(DB_PATH); c = conn.cursor()
        try:
            c.execute("INSERT INTO admins (username, password) VALUES (?, ?)", (user, generate_password_hash(pwd)))
            conn.commit(); conn.close()
            return render_template('login.html', msg="Admin berhasil dibuat!")
        except sqlite3.IntegrityError: conn.close(); return render_template('register.html', error="Username sudah ada!")
    return render_template('register.html')

@app.route('/logout')
def logout(): session.pop('admin_user', None); return redirect(url_for('login_page'))

@app.route('/dashboard')
@login_required
def dashboard():
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT id, name, phone, lid FROM whitelist ORDER BY id DESC")
    whitelist_data = [{"id": r[0], "name": r[1], "phone": r[2], "lid": r[3]} for r in c.fetchall()]
    conn.close()
    return render_template('dashboard.html', username=session['admin_user'], current_mode=get_setting('mode'), whitelist=whitelist_data)

@app.route('/api/whitelist/add', methods=['POST'])
@login_required
def api_add_whitelist():
    data = request.json
    name, raw_phone = data.get('name', '').strip(), data.get('phone', '').strip()
    if not name or not raw_phone: return jsonify({"error": "Nama dan Nomor wajib diisi!"}), 400
    phone = clean_phone(raw_phone)
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    try:
        c.execute("INSERT INTO whitelist (phone, name) VALUES (?, ?)", (phone, name))
        conn.commit(); conn.close()
        return jsonify({"status": "success"})
    except sqlite3.IntegrityError:
        conn.close(); return jsonify({"error": f"Nomor {phone} sudah terdaftar!"}), 400

@app.route('/api/whitelist/delete/<int:id>', methods=['POST'])
@login_required
def api_del_whitelist(id):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("DELETE FROM whitelist WHERE id=?", (id,)); conn.commit(); conn.close()
    return jsonify({"status": "success"})

@app.route('/api/gateway-status')
@login_required
def api_gw_status():
    def get_gw(url):
        try: return requests.get(f"{url}/qr-status", timeout=2).json()
        except Exception: return {"status": "disconnected", "qr": None}
    return jsonify({"bot": get_gw(URL_GW_BOT), "pelanggan": get_gw(URL_GW_PELANGGAN)})

@app.route('/api/set-mode', methods=['POST'])
@login_required
def api_set_mode(): set_setting('mode', request.json.get('mode', 'dual')); return jsonify({"status": "success"})

@app.route('/api/restart/<target>', methods=['POST'])
@login_required
def api_restart_container(target):
    url = URL_GW_BOT if target == 'wa-bot' else URL_GW_PELANGGAN
    try: requests.post(f"{url}/restart", timeout=1)
    except Exception: pass
    return jsonify({"status": "restarting"})

# ================= WEBHOOK GATEWAY =================
@app.route('/webhook', methods=['POST'])
def webhook_gateway():
    payload = request.json
    if not payload: return jsonify({"status": "error"}), 400
    chat_id = payload.get('from', '')
    body = payload.get('body', '').strip()
    msg_type = payload.get('type', 'chat')
    sender_clean = clean_phone(chat_id.split('@')[0])
    
    # 1. ENGINE PENGIKAT OTOMATIS (PAIRING VIA HP FISIK)
    if body.lower().startswith(('/tautkan', 'tautkan')):
        parts = body.split(' ')
        if len(parts) == 2 and parts[1].isdigit():
            target_lid = parts[1]
            conn = sqlite3.connect(DB_PATH); c = conn.cursor()
            c.execute("SELECT name FROM whitelist WHERE phone=?", (sender_clean,))
            owner = c.fetchone()
            if owner:
                c.execute("UPDATE whitelist SET lid=? WHERE phone=?", (target_lid, sender_clean))
                conn.commit(); conn.close()
                send_wa_bot(chat_id, f"✅ *TAUTAN BERHASIL!*\n\nID Unix Laptop `{target_lid}` resmi diikat ke operator *{owner[0]}*.\nSekarang kamu bisa ngetik perintah PSB dari laptop tersebut.")
            else:
                conn.close(); send_wa_bot(chat_id, "❌ Ditolak: Nomor HP kamu belum didaftarkan Admin di Dashboard.")
        return jsonify({"status": "ok"}), 200

    # 2. SATPAM PENGECEK IDENTITAS (PHONE / UNIX)
    user_name = get_whitelisted_user(sender_clean)

    if not user_name:
        # Jika pengirim asing ini ternyata berupa angka Unix (Laptop yang belum terikat), pandu dia!
        if len(sender_clean) > 13 and not sender_clean.startswith('628'):
            msg_panduan = (
                "⚠️ *LAPTOP TEKNISI BELUM TERTAUT*\n\n"
                f"ID Unix Laptop kamu: `{sender_clean}`\n\n"
                "Agar laptop ini bisa dipakai mengetik PSB, **buka WhatsApp di HP kamu**, lalu kirimkan kalimat ini ke Bot:\n\n"
                f"`/tautkan {sender_clean}`"
            )
            send_wa_bot(chat_id, msg_panduan)
        return jsonify({"status": "ignored"}), 200

    if body.lower() in ['/batal', 'batal']:
        user_data.pop(chat_id, None); send_wa_bot(chat_id, "🚫 Sesi dihentikan.")
        return jsonify({"status": "success"}), 200

    if body.lower().startswith(('/cari', 'cari')):
        parts = body.split(' ', 1)
        if len(parts) > 1:
            files = [f for f in os.listdir(EXCEL_PATH) if f.endswith('.xlsx') and not f.startswith('~')]
            hasil = [f.replace('.xlsx', '') for f in files if parts[1].lower() in f.lower()]
            send_wa_bot(chat_id, f"✅ Ditemukan {len(hasil)} data:\n" + "\n".join([f"- {h}" for f in hasil]) if hasil else "❌ Tidak ditemukan.")
        return jsonify({"status": "success"}), 200

    if body.lower().startswith(('/hapus', 'hapus')):
        parts = body.split(' ', 1)
        if len(parts) > 1:
            nama_target = parts[1].replace(" ", "_").replace("/", "_")
            target_excel = os.path.join(EXCEL_PATH, f"{nama_target}.xlsx")
            if os.path.exists(target_excel):
                os.remove(target_excel)
                for f in glob.glob(os.path.join(KTP_PATH, f"*{nama_target}*")) + glob.glob(os.path.join(RUMAH_PATH, f"*{nama_target}*")): os.remove(f)
                send_wa_bot(chat_id, f"✅ Data {nama_target} dihapus.")
            else: send_wa_bot(chat_id, "❌ Tidak ditemukan.")
        return jsonify({"status": "success"}), 200

    if body.lower() in ['/rekap', 'rekap']:
        bln = datetime.now().strftime('%Y-%m')
        files = [f for f in os.listdir(EXCEL_PATH) if f.endswith('.xlsx') and not f.startswith('~')]
        rekap = [f"- {f.replace('.xlsx', '')}" for f in files if datetime.fromtimestamp(os.path.getmtime(os.path.join(EXCEL_PATH, f))).strftime('%Y-%m') == bln]
        send_wa_bot(chat_id, f"📊 REKAP BULAN INI ({len(rekap)}):\n" + "\n".join(rekap) if rekap else "Belum ada data.")
        return jsonify({"status": "success"}), 200

    if body.lower() in ['/psb', 'psb'] and chat_id not in user_data:
        if not os.path.exists(TEMPLATE_MASTER):
            send_wa_bot(chat_id, "⚠️ Template master Excel tidak ada di NAS!")
            return jsonify({"status": "error"}), 200
        user_data[chat_id] = {'step': 0, 'data': {}}
        send_wa_bot(chat_id, f"📝 *[PSB] Mulai*\n\nMasukkan *{QUESTIONS[0][1]}*:")
        return jsonify({"status": "success"}), 200

    if chat_id in user_data:
        step = user_data[chat_id]['step']
        if step < len(QUESTIONS):
            kolom = QUESTIONS[step][0]
            jwb = body if body != '-' else ''
            if kolom == 'nik*' and (not jwb.isdigit() or len(jwb) != 16):
                send_wa_bot(chat_id, "❌ NIK wajib 16 digit angka. Masukkan ulang:"); return jsonify({"status": "ok"}), 200
            if kolom == 'no_hp*' and not jwb.isdigit():
                send_wa_bot(chat_id, "❌ No HP hanya angka. Masukkan ulang:"); return jsonify({"status": "ok"}), 200
            if kolom == 'tgl_aktif*':
                try: jwb = datetime.strptime(jwb, '%d/%m/%Y').strftime('%Y-%m-%d')
                except ValueError: send_wa_bot(chat_id, "❌ Format DD/MM/YYYY. Ulangi:"); return jsonify({"status": "ok"}), 200
            
            user_data[chat_id]['data'][kolom] = jwb
            step += 1; user_data[chat_id]['step'] = step
            send_wa_bot(chat_id, f"Masukkan *{QUESTIONS[step][1]}*:" if step < len(QUESTIONS) else "📍 Kirim *Share Location*:")
            return jsonify({"status": "ok"}), 200

        elif step == len(QUESTIONS):
            if payload.get('lat') and payload.get('lng'):
                user_data[chat_id]['data']['lat*'] = payload.get('lat')
                user_data[chat_id]['data']['lon*'] = payload.get('lng')
            else: user_data[chat_id]['data']['lat*'], user_data[chat_id]['data']['lon*'] = '', ''
            user_data[chat_id]['step'] = 9
            send_wa_bot(chat_id, "🪪 Kirimkan *Foto KTP*:")
            return jsonify({"status": "ok"}), 200

        elif step in [9, 10]:
            if not payload.get('media_base64'):
                send_wa_bot(chat_id, "❌ Wajib kirim gambar!"); return jsonify({"status": "ok"}), 200
            nama = user_data[chat_id]['data'].get('nama_pelanggan*', 'NoName').replace(" ", "_").replace("/", "_")
            prefix = "KTP" if step == 9 else "RUMAH"
            path_dir = KTP_PATH if step == 9 else RUMAH_PATH
            fpath = os.path.join(path_dir, f"{prefix}_{nama}_{datetime.now().strftime('%Y%m%d%H%M%S')}.jpg")
            with open(fpath, "wb") as fh: fh.write(base64.b64decode(payload['media_base64']))
            beri_watermark_foto(fpath, user_data[chat_id]['data'].get('lat*', '-'), user_data[chat_id]['data'].get('lon*', '-'))
            user_data[chat_id][f'final_{prefix.lower()}_path'] = fpath
            if step == 9:
                user_data[chat_id]['step'] = 10
                send_wa_bot(chat_id, "🏠 Terakhir, Kirimkan *Foto Rumah*:")
            else:
                send_wa_bot(chat_id, "⏳ Menyimpan ke NAS...")
                simpan_data_keseluruhan(chat_id)
            return jsonify({"status": "ok"}), 200

    return jsonify({"status": "idle"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
