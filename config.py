from utils import MultiOwnerList
from emoji_ids import *   # semua ID_xxx emoji diimport dari emoji_ids.py

# ============================================================
# GANTI INI SAAT GANTI PEMILIK BOT
# ============================================================

# ---------- TELEGRAM BOT ----------
BOT_TOKEN = "8721545378:AAHXo6hvyc6e2qP5ZhBu7DABliJcvpDRl3M"

OWNER_ID = MultiOwnerList([8758561227, 672705587])

# ---------- TELEGRAM API (untuk Telethon/MTProto) ----------
API_ID   = 33416936
API_HASH = "f46ac638f3d4926fffb3981995decf07"

# ---------- CHANNEL UTAMA ----------
CHANNEL_ID = [
    -1004390531779,
    -1004390531779,
    -1004390531779
]

CHANNEL_LINK_1 = "https://t.me/manxyofficiall"
CHANNEL_LINK_2 = "https://t.me/infoautoordermanxy"
CHANNEL_LINK_3 = "https://t.me/allinfomanxy"

# ---------- NGROK (Mini App HTTPS tunnel) ----------
# Isi authtoken dari dashboard ngrok.com -> Your Authtoken.
# NGROK_DOMAIN pakai domain statis gratis yang udah di-reserve di akun ngrok,
# supaya URL-nya ga ganti-ganti tiap kali container restart.
NGROK_AUTHTOKEN = "cr_3HdFOMc8pyygrcJI8eHJEIu1p41"
NGROK_DOMAIN = "urgency-casino-flight.ngrok-free.dev"

# ---------- NOTIFICATION CHANNELS ----------
# Semua ID channel notif SEKARANG bisa diatur di sini. Isi dengan ID numerik
# channel (contoh: -1001234567890). Kalau mau semua notif ke channel yang
# sama, isi saja dengan angka yang sama seperti contoh di bawah.
LINK_CH_NOTIF_GMAIL     = -1004487841585   # notif email blast selesai
LINK_CH_NOTIF_USER      = -1004487841585   # notif user baru bergabung
LINK_CH_NOTIF_PURCHASE  = (-1004487841585, -1004487841585)   # notif pembelian (session/QRIS/manual)
LINK_CH_NOTIF_FIXMERAH  = -1004487841585   # notif Fix Merah selesai
LINK_CH_NOTIF_DEPOSIT   = (-1004487841585, -1004487841585)  # notif deposit saldo berhasil
LINK_CH_NOTIF_STOCK     = (-1004487841585, -1004487841585)  # notif stok baru ditambahkan
LINK_CH_NOTIF_GIFT      = (-1004487841585, -1004487841585)   # notif pembelian gift
LINK_CH_NOTIF_RUMAHOTP  = (-1004487841585, -1004487841585)   # notif pembelian nomor RumahOTP
LINK_CH_NOTIF_CLONE     = -1004487841585   # notif clone bot baru & transaksi clone
LINK_CH_NOTIF_WITHDRAW  = (-1004487841585, -1004487841585)   # notif withdraw clone bot
LINK_CH_NOTIF_STARS     = (-1004487841585, -1004487841585)   # notif pembelian topup Stars (single & bulk)
LINK_CH_NOTIF_TON       = (-1004487841585, -1004487841585)   # notif pembelian topup TON
LINK_CH_NOTIF_PREMIUM   = (-1004487841585, -1004487841585)   # notif pembelian topup Telegram Premium

# ---------- LOG GROUP PRIVATE (untuk forward trick emoji premium ke channel) ----------
LOG_GROUP_ID = -1004483205983

# ---------- MEDIA CONFIG ----------
PHOTO_MAIN_MENU  = "https://files.catbox.moe/vwrq3b.png"
PHOTO_MENU_GIFT  = "https://files.catbox.moe/2pnr4y.png"
PHOTO_MENU_GMAIL = "https://files.catbox.moe/mn0cz6.png"
PHOTO_MENU_NOKOS = "https://files.catbox.moe/vwrq3b.png"

# ---------- LINK BOT ----------
LINK_BOT = "https://t.me/AutoorderManxyzBot"

# ---------- 2FA DEFAULT ----------
DEFAULT_2FA_PASSWORD = "#123"

# ---------- PAYMENT (Pakasir) ----------
PAKASIR_API_KEY = "PAKSIR APIKEY LU"
PAKASIR_SLUG    = "PAKASIR SLUG LU"

# ---------- PAYMENT (Nevapedia) ----------
# Ambil API Key di dashboard Nevapedia -> menu Profil.
NEVAPEDIA_API_KEY = "SKY_d56d2a32cc374190"

# ---------- GATEWAY PEMBAYARAN QRIS OTOMATIS ----------
# Gateway yang dipakai pertama kali sebelum owner pernah ganti lewat tombol
# "Ganti Gateway" di Owner Panel. Pilihan: "pakasir" atau "nevapedia".
# Setelah bot jalan, gateway aktif disimpan & diatur dari DB (tombol Owner
# Panel), bukan dari sini lagi — ini cuma nilai default awal.
PAYMENT_GATEWAY_DEFAULT = "pakasir"

# ---------- DIRECTORY ----------
SESSION_DIR = "sessions"
QR_DIR      = "qr"

# ---------- HARGA DEFAULT ----------
DEFAULT_SESSION_PRICE = 5000

# ---------- COOLDOWN ----------
COOLDOWN_DURATION = 0

# ---------- BOT MODE ----------
BOT_MODE = "normal"

# ---------- RUMAHOTP API ----------
RUMAHOTP_BASE_URL  = "https://www.rumahotp.io/api"
RUMAHOTP_API_KEY   = "rk-dev-fi2gCFcQheeZBi5p1NqfbhTI6oSddq5w"
NOKOS_PROFIT_PERCENT = 500

# ---------- SUPPORT ----------
SUPPORT_USERNAME = "manxystore"

# ---------- FOOTER LINK MENU UTAMA ----------
# Baris link yang otomatis muncul di bawah tabel menu utama (/start), dan
# juga muncul lagi setiap kali user tekan tombol "Batal"/"Kembali" yang
# balik ke menu utama. Teks di kolom pertama itu label yang tampil ke user
# (link aslinya disembunyikan di belakang teks itu), kolom kedua link
# tujuannya. Tinggal edit di sini, gak perlu sentuh kode lain. Mau
# tambah/kurang baris juga tinggal tambah/hapus tuple-nya.
FOOTER_LINKS = [
    ("Nama",                            LINK_BOT),
    ("Hubungi Customer Service",        f"https://t.me/{SUPPORT_USERNAME}"),
    ("Information Bot",                 CHANNEL_LINK_2),
    ("Information Topup Stars & TON",   CHANNEL_LINK_1),
]

# ---------- FITUR NEGO HARGA (AI) — KHUSUS MENU BUY NOKTEL ----------
# Isi API key Anthropic di sini supaya bot bisa nego harga otomatis pakai AI.
# Kalau masih kosong/placeholder ("xxxx"), fitur nego otomatis nonaktif dan
# user akan diarahkan beli dengan harga normal.
ANTHROPIC_API_KEY = "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"
NEGO_MODEL = "claude-sonnet-4-6"
NEGO_ENABLED_DEFAULT = True          # default kalau owner belum pernah atur lewat /setnego
NEGO_MAX_DISKON_PERSEN_DEFAULT = 15  # batas diskon maksimum (%) yang boleh disetujui AI
NEGO_DEAL_TTL_MENIT = 30             # harga hasil nego berlaku berapa menit sebelum hangus

# ============================================================
# FUNGSI DETEKSI BENDERA — dipindah ke src/id_emoji_bendera.py
# Fungsi dapatkan_bendera() tetap ada di sini agar kompatibel
# dengan kode lama yang import dari config.
# ============================================================

def dapatkan_bendera(phone_num: str) -> str:
    """Deteksi kode negara nomor telepon → kembalikan custom emoji bendera premium."""
    from src.id_emoji_bendera import EMOJI_BENDERA
    p = str(phone_num).replace("+", "").strip()

    # Bangun FLAG_MAP dari EMOJI_BENDERA yang sudah ada di id_emoji_bendera.py
    # Mapping kode telepon → nama key di EMOJI_BENDERA
    PHONE_TO_KEY = {
        # 3 digit
        "855": "kamboja", "856": "Laos",   "880": "Bangladesh",
        "886": "hongkong","960": "oman",   "966": "saudi Arabia",
        "971": "Emirates Arab",
        # 2 digit
        "20": "egypt",    "27": "south afrika", "31": "Netherlands",
        "32": "Belgia",   "33": "France",  "34": "Spain",
        "39": "Italy",    "40": "Romania", "44": "Inggris",
        "45": "Denmark",  "49": "Germany", "51": "peru",
        "52": "meksiko",  "54": "Argentina","55": "brazil",
        "56": "Chile",    "57": "colombia","58": "Venezuela",
        "60": "malaysia", "61": "Australia","62": "Indonesia",
        "63": "Filipina", "65": "Singapore","66": "thailand",
        "81": "japan",    "82": "south Korea","84": "vietnam",
        "86": "china",    "90": "turki",   "91": "india",
        "92": "pakistan", "95": "myanmar", "98": "Iran",
        # 1 digit
        "1": "usa",       "7": "Negara",
    }

    for code in sorted(PHONE_TO_KEY.keys(), key=len, reverse=True):
        if p.startswith(code):
            key = PHONE_TO_KEY[code]
            if key in EMOJI_BENDERA:
                eid, flag, *_ = EMOJI_BENDERA[key]
                return f"<tg-emoji emoji-id='{eid}'>{flag}</tg-emoji>"

    return "🌐"
