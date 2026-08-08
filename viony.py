# -*- coding: utf-8 -*-
import os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, 'src', 'qr', 'session_stock.db')

# Buat direktori jika belum ada
DB_DIR = os.path.dirname(DB_PATH)
os.makedirs(DB_DIR, exist_ok=True)

# ── Redam log/warning "sampah" biar console panel gak penuh ─────────────────
# Ini yang bikin muncul baris kayak "Unclosed client session", "Unclosed
# connector", "Attempt 1 at new auth_key failed", warning "async sessions
# support is an experimental feature", dsb — semuanya cuma noise teknis dari
# library (asyncio/aiohttp/telethon), BUKAN error yang perlu ditindaklanjuti,
# jadi didiemin di level WARNING ke atas aja. Kalau suatu saat perlu debug
# lebih detail, tinggal turunin level logger yang relevan jadi DEBUG lagi.
import logging
import warnings

logging.basicConfig(level=logging.WARNING)
for _noisy_logger in (
    "asyncio",          # sumber "Unclosed client session" / "Unclosed connector"
    "telethon",         # sumber "Attempt 1 at new auth_key failed" dkk
    "aiohttp",
    "aiohttp.client",
    "httpx",
    "httpcore",
):
    logging.getLogger(_noisy_logger).setLevel(logging.CRITICAL)
# CATATAN: logger "telegram"/"telegram.ext" SENGAJA gak ikut diredam ke
# CRITICAL — itu logger python-telegram-bot sendiri, dipakai buat lapor kalau
# ada exception beneran di dalam handler bot. Kalau ikut diredam, error asli
# pas ada bug malah jadi gak kelihatan sama sekali di console.

warnings.filterwarnings("ignore", message="Using async sessions support is an experimental feature")
warnings.filterwarnings("ignore", category=ResourceWarning)
# ──────────────────────────────────────────────────────────────────────────

import asyncio
import math
import re
import html
import sqlite3
import traceback
import time
import qrcode
import httpx
import telegram
from uuid import uuid4
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
from telegram.constants import ChatMemberStatus
from telethon import TelegramClient
from telethon.errors import (
    SessionPasswordNeededError, PhoneNumberInvalidError,
    PhoneCodeInvalidError, PhoneCodeExpiredError, PasswordHashInvalidError,
    FloodWaitError,
)
from telethon.sessions import StringSession

from config import *
from session_backup_handler import send_stock_backup, cmd_restore_sessions, cmd_check_sessions
from telegram import InputMediaPhoto
from utils import get_premium_country_flag
from functools import wraps
from src import notif
from src import miniorder_bridge
from src import clone_system
from src import nego_ai
from src.custom_emoji import premium_text, emoji, emoji_id, styled_button, styled_inline_keyboard
from src.main_menu import create_main_menu, create_page2_menu, GIFT_ITEMS, _fmt

# ── Footer link menu utama (masked link, sumbernya diatur di config.FOOTER_LINKS) ──
def build_footer_links_html() -> str:
    """Bangun baris-baris link (teks doang yang keliatan, url disembunyikan
    di belakangnya) buat ditaruh di bawah tabel menu utama. Dipakai baik di
    pesan /start maupun di setiap tombol Batal/Kembali yang balik ke menu
    utama, supaya link-nya selalu konsisten muncul. Isi & urutan link diatur
    sepenuhnya lewat FOOTER_LINKS di config.py, jadi kalau mau ganti teks
    atau url cukup edit di sana."""
    import config
    rows = getattr(config, "FOOTER_LINKS", [])
    lines = []
    for label, url in rows:
        if not label or not url:
            continue
        lines.append(f'{emoji("url")} <a href="{url}">{label}</a>')
    return "\n".join(lines)

# ── Retry helper untuk handle ConnectTimeout / TimedOut ──────────────────────
async def _retry(coro_func, retries=3, delay=3):
    """Jalankan coroutine, retry hingga `retries` kali jika timeout."""
    last_err = None
    for attempt in range(retries):
        try:
            return await coro_func()
        except (telegram.error.TimedOut, telegram.error.NetworkError, httpx.ConnectTimeout) as e:
            last_err = e
            if attempt < retries - 1:
                await asyncio.sleep(delay)
    raise last_err

async def safe_send_photo(context, chat_id, photo, caption="", parse_mode="HTML", reply_markup=None, **kwargs):
    return await _retry(lambda: context.bot.send_photo(
        chat_id=chat_id, photo=photo, caption=caption,
        parse_mode=parse_mode, reply_markup=reply_markup, **kwargs
    ))

async def safe_send_message(context, chat_id, text, parse_mode="HTML", reply_markup=None, **kwargs):
    return await _retry(lambda: context.bot.send_message(
        chat_id=chat_id, text=text,
        parse_mode=parse_mode, reply_markup=reply_markup, **kwargs
    ))
# ─────────────────────────────────────────────────────────────────────────────
from src import gmail_reporter
from src import rumahotp

# ==================== AUTO-FALLBACK EMOJI PREMIUM ====================
from telegram.error import BadRequest as _EmojiBadRequest

_TG_EMOJI_RE = re.compile(r'<tg-emoji\b[^>]*>([\s\S]*?)</tg-emoji>')

def _strip_tg_emoji(text):
    if not isinstance(text, str):
        return text
    return _TG_EMOJI_RE.sub(lambda m: m.group(1), text)

def _is_emoji_entity_error(err):
    msg = str(err)
    return "Entity_text_invalid" in msg or "CUSTOM_EMOJI" in msg.upper() or "custom emoji" in msg.lower()

def _patch_emoji_fallback(method_name, text_kw):
    original = getattr(telegram.Bot, method_name)
    @wraps(original)
    async def wrapper(self, *args, **kwargs):
        try:
            return await original(self, *args, **kwargs)
        except _EmojiBadRequest as e:
            if not _is_emoji_entity_error(e):
                raise
            text_val = kwargs.get(text_kw)
            if text_val is None:
                raise
            stripped = _strip_tg_emoji(text_val)
            if stripped == text_val:
                raise
            kwargs[text_kw] = stripped
            return await original(self, *args, **kwargs)
    setattr(telegram.Bot, method_name, wrapper)

for _m, _kw in [
    ("send_message", "text"),
    ("edit_message_text", "text"),
    ("send_photo", "caption"),
    ("edit_message_caption", "caption"),
    ("send_video", "caption"),
    ("send_document", "caption"),
]:
    _patch_emoji_fallback(_m, _kw)
# ==================== END AUTO-FALLBACK EMOJI PREMIUM ====================


# --- SETUP DATABASE PERMANEN ---
def setup_database():
    conn = sqlite3.connect(DB_PATH) # Pakai DB_PATH juga di sini
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('bot_mode', 'normal')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('payment_method', 'otomatis')")
    cursor.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('payment_gateway', ?)", (PAYMENT_GATEWAY_DEFAULT,))
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS blocked_users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            blocked_at INTEGER,
            reason TEXT DEFAULT ''
        )
    """)
    conn.commit()
    conn.close()

setup_database()
nego_ai.setup_nego_tables(DB_PATH)

from telegram import Bot as _TgBot

# ==================== BOT PUSAT UNTUK CEK MEMBER CHANNEL ====================
# Semua bot CLONE memakai handler yang sama persis dengan bot utama (lihat
# clone_system.spawn_clone_bot). Supaya bot clone TIDAK perlu di-invite/dijadikan
# admin di channel/grup wajib-join, pengecekan get_chat_member SELALU dilakukan
# lewat instance bot PUSAT (BOT_TOKEN di config.py), bukan lewat context.bot
# (yang kalau update datang dari clone, adalah bot clone itu sendiri).
# _MAIN_BOT_INSTANCE diisi sekali di main() setelah Application utama dibangun,
# supaya reuse koneksi yang sama (bukan bikin instance baru tiap kali dicek).
_MAIN_BOT_INSTANCE = None


def _get_main_bot_for_check():
    """Ambil instance Bot PUSAT (bukan bot clone) untuk keperluan get_chat_member."""
    global _MAIN_BOT_INSTANCE
    if _MAIN_BOT_INSTANCE is None:
        # Fallback jaga-jaga kalau dipanggil sebelum main() sempat set instance-nya
        # (mis. clone start lebih dulu / edge-case). Bikin instance baru sekali,
        # lalu simpan supaya panggilan berikutnya reuse instance yang sama.
        _MAIN_BOT_INSTANCE = _TgBot(token=BOT_TOKEN)
    return _MAIN_BOT_INSTANCE


async def check_sub(update, context):
    """
    Cek apakah user sudah join semua channel/grup wajib.

    PENTING: pengecekan SELALU pakai bot PUSAT (_get_main_bot_for_check), TIDAK
    PERNAH pakai context.bot. Ini supaya cukup bot pusat saja yang perlu jadi
    member/admin di channel/grup wajib-join tsb — bot CLONE tidak perlu
    di-invite dan tidak perlu dijadikan admin di channel manapun, tapi fitur
    wajib-join tetap jalan normal untuk semua user clone bot juga (karena
    user_id yang sama dicek ke channel yang sama, terlepas dari bot mana yang
    sedang mereka pakai).
    """
    user_id = update.effective_user.id   
    channels = CHANNEL_ID if isinstance(CHANNEL_ID, list) else [CHANNEL_ID]
    main_bot = _get_main_bot_for_check()

    try:
        for ch_id in channels:
            member = await main_bot.get_chat_member(chat_id=ch_id, user_id=user_id)
            if member.status not in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]:
                return False
        return True 
    except Exception as e:
        print(f"Error saat cek sub banyak channel: {e}")
    return False
    
# ==================== DETECT NEGARA ====================
    
os.makedirs(SESSION_DIR, exist_ok=True)
os.makedirs(QR_DIR, exist_ok=True)

# ==================== DATABASE SETUP ====================
conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
cursor = conn.cursor()
try:
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA busy_timeout=30000")
except Exception as _wal_err:
    print(f"[Warning] Gagal set WAL mode: {_wal_err}")

cursor.execute("""
CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    username TEXT,
    deposit_balance INTEGER DEFAULT 0,
    belance_balance INTEGER DEFAULT 0,
    created_at INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS user_stats (
    user_id INTEGER PRIMARY KEY,
    total_bought INTEGER DEFAULT 0,
    last_buy INTEGER DEFAULT 0
)
""")

# Withdraw saldo manual dari Mini App (Wallet) -- dibuat di sini juga (bukan
# cuma di miniapp/app.py) supaya kalau bot dijalankan duluan sebelum Mini App
# pernah dibuka, tabelnya sudah pasti ada.
cursor.execute("""
CREATE TABLE IF NOT EXISTS wallet_withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    username TEXT,
    amount INTEGER NOT NULL,
    method TEXT,
    destination TEXT,
    account_name TEXT,
    status TEXT DEFAULT 'pending',
    note TEXT,
    created_at INTEGER,
    processed_at INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS session_stock (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_string TEXT,
    phone TEXT,
    username TEXT,
    account_id INTEGER,
    price INTEGER,
    status TEXT DEFAULT 'available',
    created_at INTEGER
)
""")

try:
    cursor.execute("ALTER TABLE session_stock ADD COLUMN label TEXT DEFAULT 'No Tag'")
except:
    pass

try:
    cursor.execute("ALTER TABLE session_stock ADD COLUMN status_limit TEXT DEFAULT 'No Limit'")
except:
    pass

# === FIX PENTING: perbaiki baris session_stock yang kolom `id`-nya NULL ===
# Di beberapa file database lama, tabel session_stock kebentuk dengan kolom `id` bertipe
# TEXT PRIMARY KEY (bukan INTEGER PRIMARY KEY AUTOINCREMENT). Karena bukan alias rowid,
# kolom itu TIDAK terisi otomatis — dan karena INSERT di add_to_stock() nggak pernah
# nyertain kolom id, semua baris lama jadi punya id = NULL.
# Akibatnya: query "WHERE id=?" (dipakai buat nampilin & memvalidasi list stok) nggak
# pernah match sama row manapun, jadi listnya keliatan kosong walaupun "Total Stok"-nya
# kelihatan benar (soalnya hitungannya nggak nyaring pakai id).
# Migrasi ini isi otomatis id yang NULL pakai rowid asli si baris (unik & aman).
try:
    cursor.execute("SELECT rowid FROM session_stock WHERE id IS NULL")
    _null_id_rows = [r[0] for r in cursor.fetchall()]
    if _null_id_rows:
        for _rid in _null_id_rows:
            cursor.execute("UPDATE session_stock SET id = ? WHERE rowid = ?", (_rid, _rid))
        conn.commit()
        print(f"[MIGRASI] Perbaiki {len(_null_id_rows)} baris session_stock yang kolom id-nya kosong (NULL).")
except Exception as _mig_err:
    print(f"[MIGRASI] Gagal cek/perbaiki kolom id session_stock: {_mig_err}")

cursor.execute("""
CREATE TABLE IF NOT EXISTS pending_payments (
    id TEXT PRIMARY KEY,
    user_id INTEGER,
    amount INTEGER,
    status TEXT DEFAULT 'pending',
    qr_path TEXT,
    message_id INTEGER,
    expires_at INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS sold_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    stock_id INTEGER,
    buyer_id INTEGER,
    phone TEXT,
    username TEXT,
    account_id INTEGER,
    password TEXT,
    session_string TEXT,
    created_at INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS withdraw_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    amount INTEGER,
    method TEXT,
    payment_number TEXT,
    account_name TEXT,
    status TEXT DEFAULT 'pending',
    created_at INTEGER
)
""")

try:
    cursor.execute("ALTER TABLE withdraw_requests ADD COLUMN origin_bot_token TEXT")
except:
    pass

try:
    cursor.execute("ALTER TABLE pending_payments ADD COLUMN order_id TEXT")
except:
    pass

try:
    cursor.execute("ALTER TABLE pending_payments ADD COLUMN created_at INTEGER")
except:
    pass

try:
    # Simpan detail gift (target, gift_id, pesan, dll) sebagai JSON di baris
    # payment-nya sendiri -- bukan cuma di context.user_data yang hilang kalau
    # bot restart/crash di tengah proses kirim. Lihat _recover_stuck_gift_orders().
    cursor.execute("ALTER TABLE pending_payments ADD COLUMN gift_json TEXT")
except:
    pass

cursor.execute("""
CREATE TABLE IF NOT EXISTS nokos_orders (
    order_id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    price INTEGER NOT NULL,
    status TEXT DEFAULT 'pending',
    created_at INTEGER DEFAULT (strftime('%s','now'))
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS stars_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_code TEXT UNIQUE,
    user_id INTEGER,
    username TEXT,
    target_username TEXT,
    qty INTEGER,
    price INTEGER,
    status TEXT DEFAULT 'pending',
    paid_via TEXT,
    proof_file_id TEXT,
    result_message TEXT,
    created_at INTEGER DEFAULT (strftime('%s','now'))
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS stars_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS stars_bulk_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_code TEXT UNIQUE,
    user_id INTEGER,
    username TEXT,
    targets TEXT,
    qty_each INTEGER,
    price INTEGER,
    status TEXT DEFAULT 'pending',
    paid_via TEXT,
    proof_file_id TEXT,
    result_message TEXT,
    created_at INTEGER DEFAULT (strftime('%s','now'))
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS premium_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_code TEXT UNIQUE,
    user_id INTEGER,
    username TEXT,
    target_username TEXT,
    duration_bulan INTEGER,
    price INTEGER,
    status TEXT DEFAULT 'pending',
    paid_via TEXT,
    proof_file_id TEXT,
    result_message TEXT,
    created_at INTEGER DEFAULT (strftime('%s','now'))
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS premium_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS ton_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_code TEXT UNIQUE,
    user_id INTEGER,
    username TEXT,
    target_address TEXT,
    amount_ton REAL,
    price INTEGER,
    status TEXT DEFAULT 'pending',
    paid_via TEXT,
    proof_file_id TEXT,
    result_message TEXT,
    created_at INTEGER DEFAULT (strftime('%s','now'))
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS ton_topup_settings (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at TEXT
)
""")

conn.commit()


# ==================== CLONE BOT SYSTEM — TABEL DATABASE ====================
cursor.execute("""
CREATE TABLE IF NOT EXISTS clone_bots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    bot_token TEXT UNIQUE NOT NULL,
    bot_username TEXT,
    bot_name TEXT,
    komisi_persen REAL DEFAULT 10,
    status TEXT DEFAULT 'pending',
    created_at INTEGER DEFAULT (strftime('%s','now')),
    approved_at INTEGER,
    last_active INTEGER
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS clone_wallets (
    owner_id INTEGER PRIMARY KEY,
    saldo INTEGER DEFAULT 0,
    total_diterima INTEGER DEFAULT 0,
    total_ditarik INTEGER DEFAULT 0
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS clone_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clone_id INTEGER NOT NULL,
    owner_id INTEGER NOT NULL,
    buyer_id INTEGER,
    order_id TEXT,
    jenis TEXT,
    harga_jual INTEGER,
    komisi_persen REAL,
    komisi_rupiah INTEGER,
    created_at INTEGER DEFAULT (strftime('%s','now'))
)
""")

cursor.execute("""
CREATE TABLE IF NOT EXISTS clone_withdraw_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    method TEXT,
    payment_number TEXT,
    account_name TEXT,
    status TEXT DEFAULT 'pending',
    created_at INTEGER DEFAULT (strftime('%s','now')),
    processed_at INTEGER
)
""")

try:
    cursor.execute("ALTER TABLE clone_withdraw_requests ADD COLUMN origin_bot_token TEXT")
except:
    pass

conn.commit()

# ==================== GLOBAL STATE ====================
login_state = {}
multi_state = {}
user_states = {}
user_orders = {}
pending_direct_buy = {}
gift_manual_pending = {}  # {user_id: gift_data} — antrian gift mode manual menunggu approve owner
stars_manual_pending = {}  # {user_id: stars_data} — antrian topup stars mode manual menunggu approve owner
session_manual_pending = {}  # {user_id: pending_direct_buy_data} — antrian beli session mode manual menunggu approve owner
deposit_manual_origin = {}  # {user_id: bot_token} — bot ASAL tempat user kirim bukti TF deposit manual,
                             # dipakai supaya notif approve/tolak deposit dikirim balik lewat bot yang sama
                             # (bukan selalu bot pusat, karena Owner selalu approve lewat bot pusat)
stock_batch_queue = {}  # {user_id: {"pending": [phone,...], "done": int, "failed": [(phone, reason), ...]}} — antrian add stock banyak sekaligus
cooldown_config = {"duration": COOLDOWN_DURATION}
is_broadcasting = False

BOT_MODE = "public"

def get_bot_mode():
    global BOT_MODE
    return BOT_MODE

def get_payment_method() -> str:
    """Ambil metode payment aktif dari DB: 'otomatis' atau 'manual'"""
    try:
        import sqlite3 as _sq3
        _c = _sq3.connect(DB_PATH)
        row = _c.execute("SELECT value FROM settings WHERE key='payment_method'").fetchone()
        _c.close()
        return row[0] if row else "otomatis"
    except:
        return "otomatis"

def set_payment_method(method: str):
    """Simpan metode payment ke DB. method: 'otomatis' atau 'manual'"""
    try:
        import sqlite3 as _sq3
        _c = _sq3.connect(DB_PATH)
        _c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('payment_method', ?)", (method,))
        _c.commit()
        _c.close()
    except Exception as e:
        print(f"[set_payment_method] {e}")

def get_active_gateway() -> str:
    """Ambil gateway QRIS otomatis yang aktif dari DB: 'pakasir' atau 'nevapedia'."""
    try:
        import sqlite3 as _sq3
        _c = _sq3.connect(DB_PATH)
        row = _c.execute("SELECT value FROM settings WHERE key='payment_gateway'").fetchone()
        _c.close()
        return row[0] if row else "pakasir"
    except:
        return "pakasir"

def set_active_gateway(gateway: str):
    """Simpan gateway QRIS otomatis aktif ke DB. gateway: 'pakasir' atau 'nevapedia'."""
    try:
        import sqlite3 as _sq3
        _c = _sq3.connect(DB_PATH)
        _c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('payment_gateway', ?)", (gateway,))
        _c.commit()
        _c.close()
    except Exception as e:
        print(f"[set_active_gateway] {e}")

def check_maintenance_decorator(func):
    @wraps(func)
    async def wrapper(update, context, *args, **kwargs):
        mode = get_bot_mode()
        user_id = update.effective_user.id if update.effective_user else None
        
        if mode == "maintenance" and user_id != OWNER_ID:
            if update.callback_query:
                try: await update.callback_query.answer("Bot sedang maintenance.", show_alert=True)
                except: pass
            return # <--- Pintu tertutup rapat
        return await func(update, context, *args, **kwargs)
    return wrapper

# ==================== HELPER FUNCTIONS ====================

async def safe_answer(q, text: str = "", show_alert: bool = False) -> None:
    """Wrapper q.answer() agar tidak crash saat query sudah expired/timeout."""
    try:
        if text:
            await q.answer(text, show_alert=show_alert)
        else:
            await safe_answer(q)
    except Exception:
        pass

def is_owner(user_id: int) -> bool:
    global OWNER_ID
    
    if isinstance(OWNER_ID, list):
        return int(user_id) in [int(x) for x in OWNER_ID]
        
    if isinstance(OWNER_ID, str):
        if "," in OWNER_ID:
            list_id = [int(x.strip()) for x in OWNER_ID.split(",") if x.strip().isdigit()]
            return int(user_id) in list_id
        elif OWNER_ID.isdigit():
            return int(user_id) == int(OWNER_ID)
            
    try:
        return int(user_id) == int(OWNER_ID)
    except:
        return False

# ─── HELPER: BLOCKED USERS ────────────────────────────────────────────────────
def is_blocked(user_id: int) -> bool:
    try:
        conn_b = sqlite3.connect(DB_PATH)
        cur_b = conn_b.cursor()
        cur_b.execute("SELECT 1 FROM blocked_users WHERE user_id = ?", (int(user_id),))
        result = cur_b.fetchone()
        conn_b.close()
        return result is not None
    except:
        return False

def block_user(user_id: int, username: str = "", reason: str = ""):
    try:
        conn_b = sqlite3.connect(DB_PATH)
        cur_b = conn_b.cursor()
        cur_b.execute(
            "INSERT OR REPLACE INTO blocked_users (user_id, username, blocked_at, reason) VALUES (?, ?, ?, ?)",
            (int(user_id), username, int(time.time()), reason)
        )
        conn_b.commit()
        conn_b.close()
        return True
    except:
        return False

def unblock_user(user_id: int):
    try:
        conn_b = sqlite3.connect(DB_PATH)
        cur_b = conn_b.cursor()
        cur_b.execute("DELETE FROM blocked_users WHERE user_id = ?", (int(user_id),))
        conn_b.commit()
        conn_b.close()
        return True
    except:
        return False

def get_blocked_list():
    try:
        conn_b = sqlite3.connect(DB_PATH)
        cur_b = conn_b.cursor()
        cur_b.execute("SELECT user_id, username, blocked_at, reason FROM blocked_users ORDER BY blocked_at DESC")
        rows = cur_b.fetchall()
        conn_b.close()
        return rows
    except:
        return []

# ─── GUARD: tolak semua akses dari grup/supergroup/channel ───────────────────
async def is_private_chat(update: Update) -> bool:
    """Return True jika chat adalah private. Diam saja di grup/channel."""
    chat = update.effective_chat
    if chat and chat.type != "private":
        return False
    return True

def format_currency(amount):
    return f"Rp{amount:,}".replace(",", ".")

# Cache instance Bot per-token supaya tidak bikin objek telegram.Bot baru
# berkali-kali tiap kali ada notifikasi balik ke pembeli.
_ORIGIN_BOT_CACHE = {}

def get_origin_bot(origin_token, fallback_bot=None):
    """Ambil instance bot yang SESUAI dengan token asal transaksi (bot PUSAT
    ATAU bot CLONE tempat pembeli order), supaya notifikasi hasil approve/tolak
    dikirim balik lewat bot yang SAMA dengan tempat pembeli order — BUKAN selalu
    lewat context.bot.

    PENTING kenapa ini perlu: alur approve manual (session/gift/deposit) selalu
    diproses Owner lewat BOT PUSAT (lihat owner_notify_bot() di bawah — permintaan
    approval SELALU dikirim ke Owner via bot pusat). Akibatnya waktu Owner klik
    tombol Approve/Tolak, context.bot yang aktif di handler itu adalah BOT PUSAT,
    padahal pembelinya bisa jadi order dari BOT CLONE. Kalau notif hasil approve/
    tolak dikirim pakai context.bot begitu saja, pembeli yang order dari clone
    malah dapat notif dari bot pusat (bot yang tidak pernah mereka chat), dan
    clone_system.process_transaction_commission() juga jadi salah deteksi bot
    asal (dikira transaksi bot pusat -> komisi clone tidak pernah tercatat).

    origin_token: token bot yang disimpan waktu data pending dibuat (lihat
    field 'origin_bot_token' di pending_direct_buy / gift_pending / dll).
    fallback_bot: dipakai kalau origin_token kosong (data lama sebelum fix ini
    dibuat) atau kalau origin_token sama dengan bot yang lagi aktif.
    """
    if not origin_token:
        return fallback_bot
    if fallback_bot is not None and getattr(fallback_bot, "token", None) == origin_token:
        return fallback_bot
    cached = _ORIGIN_BOT_CACHE.get(origin_token)
    if cached is not None:
        return cached
    try:
        bot = telegram.Bot(token=origin_token)
        _ORIGIN_BOT_CACHE[origin_token] = bot
        return bot
    except Exception as e:
        print(f"[get_origin_bot] Gagal buat bot instance utk token asal: {e}")
        return fallback_bot


def owner_notify_bot(context):
    """Bot instance yang dipakai KHUSUS untuk kirim notif approval ke Owner
    (deposit/session/gift manual, dll).

    PENTING: kalau handler ini lagi jalan di CLONE BOT, context.bot adalah
    bot clone tsb — kalau notif approval dikirim pakai context.bot, Owner
    akan menerimanya lewat chat bot clone, bukan lewat bot pusat. Makanya
    di sini SELALU dipakai bot pusat (BOT_TOKEN dari config), sama seperti
    pola yang sudah dipakai di src/clone_system.py (main_bot = Bot(token=...)).
    """
    try:
        if getattr(context.bot, "token", None) == BOT_TOKEN:
            return context.bot
    except Exception:
        pass
    return telegram.Bot(token=BOT_TOKEN)

async def send_photo_to_owner(context, target_owner, photo_file_id, caption, reply_markup):
    """Kirim foto bukti transfer ke Owner, SELALU lewat bot pusat.

    file_id foto hasil upload user cuma valid untuk bot yang menerimanya —
    kalau lagi jalan di clone bot, file_id itu tidak bisa langsung dipakai
    ulang oleh bot pusat. Jadi di sini file-nya didownload dulu pakai
    context.bot (bot yang benar-benar menerima), baru diupload ulang lewat
    bot pusat ke Owner.
    """
    main_bot = owner_notify_bot(context)
    if main_bot is context.bot:
        return await context.bot.send_photo(
            chat_id=target_owner, photo=photo_file_id,
            caption=caption, parse_mode="HTML", reply_markup=reply_markup,
        )
    tg_file = await context.bot.get_file(photo_file_id)
    file_bytes = await tg_file.download_as_bytearray()
    return await main_bot.send_photo(
        chat_id=target_owner, photo=bytes(file_bytes),
        caption=caption, parse_mode="HTML", reply_markup=reply_markup,
    )

def _extract_edit_text(args, kwargs):
    if args:
        return args[0], args[1:]
    return kwargs.pop("text", None), args

async def fast_edit(query, *args, rich_html=None, log_label="FastEdit", **kwargs):
    """
    Edit callback messages quickly and safely.

    rich_html: kalau diisi, pesan LAMA dihapus dan pesan BARU dikirim sebagai
    Rich Message (tabel bergaris dkk) lewat notif.send_rich_message_to_chat,
    dengan text_value (arg pertama / kwargs['text']) sebagai fallback teks biasa
    kalau rich message gagal. Ini dipakai di semua submenu (baik InlineKeyboardMarkup
    maupun ReplyKeyboardMarkup) supaya konsisten tampil sebagai rich message,
    bukan cuma menu-menu yang reply keyboard doang.
    """
    text_value, remaining_args = _extract_edit_text(args, kwargs)
    if text_value is None:
        text_value = ""

    message = getattr(query, "message", None)

    async def _delete_and_resend():
        chat_id = message.chat_id if message else None
        bot = query._bot if hasattr(query, "_bot") else getattr(message, "_bot", None)
        if not (bot and chat_id):
            return None
        try:
            await message.delete()
        except Exception:
            pass
        if rich_html:
            return await notif.send_rich_message_to_chat(
                bot, chat_id, rich_html, text_value,
                reply_markup=kwargs.get("reply_markup"),
                log_label=log_label,
            )
        return await bot.send_message(chat_id=chat_id, text=text_value, **kwargs)

    # Reply Keyboard tidak bisa dipasang lewat edit pesan (Telegram API hanya
    # izinkan itu untuk Inline Keyboard) — jadi langsung kirim pesan baru.
    if isinstance(kwargs.get("reply_markup"), ReplyKeyboardMarkup):
        try:
            return await _delete_and_resend()
        except Exception:
            pass
        return None

    # Kalau rich_html diisi (dan reply_markup-nya Inline/None), tetap hapus +
    # kirim baru sebagai rich message — supaya submenu yang biasanya di-edit
    # di tempat juga tampil sebagai tabel bergaris, bukan cuma pesan pertama.
    if rich_html:
        try:
            return await _delete_and_resend()
        except Exception:
            pass
        return None

    has_media = bool(message and (
        getattr(message, "photo", None)
        or getattr(message, "video", None)
        or getattr(message, "animation", None)
        or getattr(message, "document", None)
    ))

    if has_media:
        try:
            return await query.edit_message_caption(caption=text_value, **kwargs)
        except Exception as err:
            err_str = str(err).lower()
            # Jika error karena reply markup terlalu panjang atau media tidak bisa diedit,
            # kirim pesan teks baru sebagai fallback
            if "reply markup is too long" in err_str or "message is not modified" in err_str or "there is no caption" in err_str:
                try:
                    chat_id = message.chat_id
                    bot = query._bot if hasattr(query, "_bot") else getattr(message, "_bot", None)
                    if bot and chat_id:
                        try:
                            await message.delete()
                        except Exception:
                            pass
                        return await bot.send_message(chat_id=chat_id, text=text_value, **kwargs)
                except Exception:
                    pass
            return None

    try:
        return await query.edit_message_text(text_value, *remaining_args, **kwargs)
    except Exception as err:
        err_str = str(err).lower()
        if "reply markup is too long" in err_str:
            try:
                chat_id = message.chat_id if message else None
                bot = query._bot if hasattr(query, "_bot") else getattr(message, "_bot", None)
                if bot and chat_id:
                    try:
                        await message.delete()
                    except Exception:
                        pass
                    return await bot.send_message(chat_id=chat_id, text=text_value, **kwargs)
            except Exception:
                pass
        return None

async def send_main_menu_new(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Kirim menu utama (Page 1 - Buy Noktel) sebagai rich message. Dipakai setelah QRIS dibatalkan/dihapus."""
    from src.main_menu import PAGE1_REPLY_MAP
    keyboard = create_main_menu(user_id, is_owner_func=is_owner)

    context.user_data["current_menu_state"] = "main_menu"
    context.user_data["active_menu_page"] = 1
    set_page_reply_map(context, "page1_main", PAGE1_REPLY_MAP)

    msg = await notif.send_rich_message_to_chat(
        context.bot, user_id, TEXT_MENU_HTML, TEXT_MENU,
        reply_markup=keyboard,
        log_label="Page1MainMenu",
    )
    return msg

async def safe_delete_message(bot, chat_id: int, message_id: int):
    if not message_id:
        return False
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception:
        return False

async def safe_delete_callback_message(query):
    try:
        if query and query.message:
            return await safe_delete_message(query.message.get_bot(), query.message.chat_id, query.message.message_id)
    except Exception:
        pass
    return False

def get_thumbnail_path():
    """Ambil foto menu utama dari config.py (PHOTO_MAIN_MENU).
    Bisa berupa URL (http/https) atau path file lokal."""
    import config
    photo = getattr(config, "PHOTO_MAIN_MENU", None)
    if photo:
        if photo.startswith("http://") or photo.startswith("https://"):
            return photo
        if os.path.exists(photo):
            return photo
    fallback = os.path.join("media", "thumbnail.jpg")
    return fallback if os.path.exists(fallback) else None


def get_gift_thumbnail_path():
    """Ambil foto khusus Menu Gift dari config.py (PHOTO_MENU_GIFT).
    Fallback ke PHOTO_MAIN_MENU jika tidak diset."""
    import config
    photo = getattr(config, "PHOTO_MENU_GIFT", None)
    if photo:
        if photo.startswith("http://") or photo.startswith("https://"):
            return photo
        if os.path.exists(photo):
            return photo
    # Fallback ke thumbnail menu utama
    return get_thumbnail_path()


def get_menu_photo_path(attr_name: str):
    """Ambil foto khusus suatu menu dari config.py berdasarkan nama atribut
    (contoh: 'PHOTO_MENU_FIXMERAH'). Fallback ke PHOTO_MAIN_MENU jika kosong/tidak diset."""
    import config
    photo = getattr(config, attr_name, None)
    if photo:
        if photo.startswith("http://") or photo.startswith("https://"):
            return photo
        if os.path.exists(photo):
            return photo
    return get_thumbnail_path()


async def send_with_menu_photo(context: ContextTypes.DEFAULT_TYPE, chat_id: int, photo_attr: str, text: str, reply_markup=None):
    """Kirim pesan baru dengan foto khusus menu (dari config.py) + caption, fallback ke teks polos."""
    thumb = get_menu_photo_path(photo_attr)
    if thumb:
        if thumb.startswith("http"):
            return await safe_send_photo(context, chat_id, photo=thumb, caption=text, reply_markup=reply_markup)
        try:
            with open(thumb, "rb") as f:
                return await safe_send_photo(context, chat_id, photo=f.read(), caption=text, reply_markup=reply_markup)
        except Exception:
            return await safe_send_message(context, chat_id, text=text, reply_markup=reply_markup)
    return await safe_send_message(context, chat_id, text=text, reply_markup=reply_markup)


async def notify_success_channel(context: ContextTypes.DEFAULT_TYPE, channel_attr: str, text: str, html_content: str = None, reply_markup=None):
    """Kirim notifikasi ke channel (ID numerik, @username, atau link https://t.me/...) yang diatur di config.py.
    Tidak melakukan apa-apa kalau config kosong / belum diisi. Tidak pernah melempar error ke pemanggil.

    Kalau html_content diisi, dikirim via trik LOG_GROUP → forward ke channel (supaya custom emoji
    premium ikut render, karena Telegram tidak izinkan bot kirim custom emoji langsung ke channel).
    Kalau html_content kosong, kirim `text` biasa lewat parse_mode=HTML.
    reply_markup diteruskan ke rich message / send_message.
    """
    import config
    from src.notif import get_start_bot_button, _send_to_log_group_then_forward
    raw_target = getattr(config, channel_attr, None)
    if not raw_target:
        return

    # Default button "Start Bot" kalau tidak ada reply_markup custom
    if reply_markup is None:
        reply_markup = get_start_bot_button("danger")

    target = str(raw_target).strip()

    # Konversi link (https://t.me/namachannel atau t.me/namachannel) -> @namachannel
    if "t.me/" in target:
        username_part = target.split("t.me/")[-1].split("?")[0].strip("/").strip()
        if username_part.startswith("+"):
            # Ini link invite private (t.me/+xxxxx) -> bot tidak bisa kirim pakai ini,
            # wajib pakai ID numerik channel (contoh: -1001234567890)
            print(f"[notify_success_channel] {channel_attr} pakai link invite ('{raw_target}'). "
                  f"Ganti dengan ID numerik channel (contoh: -1001234567890), bukan link invite.")
            return
        target = "@" + username_part
    # Konversi ID numerik dalam bentuk string -> int (chat_id channel wajib angka, contoh -1001234567890)
    elif target.lstrip("-").isdigit():
        target = int(target)

    if html_content is not None:
        # Pakai trik LOG_GROUP → forward ke channel agar custom emoji premium ikut render
        await _send_to_log_group_then_forward(
            context.bot, target, html_content, text, reply_markup=reply_markup, log_label=channel_attr
        )
        return

    try:
        await context.bot.send_message(chat_id=target, text=text, parse_mode="HTML", reply_markup=reply_markup)
    except Exception as e:
        print(f"[notify_success_channel] Gagal kirim notif ke {channel_attr} ({raw_target} -> {target}): {e}")
        print(f"[notify_success_channel] Pastikan bot sudah jadi ADMIN di channel tersebut, "
              f"dan isi config dengan ID numerik channel (contoh: -1001234567890) atau @username publik.")



async def send_photo_or_text(context: ContextTypes.DEFAULT_TYPE, chat_id: int, caption: str, reply_markup=None):
    """Kirim pesan baru dengan foto thumbnail (dari config) + caption, fallback ke teks polos kalau gak ada foto."""
    thumb_path = get_thumbnail_path()
    if thumb_path:
        if thumb_path.startswith("http"):
            return await safe_send_photo(context, chat_id, photo=thumb_path, caption=caption, reply_markup=reply_markup)
        else:
            with open(thumb_path, "rb") as photo_file:
                photo_bytes = photo_file.read()
            return await safe_send_photo(context, chat_id, photo=photo_bytes, caption=caption, reply_markup=reply_markup)
    return await safe_send_message(context, chat_id, text=caption, reply_markup=reply_markup)

def remove_user_record(user_id: int):
    try:
        cursor.execute("DELETE FROM users WHERE user_id = ?", (int(user_id),))
        conn.commit()
    except Exception as e:
        print(f"Gagal menghapus user {user_id}: {e}")

def get_all_user_ids():
    cursor.execute("SELECT user_id FROM users ORDER BY created_at ASC")
    return [int(r[0]) for r in cursor.fetchall() if r and r[0] is not None]

async def send_broadcast_payload(bot, uid: int, content, mode: str):
    if mode == "text":
        safe_text = premium_text(html.escape(str(content)))
        await bot.send_message(chat_id=uid, text=safe_text, parse_mode="HTML")
    else:
        await bot.forward_message(chat_id=uid, from_chat_id=content.chat.id, message_id=content.message_id)

async def run_broadcast(bot, chat_id: int, status_msg, users: list, content, mode: str = "text"):
    global is_broadcasting
    BROADCAST_DELAY_SEC = 1
    EDIT_PROGRESS_INTERVAL = 10
    EDIT_DELAY_SEC = 2
    success = 0
    fail = 0
    removed = []
    last_edit = 0.0

    try:
        total = len(users)
        for index, uid in enumerate(users, start=1):
            try:
                await send_broadcast_payload(bot, uid, content, mode)
                success += 1
            except Exception as err:
                code = getattr(err, "error_code", None) or getattr(getattr(err, "response", None), "status_code", None)
                desc = str(err)
                if code == 429 or "RetryAfter" in err.__class__.__name__ or "Too Many Requests" in desc:
                    retry_after = int(getattr(err, "retry_after", 3) or 3)
                    await asyncio.sleep(retry_after)
                    try:
                        await send_broadcast_payload(bot, uid, content, mode)
                        success += 1
                    except Exception as retry_err:
                        retry_desc = str(retry_err)
                        retry_code = getattr(retry_err, "error_code", None) or getattr(getattr(retry_err, "response", None), "status_code", None)
                        if retry_code == 403 or re.search(r"blocked|user is deactivated|chat not found|forbidden", retry_desc, re.I):
                            remove_user_record(uid)
                            removed.append(uid)
                        fail += 1
                else:
                    if code == 403 or re.search(r"blocked|user is deactivated|chat not found|forbidden", desc, re.I):
                        remove_user_record(uid)
                        removed.append(uid)
                    fail += 1

            now = time.time()
            if (index % EDIT_PROGRESS_INTERVAL == 0 or index == total) and (now - last_edit >= EDIT_DELAY_SEC):
                last_edit = now
                progress_text = premium_text(f"""
[online] <b>BROADCAST DIMULAI</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[done] <b>Terkirim:</b> <code>{index}/{total}</code>
[warning] <b>Gagal:</b> <code>{fail}</code></blockquote>
""")
                try:
                    await notif.edit_rich_message(bot, chat_id, status_msg, progress_text, progress_text, log_label="BroadcastProgress")
                except Exception:
                    pass

            await asyncio.sleep(BROADCAST_DELAY_SEC)

        final_text = premium_text(f"""
[done] <b>BROADCAST SELESAI</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[done] <b>Terkirim:</b> <code>{success}/{total}</code>
[warning] <b>Gagal:</b> <code>{fail}</code>
[failed] <b>User Dihapus:</b> <code>{len(removed)}</code></blockquote>
""")
        try:
            await notif.edit_rich_message(bot, chat_id, status_msg, final_text, final_text, log_label="BroadcastResult")
        except Exception:
            pass
    finally:
        is_broadcasting = False

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global is_broadcasting
    sender_id = update.effective_user.id if update.effective_user else 0
    if not is_owner(sender_id):
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] <b>Akses Ditolak</b>
<hr/>
<p>[catatan] Perintah broadcast hanya dapat digunakan oleh Owner bot.</p>"""), premium_text("[warning] <b>Akses Ditolak</b>\n\n<blockquote>[catatan] Perintah broadcast hanya dapat digunakan oleh Owner bot.</blockquote>"), log_label="AutoRich2")
        return

    if is_broadcasting:
        wait_text = premium_text("""
[warning] <b>Broadcast Sedang Berlangsung</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Sistem sedang mengirim pesan ke semua pengguna.
[panahijo] Harap tunggu hingga proses saat ini selesai sebelum mengirim broadcast baru.</blockquote>
""")
        await update.message.reply_text(wait_text, parse_mode="HTML")
        return

    users = get_all_user_ids()
    if not users:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] <b>Tidak Ada Pengguna</b>
<hr/>
<p>[catatan] Database pengguna masih kosong. Belum ada yang menggunakan bot ini.</p>"""), premium_text("[warning] <b>Tidak Ada Pengguna</b>\n\n<blockquote>[catatan] Database pengguna masih kosong. Belum ada yang menggunakan bot ini.</blockquote>"), log_label="AutoRich2")
        return

    replied = update.message.reply_to_message if update.message else None
    if replied:
        content = replied
        mode = "forward"
    else:
        message_text = " ".join(context.args).strip() if getattr(context, "args", None) else ""
        if not message_text:
            usage = premium_text("""
[spikerbiru] <b>PANDUAN BROADCAST</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Gunakan salah satu format berikut:
[panahijo] <code>/broadcast Halo semuanya</code>
[panahijo] Reply pesan lalu kirim <code>/broadcast</code> untuk meneruskan pesan sebagai forward.</blockquote>
""")
            await update.message.reply_text(usage, parse_mode="HTML")
            return
        content = message_text
        mode = "text"

    is_broadcasting = True
    start_text = premium_text(f"""
[spikerbiru] <b>BROADCAST DIMULAI</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[done] <b>Terkirim:</b> <code>0/{len(users)}</code>
[warning] <b>Gagal:</b> <code>0</code></blockquote>
""")
    status_message = await notif.send_rich_message_to_chat(
        context.bot, update.message.chat_id, start_text, start_text,
        log_label="BroadcastLoading",
    )
    asyncio.create_task(run_broadcast(context.bot, update.message.chat_id, status_message, users, content, mode))


def save_session_file(phone: str, session_string: str):
    with open(f"{SESSION_DIR}/{phone}.session", "w") as f:
        f.write(session_string)

def update_user_stats(user_id: int):
    cursor.execute("INSERT INTO user_stats (user_id, total_bought, last_buy) VALUES (?,1,?) ON CONFLICT(user_id) DO UPDATE SET total_bought = total_bought + 1, last_buy = ?", 
                   (user_id, int(time.time()), int(time.time())))
    conn.commit()

def check_cooldown(user_id: int) -> bool:
    if cooldown_config["duration"] <= 0:
        return True
    cursor.execute("SELECT last_buy FROM user_stats WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row and row[0]:
        if time.time() - row[0] < cooldown_config["duration"]:
            return False
    return True

# ==================== DATABASE FUNCTIONS ====================
def get_user(user_id):
    cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
    return cursor.fetchone()

def create_user(user_id, username):
    cursor.execute("INSERT OR IGNORE INTO users (user_id, username, created_at) VALUES (?, ?, ?)", 
                   (user_id, username, int(time.time())))
    conn.commit()

def update_balance(user_id, deposit_delta=0, belance_delta=0):
    cursor.execute(
        "UPDATE users SET deposit_balance = deposit_balance + ?, belance_balance = belance_balance + ? WHERE user_id = ?",
        (deposit_delta, belance_delta, user_id)
    )
    conn.commit()



def get_available_stock():
    cursor.execute("SELECT id, account_id, phone, username, price, label, status_limit FROM session_stock WHERE status='available' ORDER BY id ASC")
    return [{"id": r[0], "account_id": r[1], "phone": r[2], "username": r[3], "price": r[4], "tag": r[5], "status_limit": r[6]} for r in cursor.fetchall()]

# ========== FUNGSI FILTER STOCK ==========
def get_stock_by_first_digit(first_digit: int):
    cursor.execute("SELECT id, account_id, phone, username, price, label, status_limit FROM session_stock WHERE status='available' AND CAST(account_id AS TEXT) LIKE ? ORDER BY id ASC", (f"{first_digit}%",))
    return [{"id": r[0], "account_id": r[1], "phone": r[2], "username": r[3], "price": r[4], "tag": r[5], "status_limit": r[6]} for r in cursor.fetchall()]

def get_stock_by_digit_count(digit_count: int):
    cursor.execute("SELECT id, account_id, phone, username, price, label, status_limit FROM session_stock WHERE status='available' AND LENGTH(CAST(account_id AS TEXT)) = ? ORDER BY id ASC", (digit_count,))
    return [{"id": r[0], "account_id": r[1], "phone": r[2], "username": r[3], "price": r[4], "tag": r[5], "status_limit": r[6]} for r in cursor.fetchall()]

def get_stock_all():
    cursor.execute("SELECT id, account_id, phone, username, price, label, status_limit FROM session_stock WHERE status='available' ORDER BY id ASC")
    
    return [
        {
            "id": r[0], 
            "account_id": r[1], 
            "phone": r[2], 
            "username": r[3], 
            "price": r[4],
            "tag": r[5],
            "status_limit": r[6]
        } 
        for r in cursor.fetchall()
    ]

def get_stock_count_by_first_digit(first_digit: int):
    cursor.execute("SELECT COUNT(*) FROM session_stock WHERE status='available' AND CAST(account_id AS TEXT) LIKE ?", (f"{first_digit}%",))
    return cursor.fetchone()[0]

def get_stock_count_by_digit_count(digit_count: int):
    cursor.execute("SELECT COUNT(*) FROM session_stock WHERE status='available' AND LENGTH(CAST(account_id AS TEXT)) = ?", (digit_count,))
    return cursor.fetchone()[0]

def get_stock_detail(stock_id):
    cursor.execute("SELECT id, session_string, phone, username, account_id, price FROM session_stock WHERE id=? AND status='available'", (stock_id,))
    return cursor.fetchone()

def get_stock_count():
    cursor.execute("SELECT COUNT(*) FROM session_stock WHERE status='available'")
    return cursor.fetchone()[0]

# --- FITUR BARU: FILTER BERTINGKAT PEMBELI ---
def get_stock_by_filter(label: str, status_limit: str):
    cursor.execute("""
        SELECT id, account_id, phone, username, price, label, status_limit 
        FROM session_stock 
        WHERE status='available' AND label=? AND status_limit=? 
        ORDER BY id ASC
    """, (label, status_limit))
    return [{"id": r[0], "account_id": r[1], "phone": r[2], "username": r[3], "price": r[4], "tag": r[5], "status_limit": r[6]} for r in cursor.fetchall()]

def get_stock_count_by_filter(label: str, status_limit: str):
    cursor.execute("""
        SELECT COUNT(*) 
        FROM session_stock 
        WHERE status='available' AND label=? AND status_limit=?
    """, (label, status_limit))
    return cursor.fetchone()[0]
# ---------------------------------------------

def get_sold_count():
    cursor.execute("SELECT COUNT(*) FROM sold_sessions")
    return cursor.fetchone()[0]

def get_total_income():
    cursor.execute("SELECT SUM(price) FROM session_stock WHERE status='sold'")
    row = cursor.fetchone()
    return row[0] if row[0] else 0

def get_total_users():
    cursor.execute("SELECT COUNT(*) FROM users")
    return cursor.fetchone()[0]

async def check_session_alive(session_string: str, timeout: int = 10) -> bool:
    """Cek cepat apakah sebuah session string masih aktif (belum logout/expired).
    Dipakai sebelum buyer bayar, supaya stok yang udah mati gak sempat kejual."""
    if not session_string:
        return False
    client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
    try:
        await asyncio.wait_for(client.connect(), timeout=timeout)
        if not await client.is_user_authorized():
            return False
        await asyncio.wait_for(client.get_me(), timeout=timeout)
        return True
    except Exception as e:
        print(f"[check_session_alive] session mati/error: {e}")
        return False
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

async def prune_dead_stock(context, items):
    """Cek ke Telegram apakah session tiap item masih hidup (belum logout/dihapus dari HP asli).
    Kalau ternyata udah mati, otomatis dihapus dari session_stock supaya nggak nyangkut terus
    di list stok. Pakai cache sementara (per-proses) biar nggak connect ke Telegram berkali-kali
    untuk item yang sama dalam waktu singkat (hemat waktu & hindari flood wait)."""
    if not items:
        return items

    cache = context.application.bot_data.setdefault('_session_alive_cache', {})
    now = time.time()
    CACHE_TTL = 600  # 10 menit

    to_check = []
    for item in items:
        db_id = item.get('id', '') if isinstance(item, dict) else item[0]
        last_checked = cache.get(db_id, 0)
        if now - last_checked > CACHE_TTL:
            to_check.append(db_id)

    if not to_check:
        return items

    async def _check(db_id):
        try:
            row = get_stock_detail(db_id)
            sess = row[1] if row else None
            alive = await check_session_alive(sess) if sess else False
        except Exception as e:
            print(f"[prune_dead_stock] gagal cek session {db_id}: {e}")
            alive = True  # kalau gagal cek, jangan buru-buru hapus
        return db_id, alive

    results = await asyncio.gather(*[_check(did) for did in to_check], return_exceptions=True)

    dead_ids = set()
    for res in results:
        if isinstance(res, Exception):
            continue
        db_id, alive = res
        cache[db_id] = now
        if not alive:
            dead_ids.add(db_id)

    if not dead_ids:
        return items

    for did in dead_ids:
        try:
            remove_stock(did)
            print(f"[prune_dead_stock] Stock {did} dihapus otomatis (session sudah logout/dihapus).")
        except Exception as e:
            print(f"[prune_dead_stock] gagal hapus stock {did}: {e}")

    return [i for i in items if (i.get('id', '') if isinstance(i, dict) else i[0]) not in dead_ids]

def remove_stock(stock_id):
    cursor.execute("DELETE FROM session_stock WHERE id=?", (stock_id,))
    conn.commit()
    try:
        from src.webapp_notify import notify
        notify("product_deleted", {"id": f"noktel:{stock_id}"})
    except Exception:
        pass

def update_stock_price(stock_id, new_price):
    cursor.execute("UPDATE session_stock SET price=? WHERE id=?", (new_price, stock_id))
    conn.commit()
    try:
        from src.webapp_notify import notify
        notify("price_updated", {"id": f"noktel:{stock_id}", "price": new_price})
    except Exception:
        pass

def add_to_stock(session_string, phone, username, account_id, price, label='No Tag', status_limit='No Limit'):
    cursor.execute("""
        INSERT INTO session_stock (session_string, phone, username, account_id, price, status, label, status_limit, created_at)
        VALUES (?, ?, ?, ?, ?, 'available', ?, ?, ?)
    """, (session_string, phone, username, account_id, price, label, status_limit, int(time.time())))
    new_id = cursor.lastrowid
    # FIX: paksa isi kolom `id` pakai rowid, karena di database lama kolom `id` bertipe
    # TEXT PRIMARY KEY (bukan alias rowid) sehingga TIDAK terisi otomatis kalau nggak
    # disebut eksplisit di kolom INSERT. Tanpa baris ini, `id` akan tetap NULL dan bikin
    # semua query "WHERE id=?" (buat nampilin/validasi list stok) gagal match.
    cursor.execute("UPDATE session_stock SET id = ? WHERE rowid = ?", (new_id, new_id))
    conn.commit()
    try:
        from src.webapp_notify import notify
        notify("product_added", {"id": f"noktel:{new_id}"})
    except Exception:
        pass
    return new_id

def mark_as_sold(stock_id, buyer_id, session_string, phone, username, account_id):
    cursor.execute("UPDATE session_stock SET status='sold' WHERE id=?", (stock_id,))

    # FIX: pastikan insert sold_sessions benar-benar berhasil, retry kalau kena "database locked"
    inserted = False
    last_err = None
    for _attempt in range(3):
        try:
            cursor.execute("""
                INSERT INTO sold_sessions (stock_id, buyer_id, phone, username, account_id, password, session_string, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (stock_id, buyer_id, phone, username, account_id, DEFAULT_2FA_PASSWORD, session_string, int(time.time())))
            conn.commit()
            inserted = True
            break
        except Exception as _ins_err:
            last_err = _ins_err
            print(f"[Error mark_as_sold insert] attempt {_attempt+1}: {_ins_err}")
            time.sleep(0.3)

    if not inserted:
        print(f"[CRITICAL] mark_as_sold GAGAL TOTAL untuk stock_id={stock_id} buyer_id={buyer_id}: {last_err}")
        raise RuntimeError(f"Gagal mencatat sold_sessions untuk stock_id={stock_id}: {last_err}")

    new_sold_id = cursor.lastrowid

    try:
        from src.webapp_notify import notify
        notify("sold_updated", {"id": f"noktel:{stock_id}"})
    except Exception:
        pass

    return new_sold_id

def get_bought_sessions(user_id):
    cursor.execute("SELECT id, phone, account_id, created_at FROM sold_sessions WHERE buyer_id=? ORDER BY created_at DESC", (user_id,))
    return [{"id": r[0], "phone": r[1], "account_id": r[2], "created_at": r[3]} for r in cursor.fetchall()]

def get_session_detail(session_id, user_id):
    cursor.execute("""
        SELECT id, phone, username, account_id, session_string, created_at 
        FROM sold_sessions 
        WHERE id=? AND buyer_id=?
    """, (session_id, user_id))
    row = cursor.fetchone()
    print(f"DEBUG get_session_detail: session_id={session_id}, user_id={user_id}, result={row}")
    return row

def delete_sold_session(session_id):
    cursor.execute("DELETE FROM sold_sessions WHERE id=?", (session_id,))
    conn.commit()

def add_pending_payment(user_id, order_id, amount, qr_path, message_id, expires_at):
    cursor.execute("""
        INSERT INTO pending_payments
        (id, user_id, amount, status, qr_path, message_id, expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (
        order_id,
        user_id,
        amount,
        "pending",
        qr_path,
        message_id,
        expires_at
    ))
    conn.commit()
    
def get_pending_payment(order_id):
    cursor.execute("""
        SELECT id, user_id, amount, status, qr_path, message_id, expires_at
        FROM pending_payments
        WHERE id=? AND status='pending'
    """, (order_id,))
    return cursor.fetchone()    

def update_payment_status(order_id, status):
    cursor.execute("UPDATE pending_payments SET status=? WHERE id=? OR order_id=?", (status, order_id, order_id))
    conn.commit()

def delete_pending_payment(order_id):
    cursor.execute("DELETE FROM pending_payments WHERE id=? OR order_id=?", (order_id, order_id))
    conn.commit()

def get_pending_payment_by_message(user_id, message_id):
    cursor.execute("""
        SELECT id, user_id, amount, status, qr_path, message_id, expires_at
        FROM pending_payments
        WHERE user_id=? AND message_id=? AND status='pending'
        ORDER BY expires_at DESC
        LIMIT 1
    """, (user_id, message_id))
    return cursor.fetchone()

def add_withdraw_request(user_id, amount, method, payment_number, account_name, origin_bot_token=None):
    cursor.execute("""
        INSERT INTO withdraw_requests (user_id, amount, method, payment_number, account_name, created_at, origin_bot_token)
        VALUES (?, ?, ?, ?, ?, ?, ?)
    """, (user_id, amount, method, payment_number, account_name, int(time.time()), origin_bot_token))
    conn.commit()
    return cursor.lastrowid

# ==================== PAYMENT FUNCTIONS ====================
# Template QRIS custom (kucing "Scan Me!!"). Area putih tempat QR ditempel
# ada di koordinat QRIS_BOX pada gambar template berukuran 736x736 px.
QRIS_TEMPLATE_PATH = os.path.join(BASE_DIR, "media", "qris_template.jpg")
QRIS_BOX = (175, 245, 565, 635)  # (left, top, right, bottom)

def _compose_qris_template(qr_data: str, out_path: str) -> bool:
    """Tempel QR asli (dari Pakasir) ke tengah template QRIS biar tampilannya bagus.

    Sebelumnya QR di-generate lalu di-stretch (resize) pas-in ke ukuran box —
    ini yang bikin modul QR jadi geriji/gak rapi karena ukuran modul jadi pecahan
    piksel. Sekarang QR digambar langsung di ukuran piksel-per-modul bulat
    (integer box_size), baru ditengahin ke box biar tajam & rapi.
    """
    try:
        from PIL import Image as PILImage, ImageDraw

        box_w = QRIS_BOX[2] - QRIS_BOX[0]
        box_h = QRIS_BOX[3] - QRIS_BOX[1]
        border = 2  # quiet zone dalam satuan modul QR

        # 1) "Probe" dulu buat tahu berapa jumlah modul QR-nya (tergantung panjang data)
        probe = qrcode.QRCode(border=border)
        probe.add_data(qr_data)
        probe.make(fit=True)
        total_modules = probe.modules_count + (border * 2)

        # 2) Hitung piksel-per-modul BULAT (integer) biar setiap kotak QR gak pecah/blur
        box_size = max(1, min(box_w, box_h) // total_modules)

        # 3) Render ulang QR pakai box_size yang sudah pas, biar tepiannya tajam
        qr = qrcode.QRCode(border=border, box_size=box_size)
        qr.add_data(qr_data)
        qr.make(fit=True)
        qr_img = qr.make_image(fill_color="black", back_color="white").convert("RGB")

        template = PILImage.open(QRIS_TEMPLATE_PATH).convert("RGB")

        # Bersihin dulu area box jadi putih polos (hapus sisa gambar lama kalau ada)
        ImageDraw.Draw(template).rectangle(QRIS_BOX, fill="white")

        # Tengah-tengahin QR di dalam box
        qw, qh = qr_img.size
        offset_x = QRIS_BOX[0] + (box_w - qw) // 2
        offset_y = QRIS_BOX[1] + (box_h - qh) // 2
        template.paste(qr_img, (offset_x, offset_y))

        template.save(out_path, format="PNG")
        return True
    except Exception as e:
        print(f"[QRIS Template] Gagal compose template: {e}")
        return False

GATEWAY_NEVAPEDIA_PREFIX = "NV-"  # penanda di depan order_id lokal khusus transaksi Nevapedia

async def _create_qris_pakasir(amount: int):
    """Buat QRIS otomatis via Pakasir (dipanggil oleh dispatcher create_qris)."""
    try:
        order_id = f"TOPUP-{int(time.time())}-{uuid4().hex[:6]}"
        os.makedirs(QR_DIR, exist_ok=True)

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.post(
                "https://app.pakasir.com/api/transactioncreate/qris",
                json={
                    "project": PAKASIR_SLUG,
                    "order_id": order_id,
                    "amount": amount,
                    "api_key": PAKASIR_API_KEY
                }
            )
            data = response.json()
            qr_data = data.get("payment", {}).get("payment_number")
            if not qr_data:
                print(f"QRIS data kosong (Pakasir): {data}")
                return None

            qr_path = os.path.join(QR_DIR, f"{order_id}.png")

            # Coba tempel ke template custom dulu, kalau gagal fallback ke QR polos.
            if not (os.path.exists(QRIS_TEMPLATE_PATH) and _compose_qris_template(qr_data, qr_path)):
                img = qrcode.make(qr_data)
                img.save(qr_path)

            if not os.path.exists(qr_path) or os.path.getsize(qr_path) <= 0:
                print(f"QRIS file gagal dibuat: {qr_path}")
                return None

            return {"id": order_id, "amount": amount, "qr_path": qr_path, "gateway": "pakasir"}
    except Exception as e:
        print(f"Error create QRIS (Pakasir): {e}")
        return None

async def _create_qris_nevapedia(amount: int):
    """Buat invoice QRIS otomatis via Nevapedia (dipanggil oleh dispatcher create_qris).

    Beda dengan Pakasir: Nevapedia tidak menerima order_id custom dan langsung
    membalas URL gambar QR jadi (qris_image), bukan raw string EMV QR — jadi
    gambarnya di-download apa adanya, tidak lewat template compose seperti punya
    Pakasir. order_id lokal diberi prefix "NV-" supaya check_payment_status tahu
    invoice ini harus dicek ke Nevapedia, apapun gateway aktif saat itu (jaga-jaga
    kalau owner ganti gateway sementara invoice lama masih pending).
    """
    try:
        os.makedirs(QR_DIR, exist_ok=True)

        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                "https://app.nevapedia.com/api/invoice",
                params={
                    "apikey": NEVAPEDIA_API_KEY,
                    "amount": amount
                }
            )
            data = response.json()
            if not data.get("success"):
                print(f"Gagal buat invoice (Nevapedia): {data}")
                return None

            real_invoice_id = data.get("invoice_id")
            qris_image_url = data.get("qris_image")
            if not real_invoice_id or not qris_image_url:
                print(f"Data invoice kosong (Nevapedia): {data}")
                return None

            order_id = f"{GATEWAY_NEVAPEDIA_PREFIX}{real_invoice_id}"
            qr_path = os.path.join(QR_DIR, f"{real_invoice_id}.png")

            img_resp = await client.get(qris_image_url)
            if img_resp.status_code == 200 and img_resp.content:
                with open(qr_path, "wb") as f:
                    f.write(img_resp.content)

            if not os.path.exists(qr_path) or os.path.getsize(qr_path) <= 0:
                print(f"QRIS file gagal didownload (Nevapedia): {qr_path}")
                return None

            return {"id": order_id, "amount": amount, "qr_path": qr_path, "gateway": "nevapedia"}
    except Exception as e:
        print(f"Error create QRIS (Nevapedia): {e}")
        return None

async def create_qris(amount: int):
    """Dispatcher: buat QRIS lewat gateway pembayaran otomatis yang lagi aktif
    (Pakasir atau Nevapedia), diatur owner lewat tombol Ganti Gateway."""
    gateway = get_active_gateway()
    if gateway == "nevapedia":
        return await _create_qris_nevapedia(amount)
    return await _create_qris_pakasir(amount)

async def _check_payment_status_pakasir(order_id: str, amount: int) -> bool:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                "https://app.pakasir.com/api/transactiondetail",
                params={
                    "project": PAKASIR_SLUG,
                    "order_id": order_id,
                    "amount": amount,
                    "api_key": PAKASIR_API_KEY
                }
            )
            data = response.json()
            status = data.get("transaction", {}).get("status") or data.get("payment", {}).get("status") or data.get("status", "")
            return str(status).lower() in ["paid", "success", "completed"]
    except:
        return False

async def _check_payment_status_nevapedia(real_invoice_id: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            response = await client.get(
                "https://app.nevapedia.com/api/invoice/status",
                params={
                    "apikey": NEVAPEDIA_API_KEY,
                    "invoice_id": real_invoice_id
                }
            )
            data = response.json()
            status = data.get("status", "")
            return str(status).lower() in ["paid", "success", "completed"]
    except:
        return False

async def check_payment_status(order_id: str, amount: int) -> bool:
    """Dispatcher: cek status bayar. Ditentukan dari prefix order_id (bukan dari
    gateway aktif sekarang), supaya invoice Nevapedia lama tetap kecek benar
    walau owner sudah ganti gateway aktif ke Pakasir, begitu juga sebaliknya."""
    if order_id.startswith(GATEWAY_NEVAPEDIA_PREFIX):
        real_invoice_id = order_id[len(GATEWAY_NEVAPEDIA_PREFIX):]
        return await _check_payment_status_nevapedia(real_invoice_id)
    return await _check_payment_status_pakasir(order_id, amount)

# ==================== SESSION FUNCTIONS ====================
async def auto_set_2fa(client, old_password=None):
    try:
        await client.edit_2fa(current_password=old_password, new_password=DEFAULT_2FA_PASSWORD, hint="Pw")
        return True
    except:
        return False

async def get_otp_from_session(phone: str) -> str:
    try:
        import os
        session_path = f"{SESSION_DIR}/{phone}.session"
        if not os.path.exists(session_path):
            print(f"DEBUG: File session tidak ditemukan: {session_path}")
            return None
        print(f"DEBUG: File session ditemukan: {session_path}")
        
        with open(session_path, "r") as f:
            session_str = f.read()
        print(f"DEBUG: Session string length: {len(session_str)}")
        
        from telethon import TelegramClient
        from telethon.sessions import StringSession
        
        client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
        await client.connect()
        print("DEBUG: Koneksi berhasil")
        
        if not await client.is_user_authorized():
            print("DEBUG: Session TIDAK AKTIF")
            await client.disconnect()
            return None
        print("DEBUG: Session AKTIF")
        
        messages = await client.get_messages(None, limit=10)
        print(f"DEBUG: Mendapat {len(messages)} pesan")
        
        import re
        for msg in messages:
            if msg.text:
                print(f"DEBUG PESAN: {msg.text[:100]}")
                code = re.search(r"\b\d{5,6}\b", msg.text)
                if code:
                    otp = code.group(0)
                    if not otp.startswith('0') and len(otp) in [5,6]:
                        print(f"DEBUG: OTP DITEMUKAN: {otp}")
                        await client.disconnect()
                        return otp
        
        print("DEBUG: TIDAK ADA OTP")
        await client.disconnect()
        return None
    except Exception as e:
        print(f"DEBUG ERROR: {e}")
        return None
    
async def force_logout_session(session_string):
    try:
        client = TelegramClient(StringSession(session_string), API_ID, API_HASH)
        await client.connect()
        await client.log_out()
        await client.disconnect()
        return True
    except:
        return False

# ==================== BUTTONS ====================

# Urutan & label tombol Owner Menu — dipakai sekali untuk membangun ReplyKeyboardMarkup
# dan sekali lagi untuk membentuk map "label -> callback_data lama" (dipakai oleh
# route_page_reply_button/dispatch_as_callback supaya semua handler owner_* yang
# sudah ada tetap jalan tanpa diubah).
_OWNER_MENU_LAYOUT = [
    [("Statistik",         "owner_stats",            "primary", "grafik"),
     ("List Permintaan",   "owner_list_requests",    "primary", "catatan")],
    [("List User Aktif",   "owner_list_users",       "primary", "crown"),
     ("List Blokir",       "owner_list_blokir",      "primary", "catatan")],
    [("Add Saldo",         "owner_add_saldo",        "success", "duitkarung"),
     ("Kurangi Saldo",     "owner_kurangi_saldo",    "danger",  "warning")],
    [("Add Stock",         "owner_add_stock",        "success", "download"),
     ("Hapus Stock",       "owner_remove_stock",     "danger",  "warning")],
    [("Blokir User",       "owner_blokir_user",      "danger",  "batal")],
    [("Set Cooldown",      "owner_set_cooldown",     "primary", "waktu"),
     ("Ganti Mode",        "owner_change_mode",      "primary", "gear")],
    [("Setting Harga",     "owner_set_price",        "success", "dolar"),
     ("Set Harga Gift",    "gift_owner_setprice",    "success", "star")],
    [("Set Payment",       "owner_set_payment",      "success", "dolar"),
     ("Ganti MT Payment",  "owner_ganti_mt_payment", "success", "gear")],
    [("Ganti Gateway",     "owner_ganti_gateway",    "success", "gear")],
    [("ON/OFF Gift",       "gift_owner_toggle",      "success", "gear"),
     ("Connect MTProto",   "gift_owner_login",       "success", "download")],
    [("Broadcast",         "owner_broadcast",        "primary", "spikerbiru"),
     ("Backup Data",       "owner_backup_data",      "primary", "catatan")],
    [("Restore User",      "owner_restore_user",     "primary", "download"),
     ("Backup User",       "owner_backup_user",      "primary", "catatan")],
    [("Kelola Clone Bot",  "owner_clone_manage",     "primary", "roket"),
     ("Kelola Withdraw",   "owner_wd_manage",        "primary", "dolar")],
    [("Nego Harga Noktel", "owner_nego_settings",    "success", "dolar")],
    [("Stars Topup Settings", "stars_owner_menu",    "success", "stars_ico")],
    [("Premium Topup Settings", "premium_owner_menu", "success", "premium_acc")],
    [("Menu Utama",        None,                     "danger",  "back")],  # label harus sama persis dengan RKB_BACK_MAIN
]

def create_owner_menu(context: ContextTypes.DEFAULT_TYPE = None) -> ReplyKeyboardMarkup:
    from src.custom_emoji import styled_keyboard_button
    rows = []
    label_to_data = {}
    for row in _OWNER_MENU_LAYOUT:
        rows.append([
            styled_keyboard_button(label, style=style, emoji_name=emoji_name)
            for (label, _cbdata, style, emoji_name) in row
        ])
        for (label, cbdata, _style, _emoji_name) in row:
            if cbdata is not None:
                label_to_data[label] = cbdata
    if context is not None:
        set_page_reply_map(context, "owner_menu", label_to_data)
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)

def create_my_sessions_keyboard(sessions: list, page: int = 0) -> InlineKeyboardMarkup:
    per_page = 5
    total_pages = (len(sessions) + per_page - 1) // per_page if sessions else 1
    start = page * per_page
    items = sessions[start:start+per_page]

    keyboard = []
    for item in items:
        keyboard.append([styled_button(f"{item['phone']} | ID {item['account_id']}", callback_data=f"detail_{item['id']}", style="primary", emoji_name="Telegram")])

    nav = []
    if page > 0:
        nav.append(styled_button("Prev", callback_data=f"my_page_{page-1}", style="primary", emoji_name="back"))
    if page < total_pages - 1:
        nav.append(styled_button("Next", callback_data=f"my_page_{page+1}", style="primary", emoji_name="panahijo"))
    if nav:
        keyboard.append(nav)

    keyboard.append([styled_button("Kembali", callback_data="menu_back", style="danger", emoji_name="back")])
    return styled_inline_keyboard(keyboard)

def create_session_detail_keyboard(session_id: int, phone: str) -> InlineKeyboardMarkup:
    return styled_inline_keyboard([
        [styled_button("Minta OTP", callback_data=f"req_otp_{session_id}", style="success", emoji_name="lightning")],
        [styled_button("Lihat Password", callback_data=f"lihat_password_{session_id}", style="success", emoji_name="password")],
        [styled_button("Kembali", callback_data="menu_my_sessions", style="danger", emoji_name="back")]
    ])

def create_payment_keyboard(order_id: str, session_id: int) -> InlineKeyboardMarkup:
    return styled_inline_keyboard([
        [styled_button("Verifikasi Bayar", callback_data=f"verify_direct_{order_id}_{session_id}", style="success", emoji_name="verified")],
        [styled_button("Batal", callback_data="cancel_direct_buy", style="danger", emoji_name="warning")]
    ])

def create_order_success_keyboard(stock_id: int, phone: str) -> InlineKeyboardMarkup:
    return styled_inline_keyboard([
        [
            styled_button("Minta OTP", callback_data=f"req_otp_{stock_id}", style="primary", emoji_name="lightning"),
            styled_button("Lihat Password", callback_data=f"lihat_password_{stock_id}", style="success", emoji_name="password")
        ],
        [styled_button("Selesai & Logout", callback_data=f"selesai_logout_{stock_id}", style="danger", emoji_name="done")]
    ])

def create_back_button() -> InlineKeyboardMarkup:
    return styled_inline_keyboard([[styled_button("Kembali", callback_data="menu_back", style="danger", emoji_name="back")]])

def create_cancel_button() -> InlineKeyboardMarkup:
    return styled_inline_keyboard([[styled_button("Batal", callback_data="cancel_input", style="danger", emoji_name="warning")]])

def create_payment_methods_keyboard() -> InlineKeyboardMarkup:
    return styled_inline_keyboard([
        [
            styled_button("DANA", callback_data="withdraw_dana", style="primary", emoji_name="duitkarung"),
            styled_button("GOPAY", callback_data="withdraw_gopay", style="primary", emoji_name="duitkarung")
        ],
        [styled_button("SEABANK", callback_data="withdraw_seabank", style="primary", emoji_name="duitkarung")],
        [styled_button("Kembali", callback_data="menu_back", style="danger", emoji_name="back")]
    ])

# ==================== DEPOSIT BUTTONS ====================

def create_deposit_keyboard() -> InlineKeyboardMarkup:
    return styled_inline_keyboard([
        [
            styled_button("10.000", callback_data="deposit_10000", style="primary", emoji_name="dolar"),
            styled_button("25.000", callback_data="deposit_25000", style="success", emoji_name="dolar")
        ],
        [
            styled_button("50.000", callback_data="deposit_50000", style="success", emoji_name="dolar"),
            styled_button("100.000", callback_data="deposit_100000", style="primary", emoji_name="dolar")
        ],
        [styled_button("Nominal Manual", callback_data="deposit_manual", style="primary", emoji_name="catatan")],
        [styled_button("Kembali", callback_data="menu_back", style="danger", emoji_name="back")]
    ])

def create_deposit_payment_keyboard(order_id: str, amount: int) -> InlineKeyboardMarkup:
    return styled_inline_keyboard([
        [styled_button("Verifikasi Bayar", callback_data=f"verify_deposit_{order_id}_{amount}", style="success", emoji_name="verified")],
        [styled_button("Batal", callback_data="cancel_deposit", style="danger", emoji_name="warning")]
    ])

# ==================== COMMAND HANDLERS ==================

TEXT_MENU_HTML = """\
<tg-emoji emoji-id="6028530359975548369">💎</tg-emoji> <b>MANXY OFFICIAL — TOKO AKUN TELEGRAM</b>

<tg-emoji emoji-id="5990073381720953601">✨</tg-emoji> Selamat datang di platform jual beli akun Telegram terpercaya, cepat, dan otomatis.

<table bordered striped>
<tr><th><tg-emoji emoji-id="6028206863038811654">🧾</tg-emoji> Menu Layanan</th><th>Fungsi</th></tr>
<tr><td>Beli Akun</td><td>Beli session Telegram siap pakai, langsung terkirim otomatis</td></tr>
<tr><td>Deposit Saldo</td><td>Top-up via QRIS, saldo masuk otomatis setelah pembayaran</td></tr>
<tr><td>Confes Gift</td><td>Kirim gift Telegram anonim atau dengan nama ke siapa saja</td></tr>
<tr><td>Nokos AllApk</td><td>Order layanan OTP / verifikasi semua aplikasi populer</td></tr>
</table>
<hr/>
<table bordered striped>
<tr><th><tg-emoji emoji-id="4904936030232117798">⚙️</tg-emoji> Info Sistem</th><th>Status</th></tr>
<tr><td>Uptime</td><td>24 Jam Aktif</td></tr>
<tr><td>Proses Order</td><td>Otomatis &amp; Instan</td></tr>
<tr><td>Keamanan</td><td>Database Terenkripsi</td></tr>
<tr><td>Support</td><td>CS Siap Membantu</td></tr>
</table>

""" + build_footer_links_html()

TEXT_MENU = premium_text("""
[diamond1] <b>MANXY OFFICIAL — TOKO AKUN TELEGRAM</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[sparkle] Selamat datang di platform jual beli akun Telegram terpercaya, cepat, dan otomatis.

[product] <b>Menu Layanan</b>
[panahijo] Beli Session — akun Telegram siap pakai, terkirim otomatis.
[panahijo] Deposit Saldo — top-up via QRIS, masuk instan.
[panahijo] Confes Gift — kirim gift Telegram anonim atau dengan nama.
[panahijo] Nokos AllApk — order OTP / verifikasi semua aplikasi populer.

[shield] <b>Info Sistem</b>
[panahijo] Uptime 24 jam, proses order otomatis dan instan.
[panahijo] Semua transaksi tercatat aman di database terenkripsi.
[panahijo] CS siap membantu jika ada kendala.</blockquote>
""") + "\n" + build_footer_links_html()

# ---------- REPLY KEYBOARD PERSISTEN ----------
RKB_MENU_NOKTEL  = "Menu Buy Noktel"
RKB_MENU_GIFT    = "Menu Confes Gift"
RKB_MENU_GMAIL   = "Menu Report Gmail"
RKB_NOKOS_ALLAPK = "Menu Nokos AllApk"
RKB_CS           = "Customer Service"
RKB_DEPOSIT      = "Deposit Saldo"
RKB_OWNER_PANEL  = "Owner Panel"
RKB_MENU_CV      = "Menu CV Kontak"
RKB_MENU_STARS   = "Topup Stars"
RKB_MENU_TON     = "Topup TON"
RKB_BULK_STARS   = "Bulk Stars"
RKB_TELE_PREMIUM = "Telegram Premium"
RKB_MENU_CEK_ID  = "Menu Cek Id"
RKB_BACK_MAIN     = "Menu Utama"
RKB_PROFIL        = "Profil Saya"
RKB_MENU_CLONE    = "Menu Clone Bot"
RKB_MENU2_NEXT    = "Lanjut Ke Menu 2"
RKB_MENU2_BACK    = "Kembali ke Menu 1"

def create_reply_keyboard(user_id: int = 0) -> ReplyKeyboardMarkup:
    from src.custom_emoji import styled_keyboard_button
    rows = [
        [
            styled_keyboard_button(RKB_MENU_STARS, style="success", emoji_name="miniapp_stars"),
            styled_keyboard_button(RKB_MENU_TON,    style="primary", emoji_name="miniapp_ton"),
        ],
        [
            styled_keyboard_button(RKB_BULK_STARS,   style="success", emoji_name="bulkstars_ico"),
            styled_keyboard_button(RKB_TELE_PREMIUM, style="primary", emoji_name="premium_acc"),
        ],
        [
            styled_keyboard_button(RKB_MENU_NOKTEL, style="success", emoji_name="miniapp_noktel"),
            styled_keyboard_button(RKB_MENU_GIFT,   style="primary", emoji_name="miniapp_confes"),
        ],
        [styled_keyboard_button(RKB_MENU_CLONE, style="danger", emoji_name="roket")],
        [
            styled_keyboard_button(RKB_PROFIL,  style="success", emoji_name="card"),
            styled_keyboard_button(RKB_DEPOSIT, style="primary", emoji_name="duitkarung"),
        ],
        [styled_keyboard_button(RKB_CS, style="danger", emoji_name="chat")],
        [styled_keyboard_button(RKB_MENU2_NEXT, style="primary", emoji_name="panahijo")],
    ]
    if user_id and is_owner(user_id):
        from src.custom_emoji import styled_keyboard_button as _skb
        rows.append([_skb(RKB_OWNER_PANEL, style="danger", emoji_name="crown")])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)


def create_reply_keyboard_menu2(user_id: int = 0) -> ReplyKeyboardMarkup:
    from src.custom_emoji import styled_keyboard_button
    rows = [
        [styled_keyboard_button(RKB_NOKOS_ALLAPK, style="success", emoji_name="miniapp_nokos")],
        [
            styled_keyboard_button(RKB_MENU_GMAIL, style="primary", emoji_name="gmail"),
            styled_keyboard_button(RKB_MENU_CV,    style="primary", emoji_name="WhatsApp"),
        ],
        [styled_keyboard_button(RKB_MENU_CEK_ID, style="success", emoji_name="card")],
        [
            styled_keyboard_button(RKB_PROFIL,  style="danger", emoji_name="card"),
            styled_keyboard_button(RKB_DEPOSIT, style="danger", emoji_name="duitkarung"),
        ],
        [styled_keyboard_button(RKB_CS, style="primary", emoji_name="chat")],
        [styled_keyboard_button(RKB_MENU2_BACK, style="danger", emoji_name="back")],
    ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)

def push_nav(context, state: str):
    """Push state ke nav_history supaya tombol Kembali/Batal bisa kembali ke halaman sebelumnya."""
    nav_history = context.user_data.get("nav_history", [])
    current = context.user_data.get("current_menu_state", "main_menu")
    if not nav_history or nav_history[-1] != current:
        nav_history.append(current)
    context.user_data["nav_history"] = nav_history
    context.user_data["current_menu_state"] = state


# ─────────────────────────────────────────────────────────────
# SHIM: PAGE-MENU REPLY KEYBOARD
#
# Supaya tombol menu di dalam page 1-7 (yang dulunya inline button)
# bisa jadi Reply Keyboard tanpa menduplikasi ratusan handler lama,
# kita "menyamar" jadi callback_query palsu lalu panggil ulang
# handle_callback() yang sudah ada. Tombol "Kembali" TETAP inline
# seperti semula (tidak disentuh oleh shim ini).
# ─────────────────────────────────────────────────────────────
class _ReplyFakeMessage:
    def __init__(self, bot, chat_id):
        self._bot = bot
        self.chat_id = chat_id
        self.message_id = None
        self.photo = None
        self.video = None
        self.animation = None
        self.document = None

    async def reply_text(self, text=None, **kwargs):
        return await self._bot.send_message(chat_id=self.chat_id, text=text, **kwargs)

    async def reply_photo(self, photo=None, **kwargs):
        return await self._bot.send_photo(chat_id=self.chat_id, photo=photo, **kwargs)

    async def reply_document(self, document=None, **kwargs):
        return await self._bot.send_document(chat_id=self.chat_id, document=document, **kwargs)

    async def delete(self):
        return None


class _ReplyFakeQuery:
    """Meniru CallbackQuery secukupnya supaya handler lama (yang ditulis untuk
    inline button) tetap jalan walau sebenarnya dipicu dari tombol Reply Keyboard
    (pesan teks biasa). Karena Reply Keyboard tidak bisa dipasang lewat edit
    pesan, setiap 'edit' di sini otomatis dikirim ulang sebagai pesan baru."""
    def __init__(self, bot, chat_id, from_user, data):
        self._bot = bot
        self.from_user = from_user
        self.data = data
        self.id = "0"
        self.message = _ReplyFakeMessage(bot, chat_id)

    async def answer(self, *args, **kwargs):
        return None

    async def edit_message_text(self, text=None, *args, **kwargs):
        kwargs.pop("caption", None)
        return await self._bot.send_message(chat_id=self.message.chat_id, text=text, **kwargs)

    async def edit_message_caption(self, caption=None, **kwargs):
        return await self._bot.send_message(chat_id=self.message.chat_id, text=caption, **kwargs)

    async def edit_message_reply_markup(self, **kwargs):
        return None


class _ReplyFakeUpdate:
    def __init__(self, real_update, context, data):
        self.effective_user = real_update.effective_user
        self.effective_chat = real_update.effective_chat
        self.message = None
        self.callback_query = _ReplyFakeQuery(
            bot=context.bot,
            chat_id=real_update.effective_chat.id,
            from_user=real_update.effective_user,
            data=data,
        )


async def dispatch_as_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str):
    """Jalankan ulang logic handle_callback() seolah `data` adalah callback_data
    dari inline button, tapi dipicu dari tombol teks Reply Keyboard."""
    fake_update = _ReplyFakeUpdate(update, context, data)
    await handle_callback(fake_update, context)


def set_page_reply_map(context: ContextTypes.DEFAULT_TYPE, page_key: str, text_to_data: dict):
    """Simpan mapping 'teks tombol' -> 'callback_data lama' untuk halaman yang
    sedang tampil, supaya bisa di-route saat user tap tombol Reply Keyboard."""
    context.user_data["active_reply_page"] = page_key
    context.user_data["reply_menu_map"] = dict(text_to_data)


async def route_page_reply_button(update: Update, context: ContextTypes.DEFAULT_TYPE, txt: str) -> bool:
    """Cek apakah teks yang dikirim user cocok dengan salah satu tombol menu
    halaman (page 1-7) yang sedang aktif. Kalau cocok, jalankan lewat
    dispatch_as_callback dan kembalikan True."""
    reply_map = context.user_data.get("reply_menu_map") or {}
    data = reply_map.get(txt)
    if data is None:
        return False
    await dispatch_as_callback(update, context, data)
    return True


async def deposit_menu_new(context, chat_id: int):
    """Buka menu deposit sebagai pesan baru — dipanggil dari ReplyKeyboard."""
    menu_html = """\
<tg-emoji emoji-id="6089104607328342288">💰</tg-emoji> <b>DEPOSIT SALDO</b>

<table bordered striped>
<tr><th><tg-emoji emoji-id="6028206863038811654">🧾</tg-emoji> Cara Top-Up</th><th>Keterangan</th></tr>
<tr><td>Pilih Nominal</td><td>Tekan tombol nominal yang tersedia di bawah ini</td></tr>
<tr><td>Scan QRIS</td><td>Bayar lewat QRIS menggunakan aplikasi dompet digital manapun</td></tr>
<tr><td>Verifikasi</td><td>Tekan tombol Verifikasi Bayar setelah transfer selesai</td></tr>
<tr><td>Saldo Masuk</td><td>Saldo langsung masuk otomatis setelah terverifikasi</td></tr>
</table>"""
    fallback_text = premium_text("""
[duitkarung] <b>DEPOSIT SALDO</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Pilih nominal deposit di bawah ini.

[product] <b>Cara Top-Up</b>
[panahijo] Pilih nominal → Scan QRIS → Tekan Verifikasi Bayar.
[verified] Saldo masuk otomatis setelah pembayaran terverifikasi.</blockquote>
""")
    try:
        await notif.send_rich_message_to_chat(
            context.bot, chat_id, menu_html, fallback_text,
            reply_markup=create_deposit_keyboard(),
            log_label="DepositMenu",
        )
    except Exception as e:
        print(f"[deposit_menu_new] {e}")

async def send_reply_keyboard_once(context: ContextTypes.DEFAULT_TYPE, chat_id: int):
    try:
        menu_html = '<tg-emoji emoji-id="6147679667663934682">🖥️</tg-emoji> <b>Menu siap digunakan.</b> Pilih layanan yang kamu butuhkan di bawah ini.'
        fallback_text = premium_text("[panel] <b>Menu siap digunakan.</b> Pilih layanan yang kamu butuhkan di bawah ini.")
        await notif.send_rich_message_to_chat(
            context.bot, chat_id, menu_html, fallback_text,
            reply_markup=create_reply_keyboard(chat_id),
            log_label="ReplyKeyboardOnce",
        )
    except Exception as e:
        print(f"[Reply Keyboard] Gagal kirim: {e}")


async def send_root_menu_new(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Kirim tampilan AWAL (root) yang berisi ke-7 pilihan menu utama
    (Noktel/Gift/Gmail/Nokos/CV/Cek Id/Fix Merah + Deposit/CS/Owner).
    Ini yang dituju tombol Reply Keyboard 'Menu Utama' — beda dengan
    send_main_menu_new yang sebenarnya membuka Page 1 (submenu Buy Noktel)."""
    context.user_data["current_menu_state"] = "idle"
    context.user_data["active_menu_page"] = None
    context.user_data.pop("reply_menu_map", None)
    context.user_data.pop("active_reply_page", None)
    menu_html = """\
<tg-emoji emoji-id="6028530359975548369">💎</tg-emoji> <b>MANXY OFFICIAL — TOKO AKUN TELEGRAM</b>

<table bordered striped>
<tr><th><tg-emoji emoji-id="5258500400918587241">📝</tg-emoji> Pilihan Menu</th><th>Keterangan</th></tr>
<tr><td>Buy Noktel</td><td>Beli session / akun Telegram siap pakai</td></tr>
<tr><td>Confes Gift</td><td>Order gift Telegram otomatis ke siapa saja</td></tr>
<tr><td>Report Gmail</td><td>Layanan pelaporan & pengelolaan akun Gmail</td></tr>
<tr><td>Nokos AllApk</td><td>OTP & verifikasi semua aplikasi populer</td></tr>
<tr><td>Topup Stars</td><td>Beli Telegram Stars ke username manapun</td></tr>
<tr><td>Deposit</td><td>Top-up saldo via QRIS, instan & otomatis</td></tr>
</table>

<tg-emoji emoji-id="5990073381720953601">✨</tg-emoji> Pilih menu di bawah ini untuk memulai."""
    fallback_text = premium_text("""
[diamond1] <b>MANXY OFFICIAL — TOKO AKUN TELEGRAM</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[product] Buy Noktel — beli session Telegram siap pakai.
[star] Confes Gift — order gift Telegram otomatis.
[gmail] Report Gmail — kelola akun Gmail.
[globe] Nokos AllApk — OTP semua aplikasi populer.
[star] Topup Stars — beli Telegram Stars ke username manapun.
[duitkarung] Deposit — top-up saldo via QRIS instan.

[sparkle] Pilih menu di bawah ini untuk memulai.</blockquote>
""")
    await notif.send_rich_message_to_chat(
        context.bot, user_id, menu_html, fallback_text,
        reply_markup=create_reply_keyboard(user_id),
        log_label="RootMenu",
    )


async def send_page2_menu_new(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Kirim halaman Gift (page 2) sebagai pesan baru dengan reply keyboard."""
    from src.main_menu import create_page2_menu, GIFT_ITEMS, get_gift_price, is_gift_enabled, _fmt, gift_button_label
    
    context.user_data["current_menu_state"]   = "page2_gift"
    context.user_data["active_menu_page"]     = 2

    kb = create_page2_menu(user_id, is_owner_func=is_owner)
    menu_html = """\
<tg-emoji emoji-id="6028530359975548369">💎</tg-emoji> <b>MANXY OFFICIAL — AUTO ORDER GIFT</b>

<table bordered striped>
<tr><th><tg-emoji emoji-id="5895754654360277212">🎁</tg-emoji> Cara Order Gift</th><th>Langkah</th></tr>
<tr><td>1. Pilih Gift</td><td>Tap nominal gift yang ingin dikirim dari daftar di bawah</td></tr>
<tr><td>2. Username</td><td>Masukkan username Telegram akun penerima gift</td></tr>
<tr><td>3. Mode Kirim</td><td>Pilih: Anonim (tanpa nama) atau Tampil Nama pengirim</td></tr>
<tr><td>4. Bayar & Kirim</td><td>Scan QRIS, gift langsung terkirim otomatis ke penerima</td></tr>
</table>

<tg-emoji emoji-id="6028551194861899805">🛡️</tg-emoji> Semua order diproses 24 jam via MTProto. Pilih gift di bawah untuk memulai."""
    fallback_text = premium_text("""
[diamond1] <b>MANXY OFFICIAL — AUTO ORDER GIFT</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[star] Kirim gift Telegram ke siapa saja, anonim atau dengan nama.

[product] <b>Cara Order Gift</b>
[panahijo] Tap gift → masukkan username penerima.
[panahijo] Pilih mode: Anonim atau Tampil Nama.
[panahijo] Bayar via QRIS → gift langsung terkirim otomatis.

[shield] Proses 24 jam via MTProto. Pilih gift di bawah untuk mulai.</blockquote>
""")

    # NOTE: sendRichMessage Bot API belum dukung kirim sebagai photo caption,
    # jadi tampilan gift dikirim sebagai rich text message (tanpa foto thumbnail).
    msg = await notif.send_rich_message_to_chat(
        context.bot, user_id, menu_html, fallback_text,
        reply_markup=kb,
        log_label="Page2GiftMenu",
    )

    # Buat mapping dari label gift ke callback data
    page2_map = {}
    
    # Add gift items ke map
    for i in range(len(GIFT_ITEMS)):
        enabled = is_gift_enabled(i)
        label = gift_button_label(i)

        if enabled:
            page2_map[label] = f"gift_order_{i}"
        else:
            page2_map[label] = f"gift_disabled_{i}"
    
    # Add action buttons
    page2_map["Riwayat Gift"] = "gift_history"
    page2_map["Cara Order"] = "gift_cara_order"
    page2_map["Menu Utama"] = "menu_back"
    
    set_page_reply_map(context, "page2_gift", page2_map)


async def send_contact_cs_new(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Kirim halaman Customer Service sebagai pesan baru — dipakai oleh Reply Keyboard."""
    support_username = globals().get('SUPPORT_USERNAME', 'Pretygirrls')
    menu_html = f"""\
<tg-emoji emoji-id="5778673721317267508">💬</tg-emoji> <b>CUSTOMER SERVICE</b>

<table bordered striped>
<tr><th><tg-emoji emoji-id="5443038326535759644">💬</tg-emoji> Topik Bantuan</th><th>Keterangan</th></tr>
<tr><td>Transaksi</td><td>Kendala pembayaran, QRIS gagal, atau order tidak masuk</td></tr>
<tr><td>OTP & Session</td><td>Masalah login, OTP tidak muncul, atau session expired</td></tr>
<tr><td>Deposit Saldo</td><td>Saldo belum masuk setelah transfer</td></tr>
<tr><td>Lainnya</td><td>Pertanyaan umum seputar layanan bot</td></tr>
</table>
<hr/>
<table bordered striped>
<tr><th><tg-emoji emoji-id="5334998226636390258">📱</tg-emoji> Kontak Resmi</th><th>Detail</th></tr>
<tr><td>Admin / CS</td><td>@{support_username}</td></tr>
</table>"""
    fallback_text = premium_text(f"""
[chat] <b>CUSTOMER SERVICE</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[product] <b>Topik Bantuan</b>
[panahijo] Kendala transaksi / QRIS gagal / order tidak masuk.
[panahijo] OTP tidak muncul, session expired, atau masalah login.
[panahijo] Saldo belum masuk setelah deposit.
[panahijo] Pertanyaan umum seputar layanan bot.

[verified] <b>Kontak CS Resmi:</b> @{support_username}</blockquote>
""")
    keyboard = styled_inline_keyboard([
        [styled_button("Hubungi Customer Service", url=f"https://t.me/{support_username}", style="danger", emoji_name="chat")]
    ])
    await notif.send_rich_message_to_chat(
        context.bot, user_id, menu_html, fallback_text,
        reply_markup=keyboard,
        log_label="ContactCS",
    )

async def send_clone_menu_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kirim menu Clone Bot dengan reply keyboard khusus (Dompet, Withdraw, Statistik, S&K)."""
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    clones = clone_system.get_clones_by_owner(DB_PATH, uid)
    active = [c for c in clones if c["status"] == "active"]
    pending = [c for c in clones if c["status"] == "pending"]

    if active:
        status_line = f"[verified] Kamu punya <b>{len(active)}</b> clone bot aktif."
    elif pending:
        status_line = f"[waktu] Kamu punya <b>{len(pending)}</b> permintaan clone yang masih menunggu persetujuan owner."
    else:
        status_line = "[catatan] Kamu belum punya clone bot. Ikuti tutorial di bawah untuk membuat satu!"

    rich = premium_text(f"""\
[roket] <b>MENU CLONE BOT</b>
<hr/>
<p>{status_line}</p>
<p>[panahijo] Ingin punya toko sendiri seperti bot ini? Kamu bisa membuat <b>Clone Bot</b> dan mendapatkan komisi otomatis dari setiap transaksi yang terjadi di bot kamu!</p>
<p>[chat] Gunakan menu di bawah untuk kelola dompet, tarik saldo, lihat statistik, atau baca cara membuat clone.</p>""")
    fallback = premium_text(f"""\
[roket] <b>MENU CLONE BOT</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>{status_line}

[panahijo] Ingin punya toko sendiri seperti bot ini? Buat Clone Bot dan dapatkan komisi otomatis dari tiap transaksi di bot kamu!
[chat] Gunakan menu di bawah untuk kelola dompet, tarik saldo, lihat statistik, atau baca tutorial.</blockquote>
""")
    await notif.send_rich_message_to_chat(
        context.bot, chat_id, rich, fallback,
        reply_markup=clone_system.create_clone_owner_keyboard(),
        log_label="CloneMenu",
    )


async def clone_dompet_saya(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    wallet = clone_system.get_wallet(DB_PATH, uid)
    clones = clone_system.get_clones_by_owner(DB_PATH, uid)
    rich, fallback = clone_system.text_dompet(uid, wallet, clones)
    await notif.send_rich_message_to_chat(
        context.bot, chat_id, rich, fallback,
        reply_markup=clone_system.create_clone_owner_keyboard(),
        log_label="CloneDompet",
    )


async def clone_statistik(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    stats = clone_system.get_clone_stats(DB_PATH, uid)
    clones = clone_system.get_clones_by_owner(DB_PATH, uid)
    rich, fallback = clone_system.text_statistik(uid, stats, clones)
    await notif.send_rich_message_to_chat(
        context.bot, chat_id, rich, fallback,
        reply_markup=clone_system.create_clone_owner_keyboard(),
        log_label="CloneStatistik",
    )


async def clone_atur_komisi_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mulai alur atur komisi — pemilik clone atur sendiri persentase bagiannya.
    Harga jual produk TIDAK bisa diubah di sini, tetap ikut bot pusat."""
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    clones = clone_system.get_clones_by_owner(DB_PATH, uid)
    rich, fallback, active_clones = clone_system.text_atur_komisi_start(clones)

    if not active_clones:
        await notif.send_rich_message_to_chat(
            context.bot, chat_id, rich, fallback,
            reply_markup=clone_system.create_clone_owner_keyboard(),
            log_label="CloneKomisiKosong",
        )
        return

    context.user_data["current_menu_state"] = "clone_komisi_wait_input"
    context.user_data["clone_komisi_single_id"] = active_clones[0]["id"] if len(active_clones) == 1 else None
    await notif.send_rich_message_to_chat(
        context.bot, chat_id, rich, fallback,
        reply_markup=create_cancel_keyboard_clone(),
        log_label="CloneKomisiStart",
    )


async def clone_handle_komisi_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Proses input teks untuk alur atur komisi clone. Return True kalau pesan sudah ditangani."""
    state = context.user_data.get("current_menu_state", "")
    if state != "clone_komisi_wait_input":
        return False

    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    txt = (update.message.text or "").strip()

    if txt.lower() in ("batal", "cancel"):
        context.user_data["current_menu_state"] = ""
        context.user_data.pop("clone_komisi_single_id", None)
        rich = premium_text("[batal] <b>Atur komisi dibatalkan.</b>")
        fallback = premium_text("[batal] <b>Atur komisi dibatalkan.</b>")
        await notif.send_rich_message_to_chat(
            context.bot, chat_id, rich, fallback,
            reply_markup=clone_system.create_clone_owner_keyboard(),
            log_label="CloneKomisiBatal",
        )
        return True

    single_id = context.user_data.get("clone_komisi_single_id")
    parts = txt.split()
    clone_id = None
    persen_raw = None

    if single_id is not None and len(parts) == 1:
        clone_id, persen_raw = single_id, parts[0]
    elif len(parts) == 2 and parts[0].isdigit():
        clone_id, persen_raw = int(parts[0]), parts[1]

    if clone_id is None:
        rich = premium_text(
            "[warning] <b>Format tidak valid.</b>\n<hr/>\n"
            "<p>Ketik <b>ID clone</b> lalu <b>persen baru</b> dipisah spasi, contoh: <code>3 15</code></p>"
        )
        fallback = premium_text(
            "[warning] Format tidak valid. Ketik ID clone lalu persen baru dipisah spasi, contoh: 3 15"
        )
        await notif.send_rich_message_to_chat(
            context.bot, chat_id, rich, fallback,
            reply_markup=create_cancel_keyboard_clone(),
            log_label="CloneKomisiFormatSalah",
        )
        return True

    try:
        persen_raw_clean = persen_raw.replace(",", ".").replace("%", "")
        persen = float(persen_raw_clean)
    except ValueError:
        rich = premium_text("[warning] <b>Persen tidak valid.</b>\n<hr/>\n<p>Ketik angka saja, contoh: <code>15</code></p>")
        fallback = premium_text("[warning] Persen tidak valid. Ketik angka saja, contoh: 15")
        await notif.send_rich_message_to_chat(
            context.bot, chat_id, rich, fallback,
            reply_markup=create_cancel_keyboard_clone(),
            log_label="CloneKomisiPersenSalah",
        )
        return True

    ok, reason, komisi_final = clone_system.set_clone_komisi_by_owner(DB_PATH, uid, clone_id, persen)

    if not ok:
        pesan_map = {
            "out_of_range": f"Persen harus di antara {clone_system.KOMISI_MIN_PERSEN}% dan {clone_system.KOMISI_MAX_PERSEN}%.",
            "not_owner": "Clone dengan ID itu bukan milik kamu, atau tidak ditemukan.",
            "not_active": "Clone itu belum/tidak aktif.",
            "format_invalid": "Format persen tidak valid.",
        }
        pesan = pesan_map.get(reason, "Gagal mengubah komisi, coba lagi.")
        rich = premium_text(f"[warning] <b>Gagal mengubah komisi.</b>\n<hr/>\n<p>{pesan}</p>")
        fallback = premium_text(f"[warning] Gagal mengubah komisi. {pesan}")
        await notif.send_rich_message_to_chat(
            context.bot, chat_id, rich, fallback,
            reply_markup=create_cancel_keyboard_clone(),
            log_label="CloneKomisiGagal",
        )
        return True

    context.user_data["current_menu_state"] = ""
    context.user_data.pop("clone_komisi_single_id", None)
    clone = clone_system.get_clone_by_id(DB_PATH, clone_id)
    rich, fallback = clone_system.text_komisi_diubah(clone["bot_username"], komisi_final)
    await notif.send_rich_message_to_chat(
        context.bot, chat_id, rich, fallback,
        reply_markup=clone_system.create_clone_owner_keyboard(),
        log_label="CloneKomisiSukses",
    )
    return True


async def clone_snk_tutorial(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    rich, fallback = clone_system.text_snk_tutorial()
    await notif.send_rich_message_to_chat(
        context.bot, chat_id, rich, fallback,
        reply_markup=clone_system.create_clone_owner_keyboard(),
        log_label="CloneSNK",
    )


async def clone_withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mulai alur withdraw — tanya nominal dulu lewat state input teks (bukan inline button)."""
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    wallet = clone_system.get_wallet(DB_PATH, uid)

    if wallet["saldo"] <= 0:
        rich = premium_text(f"""\
[warning] <b>SALDO KOSONG</b>
<hr/>
<p>[catatan] Saldo dompet clone kamu saat ini <b>Rp 0</b>. Belum ada komisi yang bisa ditarik.</p>""")
        fallback = premium_text("""\
[warning] <b>SALDO KOSONG</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Saldo dompet clone kamu saat ini Rp 0. Belum ada komisi yang bisa ditarik.</blockquote>
""")
        await notif.send_rich_message_to_chat(
            context.bot, chat_id, rich, fallback,
            reply_markup=clone_system.create_clone_owner_keyboard(),
            log_label="CloneWDKosong",
        )
        return

    context.user_data["current_menu_state"] = "clone_wd_wait_amount"
    rich = premium_text(f"""\
[dolar] <b>WITHDRAW SALDO CLONE</b>
<hr/>
<p>[duitkarung] Saldo kamu saat ini: <b>Rp {wallet['saldo']:,}</b></p>
<p>[catatan] Ketik nominal yang ingin ditarik (angka saja, tanpa titik/koma). Tidak ada batas minimum.</p>
<p>Contoh: <code>50000</code></p>""")
    fallback = premium_text(f"""\
[dolar] <b>WITHDRAW SALDO CLONE</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>Saldo kamu saat ini: Rp {wallet['saldo']:,}

[catatan] Ketik nominal yang ingin ditarik (angka saja). Tidak ada batas minimum.
Contoh: 50000</blockquote>
""")
    await notif.send_rich_message_to_chat(
        context.bot, chat_id, rich, fallback,
        reply_markup=create_cancel_keyboard_clone(),
        log_label="CloneWDStart",
    )


def create_cancel_keyboard_clone() -> ReplyKeyboardMarkup:
    from src.custom_emoji import styled_keyboard_button
    return ReplyKeyboardMarkup(
        [[styled_keyboard_button("Batal", style="danger", emoji_name="warning")]],
        resize_keyboard=True, is_persistent=True
    )


async def clone_handle_wd_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Proses input teks untuk alur withdraw clone (nominal -> metode -> rekening -> nama).
    Dipanggil dari handle_message berdasarkan current_menu_state. Return True kalau pesan sudah ditangani."""
    state = context.user_data.get("current_menu_state", "")
    if not state.startswith("clone_wd_"):
        return False

    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    txt = (update.message.text or "").strip()

    if txt.lower() in ("batal", "cancel"):
        context.user_data["current_menu_state"] = ""
        context.user_data.pop("clone_wd_data", None)
        rich = premium_text("[batal] <b>Withdraw dibatalkan.</b>")
        fallback = premium_text("[batal] <b>Withdraw dibatalkan.</b>")
        await notif.send_rich_message_to_chat(
            context.bot, chat_id, rich, fallback,
            reply_markup=clone_system.create_clone_owner_keyboard(),
            log_label="CloneWDBatal",
        )
        return True

    if state == "clone_wd_wait_amount":
        if not txt.isdigit() or int(txt) <= 0:
            rich = premium_text("[warning] <b>Nominal tidak valid.</b>\n<hr/>\n<p>Ketik angka saja, contoh: <code>50000</code></p>")
            fallback = premium_text("[warning] Nominal tidak valid. Ketik angka saja, contoh: 50000")
            await notif.send_rich_message_to_chat(
                context.bot, chat_id, rich, fallback,
                reply_markup=create_cancel_keyboard_clone(),
                log_label="CloneWDInvalidAmount",
            )
            return True
        amount = int(txt)
        wallet = clone_system.get_wallet(DB_PATH, uid)
        if amount > wallet["saldo"]:
            rich = premium_text(f"[warning] <b>Saldo tidak cukup.</b>\n<hr/>\n<p>Saldo kamu cuma <b>Rp {wallet['saldo']:,}</b>, tidak bisa menarik Rp {amount:,}.</p>")
            fallback = premium_text(f"[warning] Saldo kamu cuma Rp {wallet['saldo']:,}, tidak bisa menarik Rp {amount:,}.")
            await notif.send_rich_message_to_chat(
                context.bot, chat_id, rich, fallback,
                reply_markup=create_cancel_keyboard_clone(),
                log_label="CloneWDSaldoKurang",
            )
            return True
        context.user_data["clone_wd_data"] = {"amount": amount}
        context.user_data["current_menu_state"] = "clone_wd_wait_method"
        rich = premium_text("[duitkarung] <b>Ketik metode penarikan</b>\n<hr/>\n<p>Contoh: <code>DANA</code>, <code>GOPAY</code>, <code>SEABANK</code>, <code>BCA</code>, dll.</p>")
        fallback = premium_text("[duitkarung] Ketik metode penarikan (contoh: DANA, GOPAY, SEABANK, BCA, dll):")
        await notif.send_rich_message_to_chat(
            context.bot, chat_id, rich, fallback,
            reply_markup=create_cancel_keyboard_clone(),
            log_label="CloneWDAskMethod",
        )
        return True

    if state == "clone_wd_wait_method":
        context.user_data.setdefault("clone_wd_data", {})["method"] = txt
        context.user_data["current_menu_state"] = "clone_wd_wait_number"
        rich = premium_text("[card] <b>Ketik nomor tujuan</b>\n<hr/>\n<p>Nomor rekening / e-wallet tujuan.</p>")
        fallback = premium_text("[card] Ketik nomor rekening / e-wallet tujuan:")
        await notif.send_rich_message_to_chat(
            context.bot, chat_id, rich, fallback,
            reply_markup=create_cancel_keyboard_clone(),
            log_label="CloneWDAskNumber",
        )
        return True

    if state == "clone_wd_wait_number":
        context.user_data.setdefault("clone_wd_data", {})["payment_number"] = txt
        context.user_data["current_menu_state"] = "clone_wd_wait_name"
        rich = premium_text("[card] <b>Ketik nama pemilik rekening</b>\n<hr/>\n<p>Nama pemilik rekening / e-wallet tujuan.</p>")
        fallback = premium_text("[card] Ketik nama pemilik rekening / e-wallet tujuan:")
        await notif.send_rich_message_to_chat(
            context.bot, chat_id, rich, fallback,
            reply_markup=create_cancel_keyboard_clone(),
            log_label="CloneWDAskName",
        )
        return True

    if state == "clone_wd_wait_name":
        data = context.user_data.get("clone_wd_data", {})
        data["account_name"] = txt
        ok, result = clone_system.create_withdraw_request(
            DB_PATH, uid, data["amount"], data["method"], data["payment_number"], data["account_name"],
            origin_bot_token=getattr(context.bot, "token", None),
        )
        context.user_data["current_menu_state"] = ""
        context.user_data.pop("clone_wd_data", None)

        if not ok:
            rich = premium_text("[warning] <b>Gagal membuat permintaan withdraw</b>\n<hr/>\n<p>Saldo berubah. Coba lagi dari menu Withdraw.</p>")
            fallback = premium_text("[warning] Gagal membuat permintaan withdraw (saldo berubah). Coba lagi dari menu Withdraw.")
            await notif.send_rich_message_to_chat(
                context.bot, chat_id, rich, fallback,
                reply_markup=clone_system.create_clone_owner_keyboard(),
                log_label="CloneWDGagalDibuat",
            )
            return True

        wd_id = result
        rich, fallback = clone_system.text_wd_diajukan(data["amount"], data["method"], data["payment_number"])
        await notif.send_rich_message_to_chat(
            context.bot, chat_id, rich, fallback,
            reply_markup=clone_system.create_clone_owner_keyboard(),
            log_label="CloneWDDiajukan",
        )

        # Notif LANGSUNG ke chat pribadi tiap owner (sama seperti approval deposit
        # manual) + inline button Approve/Tolak, supaya owner bisa proses cepat
        # tanpa harus ketik command manual. Pakai rich message (tabel + emoji
        # premium), bukan teks polos.
        try:
            _u = get_user(uid)
            _uname = _u[1] if _u and _u[1] else str(uid)
            owner_kb = styled_inline_keyboard([
                [
                    styled_button("Approve WD", callback_data=f"clone_wd_approve_{wd_id}", style="success", emoji_name="verified"),
                    styled_button("Tolak WD",   callback_data=f"clone_wd_reject_{wd_id}",  style="danger",  emoji_name="batal"),
                ]
            ])
            owner_rich = premium_text(f"""\
[dolar] <b>PERMINTAAN WITHDRAW CLONE BARU</b>
<hr/>
<table bordered striped>
<tr><th>Detail</th><th>Info</th></tr>
<tr><td>[crown] Owner Clone</td><td>@{_uname} (<code>{uid}</code>)</td></tr>
<tr><td>[dolar] Nominal</td><td><b>Rp {data['amount']:,}</b></td></tr>
<tr><td>[duitkarung] Metode</td><td>{data['method']}</td></tr>
<tr><td>[card] Nomor Tujuan</td><td><code>{data['payment_number']}</code></td></tr>
<tr><td>[card] Nama Tujuan</td><td>{data['account_name']}</td></tr>
<tr><td>[card] WD ID</td><td><code>{wd_id}</code></td></tr>
</table>
<p>[panahijo] Tekan tombol di bawah untuk approve setelah transfer manual selesai, atau tolak permintaan ini.</p>""")
            owner_fallback = premium_text(f"""\
[dolar] <b>PERMINTAAN WITHDRAW CLONE BARU</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>
Owner Clone   : @{_uname} ({uid})
Nominal       : Rp {data['amount']:,}
Metode        : {data['method']}
No. Tujuan    : {data['payment_number']}
Nama Tujuan   : {data['account_name']}
WD ID         : {wd_id}

Tekan tombol di bawah untuk approve, atau tolak permintaan ini.</blockquote>
""")
            owner_list = OWNER_ID if isinstance(OWNER_ID, list) else [OWNER_ID]
            _wd_owner_bot = owner_notify_bot(context)
            for _oid in owner_list:
                try:
                    await notif.send_rich_message_to_chat(
                        _wd_owner_bot, _oid, owner_rich, owner_fallback,
                        reply_markup=owner_kb, log_label="CloneWDOwnerNotif",
                    )
                except Exception as _e2:
                    print(f"[CloneWD] Gagal kirim notif ke owner {_oid}: {_e2}")
        except Exception as e:
            print(f"[CloneWD] Gagal notif owner: {e}")

        return True

    return False


async def clone_cmd_approvewd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command owner: /approvewd [id] — tandai withdraw selesai (dana sudah ditransfer manual)."""
    uid = update.effective_user.id
    if not is_owner(uid):
        return
    args = context.args
    if not args or not args[0].isdigit():
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            premium_text("[catatan] Format: <code>/approvewd [id]</code>"),
            premium_text("[catatan] Format: /approvewd [id]"),
            log_label="CloneWDCmdUsage",
        )
        return
    wd_id = int(args[0])
    ok, wd = clone_system.approve_withdraw(DB_PATH, wd_id)
    if not ok:
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            premium_text(f"[warning] WD #{wd_id} tidak ditemukan atau sudah diproses sebelumnya."),
            premium_text(f"[warning] WD #{wd_id} tidak ditemukan atau sudah diproses sebelumnya."),
            log_label="CloneWDCmdGagal",
        )
        return
    await notif.send_rich_message_to_chat(
        context.bot, update.effective_chat.id,
        premium_text(f"[done] <b>WD #{wd_id} SELESAI</b>\n<hr/>\n<p>Saldo owner clone sudah dipotong Rp {wd['amount']:,}.</p>"),
        premium_text(f"[done] WD #{wd_id} ditandai SELESAI. Saldo owner clone sudah dipotong Rp {wd['amount']:,}."),
        log_label="CloneWDCmdOK",
    )
    rich, fallback = clone_system.text_wd_selesai(wd)
    try:
        _wd_notif_bot = get_origin_bot(wd.get("origin_bot_token"), fallback_bot=context.bot)
        await notif.send_rich_message_to_chat(
            _wd_notif_bot, wd["owner_id"], rich, fallback,
            reply_markup=clone_system.create_clone_owner_keyboard(),
            log_label="CloneWDSelesaiNotif",
        )
    except Exception as e:
        print(f"[CloneWD] Gagal notif pemilik clone: {e}")


async def clone_cmd_rejectwd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command owner: /rejectwd [id] — tolak permintaan withdraw."""
    uid = update.effective_user.id
    if not is_owner(uid):
        return
    args = context.args
    if not args or not args[0].isdigit():
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            premium_text("[catatan] Format: <code>/rejectwd [id]</code>"),
            premium_text("[catatan] Format: /rejectwd [id]"),
            log_label="CloneWDRejectCmdUsage",
        )
        return
    wd_id = int(args[0])
    wd = clone_system.get_withdraw_by_id(DB_PATH, wd_id)
    if not wd or wd["status"] != "pending":
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            premium_text(f"[warning] WD #{wd_id} tidak ditemukan atau sudah diproses sebelumnya."),
            premium_text(f"[warning] WD #{wd_id} tidak ditemukan atau sudah diproses sebelumnya."),
            log_label="CloneWDRejectCmdGagal",
        )
        return
    clone_system.reject_withdraw(DB_PATH, wd_id)
    await notif.send_rich_message_to_chat(
        context.bot, update.effective_chat.id,
        premium_text(f"[batal] <b>WD #{wd_id} DITOLAK</b>"),
        premium_text(f"[batal] WD #{wd_id} DITOLAK."),
        log_label="CloneWDRejectCmdOK",
    )
    rich, fallback = clone_system.text_wd_ditolak(wd)
    try:
        _wd_notif_bot = get_origin_bot(wd.get("origin_bot_token"), fallback_bot=context.bot)
        await notif.send_rich_message_to_chat(
            _wd_notif_bot, wd["owner_id"], rich, fallback,
            reply_markup=clone_system.create_clone_owner_keyboard(),
            log_label="CloneWDDitolakNotif",
        )
    except Exception as e:
        print(f"[CloneWD] Gagal notif pemilik clone: {e}")


async def clone_cmd_approveclone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command owner: /approveclone [id] [komisi%] — setujui clone & langsung spawn."""
    uid = update.effective_user.id
    if not is_owner(uid):
        return
    args = context.args
    if not args or not args[0].isdigit():
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            premium_text("[catatan] Format: <code>/approveclone [id] [komisi%]</code>\n<p>Contoh: <code>/approveclone 3 10</code></p>"),
            premium_text("[catatan] Format: /approveclone [id] [komisi%] (contoh: /approveclone 3 10)"),
            log_label="CloneApproveCmdUsage",
        )
        return
    clone_id = int(args[0])
    komisi = float(args[1]) if len(args) > 1 else 10.0

    clone = clone_system.get_clone_by_id(DB_PATH, clone_id)
    if not clone:
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            premium_text(f"[warning] Clone #{clone_id} tidak ditemukan."),
            premium_text(f"[warning] Clone #{clone_id} tidak ditemukan."),
            log_label="CloneApproveCmdGagal",
        )
        return
    if clone["status"] == "active":
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            premium_text(f"[catatan] Clone #{clone_id} (@{clone['bot_username']}) sudah aktif."),
            premium_text(f"[catatan] Clone #{clone_id} (@{clone['bot_username']}) sudah aktif."),
            log_label="CloneSudahAktifCmd",
        )
        return

    clone_system.approve_clone(DB_PATH, clone_id, komisi)
    ok = clone_system.start_single_clone(clone_id, DB_PATH, register_all_handlers)
    status_note = "Bot clone sudah dijalankan." if ok else "PERINGATAN: gagal menjalankan proses clone, cek log."
    await notif.send_rich_message_to_chat(
        context.bot, update.effective_chat.id,
        premium_text(f"[done] <b>Clone #{clone_id} (@{clone['bot_username']}) DISETUJUI</b>\n<hr/>\n<p>Komisi {komisi}%. {status_note}</p>"),
        premium_text(f"[done] Clone #{clone_id} (@{clone['bot_username']}) disetujui dengan komisi {komisi}%. {status_note}"),
        log_label="CloneApproveCmdOK",
    )

    rich, fallback = clone_system.text_clone_diaktifkan(clone["bot_username"], komisi)
    try:
        await notif.send_rich_message_to_chat(
            context.bot, clone["owner_id"], rich, fallback,
            reply_markup=clone_system.create_clone_owner_keyboard(),
            log_label="CloneDiaktifkanNotif",
        )
    except Exception as e:
        print(f"[CloneApprove] Gagal notif pemilik clone: {e}")


async def clone_cmd_rejectclone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Command owner: /rejectclone [id] — tolak permintaan clone."""
    uid = update.effective_user.id
    if not is_owner(uid):
        return
    args = context.args
    if not args or not args[0].isdigit():
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            premium_text("[catatan] Format: <code>/rejectclone [id]</code>"),
            premium_text("[catatan] Format: /rejectclone [id]"),
            log_label="CloneRejectCmdUsage",
        )
        return
    clone_id = int(args[0])
    clone = clone_system.get_clone_by_id(DB_PATH, clone_id)
    if not clone:
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            premium_text(f"[warning] Clone #{clone_id} tidak ditemukan."),
            premium_text(f"[warning] Clone #{clone_id} tidak ditemukan."),
            log_label="CloneRejectCmdGagal",
        )
        return
    clone_system.reject_clone(DB_PATH, clone_id)
    await notif.send_rich_message_to_chat(
        context.bot, update.effective_chat.id,
        premium_text(f"[batal] <b>Clone #{clone_id} (@{clone['bot_username']}) DITOLAK</b>"),
        premium_text(f"[batal] Clone #{clone_id} (@{clone['bot_username']}) ditolak."),
        log_label="CloneRejectCmdOK",
    )

    rich, fallback = clone_system.text_clone_ditolak(clone["bot_username"])
    try:
        await notif.send_rich_message_to_chat(
            context.bot, clone["owner_id"], rich, fallback,
            log_label="CloneDitolakNotif",
        )
    except Exception as e:
        print(f"[CloneReject] Gagal notif pemilik clone: {e}")


async def clone_wd_approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tombol inline 'Approve WD' di notif owner — tandai WD selesai & potong saldo."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)

    wd_id = int(q.data.split("_")[-1])
    ok, wd = clone_system.approve_withdraw(DB_PATH, wd_id)
    if not ok:
        await fast_edit(
            q, f"WD #{wd_id} tidak ditemukan atau sudah diproses sebelumnya.",
            parse_mode="HTML", log_label="CloneWDApproveGagal",
        )
        return

    await fast_edit(
        q,
        premium_text(f"[done] <b>WD #{wd_id} SELESAI</b>\n<hr/>\n<p>Saldo owner clone sudah dipotong Rp {wd['amount']:,}.</p>"),
        parse_mode="HTML",
        rich_html=f"{emoji('done')} <b>WD #{wd_id} SELESAI</b>\n<hr/>\n<p>Saldo owner clone sudah dipotong Rp {wd['amount']:,}.</p>",
        log_label="CloneWDApproveOK",
    )

    rich, fallback = clone_system.text_wd_selesai(wd)
    try:
        _wd_notif_bot = get_origin_bot(wd.get("origin_bot_token"), fallback_bot=context.bot)
        await notif.send_rich_message_to_chat(
            _wd_notif_bot, wd["owner_id"], rich, fallback,
            reply_markup=clone_system.create_clone_owner_keyboard(),
            log_label="CloneWDSelesaiNotif",
        )
    except Exception as e:
        print(f"[CloneWD] Gagal notif pemilik clone: {e}")


async def miniapp_wd_approve_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tombol 'Sudah Ditransfer' di notif withdraw Wallet Mini App -- tandai
    permintaan selesai. Saldo user MEMANG sudah dipotong sejak user submit
    form withdraw di Mini App, jadi di sini tinggal update status aja."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)

    wd_id = int(q.data.split("_")[-1])
    row = cursor.execute(
        "SELECT user_id, amount, status FROM wallet_withdrawals WHERE id=?", (wd_id,)
    ).fetchone()
    if not row or row[2] != "pending":
        await fast_edit(
            q, f"WD Wallet Mini App #{wd_id} tidak ditemukan atau sudah diproses sebelumnya.",
            parse_mode="HTML", log_label="MiniWDApproveGagal",
        )
        return
    wd_user_id, amount, _ = row
    cursor.execute(
        "UPDATE wallet_withdrawals SET status='completed', processed_at=? WHERE id=?",
        (int(time.time()), wd_id),
    )
    conn.commit()

    await fast_edit(
        q,
        premium_text(f"[done] <b>WD Wallet #{wd_id} SELESAI</b>\n<hr/>\n<p>Rp {amount:,} sudah ditandai selesai ditransfer.</p>"),
        parse_mode="HTML",
        rich_html=f"{emoji('done')} <b>WD Wallet #{wd_id} SELESAI</b>\n<hr/>\n<p>Rp {amount:,} sudah ditandai selesai ditransfer.</p>",
        log_label="MiniWDApproveOK",
    )
    try:
        await notif.send_rich_message_to_chat(
            context.bot, wd_user_id,
            premium_text(f"[done] <b>Withdraw Rp {amount:,} berhasil diproses!</b>\n\n<blockquote>[catatan] Silakan cek rekening/e-wallet tujuan kamu.</blockquote>"),
            premium_text(f"[done] Withdraw Rp {amount:,} berhasil diproses! Cek rekening/e-wallet tujuan kamu."),
            log_label="MiniWDApproveNotifUser",
        )
    except Exception as e:
        print(f"[MiniWD] Gagal notif user: {e}")


async def miniapp_wd_reject_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tombol 'Tolak & Refund' di notif withdraw Wallet Mini App -- kembalikan
    saldo yang tadi sudah dipotong pas user submit form withdraw."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)

    wd_id = int(q.data.split("_")[-1])
    row = cursor.execute(
        "SELECT user_id, amount, status FROM wallet_withdrawals WHERE id=?", (wd_id,)
    ).fetchone()
    if not row or row[2] != "pending":
        await fast_edit(
            q, f"WD Wallet Mini App #{wd_id} tidak ditemukan atau sudah diproses sebelumnya.",
            parse_mode="HTML", log_label="MiniWDRejectGagal",
        )
        return
    wd_user_id, amount, _ = row
    cursor.execute(
        "UPDATE wallet_withdrawals SET status='rejected', processed_at=? WHERE id=?",
        (int(time.time()), wd_id),
    )
    update_balance(wd_user_id, belance_delta=amount)
    conn.commit()

    await fast_edit(
        q,
        premium_text(f"[batal] <b>WD Wallet #{wd_id} DITOLAK</b>\n<hr/>\n<p>Saldo Rp {amount:,} sudah dikembalikan ke user.</p>"),
        parse_mode="HTML",
        rich_html=f"{emoji('batal')} <b>WD Wallet #{wd_id} DITOLAK</b>\n<hr/>\n<p>Saldo Rp {amount:,} sudah dikembalikan ke user.</p>",
        log_label="MiniWDRejectOK",
    )
    try:
        await notif.send_rich_message_to_chat(
            context.bot, wd_user_id,
            premium_text(f"[batal] <b>Withdraw Rp {amount:,} ditolak.</b>\n\n<blockquote>[catatan] Saldo sudah dikembalikan ke akun kamu. Hubungi admin bila ada pertanyaan.</blockquote>"),
            premium_text(f"[batal] Withdraw Rp {amount:,} ditolak, saldo sudah dikembalikan ke akun kamu."),
            log_label="MiniWDRejectNotifUser",
        )
    except Exception as e:
        print(f"[MiniWD] Gagal notif user: {e}")


_MINIORDER_KIND_LABEL = {
    "stars": "⭐ Topup Stars", "ton": "💎 Topup TON", "noktel": "📱 Buy Noktel",
    "gift": "🎁 Confes Gift", "nokos": "🌐 Nokos AllApk", "deposit": "💰 Deposit Saldo",
}


async def miniapp_order_approve_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tombol 'Setujui & Kirim' di notif order manual Mini App (deposit/
    stars/ton/noktel/gift/nokos yang dibayar via QRIS/e-wallet manual). Owner
    menekan ini SETELAH ngecek foto bukti transfer yang dikirim bareng notif
    ini -- eksekusi pengiriman produk sebenarnya dilempar balik ke Mini App
    backend (proses Flask terpisah) lewat src/miniorder_bridge.py, karena di
    situ tempat semua logic pengiriman & DB order-nya berada."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q, "Memproses...")

    order_id = q.data[len("miniord_ok_"):]
    result = await miniorder_bridge.confirm_manual_order(order_id)

    if not result.get("ok"):
        await fast_edit(
            q, f"Gagal memproses order <code>{order_id}</code>: {result.get('error', 'error tidak diketahui')}",
            parse_mode="HTML", log_label="MiniOrderApproveGagal",
        )
        return

    order = result.get("order") or {}
    status = result.get("status")
    msg = result.get("message") or ""
    kind_label = _MINIORDER_KIND_LABEL.get(order.get("kind"), order.get("kind", "-"))

    if status == "success":
        header, rich_header = "[done] <b>Order Disetujui & Dikirim</b>", f"{emoji('done')} <b>Order Disetujui & Dikirim</b>"
    else:
        header, rich_header = "[warning] <b>Order Disetujui, Tapi Pengiriman Gagal</b>", f"{emoji('warning')} <b>Order Disetujui, Tapi Pengiriman Gagal</b>"

    body = (
        f"\n<hr/>\n<table bordered striped>"
        f"<tr><th>Order ID</th><td><code>{order_id}</code></td></tr>"
        f"<tr><th>Jenis</th><td>{kind_label}</td></tr>"
        f"<tr><th>Tujuan</th><td>{order.get('target', '-')}</td></tr>"
        f"<tr><th>Total</th><td>Rp{int(order.get('price', 0)):,}</td></tr>"
        f"<tr><th>Hasil</th><td>{msg}</td></tr>"
        f"</table>"
    )
    await fast_edit(
        q, premium_text(header + body), parse_mode="HTML",
        rich_html=premium_text(rich_header + body),
        log_label="MiniOrderApproveOK",
    )

    tg_user_id = order.get("tg_user_id")
    if tg_user_id:
        try:
            if status == "success":
                user_text = f"[done] <b>Order kamu disetujui!</b>\n\n<blockquote>{msg}</blockquote>"
                user_fallback = f"[done] Order kamu disetujui! {msg}"
            else:
                user_text = f"[warning] <b>Order kamu disetujui owner, tapi pengiriman gagal.</b>\n\n<blockquote>{msg} Hubungi admin untuk bantuan lebih lanjut.</blockquote>"
                user_fallback = f"[warning] Order kamu disetujui owner, tapi pengiriman gagal: {msg}. Hubungi admin."
            await notif.send_rich_message_to_chat(
                context.bot, tg_user_id, premium_text(user_text), premium_text(user_fallback),
                log_label="MiniOrderApproveNotifUser",
            )
        except Exception as e:
            print(f"[MiniOrder] Gagal notif user: {e}")


async def miniapp_order_reject_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tombol 'Tolak' di notif order manual Mini App -- order ditandai gagal,
    stok/reservasi yang sempat dipotong otomatis dikembalikan (logic-nya di
    Mini App backend lewat _reject_manual_order)."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q, "Memproses...")

    order_id = q.data[len("miniord_no_"):]
    reason = "Ditolak owner (pembayaran tidak ditemukan/tidak valid)."
    result = await miniorder_bridge.reject_manual_order(order_id, reason)

    if not result.get("ok"):
        await fast_edit(
            q, f"Gagal menolak order <code>{order_id}</code>: {result.get('error', 'error tidak diketahui')}",
            parse_mode="HTML", log_label="MiniOrderRejectGagal",
        )
        return

    order = result.get("order") or {}
    kind_label = _MINIORDER_KIND_LABEL.get(order.get("kind"), order.get("kind", "-"))
    body = (
        f"\n<hr/>\n<table bordered striped>"
        f"<tr><th>Order ID</th><td><code>{order_id}</code></td></tr>"
        f"<tr><th>Jenis</th><td>{kind_label}</td></tr>"
        f"<tr><th>Tujuan</th><td>{order.get('target', '-')}</td></tr>"
        f"<tr><th>Total</th><td>Rp{int(order.get('price', 0)):,}</td></tr>"
        f"</table>"
    )
    await fast_edit(
        q, premium_text("[batal] <b>Order Ditolak</b>" + body), parse_mode="HTML",
        rich_html=premium_text(f"{emoji('batal')} <b>Order Ditolak</b>" + body),
        log_label="MiniOrderRejectOK",
    )

    tg_user_id = order.get("tg_user_id")
    if tg_user_id:
        try:
            await notif.send_rich_message_to_chat(
                context.bot, tg_user_id,
                premium_text(f"[batal] <b>Order kamu ditolak.</b>\n\n<blockquote>{reason} Hubungi admin bila ada pertanyaan.</blockquote>"),
                premium_text(f"[batal] Order kamu ditolak. {reason}"),
                log_label="MiniOrderRejectNotifUser",
            )
        except Exception as e:
            print(f"[MiniOrder] Gagal notif user: {e}")


async def clone_wd_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tombol inline 'Tolak WD' di notif owner — tolak permintaan withdraw."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)

    wd_id = int(q.data.split("_")[-1])
    wd = clone_system.get_withdraw_by_id(DB_PATH, wd_id)
    if not wd or wd["status"] != "pending":
        await fast_edit(
            q, f"WD #{wd_id} tidak ditemukan atau sudah diproses sebelumnya.",
            parse_mode="HTML", log_label="CloneWDRejectGagal",
        )
        return

    clone_system.reject_withdraw(DB_PATH, wd_id)
    await fast_edit(
        q,
        premium_text(f"[batal] <b>WD #{wd_id} DITOLAK</b>"),
        parse_mode="HTML",
        rich_html=f"{emoji('batal')} <b>WD #{wd_id} DITOLAK</b>",
        log_label="CloneWDRejectOK",
    )

    rich, fallback = clone_system.text_wd_ditolak(wd)
    try:
        _wd_notif_bot = get_origin_bot(wd.get("origin_bot_token"), fallback_bot=context.bot)
        await notif.send_rich_message_to_chat(
            _wd_notif_bot, wd["owner_id"], rich, fallback,
            reply_markup=clone_system.create_clone_owner_keyboard(),
            log_label="CloneWDDitolakNotif",
        )
    except Exception as e:
        print(f"[CloneWD] Gagal notif pemilik clone: {e}")


async def clone_approve_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tombol inline 'Approve' di notif clone baru — approve dengan komisi default 10%."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)

    clone_id = int(q.data.split("_")[-1])
    clone = clone_system.get_clone_by_id(DB_PATH, clone_id)
    if not clone:
        await fast_edit(q, f"Clone #{clone_id} tidak ditemukan.", parse_mode="HTML", log_label="CloneApproveGagal")
        return
    if clone["status"] == "active":
        await fast_edit(q, f"Clone #{clone_id} (@{clone['bot_username']}) sudah aktif.", parse_mode="HTML", log_label="CloneSudahAktif")
        return

    komisi_default = 10.0
    clone_system.approve_clone(DB_PATH, clone_id, komisi_default)
    ok = clone_system.start_single_clone(clone_id, DB_PATH, register_all_handlers)

    await fast_edit(
        q,
        premium_text(f"[done] <b>Clone #{clone_id} (@{clone['bot_username']}) DISETUJUI</b>\n<hr/>\n<p>Komisi default {komisi_default}%. {'Bot clone sudah dijalankan.' if ok else 'PERINGATAN: gagal menjalankan proses clone.'}</p>"),
        parse_mode="HTML",
        rich_html=f"{emoji('done')} <b>Clone #{clone_id} (@{clone['bot_username']}) DISETUJUI</b>\n<hr/>\n<p>Komisi default {komisi_default}%. {'Bot clone sudah dijalankan.' if ok else 'PERINGATAN: gagal menjalankan proses clone.'}</p>",
        log_label="CloneApproveOK",
    )

    rich, fallback = clone_system.text_clone_diaktifkan(clone["bot_username"], komisi_default)
    try:
        await notif.send_rich_message_to_chat(
            context.bot, clone["owner_id"], rich, fallback,
            reply_markup=clone_system.create_clone_owner_keyboard(),
            log_label="CloneDiaktifkanNotif",
        )
    except Exception as e:
        print(f"[CloneApprove] Gagal notif pemilik clone: {e}")


async def clone_reject_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tombol inline 'Tolak' di notif clone baru — tolak permintaan clone."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)

    clone_id = int(q.data.split("_")[-1])
    clone = clone_system.get_clone_by_id(DB_PATH, clone_id)
    if not clone:
        await fast_edit(q, f"Clone #{clone_id} tidak ditemukan.", parse_mode="HTML", log_label="CloneRejectGagal")
        return

    clone_system.reject_clone(DB_PATH, clone_id)
    await fast_edit(
        q,
        premium_text(f"[batal] <b>Clone #{clone_id} (@{clone['bot_username']}) DITOLAK</b>"),
        parse_mode="HTML",
        rich_html=f"{emoji('batal')} <b>Clone #{clone_id} (@{clone['bot_username']}) DITOLAK</b>",
        log_label="CloneRejectOK",
    )

    rich, fallback = clone_system.text_clone_ditolak(clone["bot_username"])
    try:
        await notif.send_rich_message_to_chat(
            context.bot, clone["owner_id"], rich, fallback,
            log_label="CloneDitolakNotif",
        )
    except Exception as e:
        print(f"[CloneReject] Gagal notif pemilik clone: {e}")




async def clone_detect_forwarded_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Deteksi pesan forward dari @BotFather yang berisi token bot baru.
    Dipanggil dari handle_message SEBELUM pengecekan lain. Return True kalau
    pesan ini sudah ditangani sebagai pendaftaran clone (supaya handle_message
    berhenti memproses pesan yang sama lebih lanjut).
    """
    if not update.message or not update.message.text:
        return False
    msg = update.message
    text = msg.text

    token = clone_system.extract_bot_father_token(text)
    if not token:
        return False

    uid = update.effective_user.id
    chat_id = update.effective_chat.id

    # Coba ambil info bot (username & nama) langsung dari Telegram pakai token tsb
    bot_username = None
    bot_name = None
    try:
        from telegram import Bot
        test_bot = Bot(token=token)
        me = await test_bot.get_me()
        bot_username = me.username
        bot_name = me.first_name
    except Exception as e:
        rich = premium_text(f"""\
[warning] <b>TOKEN TIDAK VALID</b>
<hr/>
<p>[catatan] Token yang kamu kirim tidak bisa diverifikasi ke Telegram. Pastikan kamu forward pesan asli dari @BotFather.</p>""")
        fallback = premium_text("""\
[warning] <b>TOKEN TIDAK VALID</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Token tidak bisa diverifikasi ke Telegram. Pastikan forward pesan asli dari @BotFather.</blockquote>
""")
        await notif.send_rich_message_to_chat(context.bot, chat_id, rich, fallback, log_label="CloneTokenInvalid")
        return True

    existing = clone_system.get_clone_by_token(DB_PATH, token)
    if existing:
        rich, fallback = clone_system.text_clone_sudah_ada(existing["status"])
        await notif.send_rich_message_to_chat(context.bot, chat_id, rich, fallback, log_label="CloneTokenSudahAda")
        return True

    ok, result = clone_system.register_clone_request(DB_PATH, uid, token, bot_username, bot_name)
    if not ok:
        rich = premium_text(f"[warning] <b>GAGAL MENDAFTARKAN CLONE</b>\n<hr/>\n<p>[catatan] {result}</p>")
        fallback = premium_text(f"[warning] GAGAL MENDAFTARKAN CLONE\n{result}")
        await notif.send_rich_message_to_chat(context.bot, chat_id, rich, fallback, log_label="CloneRegisterGagal")
        return True

    clone_id = result
    rich, fallback = clone_system.text_clone_terdeteksi(bot_username, bot_name)
    await notif.send_rich_message_to_chat(
        context.bot, chat_id, rich, fallback,
        reply_markup=clone_system.create_clone_owner_keyboard(),
        log_label="CloneTerdeteksi",
    )

    # Notif LANGSUNG ke chat pribadi tiap owner + inline button Approve/Tolak,
    # sama seperti pola approval deposit manual, supaya owner bisa proses cepat.
    # Pakai rich message (tabel + emoji premium), bukan teks polos.
    try:
        owner_kb = styled_inline_keyboard([
            [
                styled_button("Approve (10%)", callback_data=f"clone_approve_{clone_id}", style="success", emoji_name="verified"),
                styled_button("Tolak",         callback_data=f"clone_reject_{clone_id}",  style="danger",  emoji_name="batal"),
            ]
        ])
        owner_rich = premium_text(f"""\
[prem1] <b>PERMINTAAN CLONE BOT BARU</b>
<hr/>
<table bordered striped>
<tr><th>Informasi Clone</th><th>Detail</th></tr>
<tr><td>[crown] Owner</td><td><code>{uid}</code></td></tr>
<tr><td>[Telegram] Bot</td><td>@{bot_username} ({bot_name})</td></tr>
<tr><td>[card] Clone ID</td><td><code>{clone_id}</code></td></tr>
</table>
<p>[panahijo] Tekan <b>Approve</b> untuk setujui dengan komisi default 10%, atau ketik <code>/approveclone {clone_id} [komisi%]</code> untuk komisi custom.</p>""")
        owner_fallback = premium_text(f"""\
[prem1] <b>PERMINTAAN CLONE BOT BARU</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>
Owner     : {uid}
Bot       : @{bot_username} ({bot_name})
Clone ID  : {clone_id}

Tekan Approve untuk setujui dengan komisi default 10%, atau ketik
/approveclone {clone_id} [komisi%] untuk komisi custom.</blockquote>
""")
        owner_list = OWNER_ID if isinstance(OWNER_ID, list) else [OWNER_ID]
        for _oid in owner_list:
            try:
                await notif.send_rich_message_to_chat(
                    context.bot, _oid, owner_rich, owner_fallback,
                    reply_markup=owner_kb, log_label="CloneBaruOwnerNotif",
                )
            except Exception as _e2:
                print(f"[CloneDetect] Gagal kirim notif ke owner {_oid}: {_e2}")
    except Exception as e:
        print(f"[CloneDetect] Gagal notif owner: {e}")

    return True



async def handle_reply_keyboard_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Tangani tap tombol Reply Keyboard (teks polos, bukan callback)."""
    if not update.message or not update.message.text:
        return False
    txt = update.message.text.strip()
    uid = update.effective_user.id

    if txt == RKB_BACK_MAIN:
        await send_root_menu_new(context, uid)
        return True
    elif txt == RKB_MENU_NOKTEL:
        await send_main_menu_new(context, uid)
        return True
    elif txt == RKB_MENU_GIFT:
        await send_page2_menu_new(context, uid)
        return True
    elif txt == RKB_MENU_GMAIL:
        await send_page3_menu_new(context, uid)
        return True
    elif txt == RKB_NOKOS_ALLAPK:
        await send_page4_menu_new(context, uid)
        return True
    elif txt == RKB_MENU_CV:
        await send_page5_menu_new(context, uid)
        return True
    elif txt == RKB_MENU_STARS:
        await send_page6_menu_new(context, uid)
        return True
    elif txt == RKB_MENU_CEK_ID:
        await send_page7_menu_new(context, uid)
        return True
    elif txt == RKB_MENU_TON:
        await send_page8_menu_new(context, uid)
        return True
    elif txt == RKB_BULK_STARS:
        await dispatch_as_callback(update, context, "bulk_stars_beli_start")
        return True
    elif txt == RKB_TELE_PREMIUM:
        await dispatch_as_callback(update, context, "premium_beli_start")
        return True
    elif txt == RKB_PROFIL:
        await dispatch_as_callback(update, context, "menu_profile")
        return True
    elif txt == RKB_DEPOSIT:
        await deposit_menu_new(context, uid)
        return True
    elif txt == RKB_CS:
        await send_contact_cs_new(context, uid)
        return True
    elif txt == RKB_OWNER_PANEL:
        if is_owner(uid):
            await send_owner_panel_new(context, uid)
        return True
    elif txt == RKB_MENU_CLONE:
        await send_clone_menu_new(update, context)
        return True
    elif txt == RKB_MENU2_NEXT:
        await context.bot.send_message(
            chat_id=uid,
            text=premium_text("[panahijo] Menu 2 dibuka."),
            parse_mode="HTML",
            reply_markup=create_reply_keyboard_menu2(uid),
        )
        return True
    elif txt == RKB_MENU2_BACK:
        await send_root_menu_new(context, uid)
        return True
    elif txt == clone_system.RKB_CLONE_DOMPET:
        await clone_dompet_saya(update, context)
        return True
    elif txt == clone_system.RKB_CLONE_WD:
        await clone_withdraw_start(update, context)
        return True
    elif txt == clone_system.RKB_CLONE_STATISTIK:
        await clone_statistik(update, context)
        return True
    elif txt == clone_system.RKB_CLONE_KOMISI:
        await clone_atur_komisi_start(update, context)
        return True
    elif txt == clone_system.RKB_CLONE_SNK:
        await clone_snk_tutorial(update, context)
        return True
    elif txt == clone_system.RKB_CLONE_KEMBALI:
        await send_root_menu_new(context, uid)
        return True

    # ---- Tombol menu DI DALAM page 1-7 (dulu inline, sekarang Reply Keyboard) ----
    if await route_page_reply_button(update, context, txt):
        return True
    return False


# ---------- PENJAGA COMMAND /START ----------
@check_maintenance_decorator
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # === GUARD: TOLAK GRUP & BLOCKED USER ===
    if not await is_private_chat(update):
        return
    if update.effective_user and is_blocked(update.effective_user.id):
        return
    # ===========================================
    user = update.effective_user
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'bot_mode'")
    status = cursor.fetchone()
    conn.close()

    if status and status[0] == "maintenance" and user.id != OWNER_ID:
        pesan = (
            "<tg-emoji emoji-id='5368806667297238348'>⚡️</tg-emoji> <b>BOT SEDANG MAINTENANCE</b>\n\n"
            "<blockquote>"
            "<tg-emoji emoji-id='5454010941479873740'>‼️</tg-emoji> Bot sedang dalam proses perawatan rutin untuk meningkatkan kualitas layanan.\n\n"
            "Mohon bersabar, sistem akan kembali aktif sebentar lagi. Terima kasih sudah menunggu! "
            "<tg-emoji emoji-id='5395483821569249369'>⏯️</tg-emoji>\n"
            "[panahijo] Pantau update di channel resmi kami."
            "</blockquote>"
        )
        await update.message.reply_text(premium_text(pesan), parse_mode="HTML")
        return

    # --- JIKA SUDAH SUB CHANNEL WAJIB ---
    if await check_sub(update, context):
        context.user_data["current_menu_state"] = "idle"

        create_user(user.id, user.username or "")
        user_row = get_user(user.id)
        deposit_balance = user_row[2] if user_row and len(user_row) > 2 and user_row[2] is not None else 0
        belance_balance = user_row[3] if user_row and len(user_row) > 3 and user_row[3] is not None else 0
        username_display = f"@{user.username}" if user.username else "-"

        menu_html = f"""\
<tg-emoji emoji-id="6028530359975548369">💎</tg-emoji> <b>MANXY OFFICIAL — TOKO AKUN TELEGRAM</b>

<tg-emoji emoji-id="5990073381720953601">✨</tg-emoji> Halo, <b>{user.first_name or 'Pengguna'}</b>! Selamat datang kembali.

<table bordered striped>
<tr><th><tg-emoji emoji-id="5769547529993588669">👑</tg-emoji> Data Akun Anda</th><th>Detail</th></tr>
<tr><td>User ID</td><td><code>{user.id}</code></td></tr>
<tr><td>Username</td><td>{username_display}</td></tr>
<tr><td>Total Deposit</td><td><code>Rp {deposit_balance:,}</code></td></tr>
<tr><td>Sisa Saldo</td><td><b>Rp {belance_balance:,}</b></td></tr>
</table>

{build_footer_links_html()}

<tg-emoji emoji-id="6298432564487001027">✅</tg-emoji> Pilih menu di bawah ini untuk memulai layanan."""

        fallback_text = premium_text(f"""
[diamond1] <b>MANXY OFFICIAL — TOKO AKUN TELEGRAM</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[crown] <b>Halo, {user.first_name or 'Pengguna'}!</b> Selamat datang kembali.

[card] <b>User ID:</b> <code>{user.id}</code>
[sparkle] <b>Username:</b> {username_display}
[duitkarung] <b>Total Deposit:</b> <code>Rp {deposit_balance:,}</code>
[dolar] <b>Sisa Saldo:</b> <b>Rp {belance_balance:,}</b></blockquote>

{build_footer_links_html()}

<blockquote>[panahijo] Pilih menu di bawah ini untuk melanjutkan.</blockquote>
""")

        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id, menu_html, fallback_text,
            reply_markup=create_reply_keyboard(user.id),
            log_label="StartMenu",
        )
        return
        
    keyboard = []
    channels = CHANNEL_ID if isinstance(CHANNEL_ID, list) else [CHANNEL_ID]
    
    for i, ch_id in enumerate(channels, start=1):
        if isinstance(ch_id, str) and "UsernameChannel" in ch_id:
            continue           
        if isinstance(ch_id, str) and ch_id.startswith("@"):
            url_channel = f"https://t.me/{ch_id.replace('@', '')}"
        else:
            import config
            config_link_attr = f"CHANNEL_LINK_{i}"
            if hasattr(config, config_link_attr):
                url_channel = getattr(config, config_link_attr)
            else:
                clean_id = str(ch_id).replace("-100", "").replace("-", "")
                url_channel = f"https://t.me/c/{clean_id}/1"
            
        keyboard.append([styled_button(f"Gabung Channel {i}", url=url_channel, style="danger", emoji_name="Telegram")])
       
    keyboard.append([styled_button("Sudah Join", callback_data="check_join_manual", style="success", emoji_name="verified")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)

    rich_peringatan = premium_text("""\
[warning] <b>VERIFIKASI CHANNEL WAJIB</b>
<hr/>
<p>[catatan] Untuk mengakses layanan bot ini, kamu <b>wajib bergabung</b> ke semua channel resmi kami terlebih dahulu.</p>
<ul>
<li>[Telegram] Tekan tombol <b>Gabung Channel</b> satu per satu di bawah.</li>
<li>[verified] Setelah semua channel dijoin, tekan tombol <b>Sudah Join</b> untuk verifikasi.</li>
<li>[shield] Verifikasi diperlukan untuk menjaga kualitas layanan kami.</li>
</ul>""")
    fallback_peringatan = premium_text("""\
[warning] <b>VERIFIKASI CHANNEL WAJIB</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Untuk mengakses layanan bot ini, kamu wajib bergabung ke semua channel resmi kami.

[Telegram] Tekan tombol <b>Gabung Channel</b> satu per satu di bawah.
[verified] Setelah semua channel dijoin, tekan tombol <b>Sudah Join</b>.
[shield] Verifikasi diperlukan untuk menjaga kualitas layanan kami.</blockquote>
""")
    await notif.send_rich_message_to_chat(
        context.bot, update.effective_chat.id,
        rich_peringatan, fallback_peringatan,
        reply_markup=reply_markup,
        log_label="ForceJoin",
    )

# ---------- HANDLER TOMBOL CEK JOIN MANUAL ----------
async def check_join_manual_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    
    user = query.from_user
    user_id = user.id
    username = user.username or "Unknown"

    # Cek apakah sudah benar-benar join
    if not await check_sub(update, context):
        await query.answer("⚠️ Kamu belum join semua channel!", show_alert=True)
        try:
            await notif.send_rich_message_to_chat(context.bot, query.message.chat_id, premium_text(f"""[warning] <b>BELUM JOIN SEMUA CHANNEL</b>
<hr/>
<p>[catatan] Kamu harus bergabung ke <b>semua channel</b> di atas sebelum dapat menggunakan layanan ini.</p>
<ul><li>[panahijo] Tekan tombol <b>Gabung Channel</b> satu per satu.</li><li>[panahijo] Setelah semua dijoin, tekan tombol <b>Sudah Join</b> kembali.</li><li>[shield] Verifikasi diperlukan untuk menjaga kualitas layanan.</li></ul>"""), premium_text("""
[warning] <b>BELUM JOIN SEMUA CHANNEL</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Kamu harus bergabung ke <b>semua channel</b> di atas sebelum dapat menggunakan layanan ini.

[panahijo] Tekan tombol <b>Gabung Channel</b> satu per satu.
[panahijo] Setelah semua dijoin, tekan tombol <b>Sudah Join</b> kembali.
[shield] Verifikasi diperlukan untuk menjaga kualitas layanan.</blockquote>
"""), log_label="AutoRich2")
        except Exception:
            pass
        return

    # Daftarkan user ke SQLite jika belum ada
    is_new_user = get_user(user_id) is None  # cek SEBELUM insert
    create_user(user_id, username)

    # ===== NOTIF CHANNEL: USER BARU BERGABUNG (hanya jika benar-benar baru) =====
    if is_new_user:
        try:
            first_name = user.first_name or "Unknown"
            await notif.notif_user_baru(context.bot, user_id, first_name, username)
        except Exception as e:
            print(f"[Error Notif User Baru]: {e}")

    # Set state idle (belum di menu utama, nunggu user tap reply keyboard)
    context.user_data["current_menu_state"] = "idle"

    # Hapus pesan verifikasi channel
    try:
        await query.message.delete()
    except Exception:
        pass

    # Kirim teks welcome + ReplyKeyboard pakai rich message
    welcome_rich = premium_text(f"""\
[diamond1] <b>MANXY OFFICIAL — TOKO AKUN TELEGRAM</b>
<hr/>
<p>[verified] <b>Verifikasi berhasil!</b> Selamat datang, <b>{user.first_name or 'Sobat'}</b>! 🎉</p>
<ul>
<li>[panahijo] Pilih menu di bawah untuk mulai belanja atau top-up saldo.</li>
<li>[shield] Semua transaksi aman, cepat, dan tercatat otomatis.</li>
<li>[star] Gunakan tombol menu di bawah layar untuk navigasi.</li>
</ul>""")
    welcome_fallback = premium_text(f"""\
[diamond1] <b>MANXY OFFICIAL — TOKO AKUN TELEGRAM</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[verified] Verifikasi berhasil! Selamat datang, <b>{user.first_name or 'Sobat'}</b>!

[panahijo] Pilih menu di bawah untuk mulai belanja atau top-up saldo.
[shield] Semua transaksi aman, cepat, dan tercatat otomatis.
[star] Gunakan tombol menu di bawah layar untuk navigasi.</blockquote>
""")
    await notif.send_rich_message_to_chat(
        context.bot, query.message.chat_id,
        welcome_rich, welcome_fallback,
        reply_markup=create_reply_keyboard(user_id),
        log_label="WelcomeMenu",
    )
    
# ==================== PROFILE HANDLER ====================
async def show_profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    push_nav(context, "menu_profile")
    username = q.from_user.username or "No Username"
    
    user = get_user(uid)

    if not user:
        try:
            import sqlite3
            import time
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            now = int(time.time())
            
            cursor.execute(
                "INSERT INTO users (user_id, username, deposit_balance, belance_balance, created_at) VALUES (?, ?, ?, ?, ?)",
                (uid, username, 0, 0, now)
            )
            conn.commit()
            conn.close()
            user = get_user(uid)
        except Exception as db_err:
            print(f"[Error Auto-Register Profile]: {db_err}")
            await fast_edit(q, premium_text("[warning] <b>Gagal Memuat Profil</b>\n\n<blockquote>[catatan] Sistem database sedang sibuk. Coba beberapa saat lagi.\n[chat] Hubungi CS jika masalah berlanjut.</blockquote>"), reply_markup=create_back_button(), parse_mode="HTML", rich_html=premium_text(f"""[warning] <b>Gagal Memuat Profil</b>
<hr/>
<ul><li>[catatan] Sistem database sedang sibuk. Coba beberapa saat lagi.</li><li>[chat] Hubungi CS jika masalah berlanjut.</li></ul>"""), log_label="AutoRich")
            return

    if not user:
        await fast_edit(q, premium_text("[warning] <b>Data Pengguna Tidak Ditemukan</b>\n\n<blockquote>[catatan] Akun kamu belum terdaftar atau data tidak tersedia.\n[panahijo] Ketik /start untuk mendaftar ulang.</blockquote>"), reply_markup=create_back_button(), parse_mode="HTML", rich_html=premium_text(f"""[warning] <b>Data Pengguna Tidak Ditemukan</b>
<hr/>
<ul><li>[catatan] Akun kamu belum terdaftar atau data tidak tersedia.</li><li>[panahijo] Ketik /start untuk mendaftar ulang.</li></ul>"""), log_label="AutoRich")
        return

    # Ambil data dengan index aman
    try:
        deposit_balance = user[2] if len(user) > 2 and user[2] is not None else 0
        belance_balance = user[3] if len(user) > 3 and user[3] is not None else 0
        created_at      = user[4] if len(user) > 4 and user[4] is not None else 0
        tgl_gabung = datetime.fromtimestamp(int(created_at)).strftime('%d/%m/%Y') if created_at else "-"
    except Exception:
        deposit_balance = 0
        belance_balance = 0
        tgl_gabung = "-"

    rich_html = f"""\
<tg-emoji emoji-id="5769547529993588669">👑</tg-emoji> <b>PROFIL PENGGUNA</b>

<table bordered striped>
<tr><th><tg-emoji emoji-id="5769547529993588669">👑</tg-emoji> Informasi Akun</th><th>Detail</th></tr>
<tr><td>User ID</td><td><code>{uid}</code></td></tr>
<tr><td>Username</td><td>@{html.escape(str(username))}</td></tr>
<tr><td>Bergabung Sejak</td><td><code>{tgl_gabung}</code></td></tr>
</table>
<hr/>
<table bordered striped>
<tr><th><tg-emoji emoji-id="6089104607328342288">💰</tg-emoji> Informasi Saldo</th><th>Jumlah</th></tr>
<tr><td>Total Deposit Masuk</td><td><code>Rp {deposit_balance:,}</code></td></tr>
<tr><td>Sisa Saldo Aktif</td><td><b>Rp {belance_balance:,}</b></td></tr>
</table>

<tg-emoji emoji-id="6298432564487001027">✅</tg-emoji> <i>Akun terverifikasi sebagai pelanggan aktif.</i>"""
    text = premium_text(f"""
[crown] <b>PROFIL PENGGUNA</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[card] <b>User ID:</b> <code>{uid}</code>
[sparkle] <b>Username:</b> @{username}
[waktu] <b>Bergabung:</b> <code>{tgl_gabung}</code>

[duitkarung] <b>Total Deposit:</b> <code>Rp {deposit_balance:,}</code>
[dolar] <b>Sisa Saldo:</b> <b>Rp {belance_balance:,}</b>

[verified] <i>Akun terverifikasi sebagai pelanggan aktif.</i></blockquote>
""")
    
    keyboard_kembali = styled_inline_keyboard([[styled_button("Kembali", callback_data="menu_back", style="danger", emoji_name="back")]])
    await fast_edit(q, text, reply_markup=keyboard_kembali, parse_mode="HTML", rich_html=rich_html, log_label="ProfileMenu")

# ==================== STOCK HANDLER ====================
async def show_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    from src.custom_emoji import styled_keyboard_button
    
    query = update.callback_query
    await query.answer()
    push_nav(context, "menu_stock")

    rich_html = """\
<tg-emoji emoji-id="5397782960512444700">📌</tg-emoji> <b>FILTER PRODUK AKUN</b>

<table bordered striped>
<tr><th><tg-emoji emoji-id="6028206863038811654">🧾</tg-emoji> Kategori Stok</th><th>Keterangan</th></tr>
<tr><td>Tag Fake</td><td>Akun dengan label Palsu / Fake account</td></tr>
<tr><td>Tag Scam</td><td>Akun dengan label Scam dari Telegram</td></tr>
<tr><td>No Tag</td><td>Akun bersih tanpa label apapun (clean)</td></tr>
</table>

<tg-emoji emoji-id="5258500400918587241">📝</tg-emoji> Pilih kategori di bawah ini untuk melihat daftar stok yang tersedia."""
    text = premium_text("""
[product] <b>FILTER PRODUK AKUN</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Pilih kategori akun yang ingin kamu lihat:

[panahijo] <b>Tag Fake</b> — akun berlabel Palsu / Fake.
[panahijo] <b>Tag Scam</b> — akun berlabel Scam dari Telegram.
[panahijo] <b>No Tag</b> — akun bersih tanpa label (clean).

[pin] Tap kategori di bawah untuk lihat stok tersedia.</blockquote>
""")

    rows = [
        [
            styled_keyboard_button("Tag Fake", style="primary", emoji_name="product"),
            styled_keyboard_button("Tag Scam", style="primary", emoji_name="warning")
        ],
        [styled_keyboard_button("No Tag", style="primary", emoji_name="pin")],
        [styled_keyboard_button(RKB_BACK_MAIN, style="danger", emoji_name="back")]
    ]
    
    kb = ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)
    
    # Setup reply map untuk menghubungkan button text ke callback data
    reply_map = {
        "Tag Fake": "buyfilter_label_Palsu",
        "Tag Scam": "buyfilter_label_Scam",
        "No Tag": "buyfilter_label_No Tag",
        RKB_BACK_MAIN: "menu_back"
    }
    set_page_reply_map(context, "menu_stock", reply_map)

    await fast_edit(query, text=text, reply_markup=kb, parse_mode="HTML", rich_html=rich_html, log_label="StockFilterMenu")
    
# ==================== SHOW STOCK ====================
async def show_filtered_stock_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    stock = context.user_data.get('stock', [])
    page = context.user_data.get('stock_page', 0)
    filter_type = context.user_data.get('filter_type', 'STOCK')
    filter_count = context.user_data.get('filter_count', len(stock))
    
    # FIX: Filter out sold/deleted items from cached stock list by validating against database
    if stock:
        valid_stock = []
        for item in stock:
            try:
                if isinstance(item, dict):
                    db_id = item.get('id', '')
                else:
                    db_id = item[0]
                
                # Check if item still available in database
                cursor.execute("SELECT id FROM session_stock WHERE id=? AND status='available'", (db_id,))
                if cursor.fetchone():
                    valid_stock.append(item)
            except Exception as e:
                print(f"[FIX] Error validating stock item: {e}")
                valid_stock.append(item)  # Keep item if validation fails
        
        stock = valid_stock

    # FIX: Cek langsung ke Telegram apakah session akun sudah logout/dihapus dari HP aslinya.
    # Kalau memang sudah mati, otomatis dihapus dari stok supaya nggak nyangkut/nampilin akun kosong.
    if stock:
        stock = await prune_dead_stock(context, stock)

    # FIX: Total stok yang ditampilkan sekarang selalu dihitung ulang dari data yang sudah
    # divalidasi di atas, bukan dari angka cache lama (ini yang bikin "Total Stok" nggak sinkron
    # sama list akun yang beneran muncul).
    filter_count = len(stock)
    context.user_data['stock'] = stock
    context.user_data['filter_count'] = filter_count

    per_page = 5
    total_pages = (len(stock) + per_page - 1) // per_page if stock else 1
    start = page * per_page
    items = stock[start:start+per_page]
    
    rich_html = f"""\
<tg-emoji emoji-id="6028530359975548369">🩵</tg-emoji> <b>{html.escape(filter_type.upper())} STOCK LIST</b>

<table bordered striped>
<tr><th>Status Stok</th><th>Detail</th></tr>
<tr><td>Total Stok</td><td><code>{filter_count} session</code></td></tr>
<tr><td>Halaman</td><td><code>{page+1} / {total_pages}</code></td></tr>
</table>"""
    text = f"""
<tg-emoji emoji-id="6028530359975548369">🩵</tg-emoji> <b>{filter_type.upper()} STOCK LIST</b> <tg-emoji emoji-id="6028530359975548369">🩵</tg-emoji>
━━━━━━━━━━━━━━━━━━━━━━━━

<blockquote><tg-emoji emoji-id="5438496463044752972">🌟</tg-emoji> <b>STATUS STOK</b>
• <b>Total Stok :</b> <code>{filter_count} session</code>

<tg-emoji emoji-id="5438496463044752972">🌟</tg-emoji> <b>HALAMAN</b>
• <b>Progress   :</b> <code>{page+1} / {total_pages}</code></blockquote>
"""
    
    uid_for_nego = q.from_user.id if q and q.from_user else None
    nego_settings = nego_ai.get_nego_settings(DB_PATH)

    keyboard = []
    for item in items:
        try:
            if isinstance(item, dict):
                phone_number = item.get('phone', '')
                account_id = item.get('account_id', item.get('id', ''))
                price = item.get('price', 0)
                db_id = item.get('id', '')
                label_val = item.get('tag') or item.get('label') or 'No Tag'
                limit_val = item.get('status_limit') or 'No Limit'
            else:
                db_id = item[0]        # Kolom 1: ID database
                phone_number = item[2]  # Kolom 3: Nomor telepon
                account_id = item[3]    # Kolom 4: ID akun Telegram
                price = item[4]         # Kolom 5: Harga akun
                label_val = item[5] if len(item) > 5 and item[5] else 'No Tag'
                limit_val = item[6] if len(item) > 6 and item[6] else 'No Limit'
                
            if phone_number:
                premium_emoji_id = get_premium_country_flag(str(phone_number))
            else:
                premium_emoji_id = "globe"
                
            if not premium_emoji_id or premium_emoji_id == "sparkle":
                premium_emoji_id = "globe"

            # Cek apakah user ini SUDAH pernah dapat harga hasil nego khusus
            # untuk item ini (tidak berlaku untuk user lain).
            display_price, is_nego_price = price, False
            if uid_for_nego is not None:
                display_price, is_nego_price = nego_ai.get_effective_price(DB_PATH, uid_for_nego, db_id, price)
            
            # FIX: Apply markup harga otomatis jika ini clone bot
            try:
                display_price = clone_system.apply_clone_price_markup(context.bot.token, DB_PATH, display_price)
            except Exception:
                pass  # jika error, pakai harga original
            
            price_label = f"{format_currency(display_price)}" + (" (Nego)" if is_nego_price else "")
            
            # FIX: Nomor HP TIDAK ditampilkan ke pembeli (privasi) — yang tampil cuma
            # ID akun, label (Palsu/Scam/No Tag), status spambot (Limit/No Limit), dan harga.
            # Flag bendera premium tetap muncul sebagai ikon tombol (dari prefix nomor HP).
            button_text = f"ID {account_id} ({label_val}) - ({limit_val})\n{price_label}"
            
            # Buat susunan tombol inline
            row = [
                styled_button(
                    text=button_text, 
                    callback_data=f"buy_{db_id}", 
                    style="primary", 
                    emoji_name=premium_emoji_id
                ),
                styled_button(
                    text="QRIS", 
                    callback_data=f"direct_buy_{db_id}", 
                    style="success", 
                    emoji_name="card"
                )
            ]
            keyboard.append(row)
            if nego_settings["enabled"] and not is_nego_price:
                keyboard.append([
                    styled_button(
                        text=f"Nego Harga ID {account_id}",
                        callback_data=f"negostart_{db_id}",
                        style="danger",
                        emoji_name="dolar"
                    )
                ])
        except Exception as err:
            print(f"Gagal memproses item stok: {err}")
            continue
    
    nav = []
    if page > 0:
        nav.append(styled_button("PREV", callback_data=f"stock_page_{page-1}", style="primary", emoji_name="back"))
    if page < total_pages - 1:
        nav.append(styled_button("NEXT", callback_data=f"stock_page_{page+1}", style="primary", emoji_name="panahijo"))
    if nav:
        keyboard.append(nav)
    
    keyboard.append([styled_button("Kembali ke Filter", callback_data="menu_stock", style="danger", emoji_name="back")])
    
    await fast_edit(q, premium_text(text), reply_markup=styled_inline_keyboard(keyboard), parse_mode="HTML", rich_html=rich_html, log_label="StockList")

async def show_stock_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    page = int(q.data.split("_")[2])
    context.user_data['stock_page'] = page
    
    await show_filtered_stock_page(update, context)
    
#===================== ALL STOCK =======================
async def show_all_stock_list_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    rows = get_stock_all()

    if not rows:
        text = f"<tg-emoji emoji-id='{ID_STOCK}'>⚠️</tg-emoji> <b>Maaf, saat ini belum ada stok akun yang tersedia.</b>"
        keyboard = [[styled_button(text=" Kembali ", callback_data="menu_back", style="danger", emoji_name="back")]]
        await fast_edit(query, premium_text(text), parse_mode="HTML", reply_markup=styled_inline_keyboard(keyboard), rich_html=text, log_label="AllStockEmpty")
        return

    text = f"<tg-emoji emoji-id='5364265190353286344'>📊</tg-emoji> <b>RINCIAN STOK READY</b>\nTotal: <b>{len(rows)} Akun</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    table_rows = ""
    
    # Inisialisasi list keyboard baru
    keyboard = []

    for item in rows[:50]:
        if isinstance(item, dict):
            phone = item.get('phone', '')
            account_id = item.get('account_id', item.get('id', '?'))
            db_id = item.get('id', account_id)
            label = item.get('tag', item.get('label', 'No Tag'))
        else:
            phone = item[2] if len(item) > 2 else (item[1] if len(item) > 1 else '')
            account_id = item[1] if len(item) > 1 else '?'
            db_id = item[0] if len(item) > 0 else account_id
            label = item[5] if len(item) > 5 else "No Tag"
        
        flag_id = get_premium_country_flag(phone) or "globe"
        
        keyboard.append([
            styled_button(
                text=f"ID {account_id} [ {label} ]", 
                # FIX: callback_data harus pakai ID database (kolom `id`), bukan account_id
                # Telegram, karena process_buy() lookup pakai stock_id dari database.
                callback_data=f"buy_{db_id}",
                emoji_name=flag_id
            )
        ])
        
        # Tambahkan ke teks agar terlihat di pesan juga
        text += f"ID <code>{account_id}</code> [ <b>{label}</b> ]\n"
        table_rows += f"<tr><td><code>{account_id}</code></td><td>{label}</td></tr>\n"

    rich_html = f"""\
<tg-emoji emoji-id="5364265190353286344">📊</tg-emoji> <b>RINCIAN STOK READY</b>
Total: <b>{len(rows)} Akun</b>

<table bordered striped>
<tr><th>ID Akun</th><th>Label</th></tr>
{table_rows}</table>"""

    # Tambahkan tombol kembali
    keyboard.append([
        styled_button(text="Kembali ke Menu Utama", callback_data="menu_back", style="danger", emoji_name="back")
    ])
    
    await fast_edit(query, premium_text(text), parse_mode="HTML", reply_markup=styled_inline_keyboard(keyboard), rich_html=rich_html, log_label="AllStockList")

# ==================== NEGO HARGA (AI) — KHUSUS BUY NOKTEL ====================
async def start_nego_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User tekan tombol 'Nego Harga' pada salah satu item Buy Noktel."""
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id

    nego_settings = nego_ai.get_nego_settings(DB_PATH)
    if not nego_settings["enabled"]:
        await safe_answer(q, "Fitur nego harga sedang dimatikan oleh owner.", show_alert=True)
        return

    stock_id = int(q.data.split("_", 1)[1])
    stock = get_stock_detail(stock_id)
    if not stock:
        await fast_edit(
            q, premium_text("[warning] <b>Session Tidak Tersedia</b>"), reply_markup=create_back_button(),
            parse_mode="HTML", rich_html=f"{emoji('warning')} <b>Session Tidak Tersedia</b>", log_label="NegoStockGone"
        )
        return
    stock_id, sess, phone, user, aid, harga_asli = stock

    # Kalau user ini sudah punya deal aktif untuk item ini, jangan mulai chat baru
    existing = nego_ai.get_active_deal(DB_PATH, uid, stock_id)
    if existing:
        await safe_answer(q, "Kamu sudah punya harga hasil nego untuk item ini. Langsung beli aja!", show_alert=True)
        return

    context.user_data["nego_state"] = {
        "stock_id": stock_id,
        "harga_asli": harga_asli,
        "floor": nego_ai.compute_floor(harga_asli, nego_settings["max_diskon_persen"]),
        "phone": phone,
        "account_id": aid,
        "history": [],
    }
    context.user_data["current_menu_state"] = "nego_chat"

    text_rich = premium_text(f"""\
[dolar] <b>NEGO HARGA — ID {aid}</b>
<hr/>
<p>[card] Harga normal: <b>{format_currency(harga_asli)}</b></p>
<p>[catatan] Tulis langsung penawaran harga kamu di chat ini (contoh: <i>boleh {format_currency(int(harga_asli*0.85))} gak?</i>). Bot AI kami akan nego balik sama kamu.</p>
<p>[shield] Harga hasil nego ini HANYA berlaku buat kamu & item ini saja.</p>""")
    fallback = premium_text(f"""
[dolar] <b>NEGO HARGA — ID {aid}</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[card] Harga normal: <b>{format_currency(harga_asli)}</b>

[catatan] Tulis langsung penawaran harga kamu di chat ini. Bot AI kami akan nego balik sama kamu.
[shield] Harga hasil nego ini HANYA berlaku buat kamu & item ini saja.</blockquote>
""")
    keyboard = styled_inline_keyboard([
        [styled_button("Batal Nego", callback_data=f"negocancel_{stock_id}", style="danger", emoji_name="warning")]
    ])
    await fast_edit(q, text_rich, reply_markup=keyboard, parse_mode="HTML", rich_html=text_rich, log_label="NegoStart")
    try:
        await notif.send_rich_message_to_chat(context.bot, q.message.chat_id, text_rich, fallback, reply_markup=keyboard, log_label="NegoStart2")
    except Exception:
        pass


async def cancel_nego_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    context.user_data.pop("nego_state", None)
    context.user_data["current_menu_state"] = "idle"
    await fast_edit(
        q, premium_text("[warning] <b>Nego dibatalkan.</b> Kamu bisa beli dengan harga normal kapan saja."),
        reply_markup=create_back_button(), parse_mode="HTML",
        rich_html=f"{emoji('warning')} <b>Nego dibatalkan.</b> Kamu bisa beli dengan harga normal kapan saja.",
        log_label="NegoCancel",
    )


async def handle_nego_chat_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Dipanggil dari handle_message() saat current_menu_state == 'nego_chat'. Return True kalau ditangani."""
    if not update.message or not update.message.text:
        return False

    uid = update.effective_user.id
    state = context.user_data.get("nego_state")
    if not state:
        context.user_data["current_menu_state"] = "idle"
        return False

    stock_id = state["stock_id"]
    harga_asli = state["harga_asli"]
    floor = state["floor"]
    user_message = update.message.text.strip()

    thinking_msg = None
    try:
        thinking_msg = await update.message.reply_text(premium_text("[loading] <i>Bot sedang mikir tawaranmu...</i>"), parse_mode="HTML")
    except Exception:
        pass

    result = await nego_ai.nego_chat(state["history"], harga_asli, floor, user_message)

    if thinking_msg:
        try:
            await thinking_msg.delete()
        except Exception:
            pass

    # Simpan riwayat chat (dipakai supaya AI ingat konteks nego sebelumnya)
    state["history"].append({"role": "user", "content": user_message})
    state["history"].append({"role": "assistant", "content": result["balas"]})
    # Batasi riwayat supaya payload API tidak membengkak
    state["history"] = state["history"][-16:]

    if result["deal"] and result["harga_deal"]:
        harga_deal = int(result["harga_deal"])
        nego_ai.save_deal(DB_PATH, uid, stock_id, harga_asli, harga_deal)
        context.user_data.pop("nego_state", None)
        context.user_data["current_menu_state"] = "idle"

        rich = premium_text(f"""\
[verified] <b>NEGO BERHASIL!</b>
<hr/>
<p>{html.escape(result['balas'])}</p>
<p>[dolar] Harga khusus buat kamu: <b>{format_currency(harga_deal)}</b> <s>{format_currency(harga_asli)}</s></p>
<p>[catatan] Harga ini cuma berlaku buat kamu & item ini, dan ada batas waktunya. Langsung checkout ya!</p>""")
        fallback = premium_text(f"""
[verified] <b>NEGO BERHASIL!</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>{html.escape(result['balas'])}

[dolar] Harga khusus buat kamu: {format_currency(harga_deal)} (harga normal {format_currency(harga_asli)})
[catatan] Harga ini cuma berlaku buat kamu & item ini, dan ada batas waktunya. Langsung checkout ya!</blockquote>
""")
        keyboard = styled_inline_keyboard([
            [
                styled_button("Beli (Saldo)", callback_data=f"buy_{stock_id}", style="success", emoji_name="verified"),
                styled_button("Beli via QRIS", callback_data=f"direct_buy_{stock_id}", style="primary", emoji_name="card"),
            ]
        ])
        await notif.send_rich_message_to_chat(context.bot, update.effective_chat.id, rich, fallback, reply_markup=keyboard, log_label="NegoDeal")
        return True

    context.user_data["nego_state"] = state
    rich = premium_text(f"[dolar] {html.escape(result['balas'])}")
    fallback = premium_text(f"[dolar] {html.escape(result['balas'])}")
    keyboard = styled_inline_keyboard([
        [styled_button("Batal Nego", callback_data=f"negocancel_{stock_id}", style="danger", emoji_name="warning")]
    ])
    await notif.send_rich_message_to_chat(context.bot, update.effective_chat.id, rich, fallback, reply_markup=keyboard, log_label="NegoReply")
    return True


async def cmd_setnego(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Owner-only. Atur fitur nego harga (khusus Buy Noktel):
      /setnego on            -> nyalakan fitur nego
      /setnego off           -> matikan fitur nego
      /setnego 15            -> set batas diskon maksimum jadi 15%
      /setnego status        -> lihat status & batas saat ini
    """
    uid = update.effective_user.id
    if not is_owner(uid):
        return

    args = context.args if getattr(context, "args", None) else []
    settings = nego_ai.get_nego_settings(DB_PATH)

    if not args or args[0].lower() == "status":
        status_text = premium_text(f"""\
[dolar] <b>STATUS FITUR NEGO HARGA</b>
<hr/>
<p>[verified] Status: <b>{'AKTIF' if settings['enabled'] else 'NONAKTIF'}</b></p>
<p>[card] Maks. Diskon: <b>{settings['max_diskon_persen']}%</b></p>
<p>[catatan] Perintah: <code>/setnego on</code> | <code>/setnego off</code> | <code>/setnego &lt;persen&gt;</code></p>""")
        await notif.send_rich_message_to_chat(context.bot, update.effective_chat.id, status_text, status_text, log_label="SetNegoStatus")
        return

    arg = args[0].lower()
    if arg == "on":
        nego_ai.set_nego_enabled(DB_PATH, True)
        msg = "[verified] Fitur nego harga <b>diaktifkan</b>."
    elif arg == "off":
        nego_ai.set_nego_enabled(DB_PATH, False)
        msg = "[warning] Fitur nego harga <b>dinonaktifkan</b>."
    elif arg.isdigit():
        persen = nego_ai.set_nego_max_diskon(DB_PATH, int(arg))
        msg = f"[verified] Batas diskon maksimum di-set ke <b>{persen}%</b>."
    else:
        msg = "[warning] Format salah. Pakai: <code>/setnego on</code> / <code>/setnego off</code> / <code>/setnego 15</code>"

    text = premium_text(msg)
    await notif.send_rich_message_to_chat(context.bot, update.effective_chat.id, text, text, log_label="SetNego")


# ==================== OWNER PANEL — NEGO HARGA (INLINE) ====================
NEGO_PERSEN_PRESETS = [5, 10, 15, 20, 25, 30]


def _build_nego_panel_keyboard(settings: dict):
    toggle_btn = (
        styled_button("Matikan Nego", callback_data="nego_toggle_off", style="danger", emoji_name="warning")
        if settings["enabled"] else
        styled_button("Aktifkan Nego", callback_data="nego_toggle_on", style="success", emoji_name="verified")
    )
    persen_rows = []
    for i in range(0, len(NEGO_PERSEN_PRESETS), 3):
        persen_rows.append([
            styled_button(
                f"{p}%" + (" ✓" if p == settings["max_diskon_persen"] else ""),
                callback_data=f"nego_persen_{p}",
                style="primary" if p != settings["max_diskon_persen"] else "success",
                emoji_name="dolar",
            )
            for p in NEGO_PERSEN_PRESETS[i:i+3]
        ])
    return styled_inline_keyboard(
        [[toggle_btn]]
        + persen_rows
        + [
            [styled_button("Input Manual %", callback_data="nego_persen_custom", style="primary", emoji_name="gear")],
            [styled_button("Kembali", callback_data="menu_owner", style="danger", emoji_name="back")],
        ]
    )


def _nego_panel_text(settings: dict):
    rich = premium_text(f"""\
[diamond1] <b>PENGATURAN NEGO HARGA — BUY NOKTEL</b>
<hr/>
<p>[verified] Status: <b>{'AKTIF' if settings['enabled'] else 'NONAKTIF'}</b></p>
<p>[card] Batas Diskon Maksimum: <b>{settings['max_diskon_persen']}%</b></p>
<p>[catatan] Fitur ini HANYA berlaku di menu Buy Noktel. Harga hasil nego cuma berlaku untuk user & item yang bersangkutan.</p>""")
    fallback = premium_text(f"""
[diamond1] <b>PENGATURAN NEGO HARGA — BUY NOKTEL</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[verified] Status: {'AKTIF' if settings['enabled'] else 'NONAKTIF'}
[card] Batas Diskon Maksimum: {settings['max_diskon_persen']}%

[catatan] Fitur ini HANYA berlaku di menu Buy Noktel. Harga hasil nego cuma berlaku untuk user & item yang bersangkutan.</blockquote>
""")
    return rich, fallback


async def owner_nego_settings_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)

    settings = nego_ai.get_nego_settings(DB_PATH)
    rich, fallback = _nego_panel_text(settings)
    await fast_edit(q, rich, reply_markup=_build_nego_panel_keyboard(settings), parse_mode="HTML", rich_html=rich, log_label="OwnerNegoPanel")


async def owner_nego_toggle_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    nego_ai.set_nego_enabled(DB_PATH, q.data == "nego_toggle_on")
    await safe_answer(q, "Fitur nego diaktifkan!" if q.data == "nego_toggle_on" else "Fitur nego dimatikan!")
    settings = nego_ai.get_nego_settings(DB_PATH)
    rich, fallback = _nego_panel_text(settings)
    await fast_edit(q, rich, reply_markup=_build_nego_panel_keyboard(settings), parse_mode="HTML", rich_html=rich, log_label="OwnerNegoToggle")


async def owner_nego_persen_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return

    if q.data == "nego_persen_custom":
        await safe_answer(q)
        context.user_data["current_menu_state"] = "owner_wait_nego_persen"
        text = premium_text("[catatan] <b>Kirim angka persen batas diskon</b> (0-90), contoh: <code>18</code>")
        keyboard = styled_inline_keyboard([[styled_button("Batal", callback_data="owner_nego_settings", style="danger", emoji_name="warning")]])
        await fast_edit(q, text, reply_markup=keyboard, parse_mode="HTML", rich_html=text, log_label="OwnerNegoCustomAsk")
        return

    persen = int(q.data.split("_")[-1])
    nego_ai.set_nego_max_diskon(DB_PATH, persen)
    await safe_answer(q, f"Batas diskon di-set ke {persen}%!")
    settings = nego_ai.get_nego_settings(DB_PATH)
    rich, fallback = _nego_panel_text(settings)
    await fast_edit(q, rich, reply_markup=_build_nego_panel_keyboard(settings), parse_mode="HTML", rich_html=rich, log_label="OwnerNegoPersen")


async def owner_nego_custom_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Dipanggil dari handle_message() saat current_menu_state == 'owner_wait_nego_persen'."""
    if not update.message or not update.message.text:
        return False
    uid = update.effective_user.id
    if not is_owner(uid):
        return False

    txt = update.message.text.strip()
    if not txt.isdigit():
        text = premium_text("[warning] Masukkan angka saja, contoh: <code>18</code>")
        await notif.send_rich_message_to_chat(context.bot, update.effective_chat.id, text, text, log_label="OwnerNegoCustomInvalid")
        return True

    persen = nego_ai.set_nego_max_diskon(DB_PATH, int(txt))
    context.user_data["current_menu_state"] = "idle"
    settings = nego_ai.get_nego_settings(DB_PATH)
    rich, fallback = _nego_panel_text(settings)
    await notif.send_rich_message_to_chat(
        context.bot, update.effective_chat.id, rich, fallback,
        reply_markup=_build_nego_panel_keyboard(settings), log_label="OwnerNegoCustomSet",
    )
    return True


# ==================== BUY HANDLERS ====================
async def process_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    
    stock_id = int(q.data.split("_")[1])
    
    if not check_cooldown(uid):
        await safe_answer(q, "Cooldown! Tunggu sebentar.", show_alert=True)
        return
    
    stock = get_stock_detail(stock_id)
    if not stock:
        await fast_edit(
            q, premium_text("[warning] <b>Session Tidak Tersedia</b>"), reply_markup=create_back_button(),
            parse_mode="HTML", rich_html=f"{emoji('warning')} <b>Session Tidak Tersedia</b>", log_label="BuyStockGone"
        )
        return
    
    stock_id, sess, phone, user, aid, price = stock
    # Kalau user ini punya harga hasil nego khusus untuk item ini, pakai itu.
    # User lain tetap kena harga_asli (price dari kolom session_stock).
    price, _is_nego_price = nego_ai.get_effective_price(DB_PATH, uid, stock_id, price)
    
    # SIMPAN base_price sebelum di-apply markup (untuk kalkulasi komisi yang akurat)
    base_price = price
    
    # FIX: Apply markup harga otomatis jika ini transaksi di clone bot
    # Harga ditampilkan ke customer dengan markup komisi pemilik clone
    # Tapi saldo yang dipotong tetap sesuai harga yang tertera di sini
    try:
        price = clone_system.apply_clone_price_markup(context.bot.token, DB_PATH, price)
    except Exception:
        pass  # jika error, pakai harga original
    
    user_data = get_user(uid)
    # FIX: Cek belance_balance (user_data[3]) bukan deposit_balance (user_data[2])
    available_balance = user_data[3] if user_data and len(user_data) > 3 else 0
    if not user_data or available_balance < price:
        await fast_edit(
            q,
            premium_text(f"[warning] <b>Balance Tidak Cukup!</b>\n\n<blockquote>[duitkarung] Balance: <code>{format_currency(available_balance)}</code>\n[dolar] Harga: <code>{format_currency(price)}</code></blockquote>"),
            reply_markup=create_back_button(), parse_mode="HTML",
            rich_html=(
                f"{emoji('warning')} <b>Balance Tidak Cukup!</b>\n<hr/>\n"
                f"<p>{emoji('duitkarung')} Balance: <code>{format_currency(available_balance)}</code></p>\n"
                f"<p>{emoji('dolar')} Harga: <code>{format_currency(price)}</code></p>"
            ),
            log_label="BuyBalanceLess",
        )
        return

    # === FIX: CEK SESSION MASIH AKTIF SEBELUM POTONG SALDO (samain dengan flow direct buy/QRIS) ===
    checking_msg = None
    try:
        checking_msg = await q.message.reply_text(premium_text("[loading] <b>Memverifikasi status akun, harap tunggu...</b>"), parse_mode="HTML")
    except Exception:
        pass

    is_alive = await check_session_alive(sess)

    if checking_msg:
        try:
            await checking_msg.delete()
        except Exception:
            pass

    if not is_alive:
        remove_stock(stock_id)
        context.user_data.pop('stock', None)
        context.user_data.pop('stock_page', None)
        await fast_edit(
            q,
            premium_text("""[warning] <b>SESSION SUDAH TIDAK AKTIF</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Akun ini ternyata sudah logout/expired dan otomatis dihapus dari stok. Saldo kamu tidak dipotong.
[panahijo] Silakan pilih akun lain yang masih tersedia.</blockquote>"""),
            reply_markup=create_back_button(),
            parse_mode="HTML",
            rich_html=(
                f"{emoji('warning')} <b>SESSION SUDAH TIDAK AKTIF</b>\n<hr/>\n"
                f"<p>{emoji('catatan')} Akun ini ternyata sudah logout/expired dan otomatis dihapus dari stok. Saldo kamu tidak dipotong.</p>\n"
                f"<p>{emoji('panahijo')} Silakan pilih akun lain yang masih tersedia.</p>"
            ),
            log_label="BuySessionDead",
        )
        return
    
    # 1. Eksekusi perubahan data ke database
    # ATOMIC: UPDATE hanya berhasil jika status masih 'available' — cegah double-buy
    try:
        import sqlite3 as _sq3
        _ac = _sq3.connect(DB_PATH)
        _ac.execute("UPDATE session_stock SET status='sold' WHERE id=? AND status='available'", (stock_id,))
        _changed_stock = _ac.execute("SELECT changes()").fetchone()[0]
        _ac.commit()
        _ac.close()
    except Exception as _ae:
        print(f"[Error Atomic SessionBuy]: {_ae}")
        _changed_stock = 0

    if not _changed_stock:
        await fast_edit(
            q, premium_text("[warning] <b>Session Sudah Habis Terjual ke User Lain!</b>"),
            reply_markup=create_back_button(), parse_mode="HTML",
            rich_html=f"{emoji('warning')} <b>Session Sudah Habis Terjual ke User Lain!</b>",
            log_label="BuyRace",
        )
        return

    # FIX: Kurangi belance_balance (available balance) bukan deposit_balance
    update_balance(uid, belance_delta=-price)
    # mark_as_sold hanya catat sold_sessions & notif (status sudah di-set di atas)
    # FIX: retry kalau kena "database locked", dan JANGAN diam-diam kalau gagal total
    _insert_ok = False
    sold_session_id = None
    _last_insert_err = None
    for _attempt in range(3):
        try:
            cursor.execute("""
                INSERT INTO sold_sessions (stock_id, buyer_id, phone, username, account_id, password, session_string, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (stock_id, uid, phone, user, aid, DEFAULT_2FA_PASSWORD, sess, int(time.time())))
            conn.commit()
            _insert_ok = True
            sold_session_id = cursor.lastrowid
            break
        except Exception as _se:
            _last_insert_err = _se
            print(f"[Error sold_sessions insert] attempt {_attempt+1}: {_se}")
            time.sleep(0.3)

    if not _insert_ok:
        print(f"[CRITICAL] Insert sold_sessions GAGAL TOTAL stock_id={stock_id} buyer_id={uid}: {_last_insert_err}")
        await fast_edit(
            q,
            premium_text("[warning] <b>Pembelian gagal dicatat karena kendala database. Saldo akan otomatis dikembalikan, silakan coba beli lagi.</b>"),
            reply_markup=create_back_button(), parse_mode="HTML"
        , rich_html=premium_text(f"""[warning] <b>Pembelian gagal dicatat karena kendala database. Saldo akan otomatis dikembalikan, silakan coba beli lagi.</b>"""), log_label="AutoRich")
        update_balance(uid, belance_delta=price)
        try:
            cursor.execute("UPDATE session_stock SET status='available' WHERE id=?", (stock_id,))
            conn.commit()
        except Exception as _rb_err:
            print(f"[CRITICAL] Gagal rollback stock status: {_rb_err}")
        return
    update_user_stats(uid)
    if _is_nego_price:
        nego_ai.clear_deal(DB_PATH, uid, stock_id)
    
    # =====================================================================
    # FIX NOTIF: Kirim Notifikasi Pembelian ke Channel (Potong Saldo)
    # =====================================================================
    try:
        # Membuat Order ID unik berbasis waktu Unix untuk tanda di channel
        order_id_potong = f"BUY-{int(time.time())}"
        _u = get_user(uid)
        _uname = _u[1] if _u and _u[1] else None
        _sisa = _u[3] if _u and len(_u) > 3 else None
        await notif.notif_pembelian_channel(
            bot=context.bot,
            user_id=uid,
            phone=phone,
            account_id=aid,
            amount=price,
            order_id=order_id_potong,
            username=_uname,
            saldo_sisa=_sisa
        )
    except Exception as n_err:
        print(f"[Error Trigger Notif Saldo]: {n_err}")
    # =====================================================================
    try:
        await clone_system.process_transaction_commission(
            context.bot, DB_PATH, uid, order_id_potong, "Beli Session", price, base_price
        )
    except Exception as _ce:
        print(f"[CloneCommission] {_ce}")
    # =====================================================================
    
    # FIX: Clear cached stock list so it reloads from database on next view
    context.user_data.pop('stock', None)
    context.user_data.pop('stock_page', None)
    
    # 2. Kirim struk sukses ke pembeli
    success_text = premium_text(f"""
[done] <b>PEMBAYARAN BERHASIL — AKUN SIAP DIGUNAKAN</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[product] <b>Detail Akun Anda</b>

[Telegram] <b>ID Akun:</b> <code>{aid}</code>
[WhatsApp] <b>Nomor:</b> <code>{phone}</code>
[password] <b>Password 2FA:</b> <code>{DEFAULT_2FA_PASSWORD}</code>
[verified] <b>Status:</b> CLEAN — Siap Login

[catatan] Simpan data ini baik-baik. Gunakan menu OTP di bawah jika dibutuhkan kode verifikasi.</blockquote>
""")
    success_rich_html = f"""\
{emoji('done')} <b>PEMBAYARAN BERHASIL — AKUN SIAP DIGUNAKAN</b>

<table bordered striped>
<tr><th>Detail Akun</th><th>Isi</th></tr>
<tr><td>ID Akun</td><td><code>{aid}</code></td></tr>
<tr><td>Nomor</td><td><code>{phone}</code></td></tr>
<tr><td>Password 2FA</td><td><code>{DEFAULT_2FA_PASSWORD}</code></td></tr>
<tr><td>Status</td><td>CLEAN — Siap Login</td></tr>
</table>

{emoji('catatan')} Simpan data ini baik-baik. Gunakan menu OTP di bawah jika dibutuhkan kode verifikasi."""
    await fast_edit(q, success_text, reply_markup=create_order_success_keyboard(sold_session_id, phone), parse_mode="HTML", rich_html=success_rich_html, log_label="BuySuccess")

async def process_direct_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    
    session_id = int(q.data.split("_")[2])
    
    if not check_cooldown(uid):
        await safe_answer(q, "Cooldown! Tunggu sebentar.", show_alert=True)
        return
    
    stock = get_stock_detail(session_id)
    if not stock:
        await fast_edit(
            q, premium_text("[warning] <b>Session Tidak Tersedia</b>"), reply_markup=create_back_button(),
            parse_mode="HTML", rich_html=f"{emoji('warning')} <b>Session Tidak Tersedia</b>", log_label="DirectBuyStockGone"
        )
        return
    
    stock_id, sess, phone, user, aid, price = stock
    # Kalau user ini punya harga hasil nego khusus untuk item ini, pakai itu.
    price, _is_nego_price = nego_ai.get_effective_price(DB_PATH, uid, stock_id, price)
    
    # SIMPAN base_price sebelum di-apply markup (untuk kalkulasi komisi yang akurat)
    base_price = price
    
    # FIX: Apply markup harga otomatis jika ini transaksi di clone bot
    try:
        price = clone_system.apply_clone_price_markup(context.bot.token, DB_PATH, price)
    except Exception:
        pass  # jika error, pakai harga original

    # === CEK SESSION MASIH AKTIF SEBELUM DITAWARKAN KE BUYER ===
    checking_msg = None
    try:
        checking_msg = await q.message.reply_text(premium_text("[loading] <b>Memverifikasi status akun, harap tunggu...</b>"), parse_mode="HTML")
    except Exception:
        pass

    is_alive = await check_session_alive(sess)

    if not is_alive:
        remove_stock(stock_id)
        if checking_msg:
            try:
                await checking_msg.delete()
            except Exception:
                pass
        await fast_edit(
            q,
            premium_text("""[warning] <b>AKUN TIDAK AKTIF — STOK DIHAPUS OTOMATIS</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Akun ini ternyata sudah logout atau expired di perangkat aslinya.
[shield] Stok otomatis dihapus untuk menjaga kualitas layanan.
[panahijo] Silakan kembali dan pilih akun lain yang masih tersedia.</blockquote>"""),
            reply_markup=create_back_button(),
            parse_mode="HTML",
            rich_html=(
                f"{emoji('warning')} <b>AKUN TIDAK AKTIF — STOK DIHAPUS OTOMATIS</b>\n<hr/>\n"
                f"<p>{emoji('catatan')} Akun ini ternyata sudah logout atau expired di perangkat aslinya.</p>\n"
                f"<p>{emoji('shield')} Stok otomatis dihapus untuk menjaga kualitas layanan.</p>\n"
                f"<p>{emoji('panahijo')} Silakan kembali dan pilih akun lain yang masih tersedia.</p>"
            ),
            log_label="DirectBuySessionDead",
        )
        return

    if checking_msg:
        try:
            await checking_msg.delete()
        except Exception:
            pass
    
    pending_direct_buy[uid] = {
        'session_id': session_id,
        'stock_id': stock_id,
        'session_string': sess,
        'phone': phone,
        'username': user,
        'account_id': aid,
        'price': price,
        'is_nego_price': _is_nego_price,
        # Simpan token bot TEMPAT pembeli order (pusat ATAU clone tertentu).
        # Dipakai nanti supaya notif approve/tolak manual dikirim balik lewat
        # bot yang SAMA dengan tempat order dibuat, bukan lewat bot pusat
        # (yang selalu jadi context.bot saat Owner approve). Lihat get_origin_bot().
        'origin_bot_token': getattr(context.bot, 'token', None),
    }
    
    confirm_text = f"""
DIRECT PURCHASE

Phone    : {phone}
ID       : {aid}
Price    : {format_currency(price)}

You will pay via QRIS
"""
    
    keyboard = styled_inline_keyboard([
        [styled_button("Confirm & Pay", callback_data=f"confirm_direct_{session_id}", style="success", emoji_name="card")],
        [styled_button("Batal", callback_data="cancel_direct_buy", style="danger", emoji_name="warning")]
    ])

    confirm_text = premium_text(f"""
[card] <b>KONFIRMASI PEMBELIAN</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[product] <b>Detail Akun yang Dipilih</b>

[WhatsApp] <b>Nomor:</b> <code>{phone}</code>
[Telegram] <b>ID Akun:</b> <code>{aid}</code>
[dolar] <b>Harga:</b> <b>{format_currency(price)}</b>

[catatan] Pastikan detail di atas sudah benar sebelum melanjutkan pembayaran via QRIS.
[shield] Transaksi akan diproses otomatis setelah pembayaran terverifikasi.</blockquote>
""")
    confirm_rich_html = f"""\
{emoji('card')} <b>KONFIRMASI PEMBELIAN</b>

<table bordered striped>
<tr><th>Detail Akun</th><th>Isi</th></tr>
<tr><td>Nomor</td><td><code>{phone}</code></td></tr>
<tr><td>ID Akun</td><td><code>{aid}</code></td></tr>
<tr><td>Harga</td><td><b>{format_currency(price)}</b></td></tr>
</table>

<p>{emoji('catatan')} Pastikan detail di atas sudah benar sebelum melanjutkan pembayaran via QRIS.</p>
<p>{emoji('shield')} Transaksi akan diproses otomatis setelah pembayaran terverifikasi.</p>"""
    await fast_edit(q, confirm_text, reply_markup=keyboard, parse_mode="HTML", rich_html=confirm_rich_html, log_label="DirectBuyConfirm")

async def confirm_direct_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    session_id = int(q.data.split("_")[2])
    
    if uid not in pending_direct_buy:
        await fast_edit(q, premium_text("[warning] <b>Data Akun Tidak Ditemukan</b>\n\n<blockquote>[catatan] Data session tidak ditemukan atau sudah kadaluarsa.\n[panahijo] Silakan ulangi proses pembelian dari awal.</blockquote>"), reply_markup=create_back_button(), parse_mode="HTML", rich_html=premium_text(f"""[warning] <b>Data Akun Tidak Ditemukan</b>
<hr/>
<ul><li>[catatan] Data session tidak ditemukan atau sudah kadaluarsa.</li><li>[panahijo] Silakan ulangi proses pembelian dari awal.</li></ul>"""), log_label="AutoRich")
        return
    
    data = pending_direct_buy[uid]
    if data['session_id'] != session_id:
        await fast_edit(
            q, premium_text("[warning] <b>Data Tidak Cocok (Mismatch)</b>"), reply_markup=create_back_button(),
            parse_mode="HTML", rich_html=f"{emoji('warning')} <b>Data Tidak Cocok (Mismatch)</b>", log_label="DirectBuyMismatch"
        )
        return
    
    price = data['price']
    phone = data['phone']
    aid = data['account_id']
    stock_id = data['stock_id']
    
    # ── FIX: CEK METODE PAYMENT (MANUAL ATAU OTOMATIS) ──────────────────
    if get_payment_method() == "manual":
        # Mode manual: tampilkan instruksi transfer manual
        context.user_data["current_menu_state"] = "session_wait_bukti"
        context.user_data["_session_manual_amount"] = price
        context.user_data["_session_manual_phone"] = phone
        context.user_data["_session_manual_aid"] = aid
        context.user_data["_session_manual_stock"] = stock_id
        context.user_data["_session_manual_session_id"] = session_id
        
        pay_lines = []
        for label, key, _ in PAYMENT_METHODS_LIST:
            if key == "qris":
                continue
            info = get_payment_info(key)
            if info:
                pay_lines.append(f"[panahijo] <b>{label}:</b> <code>{info}</code>")
        rekening_text = "\n".join(pay_lines) if pay_lines else ""
        qris_file_id = get_payment_info("qris")
        
        text_manual = premium_text(f"""[card] <b>INSTRUKSI PEMBAYARAN MANUAL — TELEGRAM SESSION</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[WhatsApp] <b>Nomor:</b> <code>{phone}</code>
[Telegram] <b>ID Akun:</b> <code>{aid}</code>
[dolar] <b>Nominal Transfer:</b> <b>Rp {price:,}</b>

{"" if not rekening_text else rekening_text + chr(10)}{"[card] Scan QRIS di bawah ini atau transfer ke rekening di atas." if qris_file_id and rekening_text else ("[card] Scan QRIS di bawah ini untuk transfer." if qris_file_id else ("[warning] Info rekening belum diset oleh owner." if not rekening_text else ""))}

[warning] Transfer TEPAT nominal <b>Rp {price:,}</b> — jangan dibulatkan.
[catatan] Setelah transfer, kirim <b>foto/screenshot bukti transfer</b> ke chat bot ini.
[shield] Session Telegram akan dikirim segera setelah Owner menyetujui bukti kamu.</blockquote>""")
        
        kb_manual = styled_inline_keyboard([[styled_button("Batal", callback_data="cancel_session_manual", style="danger", emoji_name="back")]])
        
        if qris_file_id:
            try:
                await q.message.reply_photo(
                    photo=qris_file_id,
                    caption=text_manual,
                    parse_mode="HTML",
                    reply_markup=kb_manual
                )
                await fast_edit(q, premium_text("[done] <b>Instruksi Pembayaran Terkirim</b>\n\n<blockquote>[catatan] Kirim foto/screenshot bukti transfer ke bot ini setelah selesai transfer.\n[shield] Session akan dikirim setelah Owner menyetujui.</blockquote>"), parse_mode="HTML", rich_html=premium_text(f"""[done] <b>Instruksi Pembayaran Terkirim</b>
<hr/>
<ul><li>[catatan] Kirim foto/screenshot bukti transfer ke bot ini setelah selesai transfer.</li><li>[shield] Session akan dikirim setelah Owner menyetujui.</li></ul>"""), log_label="AutoRich")
                return
            except Exception as e:
                print(f"[session_manual QRIS foto] {e}")
        
        await q.message.reply_text(text_manual, parse_mode="HTML", reply_markup=kb_manual)
        await fast_edit(q, premium_text("[done] <b>Instruksi Pembayaran Terkirim</b>\n\n<blockquote>[catatan] Kirim foto/screenshot bukti transfer ke bot ini setelah selesai transfer.\n[shield] Session akan dikirim setelah Owner menyetujui.</blockquote>"), parse_mode="HTML", rich_html=premium_text(f"""[done] <b>Instruksi Pembayaran Terkirim</b>
<hr/>
<ul><li>[catatan] Kirim foto/screenshot bukti transfer ke bot ini setelah selesai transfer.</li><li>[shield] Session akan dikirim setelah Owner menyetujui.</li></ul>"""), log_label="AutoRich")
        return
    # ──────────────────────────────────────────────────────────────────────
    
    status_msg = await notif.send_rich_message_to_chat(
        context.bot, q.message.chat_id,
        f"{emoji('loading')} <b>Membuat QRIS pembayaran, harap tunggu sebentar...</b>",
        premium_text("[loading] <b>Membuat QRIS pembayaran, harap tunggu sebentar...</b>"),
        log_label="DirectBuyQrisLoading",
    )
    
    trx = await create_qris(price)
    if not trx:
        if status_msg is not None:
            await notif.edit_rich_message(
                context.bot, q.message.chat_id, status_msg,
                f"{emoji('warning')} <b>Gagal membuat QRIS. Coba lagi atau hubungi CS jika masalah berlanjut.</b>",
                premium_text("[warning] <b>Gagal membuat QRIS. Coba lagi atau hubungi CS jika masalah berlanjut.</b>"),
                reply_markup=create_back_button(), log_label="DirectBuyQrisFailed",
            )
        else:
            await notif.send_rich_message_to_chat(
                context.bot, q.message.chat_id,
                premium_text("[warning] <b>Gagal membuat QRIS. Coba lagi atau hubungi CS jika masalah berlanjut.</b>"),
                premium_text("[warning] <b>Gagal membuat QRIS. Coba lagi atau hubungi CS jika masalah berlanjut.</b>"),
                reply_markup=create_back_button(), log_label="DirectBuyQrisFailed",
            )
        return
    
    expires_at = int(time.time()) + 300
    
    with open(trx['qr_path'], "rb") as f:
        qr_msg = await q.message.reply_photo(
            f,
            caption=premium_text(f"""
[card] <b>PEMBAYARAN QRIS</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[product] <b>Detail Pesanan</b>

[WhatsApp] <b>Nomor:</b> <code>{phone}</code>
[Telegram] <b>ID Akun:</b> <code>{aid}</code>
[dolar] <b>Nominal Bayar:</b> <b>{format_currency(price)}</b>
[verified] <b>Order ID:</b> <code>{trx['id']}</code>
[waktu] <b>Batas Waktu:</b> 5 menit

[catatan] Scan QRIS di atas menggunakan aplikasi dompet digital, lalu tekan <b>Verifikasi Bayar</b> setelah transfer selesai.</blockquote>
"""),
            reply_markup=create_payment_keyboard(trx['id'], session_id),
            parse_mode="HTML"
        )
    
    add_pending_payment(uid, trx['id'], price, trx['qr_path'], qr_msg.message_id, expires_at)
    pending_direct_buy[uid]['order_id'] = trx['id']
    pending_direct_buy[uid]['qr_path'] = trx['qr_path']
    pending_direct_buy[uid]['message_id'] = qr_msg.message_id
    
    try:
        await fast_edit(q, 
            premium_text("""[card] <b>QRIS SIAP DIBAYAR</b>

<blockquote>[catatan] Scan QRIS pada pesan di atas menggunakan aplikasi dompet digital kamu.
[panahijo] Setelah transfer selesai, tekan tombol <b>Verifikasi Bayar</b> untuk konfirmasi.
[waktu] QRIS berlaku selama <b>5 menit</b> sejak dikirim.</blockquote>"""),
            parse_mode="HTML"
        , rich_html=premium_text(f"""[card] <b>QRIS SIAP DIBAYAR</b>
<hr/>
<ul><li>[catatan] Scan QRIS pada pesan di atas menggunakan aplikasi dompet digital kamu.</li><li>[panahijo] Setelah transfer selesai, tekan tombol <b>Verifikasi Bayar</b> untuk konfirmasi.</li><li>[waktu] QRIS berlaku selama <b>5 menit</b> sejak dikirim.</li></ul>"""), log_label="AutoRich")
    except Exception:
        pass

    try:
        await notif.edit_rich_message(
            context.bot, q.message.chat_id, status_msg,
            f"{emoji('done')} <b>QRIS siap. Silakan scan untuk melanjutkan pembayaran.</b>",
            premium_text("[done] <b>QRIS siap. Silakan scan untuk melanjutkan pembayaran.</b>"),
            log_label="DirectBuyQrisDone",
        )
    except Exception:
        pass


async def verify_direct_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    parts = q.data.split("_")
    order_id = parts[2]
    session_id = int(parts[3])
    
    status_msg = None
    
    payment = get_pending_payment(order_id)
    if not payment:
        await notif.send_rich_message_to_chat(
            context.bot, q.message.chat_id,
            '<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Order Tidak Ditemukan</b>\n\n<blockquote>Order ini mungkin sudah kadaluarsa atau tidak valid. Silakan coba beli kembali dari awal.</blockquote>',
            premium_text("[warning] <b>Order tidak ditemukan!</b>"),
            reply_markup=create_back_button(), log_label="OrderNotFound",
        )
        return
    
    pid, user_id, amount, status, qr_path, msg_id, expires_at = payment
    
    if uid != user_id:
        await notif.send_rich_message_to_chat(
            context.bot, q.message.chat_id,
            '<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Akses Ditolak</b>\n\n<blockquote>Order ini tidak terdaftar atas nama kamu.</blockquote>',
            premium_text("[warning] <b>Bukan order anda!</b>"),
            reply_markup=create_back_button(), log_label="OrderNotOwned",
        )
        return
    
    if status != "pending":
        await notif.send_rich_message_to_chat(
            context.bot, q.message.chat_id,
            f'<tg-emoji emoji-id="5258500400918587241">📝</tg-emoji> Status: <code>{status}</code>',
            premium_text(f"[catatan] Status order ini: <code>{status}</code> — tidak dapat diproses ulang."),
            reply_markup=create_back_button(), log_label="OrderStatus",
        )
        return
    
    if int(time.time()) > expires_at:
        update_payment_status(order_id, "expired")
        try:
            if os.path.exists(qr_path):
                os.remove(qr_path)
        except:
            pass
        await notif.send_rich_message_to_chat(
            context.bot, q.message.chat_id,
            '<tg-emoji emoji-id="6093456762113888541">🕐</tg-emoji> <b>Payment expired!</b> Silakan coba lagi.',
            premium_text("[waktu] <b>Waktu Pembayaran Habis</b>\n\n<blockquote>[catatan] QRIS sudah melewati batas waktu 5 menit.\n[panahijo] Silakan mulai ulang proses pembelian dari awal.</blockquote>"),
            reply_markup=create_back_button(), log_label="PaymentExpired",
        )
        return
    
    status_msg = await notif.send_rich_message_to_chat(
        context.bot, q.message.chat_id,
        premium_text("[loading] <b>Memeriksa status pembayaran, harap tunggu...</b>"),
        premium_text("[loading] <b>Memeriksa status pembayaran, harap tunggu...</b>"),
        log_label="DirectBuyVerifyLoading",
    )
    
    is_paid = await check_payment_status(order_id, amount)
    
    if is_paid:
        # ── ATOMIC GUARD: cegah double-kredit jika user spam klik ────────────
        try:
            import sqlite3 as _sq3
            _ac = _sq3.connect(DB_PATH)
            _ac.execute(
                "UPDATE pending_payments SET status='paid' WHERE (id=? OR order_id=?) AND status='pending'",
                (order_id, order_id)
            )
            _changed = _ac.execute("SELECT changes()").fetchone()[0]
            _ac.commit()
            _ac.close()
        except Exception as _ae:
            print(f"[Error Atomic Guard SessionBuy]: {_ae}")
            _changed = 0

        if not _changed:
            if status_msg:
                await notif.edit_rich_message(
                    context.bot, q.message.chat_id, status_msg,
                    premium_text("[warning] <b>Pembayaran Sudah Diproses</b>\n\n<blockquote>[catatan] Order ini sudah berhasil diproses sebelumnya. Cek riwayat order kamu di menu Profil.</blockquote>"),
                    premium_text("[warning] <b>Pembayaran Sudah Diproses</b>\n\n<blockquote>[catatan] Order ini sudah berhasil diproses sebelumnya. Cek riwayat order kamu di menu Profil.</blockquote>"),
                    reply_markup=create_back_button(), log_label="DirectBuyAlreadyProcessed",
                )
            return
        # ─────────────────────────────────────────────────────────────────────

        if uid in pending_direct_buy:
            data = pending_direct_buy[uid]
            stock_id = data['stock_id']
            phone = data['phone']
            aid = data['account_id']
            sess = data['session_string']
            username = data['username']
            
            try:
                sold_session_id = mark_as_sold(stock_id, uid, sess, phone, username, aid)
            except Exception as _mas_err:
                print(f"[CRITICAL] mark_as_sold gagal di process_direct_buy: {_mas_err}")
                await notif.edit_rich_message(
                    context.bot, q.message.chat_id, status_msg,
                    premium_text(f"[warning] <b>Pembayaran Diterima — Gagal Catat Sesi</b>\n\n<blockquote>[catatan] Pembayaran sukses namun terjadi kendala teknis saat mencatat session.\n[panahijo] Hubungi admin/CS dengan menyertakan Order ID berikut:\n\n<code>{order_id}</code></blockquote>"),
                    premium_text(f"[warning] <b>Pembayaran Diterima — Gagal Catat Sesi</b>\n\n<blockquote>[catatan] Pembayaran sukses namun terjadi kendala teknis saat mencatat session.\n[panahijo] Hubungi admin/CS dengan menyertakan Order ID berikut:\n\n<code>{order_id}</code></blockquote>"),
                    reply_markup=create_back_button(), log_label="DirectBuyMarkSoldFailed",
                )
                return
            update_user_stats(uid)
            if data.get('is_nego_price'):
                nego_ai.clear_deal(DB_PATH, uid, stock_id)
            
            try:
                _u2 = get_user(uid)
                _uname2 = _u2[1] if _u2 and _u2[1] else None
                _sisa2 = _u2[3] if _u2 and len(_u2) > 3 else None
                await notif.notif_pembelian_channel(
                    bot=context.bot,
                    user_id=uid,
                    phone=phone,
                    account_id=aid,
                    amount=amount,
                    order_id=order_id,
                    username=_uname2,
                    saldo_sisa=_sisa2
                )
            except Exception as n_err:
                print(f"[Error Trigger Notif Direct]: {n_err}")
            try:
                await clone_system.process_transaction_commission(
                    context.bot, DB_PATH, uid, order_id, "Beli Session (QRIS)", amount, base_price
                )
            except Exception as _ce:
                print(f"[CloneCommission] {_ce}")
            
            try:
                if os.path.exists(qr_path):
                    os.remove(qr_path)
            except:
                pass
            
            # =====================================================================
            # FIX UTAMA: HAPUS TOTAL PESAN GAMBAR QRIS NYA, JANGAN DI-EDIT CAPTION
            # =====================================================================
            try:
                # Gunakan msg_id (ID pesan gambar QRIS yang dikirim oleh sistem saat checkout)
                await context.bot.delete_message(chat_id=uid, message_id=msg_id)
            except Exception as d_err:
                print(f"[Debug] Gagal hapus pesan gambar QRIS: {d_err}")
            # =====================================================================

            del pending_direct_buy[uid]
            
            password_2fa = globals().get('DEFAULT_2FA_PASSWORD', '#1')
            
            # FIX: Clear cached stock list so next view reloads from database
            context.user_data.pop('stock', None)
            context.user_data.pop('stock_page', None)
            
            teks_sukses_bayar = premium_text(f"""
[done] <b>PEMBAYARAN BERHASIL — AKUN SIAP DIGUNAKAN</b>

<pre>
ID Akun      : {aid}
Nomor        : {phone}
Password 2FA : {password_2fa}
Status       : CLEAN — Siap Login
</pre>

[catatan] Simpan data ini baik-baik. Gunakan menu OTP di bawah jika dibutuhkan kode verifikasi.
""")
            rich_sukses_bayar = f"""\
{emoji('done')} <b>PEMBAYARAN BERHASIL — AKUN SIAP DIGUNAKAN</b>

<table bordered striped>
<tr><th>Detail Akun</th><th>Isi</th></tr>
<tr><td>ID Akun</td><td><code>{aid}</code></td></tr>
<tr><td>Nomor</td><td><code>{phone}</code></td></tr>
<tr><td>2FA</td><td><code>{password_2fa}</code></td></tr>
<tr><td>Status</td><td><b>CLEAN — Siap Login</b></td></tr>
</table>

{emoji('catatan')} Simpan data ini baik-baik. Gunakan menu OTP di bawah jika dibutuhkan kode verifikasi."""
            await notif.edit_rich_message(
                context.bot, q.message.chat_id, status_msg,
                rich_sukses_bayar, teks_sukses_bayar,
                reply_markup=create_order_success_keyboard(sold_session_id, phone), log_label="DirectBuySukses",
            )
            
            owner = globals().get('OWNER_ID', uid) 
            formatted_amount = f"Rp {amount:,}"
            
            try:
                owner_html = f"""\
<tg-emoji emoji-id="5373261557700509032">📱</tg-emoji> <b>DIRECT PURCHASE</b>

<table bordered striped>
<tr><th>Informasi Transaksi</th><th>Detail</th></tr>
<tr><td>User</td><td><code>{uid}</code></td></tr>
<tr><td>Nomor</td><td><code>{phone}</code></td></tr>
<tr><td>ID</td><td><code>{aid}</code></td></tr>
<tr><td>Nominal</td><td><b>{formatted_amount}</b></td></tr>
<tr><td>Order</td><td><code>{order_id}</code></td></tr>
</table>"""
                owner_fallback = premium_text(f"""[product] <b>DIRECT PURCHASE</b>

<blockquote>[card] <b>User:</b> <code>{uid}</code>
[WhatsApp] <b>Nomor:</b> <code>{phone}</code>
[Telegram] <b>ID:</b> <code>{aid}</code>
[dolar] <b>Nominal:</b> <b>{formatted_amount}</b>
[verified] <b>Order:</b> <code>{order_id}</code></blockquote>""")
                await notif.send_rich_message_to_chat(
                    context.bot, owner, owner_html, owner_fallback,
                    log_label="DirectPurchaseOwner",
                )
            except:
                pass
        else:
            await notif.send_rich_message_to_chat(
                context.bot, q.message.chat_id,
                '<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Data Session Tidak Ditemukan</b>\n\n<blockquote>Data sesi tidak tersedia. Silakan kembali ke menu utama dan ulangi proses.</blockquote>',
                premium_text("[warning] <b>Session data not found!</b>"),
                reply_markup=create_main_menu(uid), log_label="SessionDataNotFound",
            )
    else:
        await notif.edit_rich_message(
            context.bot, q.message.chat_id, status_msg,
            premium_text("""[warning] <b>PEMBAYARAN BELUM TERDETEKSI</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Sistem belum menerima konfirmasi pembayaran kamu.

[panahijo] Pastikan QRIS sudah di-scan dan transfer berhasil.
[panahijo] Tunggu 10–30 detik, lalu tekan <b>Verifikasi Bayar</b> kembali.
[warning] Jika masih gagal, hubungi CS dengan menyertakan Order ID.</blockquote>"""),
            premium_text("""[warning] <b>PEMBAYARAN BELUM TERDETEKSI</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Sistem belum menerima konfirmasi pembayaran kamu.

[panahijo] Pastikan QRIS sudah di-scan dan transfer berhasil.
[panahijo] Tunggu 10–30 detik, lalu tekan <b>Verifikasi Bayar</b> kembali.
[warning] Jika masih gagal, hubungi CS dengan menyertakan Order ID.</blockquote>"""),
            reply_markup=create_payment_keyboard(order_id, session_id), log_label="DirectBuyNotDetected",
        )

async def cancel_direct_buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id

    if uid in pending_direct_buy:
        data = pending_direct_buy[uid]
        if 'qr_path' in data:
            try:
                if os.path.exists(data['qr_path']):
                    os.remove(data['qr_path'])
            except Exception:
                pass
        if 'order_id' in data:
            delete_pending_payment(data['order_id'])
        del pending_direct_buy[uid]

    # Hapus pesan QRIS (foto) yang sedang tampil
    if q.message:
        await safe_delete_message(context.bot, q.message.chat_id, q.message.message_id)

    # Kirim menu utama DENGAN FOTO (sudah include thumbnail)
    await send_main_menu_new(context, uid)

async def handle_session_bukti_tf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Tangkap foto bukti TF beli session (mode manual) → kirim ke owner untuk approve."""
    if context.user_data.get("current_menu_state") != "session_wait_bukti":
        return False
    if not update.message or not update.message.photo:
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            '<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> Kirim <b>foto/screenshot</b> bukti transfer ya, bukan teks.',
            premium_text("[warning] <b>Format Tidak Sesuai</b>\n\n<blockquote>[catatan] Harap kirim <b>foto/screenshot</b> bukti transfer, bukan pesan teks.\n[panahijo] Ambil screenshot dari aplikasi banking/dompet kamu lalu kirim ke sini.</blockquote>"),
            log_label="BuktiTFSessionWrongType",
        )
        return True

    uid = update.effective_user.id
    uname = update.effective_user.username or str(uid)
    data = pending_direct_buy.get(uid)
    if not data:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] <b>Sesi Pembelian Kadaluarsa</b>
<hr/>
<ul><li>[catatan] Waktu sesi pembelian kamu habis.</li><li>[panahijo] Silakan ulangi dari menu <b>Buy Noktel</b>.</li></ul>"""), premium_text("[warning] <b>Sesi Pembelian Kadaluarsa</b>\n\n<blockquote>[catatan] Waktu sesi pembelian kamu habis.\n[panahijo] Silakan ulangi dari menu <b>Buy Noktel</b>.</blockquote>"), log_label="AutoRich2")
        context.user_data["current_menu_state"] = "main_menu"
        return True

    # Simpan data pembelian ke dict global agar bisa diakses dari callback owner
    session_manual_pending[uid] = dict(data)
    context.user_data["current_menu_state"] = "main_menu"

    phone = data.get("phone", "")
    aid   = data.get("account_id", "")
    price = data.get("price", 0)

    try:
        target_owner = OWNER_ID[0] if isinstance(OWNER_ID, list) else OWNER_ID
        kb = styled_inline_keyboard([
            [
                styled_button("✅ Approve", callback_data=f"session_approve_manual_{uid}", style="success", emoji_name="verified"),
                styled_button("❌ Tolak",   callback_data=f"session_tolak_manual_{uid}",   style="danger",  emoji_name="batal"),
            ]
        ])
        caption = premium_text(f"""[card] <b>PENGAJUAN BELI SESSION MANUAL</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[card] <b>User:</b> @{uname} (<code>{uid}</code>)
[WhatsApp] <b>Nomor:</b> <code>{phone}</code>
[Telegram] <b>ID Akun:</b> <code>{aid}</code>
[dolar] <b>Nominal:</b> <b>Rp {price:,}</b></blockquote>""")
        await send_photo_to_owner(context, target_owner, update.message.photo[-1].file_id, caption, kb)
    except Exception as e:
        print(f"[Session Bukti TF Owner] {e}")

    await notif.send_rich_message_to_chat(
        context.bot, update.effective_chat.id,
        '<tg-emoji emoji-id="6298432564487001027">✅</tg-emoji> <b>BUKTI TRANSFER TERKIRIM</b>\n\n'
        '<tg-emoji emoji-id="5258500400918587241">📝</tg-emoji> Bukti transfer kamu sudah dikirim ke Owner.\n'
        '<tg-emoji emoji-id="6093456762113888541">🕐</tg-emoji> Data akun akan dikirim setelah disetujui.',
        premium_text("""[done] <b>BUKTI TRANSFER TERKIRIM</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Bukti transfer kamu sudah dikirim ke Owner.
[waktu] Data akun akan dikirim setelah disetujui.</blockquote>"""),
        log_label="BuktiTerkirimSession",
    )
    return True


async def session_approve_manual_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner approve beli session manual → kirim data akun (session) ke buyer."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q, "⏳ Memproses order...")

    parts = q.data.split("_")
    target_uid = int(parts[-1])

    data = session_manual_pending.pop(target_uid, None)
    if not data:
        await safe_answer(q, "Order ini sudah diproses/kadaluarsa.", show_alert=True)
        return

    stock_id = data.get("stock_id")
    phone    = data.get("phone", "")
    aid      = data.get("account_id", "")
    sess     = data.get("session_string")
    username = data.get("username")
    price    = data.get("price", 0)
    # Bot TEMPAT pembeli order (pusat/clone) — dipakai untuk semua notifikasi
    # ke pembeli di bawah ini. context.bot di handler ini SELALU bot pusat
    # (karena Owner approve lewat bot pusat), jadi tidak boleh dipakai langsung
    # untuk kirim balik ke pembeli.
    buyer_bot = get_origin_bot(data.get("origin_bot_token"), fallback_bot=context.bot)

    # Pastikan stok belum sold oleh proses lain (mis. double approve)
    stock_check = get_stock_detail(stock_id) if stock_id else None
    if not stock_check:
        try:
            await notif.send_rich_message_to_chat(
                buyer_bot, target_uid,
                '<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Maaf, stok sudah tidak tersedia.</b> Saldo/pembayaran akan ditinjau ulang oleh Owner.',
                premium_text("[warning] <b>Stok Habis Saat Proses</b>\n\n<blockquote>[catatan] Maaf, akun yang kamu pesan baru saja terjual ke pembeli lain.\n[panahijo] Pembayaran kamu akan ditinjau ulang oleh Owner.\n[chat] Hubungi CS jika ada kendala.</blockquote>"),
                log_label="StockGoneOnApprove",
            )
        except Exception:
            pass
        try:
            await q.message.edit_caption(
                caption=premium_text(f"[warning] <b>STOK HABIS SAAT APPROVE</b>\n<blockquote>User: <code>{target_uid}</code></blockquote>"),
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    try:
        sold_session_id = mark_as_sold(stock_id, target_uid, sess, phone, username, aid)
    except Exception as _mas_err:
        print(f"[CRITICAL] mark_as_sold gagal di session_approve_manual_handler: {_mas_err}")
        try:
            await notif.send_rich_message_to_chat(
                buyer_bot, target_uid,
                '<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Terjadi kendala saat mencatat sesi. Hubungi Owner.</b>',
                premium_text("[warning] <b>Kendala Teknis</b>\n\n<blockquote>[catatan] Terjadi kendala saat mencatat sesi ke database.\n[panahijo] Hubungi Owner atau CS dengan menyertakan detail transaksi kamu.</blockquote>"),
                log_label="MarkSoldFailed",
            )
        except Exception:
            pass
        await safe_answer(q, "Gagal mencatat sold_sessions, cek log!", show_alert=True)
        return
    update_user_stats(target_uid)
    pending_direct_buy.pop(target_uid, None)
    if data.get('is_nego_price'):
        nego_ai.clear_deal(DB_PATH, target_uid, stock_id)

    try:
        _u2 = get_user(target_uid)
        _uname2 = _u2[1] if _u2 and _u2[1] else None
        _sisa2 = _u2[3] if _u2 and len(_u2) > 3 else None
        await notif.notif_pembelian_channel(
            bot=context.bot,
            user_id=target_uid,
            phone=phone,
            account_id=aid,
            amount=price,
            order_id=f"MANUAL-{int(time.time())}",
            username=_uname2,
            saldo_sisa=_sisa2
        )
    except Exception as n_err:
        print(f"[Error Trigger Notif Manual Session]: {n_err}")
    try:
        # PENTING: pakai buyer_bot (bot ASAL transaksi), bukan context.bot.
        # process_transaction_commission() mendeteksi clone dari token bot yang
        # dikirim ke sini — kalau dikirim context.bot (selalu bot pusat saat
        # Owner approve), transaksi manual dari clone TIDAK PERNAH kehitung
        # komisinya karena selalu dianggap transaksi bot pusat.
        await clone_system.process_transaction_commission(
            buyer_bot, DB_PATH, target_uid, f"MANUAL-{int(time.time())}", "Beli Session (Manual)", price
        )
    except Exception as _ce:
        print(f"[CloneCommission] {_ce}")

    password_2fa = globals().get('DEFAULT_2FA_PASSWORD', '#1')
    rich_html_success = f"""\
<tg-emoji emoji-id="6298432564487001027">✅</tg-emoji> <b>ORDER BERHASIL</b>

<table bordered striped>
<tr><th>Detail Akun</th><th>Isi</th></tr>
<tr><td>ID Akun</td><td><code>{aid}</code></td></tr>
<tr><td>Nomor</td><td><code>{phone}</code></td></tr>
<tr><td>2FA</td><td><code>{password_2fa}</code></td></tr>
<tr><td>Status</td><td><b>CLEAN</b></td></tr>
</table>"""
    teks_sukses_bayar = premium_text(f"""
[done] <b>ORDER DISETUJUI — AKUN SIAP DIGUNAKAN</b>

<pre>
ID Akun      : {aid}
Nomor        : {phone}
Password 2FA : {password_2fa}
Status       : CLEAN — Siap Login
</pre>

[catatan] Simpan data ini baik-baik. Gunakan menu OTP jika dibutuhkan kode verifikasi.
""")
    try:
        await notif.send_rich_message_to_chat(
            buyer_bot, target_uid, rich_html_success, teks_sukses_bayar,
            reply_markup=create_order_success_keyboard(sold_session_id, phone),
            log_label="OrderSuksesManualSession",
        )
    except Exception as e:
        print(f"[Error Kirim Order Sukses Manual Session]: {e}")

    try:
        await q.message.edit_caption(
            caption=premium_text(f"[done] <b>SESSION DISETUJUI ✓</b>\n<blockquote>User: <code>{target_uid}</code>\n[Telegram] ID: <code>{aid}</code></blockquote>"),
            parse_mode="HTML"
        )
    except Exception:
        pass


async def session_tolak_manual_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner tolak beli session manual."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q)

    parts = q.data.split("_")
    target_uid = int(parts[-1])

    data = session_manual_pending.pop(target_uid, None)
    pending_direct_buy.pop(target_uid, None)
    phone = data.get("phone", "") if data else ""
    price = data.get("price", 0) if data else 0
    # Bot ASAL order pembeli (pusat/clone) — notif tolak harus lewat bot ini,
    # bukan context.bot (yang selalu bot pusat karena Owner tolak dari pusat).
    buyer_bot = get_origin_bot(data.get("origin_bot_token") if data else None, fallback_bot=context.bot)

    rich_tolak = f"""\
<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>PEMBELIAN MANUAL DITOLAK</b>

<table bordered striped>
<tr><th>Info</th><th>Detail</th></tr>
<tr><td>Nomor</td><td><code>{phone}</code></td></tr>
<tr><td>Nominal</td><td>Rp {price:,}</td></tr>
</table>

<tg-emoji emoji-id="5258500400918587241">📝</tg-emoji> Bukti transfer kamu tidak valid atau tidak sesuai nominal yang diminta. Pastikan nominal transfer <b>tepat sama</b> dengan harga yang tertera. Hubungi CS jika ada pertanyaan lebih lanjut."""
    fallback_tolak = premium_text(f"""[warning] <b>PEMBELIAN MANUAL DITOLAK</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[WhatsApp] <b>Nomor:</b> <code>{phone}</code>
[dolar] <b>Nominal:</b> Rp {price:,}

[catatan] Bukti transfer kamu tidak valid atau tidak sesuai nominal yang diminta.
[panahijo] Pastikan nominal transfer <b>tepat sama</b> dengan harga yang tertera.
[chat] Hubungi CS jika ada pertanyaan lebih lanjut.</blockquote>""")
    try:
        await notif.send_rich_message_to_chat(
            buyer_bot, target_uid, rich_tolak, fallback_tolak,
            log_label="SessionManualDitolak",
        )
    except Exception as e:
        print(f"[Notif Tolak Session Manual] {e}")

    try:
        await q.message.edit_caption(
            caption=premium_text(f"[batal] <b>SESSION DITOLAK ✗</b>\n<blockquote>User: <code>{target_uid}</code></blockquote>"),
            parse_mode="HTML"
        )
    except Exception:
        pass


# ==================== DEPOSIT HANDLERS ====================
async def ask_deposit_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    context.user_data["current_menu_state"] = "deposit_ask_manual"
    text = premium_text("""
[duitkarung] <b>ISI NOMINAL DEPOSIT</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Ketik nominal deposit yang kamu inginkan (angka saja, tanpa titik atau koma).

[panahijo] Minimal: <b>Rp 1.000</b>
[panahijo] Maksimal: <b>Rp 5.000.000</b>

Contoh penulisan: <code>75000</code></blockquote>
""")
    kb = styled_inline_keyboard([[styled_button("Batal", callback_data="menu_deposit", style="danger", emoji_name="back")]])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""[duitkarung] <b>ISI NOMINAL DEPOSIT</b>
<hr/>
<p>[catatan] Ketik nominal deposit yang kamu inginkan (angka saja, tanpa titik atau koma).</p>
<ul><li>[panahijo] Minimal: <b>Rp 1.000</b></li><li>[panahijo] Maksimal: <b>Rp 5.000.000</b></li></ul>
<p>Contoh penulisan: <code>75000</code></p>"""), log_label="AutoRich")


async def _deposit_manual_start(update, context, amount: int):
    """Alur deposit manual: minta user kirim bukti transfer. Tampilkan QRIS foto jika tersedia."""
    q = update.callback_query
    uid = q.from_user.id
    context.user_data["_dm_amount"] = amount
    context.user_data["current_menu_state"] = "deposit_wait_bukti"

    # Ambil semua info rekening (non-QRIS) yang sudah diset
    pay_lines = []
    for label, key, _ in PAYMENT_METHODS_LIST:
        if key == "qris":
            continue  # QRIS ditampilkan sebagai foto, bukan teks
        info = get_payment_info(key)
        if info:
            pay_lines.append(f"[panahijo] <b>{label}:</b> <code>{info}</code>")
    rekening_text = "\n".join(pay_lines) if pay_lines else ""

    qris_file_id = get_payment_info("qris")

    text = premium_text(f"""[duitkarung] <b>INSTRUKSI DEPOSIT MANUAL</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[dolar] <b>Nominal Transfer:</b> <b>Rp {amount:,}</b>

{"" if not rekening_text else rekening_text + chr(10)}{"[card] Transfer via QRIS di bawah ini atau rekening di atas." if qris_file_id and rekening_text else ("[card] Scan QRIS di bawah ini untuk transfer." if qris_file_id else ("[warning] Info rekening belum diset oleh owner." if not rekening_text else ""))}

[warning] Transfer TEPAT nominal <b>Rp {amount:,}</b> — jangan dibulatkan.
[catatan] Setelah transfer, kirim <b>foto/screenshot bukti transfer</b> ke chat bot ini.
[shield] Saldo akan masuk otomatis setelah Owner menyetujui bukti kamu.</blockquote>""")
    kb = styled_inline_keyboard([
        [styled_button("Batal", callback_data="cancel_deposit", style="danger", emoji_name="back")]
    ])

    # Jika ada foto QRIS, kirim sebagai foto dengan caption
    if qris_file_id:
        try:
            # Hapus pesan menu lama dulu
            try:
                await q.message.delete()
            except Exception:
                pass
            await context.bot.send_photo(
                chat_id=uid,
                photo=qris_file_id,
                caption=text,
                parse_mode="HTML",
                reply_markup=kb
            )
            return
        except Exception as e:
            print(f"[deposit_manual_start QRIS foto] {e}")
            # Fallback ke teks biasa

    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""[duitkarung] <b>INSTRUKSI DEPOSIT MANUAL</b>
<hr/>
<p>[dolar] <b>Nominal Transfer:</b> <b>Rp {amount:,}</b></p>
<p>{"" if not rekening_text else rekening_text + chr(10)}{"[card] Transfer via QRIS di bawah ini atau rekening di atas." if qris_file_id and rekening_text else ("[card] Scan QRIS di bawah ini untuk transfer." if qris_file_id else ("[warning] Info rekening belum diset oleh owner." if not rekening_text else ""))}</p>
<ul><li>[warning] Transfer TEPAT nominal <b>Rp {amount:,}</b> — jangan dibulatkan.</li><li>[catatan] Setelah transfer, kirim <b>foto/screenshot bukti transfer</b> ke chat bot ini.</li><li>[shield] Saldo akan masuk otomatis setelah Owner menyetujui bukti kamu.</li></ul>"""), log_label="AutoRich")


async def handle_deposit_bukti_tf(update, context) -> bool:
    """Tangkap foto bukti TF dari user (mode manual)."""
    if context.user_data.get("current_menu_state") != "deposit_wait_bukti":
        return False
    if not update.message or not update.message.photo:
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            '<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> Kirim <b>foto/screenshot</b> bukti transfer ya, bukan teks.',
            premium_text("[warning] <b>Format Tidak Sesuai</b>\n\n<blockquote>[catatan] Harap kirim <b>foto/screenshot</b> bukti transfer, bukan pesan teks.\n[panahijo] Ambil screenshot dari aplikasi banking/dompet kamu lalu kirim ke sini.</blockquote>"),
            log_label="BuktiTFDepositWrongType",
        )
        return True

    uid    = update.effective_user.id
    amount = context.user_data.get("_dm_amount", 0)
    uname  = update.effective_user.username or str(uid)
    context.user_data["current_menu_state"] = "main_menu"
    # Simpan bot ASAL user kirim bukti (pusat/clone), supaya notif approve/tolak
    # nanti bisa dikirim balik lewat bot yang sama. Lihat get_origin_bot().
    deposit_manual_origin[uid] = getattr(context.bot, "token", None)

    # Kirim foto ke owner untuk di-approve
    try:
        target_owner = OWNER_ID[0] if isinstance(OWNER_ID, list) else OWNER_ID
        kb = styled_inline_keyboard([
            [
                styled_button("✅ Approve",  callback_data=f"owner_approve_dm_{uid}_{amount}_{update.message.message_id}", style="success", emoji_name="verified"),
                styled_button("❌ Tolak",    callback_data=f"owner_tolak_dm_{uid}_{amount}_{update.message.message_id}",   style="danger",  emoji_name="batal"),
            ]
        ])
        caption = premium_text(f"""[duitkarung] <b>PENGAJUAN DEPOSIT MANUAL</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[card] <b>User:</b> @{uname} (<code>{uid}</code>)
[dolar] <b>Nominal Deposit:</b> <b>Rp {amount:,}</b>
[catatan] Cek bukti transfer di atas, lalu approve atau tolak.</blockquote>""")
        await send_photo_to_owner(context, target_owner, update.message.photo[-1].file_id, caption, kb)
    except Exception as e:
        print(f"[Bukti TF Owner] {e}")

    await notif.send_rich_message_to_chat(
        context.bot, update.effective_chat.id,
        f'<tg-emoji emoji-id="6298432564487001027">✅</tg-emoji> <b>BUKTI TRANSFER TERKIRIM</b>\n\n'
        f'<table bordered striped>\n<tr><th>Info</th><th>Detail</th></tr>\n'
        f'<tr><td>Status</td><td>Bukti transfer sudah dikirim ke Owner</td></tr>\n'
        f'<tr><td>Nominal</td><td><b>Rp {amount:,}</b></td></tr>\n</table>\n\n'
        f'<tg-emoji emoji-id="6093456762113888541">🕐</tg-emoji> Saldo akan masuk setelah disetujui.',
        premium_text(f"""[done] <b>BUKTI TRANSFER TERKIRIM</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Bukti transfer kamu sudah dikirim ke Owner.
[waktu] Saldo <b>Rp {amount:,}</b> akan masuk setelah disetujui.</blockquote>"""),
        log_label="BuktiTerkirimDeposit",
    )
    return True


# ==================== PHOTO MESSAGE HANDLER ====================
async def handle_photo_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler untuk semua pesan foto (bukti transfer user, upload QRIS foto owner)."""
    # === GUARD: TOLAK GRUP & BLOCKED USER ===
    if not await is_private_chat(update):
        return
    if update.effective_user and is_blocked(update.effective_user.id):
        return
    # ===========================================
    uid = update.effective_user.id
    state = context.user_data.get("current_menu_state", "")

    # 1. User kirim foto bukti TF deposit manual
    if state == "deposit_wait_bukti":
        await handle_deposit_bukti_tf(update, context)
        return

    # 1b. User kirim foto bukti TF gift manual
    if state == "gift_wait_bukti":
        await handle_gift_bukti_tf(update, context)
        return

    # 1c. User kirim foto bukti TF beli session (Buy Noktel) manual
    if state == "session_wait_bukti":
        await handle_session_bukti_tf(update, context)
        return

    # 1d. User kirim foto bukti TF topup Stars manual
    if state == "stars_wait_bukti":
        await handle_stars_bukti_tf(update, context)
        return

    # 1e. User kirim foto bukti TF topup TON manual
    if state == "ton_wait_bukti":
        await handle_ton_bukti_tf(update, context)
        return

    # 2. Owner upload foto QRIS
    if state == "owner_wait_qris_photo" and is_owner(uid):
        if not update.message or not update.message.photo:
            await notif.send_rich_message_to_chat(
                context.bot, update.effective_chat.id,
                '<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> Kirim <b>foto</b> gambar QRIS ya, bukan teks.',
                premium_text("[warning] <b>Format Tidak Sesuai</b>\n\n<blockquote>[catatan] Harap kirim <b>gambar/foto</b> QRIS, bukan pesan teks.\n[panahijo] Upload foto QRIS kamu ke sini.</blockquote>"),
                log_label="OwnerQRISWrongType",
            )
            return
        file_id = update.message.photo[-1].file_id
        set_payment_info("qris", file_id)
        context.user_data["current_menu_state"] = "main_menu"
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            '<tg-emoji emoji-id="6298432564487001027">✅</tg-emoji> <b>Foto QRIS berhasil disimpan!</b>\n\n'
            '<tg-emoji emoji-id="6028206863038811654">🧾</tg-emoji> Foto ini akan ditampilkan ke user saat deposit manual.',
            premium_text("[done] <b>Foto QRIS berhasil disimpan!</b>\n<blockquote>[card] Foto ini akan ditampilkan ke user saat deposit manual.</blockquote>"),
            reply_markup=create_owner_menu(context),
            log_label="OwnerQRISSaved",
        )
        return

    # 3. Fallback: abaikan foto lain yang tidak relevan
# ================================================================

async def handle_deposit_manual_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Tangkap nominal manual dari chat."""
    if context.user_data.get("current_menu_state") != "deposit_ask_manual":
        return False

    raw = re.sub(r"\D", "", update.message.text.strip())
    if not raw:
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            '<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> Masukkan angka yang valid. Contoh: <code>75000</code>',
            premium_text("[warning] Masukkan angka yang valid. Contoh: <code>75000</code>"),
            log_label="DepositInvalidAmount",
        )
        return True

    amount = int(raw)
    if amount < 1000 or amount > 5000000:
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            '<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> Nominal harus antara <b>Rp 1.000</b> sampai <b>Rp 5.000.000</b>.',
            premium_text("[warning] Nominal harus antara <b>Rp 1.000</b> sampai <b>Rp 5.000.000</b>."),
            log_label="DepositAmountOutOfRange",
        )
        return True

    context.user_data["current_menu_state"] = "main_menu"
    context.user_data["_deposit_manual_amount"] = amount

    # ── Cek metode payment ─────────────────────────────────────
    if get_payment_method() == "manual":
        uid = update.effective_user.id
        context.user_data["_dm_amount"] = amount
        context.user_data["current_menu_state"] = "deposit_wait_bukti"
        pay_lines = []
        for label, key, _ in PAYMENT_METHODS_LIST:
            if key == "qris":
                continue
            info = get_payment_info(key)
            if info:
                pay_lines.append(f"[panahijo] <b>{label}:</b> <code>{info}</code>")
        rekening_text = "\n".join(pay_lines) if pay_lines else ""
        qris_file_id = get_payment_info("qris")

        text_manual = premium_text(f"""[duitkarung] <b>INSTRUKSI DEPOSIT MANUAL</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[dolar] <b>Nominal Transfer:</b> <b>Rp {amount:,}</b>

{"" if not rekening_text else rekening_text + chr(10)}{"[card] Scan QRIS di bawah ini atau transfer ke rekening di atas." if qris_file_id and rekening_text else ("[card] Scan QRIS di bawah ini untuk transfer." if qris_file_id else ("[warning] Info rekening belum diset oleh owner." if not rekening_text else ""))}

[warning] Transfer TEPAT nominal <b>Rp {amount:,}</b> — jangan dibulatkan.
[catatan] Setelah transfer, kirim <b>foto/screenshot bukti transfer</b> ke chat bot ini.
[shield] Saldo akan masuk otomatis setelah Owner menyetujui bukti kamu.</blockquote>""")

        kb_manual = styled_inline_keyboard([[styled_button("Batal", callback_data="cancel_deposit", style="danger", emoji_name="back")]])

        if qris_file_id:
            try:
                await context.bot.send_photo(
                    chat_id=uid,
                    photo=qris_file_id,
                    caption=text_manual,
                    parse_mode="HTML",
                    reply_markup=kb_manual
                )
                return True
            except Exception as e:
                print(f"[deposit_manual QRIS foto] {e}")
        await update.message.reply_text(text_manual, parse_mode="HTML", reply_markup=kb_manual)
        return True
    # ──────────────────────────────────────────────────────────

    status_msg = await notif.send_rich_message_to_chat(
        context.bot, update.effective_chat.id,
        premium_text("[loading] <b>Membuat QRIS pembayaran, harap tunggu sebentar...</b>"),
        premium_text("[loading] <b>Membuat QRIS pembayaran, harap tunggu sebentar...</b>"),
        log_label="DepositManualLoading",
    )

    uid = update.effective_user.id
    trx = await create_qris(amount)
    if not trx:
        _qris_fail_msg = premium_text("[warning] <b>Gagal membuat QRIS.</b>\n\n<blockquote>[catatan] Sistem tidak menerima data QR dari payment gateway. Silakan coba lagi beberapa saat.</blockquote>")
        if status_msg is not None:
            await notif.edit_rich_message(
                context.bot, update.effective_chat.id, status_msg,
                _qris_fail_msg, _qris_fail_msg,
                reply_markup=create_back_button(), log_label="DepositManualQrisFailed",
            )
        else:
            await notif.send_rich_message_to_chat(
                context.bot, update.effective_chat.id,
                _qris_fail_msg, _qris_fail_msg,
                reply_markup=create_back_button(), log_label="DepositManualQrisFailed",
            )
        return True

    expires_at = int(time.time()) + 300
    try:
        with open(trx["qr_path"], "rb") as qr_file:
            photo_bytes = qr_file.read()
        qr_msg = await safe_send_photo(
            context, uid, photo=photo_bytes,
            caption=premium_text(f"""
[duitkarung] <b>DEPOSIT QRIS</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[dolar] <b>Nominal:</b> <b>Rp {amount:,}</b>
[verified] <b>Order:</b> <code>{trx['id']}</code>
[waktu] <b>Batas Waktu:</b> 5 menit

[catatan] Scan QRIS ini untuk menyelesaikan pembayaran, lalu tekan <b>Verifikasi Bayar</b>.</blockquote>
"""),
            reply_markup=create_deposit_payment_keyboard(trx["id"], amount),
        )
    except Exception as e:
        print(f"[Eror QRIS Manual] {e}")
        await notif.edit_rich_message(
            context.bot, update.effective_chat.id, status_msg,
            premium_text("[warning] <b>Gagal mengirim QRIS karena koneksi server sedang padat.</b>"),
            premium_text("[warning] <b>Gagal mengirim QRIS karena koneksi server sedang padat.</b>"),
            reply_markup=create_back_button(), log_label="DepositManualQrisSendFailed",
        )
        return True

    add_pending_payment(uid, trx["id"], amount, trx["qr_path"], qr_msg.message_id, expires_at)

    try:
        await notif.edit_rich_message(
            context.bot, update.effective_chat.id, status_msg,
            premium_text("[done] <b>QRIS berhasil dibuat, silakan lanjutkan pembayaran di atas.</b>"),
            premium_text("[done] <b>QRIS berhasil dibuat, silakan lanjutkan pembayaran di atas.</b>"),
            log_label="DepositManualQrisSent",
        )
    except Exception:
        pass
    return True
    
    
async def deposit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    push_nav(context, "deposit_menu")
    
    text = """
[duitkarung] <b>DEPOSIT SALDO</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[card] Pilih nominal deposit yang tersedia di bawah ini.
[verified] Pembayaran menggunakan QRIS dan saldo akan masuk otomatis setelah transaksi terverifikasi.</blockquote>
"""
    rich_html = f"""\
{emoji('duitkarung')} <b>DEPOSIT SALDO</b>
<hr/>
<ul>
<li>{emoji('card')} Pilih nominal deposit yang tersedia di bawah ini.</li>
<li>{emoji('verified')} Pembayaran menggunakan QRIS dan saldo akan masuk otomatis setelah transaksi terverifikasi.</li>
</ul>"""
    await fast_edit(q, premium_text(text), reply_markup=create_deposit_keyboard(), parse_mode="HTML", rich_html=rich_html, log_label="DepositMenu")

async def process_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    amount = int(q.data.split("_")[1])

    # ── Cek metode payment aktif ──────────────────────────────────
    if get_payment_method() == "manual":
        await _deposit_manual_start(update, context, amount)
        return
    # ─────────────────────────────────────────────────────────────

    status_msg = await notif.send_rich_message_to_chat(
        context.bot, q.message.chat_id,
        premium_text("[loading] <b>Membuat QRIS pembayaran, harap tunggu sebentar...</b>"),
        premium_text("[loading] <b>Membuat QRIS pembayaran, harap tunggu sebentar...</b>"),
        log_label="DepositQrisLoading",
    )

    trx = await create_qris(amount)
    if not trx:
        _qris_fail_msg = premium_text("[warning] <b>Gagal membuat QRIS.</b>\n\n<blockquote>[catatan] Sistem tidak menerima data QR dari payment gateway. Silakan coba lagi beberapa saat.</blockquote>")
        if status_msg is not None:
            await notif.edit_rich_message(
                context.bot, q.message.chat_id, status_msg,
                _qris_fail_msg, _qris_fail_msg,
                reply_markup=create_back_button(), log_label="DepositQrisFailed",
            )
        else:
            await notif.send_rich_message_to_chat(
                context.bot, q.message.chat_id,
                _qris_fail_msg, _qris_fail_msg,
                reply_markup=create_back_button(), log_label="DepositQrisFailed",
            )
        return

    expires_at = int(time.time()) + 300

    # FIX: Tambahkan try-except dan retry agar tidak Timed Out & memicu expired palsu
    try:
        with open(trx["qr_path"], "rb") as qr_file:
            photo_bytes = qr_file.read()
        qr_msg = await safe_send_photo(
            context, uid, photo=photo_bytes,
            caption=premium_text(f"""
[duitkarung] <b>DEPOSIT QRIS</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[dolar] <b>Nominal:</b> <b>Rp {amount:,}</b>
[verified] <b>Order:</b> <code>{trx['id']}</code>
[waktu] <b>Batas Waktu:</b> 5 menit

[catatan] Scan QRIS ini untuk menyelesaikan pembayaran, lalu tekan <b>Verifikasi Bayar</b>.</blockquote>
"""),
            reply_markup=create_deposit_payment_keyboard(trx["id"], amount),
        )
    except Exception as e:
        print(f"[Eror QRIS] Gagal kirim foto QRIS karena koneksi lambat: {e}")
        await notif.edit_rich_message(
            context.bot, q.message.chat_id, status_msg,
            premium_text("[warning] <b>Gagal mengirim QRIS karena koneksi server sedang padat.</b>\n\n<blockquote>[catatan] Silakan klik ulang tombol Deposit beberapa saat lagi.</blockquote>"),
            premium_text("[warning] <b>Gagal mengirim QRIS karena koneksi server sedang padat.</b>\n\n<blockquote>[catatan] Silakan klik ulang tombol Deposit beberapa saat lagi.</blockquote>"),
            reply_markup=create_back_button(), log_label="DepositQrisSendFailed",
        )
        return

    add_pending_payment(uid, trx["id"], amount, trx["qr_path"], qr_msg.message_id, expires_at)

    # Hapus pesan menu/thumbnail lama agar tidak terlihat seperti gambar QRIS.
    await safe_delete_message(context.bot, q.message.chat_id, q.message.message_id)

    try:
        await notif.edit_rich_message(
            context.bot, q.message.chat_id, status_msg,
            premium_text("[done] <b>QRIS siap. Silakan scan untuk melanjutkan pembayaran.</b>"),
            premium_text("[done] <b>QRIS siap. Silakan scan untuk melanjutkan pembayaran.</b>"),
            log_label="DepositQrisReady",
        )
    except Exception:
        pass


async def verify_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    import time
    import os
    
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    parts = q.data.split("_")
    order_id = parts[2]
    amount = int(parts[3])
    
    payment = get_pending_payment(order_id)
    if not payment:
        await notif.send_rich_message_to_chat(context.bot, q.message.chat_id, premium_text(f"""[warning] <b>Order tidak ditemukan!</b>"""), premium_text("[warning] <b>Order tidak ditemukan!</b>"), reply_markup=create_back_button(), log_label="AutoRich2")
        return
    
    pid, user_id, amt, status, qr_path, msg_id, expires_at = payment
    
    if uid != user_id:
        await notif.send_rich_message_to_chat(context.bot, q.message.chat_id, premium_text(f"""[warning] <b>Bukan order anda!</b>"""), premium_text("[warning] <b>Bukan order anda!</b>"), reply_markup=create_back_button(), log_label="AutoRich2")
        return
    
    if int(time.time()) > expires_at:
        update_payment_status(order_id, "expired")
        try:
            if os.path.exists(qr_path):
                os.remove(qr_path)
        except:
            pass
        await notif.send_rich_message_to_chat(context.bot, q.message.chat_id, premium_text(f"""[waktu] <b>Pembayaran expired!</b>"""), premium_text("[waktu] <b>Pembayaran expired!</b>"), reply_markup=create_back_button(), log_label="AutoRich2")
        return
    
    status_msg = await notif.send_rich_message_to_chat(
        context.bot, q.message.chat_id,
        premium_text("[loading] <b>Memeriksa status pembayaran, harap tunggu...</b>"),
        premium_text("[loading] <b>Memeriksa status pembayaran, harap tunggu...</b>"),
        log_label="DepositVerifyLoading",
    )
    
    is_paid = await check_payment_status(order_id, amount)
    
    if is_paid:
        # ── ATOMIC GUARD: satu-satunya titik yang boleh kredit saldo ──────────
        # UPDATE hanya berhasil jika status masih 'pending'. Jika user spam klik
        # atau ada race condition 2 request bersamaan, hanya satu yang lolos.
        try:
            import sqlite3 as _sq3
            _ac = _sq3.connect(DB_PATH)
            _ac.execute(
                "UPDATE pending_payments SET status='paid' WHERE (id=? OR order_id=?) AND status='pending'",
                (order_id, order_id)
            )
            _changed = _ac.execute("SELECT changes()").fetchone()[0]
            _ac.commit()
            _ac.close()
        except Exception as _ae:
            print(f"[Error Atomic Guard Deposit]: {_ae}")
            _changed = 0

        if not _changed:
            # Sudah diproses sebelumnya — tolak tanpa kredit saldo
            # FIX: ReplyKeyboardMarkup (create_main_menu) tidak bisa dipasang lewat edit_text,
            # Telegram hanya izinkan itu untuk InlineKeyboardMarkup → hapus & kirim pesan baru
            try:
                _smid = notif.rich_message_id(status_msg)
                if _smid:
                    await context.bot.delete_message(chat_id=q.message.chat_id, message_id=_smid)
            except Exception:
                pass
            await notif.send_rich_message_to_chat(context.bot, q.message.chat_id, premium_text(f"""[warning] <b>Deposit ini sudah diproses sebelumnya.</b>
<hr/>
<p>[catatan] Jika saldo belum masuk, hubungi CS.</p>"""), premium_text("[warning] <b>Deposit ini sudah diproses sebelumnya.</b>\n\n<blockquote>[catatan] Jika saldo belum masuk, hubungi CS.</blockquote>"), reply_markup=create_main_menu(uid), log_label="AutoRich2")
            return
        # ─────────────────────────────────────────────────────────────────────

        try:
            import sqlite3
            conn = sqlite3.connect(DB_PATH)
            cursor = conn.cursor()
            cursor.execute("SELECT user_id FROM users WHERE user_id = ?", (uid,))
            user_exists = cursor.fetchone()
            if user_exists:
                cursor.execute("UPDATE users SET deposit_balance = deposit_balance + ?, belance_balance = belance_balance + ? WHERE user_id = ?", (amount, amount, uid))
                conn.commit()
                print(f"[SUCCESS] Saldo {amount} masuk ke akun {uid}")
            else:
                cursor.execute("INSERT INTO users (user_id, deposit_balance, belance_balance) VALUES (?, ?, ?)", (uid, amount, amount))
                conn.commit()
                print(f"[SUCCESS] User baru {uid} dibuat, saldo {amount} masuk")
            conn.close()
        except Exception as db_err:
            print(f"[Error Database Final]: {db_err}")

        try:
            if os.path.exists(qr_path):
                os.remove(qr_path)
        except:
            pass
        
        # === BAGIAN PERUBAHAN: MENGHAPUS PESAN GAMBAR QRIS SECARA TOTAL ===
        try:
            if msg_id:
                await context.bot.delete_message(chat_id=uid, message_id=msg_id)
                print(f"[SUCCESS] Pesan gambar QRIS dengan ID {msg_id} berhasil dihapus.")
        except Exception as del_err:
            print(f"[Error Hapus Pesan QRIS]: {del_err}")
            # Fallback jika gagal dihapus (misal pesan sudah lewat 48 jam), jalankan edit caption bawaanmu
            try:
                await context.bot.edit_message_caption(
                    chat_id=uid,
                    message_id=msg_id,
                    caption=premium_text("[done] <b>Deposit berhasil (Lunas).</b>"),
                    parse_mode="HTML"
                )
            except:
                pass

        # NAMPILKEUN SALDO UPDATE INDEPENDEN
        new_balance = amount
        try:
            u_check = get_user(uid)
            if u_check and len(u_check) > 3:
                new_balance = u_check[3] or amount
        except:
            pass
        
        # FIX: sama seperti di atas — ReplyKeyboardMarkup butuh pesan baru, bukan edit
        try:
            _smid = notif.rich_message_id(status_msg)
            if _smid:
                await context.bot.delete_message(chat_id=q.message.chat_id, message_id=_smid)
        except Exception:
            pass
        await notif.send_rich_message_to_chat(
            context.bot, uid,
            premium_text(f"""[done] <b>DEPOSIT BERHASIL</b>
<hr/>
<table bordered striped>
<tr><th>Info</th><th>Detail</th></tr>
<tr><td>[dolar] Nominal</td><td><b>Rp {amount:,}</b></td></tr>
<tr><td>[duitkarung] Saldo Baru</td><td><b>Rp {new_balance:,}</b></td></tr>
</table>"""),
            premium_text(f"""[done] <b>DEPOSIT BERHASIL</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[dolar] <b>Nominal:</b> <b>Rp {amount:,}</b>
[duitkarung] <b>Saldo Baru:</b> <b>Rp {new_balance:,}</b></blockquote>
"""),
            reply_markup=create_main_menu(uid), log_label="DepositBerhasilUser",
        )

        # NOTIFIKASI OWNER (SELALU lewat bot pusat, anti nyasar ke bot clone)
        try:
            target_owner = OWNER_ID[0] if isinstance(OWNER_ID, list) else OWNER_ID
            await notif.send_rich_message_to_chat(
                owner_notify_bot(context), target_owner,
                premium_text(f"""[duitkarung] <b>DEPOSIT BERHASIL</b>
<hr/>
<table bordered striped>
<tr><th>Info</th><th>Detail</th></tr>
<tr><td>[card] User</td><td><code>{uid}</code></td></tr>
<tr><td>[dolar] Nominal</td><td><b>Rp {amount:,}</b></td></tr>
<tr><td>[verified] Order</td><td><code>{order_id}</code></td></tr>
</table>"""),
                premium_text(f"""[duitkarung] <b>DEPOSIT BERHASIL</b>

<blockquote>[card] <b>User:</b> <code>{uid}</code>
[dolar] <b>Nominal:</b> <b>Rp {amount:,}</b>
[verified] <b>Order:</b> <code>{order_id}</code></blockquote>"""),
                log_label="DepositBerhasilOwner",
            )
        except Exception as e:
            print(f"[Error Notif Owner Fix]: {e}")
        
        try:
            _u = get_user(uid)
            _uname = _u[1] if _u and _u[1] else None
            _new_bal = _u[3] if _u and len(_u) > 3 else new_balance
            await notif.notif_deposit_channel(context.bot, uid, amount, order_id, username=_uname, new_balance=_new_bal)
        except Exception as e:
            print(f"[Error Notif Channel]: {e}")
        
    else:
        await notif.edit_rich_message(
            context.bot, q.message.chat_id, status_msg,
            premium_text("""[warning] <b>Pembayaran deposit belum terdeteksi.</b>

<blockquote>[catatan] Pastikan nominal transfer sesuai QRIS, tunggu beberapa saat, lalu tekan <b>Verifikasi Bayar</b> kembali.</blockquote>"""),
            premium_text("""[warning] <b>Pembayaran deposit belum terdeteksi.</b>

<blockquote>[catatan] Pastikan nominal transfer sesuai QRIS, tunggu beberapa saat, lalu tekan <b>Verifikasi Bayar</b> kembali.</blockquote>"""),
            reply_markup=create_deposit_payment_keyboard(order_id, amount), log_label="DepositNotDetected",
        )

async def cancel_deposit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id

    payment = get_pending_payment_by_message(uid, q.message.message_id if q.message else 0)
    if payment:
        order_id, _user_id, _amount, _status, qr_path, _msg_id, _expires_at = payment
        delete_pending_payment(order_id)
        try:
            if qr_path and os.path.exists(qr_path):
                os.remove(qr_path)
        except Exception:
            pass

    # Hapus gambar QRIS supaya tidak ikut menjadi menu utama.
    if q.message:
        await safe_delete_message(context.bot, q.message.chat_id, q.message.message_id)

    await send_main_menu_new(context, uid)

# ==================== MY SESSIONS HANDLER ====================
async def show_my_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    push_nav(context, "menu_my_sessions")
    
    sessions = get_bought_sessions(uid)
    if not sessions:
        rich_html = """\
<tg-emoji emoji-id="5258500400918587241">📝</tg-emoji> <b>HISTORY ORDER</b>

<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> Belum ada order yang tercatat pada akun Anda.
<tg-emoji emoji-id="5215480011322042129">➡️</tg-emoji> Silakan pilih menu <b>Order OTP</b> untuk memulai pembelian."""
        text = premium_text("""
[catatan] <b>HISTORY ORDER</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[warning] Belum ada order yang tercatat pada akun Anda.
[panahijo] Silakan pilih menu <b>Order OTP</b> untuk memulai pembelian.</blockquote>
""")
        await fast_edit(q, text, reply_markup=create_back_button(), parse_mode="HTML", rich_html=rich_html, log_label="MySessionsEmpty")
        return
    
    context.user_data['my_sessions'] = sessions
    context.user_data['my_page'] = 0
    await show_my_sessions_page(update, context)

async def show_my_sessions_page(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    sessions = context.user_data.get('my_sessions', [])
    page = context.user_data.get('my_page', 0)
    total_pages = (len(sessions) + 4) // 5 if sessions else 1
    
    rich_html = f"""\
<tg-emoji emoji-id="5258500400918587241">📝</tg-emoji> <b>HISTORY ORDER</b>

<table bordered striped>
<tr><th>Info</th><th>Detail</th></tr>
<tr><td>Halaman</td><td><code>{page+1}/{total_pages}</code></td></tr>
<tr><td>Total Session</td><td><code>{len(sessions)}</code></td></tr>
</table>"""
    text = f"""
[catatan] <b>HISTORY ORDER</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[waktu] <b>Halaman:</b> <code>{page+1}/{total_pages}</code>
[product] <b>Total Session:</b> <code>{len(sessions)}</code></blockquote>
"""
    await fast_edit(q, premium_text(text), reply_markup=create_my_sessions_keyboard(sessions, page), parse_mode="HTML", rich_html=rich_html, log_label="MySessionsPage")

async def show_session_detail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    
    # Debug: print raw data
    print(f"DEBUG show_session_detail: {q.data}")
    
    try:
        session_id = int(q.data.split("_")[1])
        print(f"DEBUG session_id: {session_id}")
    except Exception as e:
        print(f"ERROR parsing session_id: {e}")
        await fast_edit(
            q, premium_text("[warning] <b>Format data tidak valid.</b>"),
            reply_markup=create_back_button(), parse_mode="HTML",
            rich_html='<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Format data tidak valid.</b>',
            log_label="SessionDetailBadFormat",
        )
        return
    
    row = get_session_detail(session_id, uid)
    print(f"DEBUG row: {row}")
    
    if not row:
        await fast_edit(
            q, premium_text("""[warning] <b>Session tidak ditemukan.</b>\n\n<blockquote>[catatan] Session mungkin sudah dihapus atau bukan milik akun Anda.</blockquote>"""),
            reply_markup=create_back_button(), parse_mode="HTML",
            rich_html='<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Session tidak ditemukan.</b>\n\nSession mungkin sudah dihapus atau bukan milik akun Anda.',
            log_label="SessionNotFound",
        )
        return
    
    sid, phone, user, aid, sess, created = row
    
    rich_html = f"""\
<tg-emoji emoji-id="5373261557700509032">📱</tg-emoji> <b>DETAIL SESSION</b>

<table bordered striped>
<tr><th>Info Akun</th><th>Detail</th></tr>
<tr><td>ID Telegram</td><td><code>{aid}</code></td></tr>
<tr><td>Nomor</td><td><code>{phone}</code></td></tr>
<tr><td>Username</td><td>@{user}</td></tr>
<tr><td>2FA Password</td><td><code>{DEFAULT_2FA_PASSWORD}</code></td></tr>
<tr><td>Dibeli</td><td><code>{datetime.fromtimestamp(created).strftime('%d/%m/%Y %H:%M')}</code></td></tr>
</table>

<tg-emoji emoji-id="6028551194861899805">🛡️</tg-emoji> <b>Session String Preview:</b>
<code>{sess[:100]}...</code>"""
    text = f"""
[Telegram] <b>DETAIL SESSION</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[card] <b>ID Telegram:</b> <code>{aid}</code>
[WhatsApp] <b>Nomor:</b> <code>{phone}</code>
[crown] <b>Username:</b> @{user}
[password] <b>2FA Password:</b> <code>{DEFAULT_2FA_PASSWORD}</code>
[waktu] <b>Dibeli:</b> <code>{datetime.fromtimestamp(created).strftime('%d/%m/%Y %H:%M')}</code>

[shield] <b>Session String Preview:</b>
<code>{sess[:100]}...</code></blockquote>
"""
    await fast_edit(q, premium_text(text), reply_markup=create_order_success_keyboard(sid, phone), parse_mode="HTML", rich_html=rich_html, log_label="SessionDetail")


async def lihat_password(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    session_id = int(q.data.split("_")[2])
    
    row = get_session_detail(session_id, uid)
    if not row:
        await fast_edit(
            q, premium_text("[warning] <b>Data Akun Tidak Ditemukan</b>\n\n<blockquote>[catatan] Data session tidak ditemukan atau sudah kadaluarsa.\n[panahijo] Silakan ulangi proses pembelian dari awal.</blockquote>"),
            reply_markup=create_back_button(), parse_mode="HTML",
            rich_html='<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Session tidak ditemukan.</b>',
            log_label="LihatPasswordSessionNotFound",
        )
        return
    
    sid, phone, user, aid, sess, created = row
    
    rich_html = f"""\
<tg-emoji emoji-id="5879895758202735862">🔒</tg-emoji> <b>2FA PASSWORD</b>

<table bordered striped>
<tr><th>Info Akun</th><th>Detail</th></tr>
<tr><td>Nomor</td><td><code>{phone}</code></td></tr>
<tr><td>ID Akun</td><td><code>{aid}</code></td></tr>
<tr><td>Password</td><td><code>{DEFAULT_2FA_PASSWORD}</code></td></tr>
</table>

<tg-emoji emoji-id="6028551194861899805">🛡️</tg-emoji> Simpan password ini dengan aman."""
    text = premium_text(f"""
[password] <b>2FA PASSWORD</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[WhatsApp] <b>Nomor:</b> <code>{phone}</code>
[Telegram] <b>ID Akun:</b> <code>{aid}</code>
[password] <b>Password:</b> <code>{DEFAULT_2FA_PASSWORD}</code>

[shield] Simpan password ini dengan aman.</blockquote>
""")
    
    keyboard = styled_inline_keyboard([
        [styled_button("Kembali", callback_data=f"detail_{session_id}", style="danger", emoji_name="back")]
    ])
    await fast_edit(q, text, reply_markup=keyboard, parse_mode="HTML", rich_html=rich_html, log_label="LihatPassword")

async def logout_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    session_id = int(q.data.split("_")[2])
    
    row = get_session_detail(session_id, uid)
    if not row:
        await fast_edit(q, premium_text("[warning] <b>Data Akun Tidak Ditemukan</b>\n\n<blockquote>[catatan] Data session tidak ditemukan atau sudah kadaluarsa.\n[panahijo] Silakan ulangi proses pembelian dari awal.</blockquote>"), reply_markup=create_back_button(), parse_mode="HTML", rich_html=premium_text(f"""[warning] <b>Data Akun Tidak Ditemukan</b>
<hr/>
<ul><li>[catatan] Data session tidak ditemukan atau sudah kadaluarsa.</li><li>[panahijo] Silakan ulangi proses pembelian dari awal.</li></ul>"""), log_label="AutoRich")
        return
    
    sid, phone, user, aid, sess, created = row
    
    await fast_edit(
        q, premium_text("[loading] <b>Logging out from session...</b>"), parse_mode="HTML",
        rich_html=f"{emoji('loading')} <b>Logging out from session...</b>", log_label="LogoutProgress"
    )
    
    success = await force_logout_session(sess)
    delete_sold_session(sid)
    
    try:
        session_file = f"{SESSION_DIR}/{phone}.session"
        if os.path.exists(session_file):
            os.remove(session_file)
    except:
        pass
    
    if success:
        await safe_answer(q, "Logout successful!", show_alert=True)
    else:
        await safe_answer(q, "Logout failed, but session removed", show_alert=True)
    
    await show_my_sessions(update, context)

async def selesai_logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    session_id = int(q.data.split("_")[2])
    
    row = get_session_detail(session_id, uid)
    if not row:
        await fast_edit(
            q, premium_text("[warning] <b>Session Tidak Ditemukan!</b>"), reply_markup=create_main_menu(uid),
            parse_mode="HTML", rich_html=f"{emoji('warning')} <b>Session Tidak Ditemukan!</b>", log_label="SelesaiLogoutGone"
        )
        return
    
    sid, phone, user, aid, sess, created = row
    
    await fast_edit(
        q, premium_text("[loading] <b>Logging out from session...</b>"), parse_mode="HTML",
        rich_html=f"{emoji('loading')} <b>Logging out from session...</b>", log_label="SelesaiLogoutProgress"
    )
    
    success = await force_logout_session(sess)
    delete_sold_session(sid)
    
    try:
        session_file = f"{SESSION_DIR}/{phone}.session"
        if os.path.exists(session_file):
            os.remove(session_file)
    except:
        pass
    
    if success:
        await safe_answer(q, "Logout successful!", show_alert=True)
    else:
        await safe_answer(q, "Logout failed, but session removed", show_alert=True)
    
    await fast_edit(
        q, premium_text("[done] <b>Session Has Been Logged Out!</b>"), reply_markup=create_main_menu(uid),
        parse_mode="HTML", rich_html=f"{emoji('done')} <b>Session Has Been Logged Out!</b>", log_label="SelesaiLogoutDone"
    )

async def req_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    
    parts = q.data.split("_")
    if len(parts) < 3:
        await fast_edit(q, premium_text("[warning] <b>Format callback tidak valid.</b>"), parse_mode="HTML", rich_html=premium_text(f"""[warning] <b>Format callback tidak valid.</b>"""), log_label="AutoRich")
        return
    
    identifier = parts[2]
    
    # === PERBAIKAN DATABASE AMAN (NOMOR 1) ===
    row = None
    try:
        import sqlite3
        conn = sqlite3.connect(DB_PATH)
        local_cursor = conn.cursor()
        
        if identifier.isdigit():
            session_id = int(identifier)
            local_cursor.execute("SELECT id, phone FROM sold_sessions WHERE id=? AND buyer_id=?", (session_id, uid))
        else:
            local_cursor.execute("SELECT id, phone FROM sold_sessions WHERE phone=? AND buyer_id=?", (identifier, uid))
            
        row = local_cursor.fetchone()
        conn.close()
    except Exception as db_err:
        print(f"[Error DB req_otp]: {db_err}")
        await fast_edit(q, premium_text("[warning] <b>Gagal terhubung ke database. Coba lagi!</b>"), parse_mode="HTML", rich_html=premium_text(f"""[warning] <b>Gagal terhubung ke database. Coba lagi!</b>"""), log_label="AutoRich")
        return
    
    if not row:
        await fast_edit(q, premium_text(f"[warning] <b>Session tidak ditemukan untuk user</b> <code>{uid}</code>"), parse_mode="HTML", rich_html=premium_text(f"""[warning] <b>Session tidak ditemukan untuk user</b> <code>{uid}</code>"""), log_label="AutoRich")
        return
            
    sid, phone = row

    await fast_edit(q, premium_text(f"[loading] <b>Mencari OTP untuk nomor:</b> <code>{phone}</code>"), parse_mode="HTML", rich_html=premium_text(f"""[loading] <b>Mencari OTP untuk nomor:</b> <code>{phone}</code>"""), log_label="AutoRich")
    
    otp = await get_otp_from_session(phone)

    if otp:
        text = premium_text(f"""
[password] <b>KODE OTP VIA BOT</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[WhatsApp] <b>Nomor:</b> <code>{phone}</code>
[password] <b>OTP:</b> <code>{otp}</code>
[waktu] OTP berlaku selama 5 menit.</blockquote>
""")
        keyboard = styled_inline_keyboard([
            [styled_button("Kembali", callback_data=f"detail_{sid}", style="danger", emoji_name="back")]
        ])
        await fast_edit(q, text, reply_markup=keyboard, parse_mode="HTML", rich_html=premium_text(f"""[password] <b>KODE OTP VIA BOT</b>
<hr/>
<ul><li>[WhatsApp] <b>Nomor:</b> <code>{phone}</code></li><li>[password] <b>OTP:</b> <code>{otp}</code></li><li>[waktu] OTP berlaku selama 5 menit.</li></ul>"""), log_label="AutoRich")
    else:
        keyboard = styled_inline_keyboard([
            [styled_button("Coba Lagi", callback_data=f"req_otp_{sid}", style="primary", emoji_name="lightning")],
            [styled_button("Kembali", callback_data=f"detail_{sid}", style="danger", emoji_name="back")]
        ])
        await fast_edit(
            q,
            premium_text(f"""[warning] <b>Gagal mendapatkan OTP untuk</b> <code>{phone}</code>

<blockquote>[catatan] Kemungkinan penyebab:
[TopOne] Session sudah tidak aktif.
[TopTwo] Belum ada pesan OTP masuk.
[TopThree] Silakan tunggu 1 menit lalu coba lagi.</blockquote>"""),
            reply_markup=keyboard,
            parse_mode="HTML"
        , rich_html=premium_text(f"""[warning] <b>Gagal mendapatkan OTP untuk</b> <code>{phone}</code>
<hr/>
<ul><li>[catatan] Kemungkinan penyebab:</li><li>[TopOne] Session sudah tidak aktif.</li><li>[TopTwo] Belum ada pesan OTP masuk.</li><li>[TopThree] Silakan tunggu 1 menit lalu coba lagi.</li></ul>"""), log_label="AutoRich")
        
async def show_guide(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    rich_html = """\
<tg-emoji emoji-id="5258500400918587241">📝</tg-emoji> <b>CARA PENGGUNAAN</b>

<table bordered striped>
<tr><th>Langkah</th><th>Keterangan</th></tr>
<tr><td>1</td><td>Pilih <b>Order OTP</b> untuk mencari akun berdasarkan kategori</td></tr>
<tr><td>2</td><td>Pilih produk yang tersedia, lalu lakukan pembelian menggunakan saldo atau QRIS</td></tr>
<tr><td>3</td><td>Setelah order berhasil, gunakan menu <b>History Order</b> untuk melihat detail session, OTP, password, atau logout</td></tr>
<tr><td>4</td><td>Gunakan menu <b>Deposit</b> apabila saldo belum mencukupi</td></tr>
</table>

<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> Pastikan transaksi dilakukan sesuai instruksi sistem agar pembayaran dapat diverifikasi otomatis."""
    text = premium_text("""
[catatan] <b>CARA PENGGUNAAN</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[TopOne] Pilih <b>Order OTP</b> untuk mencari akun berdasarkan kategori.
[TopTwo] Pilih produk yang tersedia, lalu lakukan pembelian menggunakan saldo atau QRIS.
[TopThree] Setelah order berhasil, gunakan menu <b>History Order</b> untuk melihat detail session, OTP, password, atau logout.
[TopOther] Gunakan menu <b>Deposit</b> apabila saldo belum mencukupi.

[warning] Pastikan transaksi dilakukan sesuai instruksi sistem agar pembayaran dapat diverifikasi otomatis.</blockquote>
""")
    await fast_edit(q, text, reply_markup=create_back_button(), parse_mode="HTML", rich_html=rich_html, log_label="GuideMenu")

async def show_top_buyer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    cursor.execute("SELECT user_id, total_bought, last_buy FROM user_stats ORDER BY total_bought DESC, last_buy DESC LIMIT 10")
    rows = cursor.fetchall()
    text = premium_text("[crown] <b>TOP BUYER</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n")
    table_rows = ""
    if not rows:
        text += premium_text("<blockquote>[catatan] Belum ada data pembelian yang tercatat.</blockquote>")
        table_rows = "<tr><td colspan=\"4\">Belum ada data pembelian yang tercatat.</td></tr>"
    else:
        text += "<blockquote>"
        for idx, (buyer_id, total_bought, last_buy) in enumerate(rows, start=1):
            rank_emoji = "TopOne" if idx == 1 else "TopTwo" if idx == 2 else "TopThree" if idx == 3 else "TopOther"
            last = datetime.fromtimestamp(last_buy).strftime('%d/%m/%Y %H:%M') if last_buy else "-"
            text += premium_text(f"[{rank_emoji}] <b>#{idx}</b> User <code>{buyer_id}</code> — <b>{total_bought}</b> order | <code>{last}</code>\n")
            table_rows += f"<tr><td>#{idx}</td><td><code>{buyer_id}</code></td><td>{total_bought} order</td><td><code>{last}</code></td></tr>\n"
        text += "</blockquote>"
    rich_html = f"""\
<tg-emoji emoji-id="5769547529993588669">👑</tg-emoji> <b>TOP BUYER</b>

<table bordered striped>
<tr><th>Rank</th><th>User</th><th>Order</th><th>Terakhir</th></tr>
{table_rows}</table>"""
    await fast_edit(q, text, reply_markup=create_back_button(), parse_mode="HTML", rich_html=rich_html, log_label="TopBuyer")

async def show_popular_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    cursor.execute("SELECT COALESCE(label, 'No Tag'), COALESCE(status_limit, 'No Limit'), COUNT(*) FROM session_stock WHERE status='available' GROUP BY label, status_limit ORDER BY COUNT(*) DESC LIMIT 6")
    rows = cursor.fetchall()
    text = premium_text("[star] <b>PRODUK POPULER</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n")
    table_rows = ""
    if not rows:
        text += premium_text("<blockquote>[warning] Belum ada produk populer karena stok tersedia masih kosong.</blockquote>")
        table_rows = "<tr><td colspan=\"3\">Belum ada produk populer karena stok tersedia masih kosong.</td></tr>"
    else:
        text += "<blockquote>"
        for idx, (label, limit_status, total) in enumerate(rows, start=1):
            marker = "TopOne" if idx == 1 else "TopTwo" if idx == 2 else "TopThree" if idx == 3 else "TopOther"
            text += premium_text(f"[{marker}] <b>{label}</b> / <code>{limit_status}</code> — <b>{total}</b> stok tersedia\n")
            table_rows += f"<tr><td>#{idx}</td><td>{html.escape(str(label))} / <code>{html.escape(str(limit_status))}</code></td><td><b>{total}</b> stok</td></tr>\n"
        text += premium_text("\n[panahijo] Pilih <b>Order OTP</b> untuk memulai order berdasarkan kategori.")
        text += "</blockquote>"
    rich_html = f"""\
<tg-emoji emoji-id="5438496463044752972">⭐</tg-emoji> <b>PRODUK POPULER</b>

<table bordered striped>
<tr><th>Rank</th><th>Produk</th><th>Stok</th></tr>
{table_rows}</table>

<tg-emoji emoji-id="5215480011322042129">➡️</tg-emoji> Pilih <b>Order OTP</b> untuk memulai order berdasarkan kategori."""
    keyboard = styled_inline_keyboard([
        [styled_button("Order OTP", callback_data="menu_stock", style="success", emoji_name="Telegram")],
        [styled_button("Kembali", callback_data="menu_back", style="danger", emoji_name="back")]
    ])
    await fast_edit(q, text, reply_markup=keyboard, parse_mode="HTML", rich_html=rich_html, log_label="PopularProducts")

async def show_contact_cs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    support_username = globals().get('SUPPORT_USERNAME', 'Pretygirrls')
    rich_html = f"""\
<tg-emoji emoji-id="5778673721317267508">💬</tg-emoji> <b>CUSTOMER SERVICE</b>

<tg-emoji emoji-id="5215659875962462292">📢</tg-emoji> Hubungi customer service resmi apabila membutuhkan bantuan transaksi, kendala OTP, deposit, atau pengelolaan session.

<table bordered striped>
<tr><th>Kontak</th><th>Detail</th></tr>
<tr><td>Support</td><td>@{support_username}</td></tr>
</table>"""
    text = premium_text(f"""
[chat] <b>CUSTOMER SERVICE</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[spikerbiru] Hubungi customer service resmi apabila membutuhkan bantuan transaksi, kendala OTP, deposit, atau pengelolaan session.

[verified] <b>Support:</b> @{support_username}</blockquote>
""")
    keyboard = styled_inline_keyboard([
        [styled_button("Hubungi Customer Service", url=f"https://t.me/{support_username}", style="danger", emoji_name="chat")],
        [styled_button("Kembali", callback_data="menu_back", style="danger", emoji_name="back")]
    ])
    await fast_edit(q, text, reply_markup=keyboard, parse_mode="HTML", rich_html=rich_html, log_label="ContactCSInline")

# ==================== HALAMAN 2 - AUTO ORDER GIFT HANDLERS ====================

import time as _time
import uuid as _uuid

async def show_page2_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buka halaman 2 — Auto Order Gift."""
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    push_nav(context, "page2_gift")
    context.user_data["active_menu_page"] = 2

    from src.main_menu import create_page2_menu
    kb = create_page2_menu(uid, is_owner_func=is_owner)

    rich_html = """\
<tg-emoji emoji-id="6028530359975548369">💎</tg-emoji> <b>MANXY OFFICIAL — AUTO ORDER GIFT</b>

<tg-emoji emoji-id="5438496463044752972">⭐</tg-emoji> Pilih gift yang ingin dikirimkan ke akun Telegram tujuan.

<table bordered striped>
<tr><th>Layanan Gift Otomatis</th><th>Keterangan</th></tr>
<tr><td>1</td><td>Pilih hadiah dari daftar di bawah</td></tr>
<tr><td>2</td><td>Masukkan username Telegram penerima</td></tr>
<tr><td>3</td><td>Pilih tampilan pengirim (Anonim / Tampil Nama)</td></tr>
<tr><td>4</td><td>Bayar via QRIS, gift langsung terkirim otomatis</td></tr>
</table>

<tg-emoji emoji-id="6028551194861899805">🛡️</tg-emoji> Semua transaksi diproses 24 jam via MTProto."""
    text = premium_text("""
[diamond1] <b>MANXY OFFICIAL — AUTO ORDER GIFT</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[star] Pilih gift yang ingin dikirimkan ke akun Telegram tujuan.

[product] <b>Layanan Gift Otomatis</b>
[panahijo] Pilih hadiah dari daftar di bawah.
[panahijo] Masukkan username Telegram penerima.
[panahijo] Pilih tampilan pengirim (Anonim / Tampil Nama).
[panahijo] Bayar via QRIS, gift langsung terkirim otomatis.

[shield] Semua transaksi diproses 24 jam via MTProto.</blockquote>
""")
    # PENTING: create_page2_menu() sekarang mengembalikan ReplyKeyboardMarkup
    # (list gift dipindah dari inline ke Reply Keyboard). Reply keyboard TIDAK
    # BISA dipasang lewat edit_message_* — Telegram akan menolak dengan
    # "BadRequest: Inline keyboard expected". fast_edit() sudah menangani ini
    # dengan otomatis hapus pesan lama & kirim pesan baru kalau reply_markup-nya
    # ReplyKeyboardMarkup, jadi selalu pakai fast_edit di sini, bukan manual
    # edit_message_caption/edit_message_text/edit_message_reply_markup.
    # rich_html diisi supaya pesan baru itu dikirim sebagai Rich Message (tabel).
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=rich_html, log_label="Page2GiftMenuCB")




async def show_gift_info(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q, "🎁 Auto Order Gift — Pengiriman Otomatis via MTProto", show_alert=True)


async def show_gift_disabled(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q, "❌ Gift ini sedang dinonaktifkan oleh owner.", show_alert=True)


async def show_gift_cara_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    rich_html = f"""\
{emoji('catatan')} <b>CARA ORDER GIFT</b>

<table bordered striped>
<tr><th>Langkah</th><th>Keterangan</th></tr>
<tr><td>1</td><td>Pilih hadiah dari daftar menu Gift</td></tr>
<tr><td>2</td><td>Masukkan <b>username</b> akun Telegram tujuan (tanpa @)</td></tr>
<tr><td>3</td><td>Pilih apakah pengiriman <b>anonim</b> atau tampil nama</td></tr>
<tr><td>4</td><td>Scan <b>QRIS</b> dan konfirmasi pembayaran</td></tr>
<tr><td>5</td><td>Gift otomatis terkirim setelah pembayaran terverifikasi</td></tr>
</table>

{emoji('warning')} Pastikan username tujuan aktif dan dapat menerima gift.
{emoji('spikerbiru')} Proses pengiriman berlangsung dalam hitungan detik."""
    text = premium_text("""
[catatan] <b>CARA ORDER GIFT</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[TopOne] Pilih hadiah dari daftar menu Gift.
[TopTwo] Masukkan <b>username</b> akun Telegram tujuan (tanpa @).
[TopThree] Pilih apakah pengiriman <b>anonim</b> atau tampil nama.
[TopOther] Scan <b>QRIS</b> dan konfirmasi pembayaran.
[done] Gift otomatis terkirim setelah pembayaran terverifikasi.

[warning] Pastikan username tujuan aktif dan dapat menerima gift.
[spikerbiru] Proses pengiriman berlangsung dalam hitungan detik.</blockquote>
""")
    kb = styled_inline_keyboard([[styled_button("Kembali ke Gift Menu", callback_data="menu_page_2_back", style="success", emoji_name="back")]])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=rich_html, log_label="GiftCaraOrder")


async def show_gift_history(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS gift_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, order_id TEXT,
                gift_emoji TEXT, gift_label TEXT, gift_custom_emoji_id TEXT,
                target_username TEXT, amount INTEGER, gift_id TEXT,
                is_anon INTEGER DEFAULT 0, recipient_msg TEXT,
                status TEXT DEFAULT 'pending', created_at INTEGER
            )
        """)
        conn.commit()
        cursor.execute(
            "SELECT gift_emoji, gift_label, target_username, amount, status, created_at, gift_custom_emoji_id FROM gift_orders WHERE user_id=? ORDER BY created_at DESC LIMIT 10",
            (uid,)
        )
        rows = cursor.fetchall()
    except Exception:
        rows = []

    if not rows:
        text = premium_text("""
[catatan] <b>RIWAYAT GIFT</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[warning] Belum ada riwayat order gift.
Pilih hadiah dari menu Gift untuk memulai!</blockquote>
""")
        rich_html = f"""\
{emoji('catatan')} <b>RIWAYAT GIFT</b>

{emoji('warning')} Belum ada riwayat order gift. Pilih hadiah dari menu Gift untuk memulai!"""
    else:
        from datetime import datetime as _dt
        lines = [premium_text("[catatan] <b>RIWAYAT GIFT (10 Terakhir)</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>")]
        table_rows = ""
        for i, (em, lb, tgt, amt, st, ts, cust_eid) in enumerate(rows, 1):
            tgl = _dt.fromtimestamp(ts).strftime("%d/%m %H:%M") if ts else "-"
            ic  = "[done]" if st == "success" else "[warning]"
            status_label = "Sukses" if st == "success" else "Pending/Gagal"
            gift_icon_html = f'<tg-emoji emoji-id="{cust_eid}">{em}</tg-emoji>' if cust_eid else em
            lines.append(premium_text(f"{ic} #{i} {em} <b>{lb}</b> → @{tgt} | {format_currency(amt)} | <code>{tgl}</code>\n"))
            table_rows += f"<tr><td>{gift_icon_html} {html.escape(str(lb))}</td><td>@{html.escape(str(tgt))}</td><td>{format_currency(amt)}</td><td>{status_label}</td><td><code>{tgl}</code></td></tr>\n"
        lines.append("</blockquote>")
        text = "".join(lines)
        rich_html = f"""\
{emoji('catatan')} <b>RIWAYAT GIFT (10 Terakhir)</b>

<table bordered striped>
<tr><th>Gift</th><th>Tujuan</th><th>Harga</th><th>Status</th><th>Waktu</th></tr>
{table_rows}</table>"""

    kb = styled_inline_keyboard([[styled_button("Kembali ke Gift Menu", callback_data="menu_page_2_back", style="success", emoji_name="back")]])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=rich_html, log_label="GiftHistory")


async def handle_gift_order_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User pilih jenis gift → simpan ke user_data → minta username."""
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id

    from src.main_menu import GIFT_ITEMS, get_gift_price, is_gift_enabled, _fmt
    idx = int(q.data.split("_")[-1])
    if idx < 0 or idx >= len(GIFT_ITEMS):
        await safe_answer(q, "Gift tidak ditemukan!", show_alert=True); return
    if not is_gift_enabled(idx):
        await safe_answer(q, "❌ Gift ini sedang tidak tersedia.", show_alert=True); return

    g = GIFT_ITEMS[idx]
    price = get_gift_price(idx)
    
    # SIMPAN base_price sebelum di-apply markup (untuk kalkulasi komisi yang akurat)
    base_price = price
    
    # FIX: Apply markup harga otomatis jika ini clone bot
    try:
        price = clone_system.apply_clone_price_markup(context.bot.token, DB_PATH, price)
    except Exception:
        pass  # jika error, pakai harga original
    
    context.user_data["gift_pending"] = {
        "idx": idx, "emoji": g["emoji"], "label": g["label"],
        "price": price, "gift_id": g["gift_id"],
        "base_price": base_price,  # SIMPAN base_price untuk komisi yang akurat
        "custom_emoji_id": g.get("custom_emoji_id",""),
        # Token bot tempat pembeli order (pusat/clone) — dipakai supaya notif
        # approve/tolak gift manual & deteksi komisi clone tetap benar walau
        # Owner memprosesnya lewat bot pusat. Lihat get_origin_bot().
        "origin_bot_token": getattr(context.bot, "token", None),
    }
    context.user_data["current_menu_state"] = "gift_ask_username"

    # Tampilkan custom emoji jika ada
    ceid = g.get("custom_emoji_id","")
    if ceid:
        emoji_html = f"<tg-emoji emoji-id='{ceid}'>{g['emoji']}</tg-emoji>"
    else:
        emoji_html = g["emoji"]

    text = premium_text(f"""
[star] <b>ORDER GIFT — {emoji_html} {g['label']}</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[dolar] <b>Harga:</b> <code>{_fmt(price)}</code>

[product] Masukkan <b>username</b> Telegram tujuan (tanpa @).
Contoh: <code>usernamekak</code>

[warning] Pastikan username aktif dan bisa menerima gift.</blockquote>
""")
    rich_html = f"""\
{emoji('star')} <b>ORDER GIFT — {emoji_html} {g['label']}</b>
<hr/>
<p>{emoji('dolar')} <b>Harga:</b> <code>{_fmt(price)}</code></p>
<p>{emoji('product')} Masukkan <b>username</b> Telegram tujuan (tanpa @).</p>
<p>Contoh: <code>usernamekak</code></p>
<p>{emoji('warning')} Pastikan username aktif dan bisa menerima gift.</p>"""
    kb = styled_inline_keyboard([[styled_button("Batal", callback_data="menu_page_2_back", style="danger", emoji_name="back")]])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=rich_html, log_label="GiftOrderSelect")


async def handle_gift_username_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Tangkap teks username dari chat (dipanggil dari handle_message)."""
    uid = update.effective_user.id
    if context.user_data.get("current_menu_state") != "gift_ask_username":
        return False
    gift_data = context.user_data.get("gift_pending")
    if not gift_data:
        return False

    uname = update.message.text.strip().lstrip("@")
    if not uname or len(uname) < 3:
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            '<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> Username tidak valid (min. 3 karakter). Coba lagi.',
            premium_text("[warning] Username tidak valid (min. 3 karakter). Coba lagi."),
            log_label="GiftUsernameInvalid",
        )
        return True

    gift_data["target_username"] = uname
    context.user_data["gift_pending"] = gift_data
    context.user_data["current_menu_state"] = "gift_ask_visibility"

    g_emoji = gift_data.get("emoji","🎁")
    g_label = gift_data.get("label","Gift")
    ceid    = gift_data.get("custom_emoji_id","")
    price   = gift_data.get("price",0)
    from src.main_menu import _fmt

    emoji_html = f"<tg-emoji emoji-id='{ceid}'>{g_emoji}</tg-emoji>" if ceid else g_emoji

    rich_html = f"""\
<tg-emoji emoji-id="6147679667663934682">🖥️</tg-emoji> <b>PILIH MODE PENGIRIMAN</b>

<table bordered striped>
<tr><th>Detail Order</th><th>Isi</th></tr>
<tr><td>Gift</td><td>{emoji_html} {g_label}</td></tr>
<tr><td>Tujuan</td><td>@{uname}</td></tr>
<tr><td>Harga</td><td><code>{_fmt(price)}</code></td></tr>
</table>

<tg-emoji emoji-id="5258500400918587241">📝</tg-emoji> Apakah nama pengirim ditampilkan atau anonim?"""
    text = premium_text(f"""
[panel] <b>PILIH MODE PENGIRIMAN</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[star] Gift: {emoji_html} {g_label}
[product] Tujuan: @{uname}
[dolar] Harga: <code>{_fmt(price)}</code>

[catatan] Apakah nama pengirim ditampilkan atau anonim?</blockquote>
""")
    from src.custom_emoji import styled_keyboard_button, clean_button_label
    rows = [
        [
            styled_keyboard_button("👤 Tampil Nama", style="success", emoji_name="verified"),
            styled_keyboard_button("🎩 Anonim",       style="primary", emoji_name="panel"),
        ],
        [styled_keyboard_button("Batal", style="danger", emoji_name="back")]
    ]
    kb = ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)
    # PENTING: styled_keyboard_button() membuang emoji dari teks tombol lewat
    # clean_button_label(), jadi teks yang BENERAN dikirim balik oleh Telegram
    # saat tombol ditekan itu TANPA emoji. Key reply_map wajib disamakan lewat
    # clean_button_label() juga, kalau tidak taps-nya gak akan pernah cocok
    # (keliatan seperti "stuck"/gak ada respon).
    reply_map = {
        clean_button_label("👤 Tampil Nama"): "gift_vis_show",
        clean_button_label("🎩 Anonim"):       "gift_vis_anon",
        clean_button_label("Batal"):          "menu_page_2_back",
    }
    set_page_reply_map(context, "gift_ask_visibility", reply_map)
    await notif.send_rich_message_to_chat(
        context.bot, update.effective_chat.id, rich_html, text,
        reply_markup=kb, log_label="GiftVisibilityAsk",
    )
    return True


async def handle_gift_ask_visibility(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Pilih anonim / tampil nama → lanjut tanya pesan custom."""
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    is_anon = (q.data == "gift_vis_anon")
    gift_data = context.user_data.get("gift_pending", {})
    gift_data["anon"] = is_anon
    context.user_data["gift_pending"] = gift_data

    target    = gift_data.get("target_username","?")
    g_emoji   = gift_data.get("emoji","🎁")
    g_label   = gift_data.get("label","Gift")
    ceid      = gift_data.get("custom_emoji_id","")
    price     = gift_data.get("price",0)
    from src.main_menu import _fmt
    emoji_html = f"<tg-emoji emoji-id='{ceid}'>{g_emoji}</tg-emoji>" if ceid else g_emoji
    vis_txt    = f"{emoji('shield')} Anonim (nama tersembunyi)" if is_anon else f"{emoji('crown')} Tampil nama pengirim"

    text = premium_text(f"""
[catatan] <b>TAMBAHKAN PESAN GIFT?</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[star] <b>Gift:</b> {emoji_html} {g_label}
[product] <b>Tujuan:</b> @{target}
[dolar] <b>Harga:</b> <code>{_fmt(price)}</code>
[panel] <b>Mode:</b> {vis_txt}

[spikerbiru] Mau kasih pesan/ucapan yang ikut terkirim bareng gift ini?</blockquote>
""")
    from src.custom_emoji import styled_keyboard_button, clean_button_label
    rows = [
        [styled_keyboard_button("✍️ Tulis Pesan",       style="primary", emoji_name="catatan")],
        [styled_keyboard_button("⏭️ Lanjut Tanpa Pesan", style="success", emoji_name="done")],
        [styled_keyboard_button("Batal", style="danger", emoji_name="back")]
    ]
    kb = ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)
    reply_map = {
        clean_button_label("✍️ Tulis Pesan"):        "gift_msg_write",
        clean_button_label("⏭️ Lanjut Tanpa Pesan"): "gift_msg_skip",
        clean_button_label("Batal"):                "menu_page_2_back",
    }
    set_page_reply_map(context, "gift_ask_message_choice", reply_map)
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""[catatan] <b>TAMBAHKAN PESAN GIFT?</b>
<hr/>
<ul><li>[star] <b>Gift:</b> {emoji_html} {g_label}</li><li>[product] <b>Tujuan:</b> @{target}</li><li>[dolar] <b>Harga:</b> <code>{_fmt(price)}</code></li><li>[panel] <b>Mode:</b> {vis_txt}</li></ul>
<p>[spikerbiru] Mau kasih pesan/ucapan yang ikut terkirim bareng gift ini?</p>"""), log_label="AutoRich")
    
async def show_gift_final_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tampilkan konfirmasi akhir (gift+target+mode+pesan) sebelum bayar QRIS."""
    gift_data = context.user_data.get("gift_pending", {})
    target    = gift_data.get("target_username","?")
    g_emoji   = gift_data.get("emoji","🎁")
    g_label   = gift_data.get("label","Gift")
    ceid      = gift_data.get("custom_emoji_id","")
    price     = gift_data.get("price",0)
    is_anon   = gift_data.get("anon", False)
    msg_text  = gift_data.get("message","")
    from src.main_menu import _fmt
    emoji_html = f"<tg-emoji emoji-id='{ceid}'>{g_emoji}</tg-emoji>" if ceid else g_emoji
    vis_txt    = f"{emoji('shield')} Anonim (nama tersembunyi)" if is_anon else f"{emoji('crown')} Tampil nama pengirim"
    msg_preview = f"\n[catatan] <b>Pesan:</b> <i>{msg_text}</i>" if msg_text else ""

    rich_html = f"""\
<tg-emoji emoji-id="6298432564487001027">✅</tg-emoji> <b>KONFIRMASI ORDER GIFT</b>

<table bordered striped>
<tr><th>Detail Order</th><th>Isi</th></tr>
<tr><td>Gift</td><td>{emoji_html} {g_label}</td></tr>
<tr><td>Tujuan</td><td>@{target}</td></tr>
<tr><td>Harga</td><td><code>{_fmt(price)}</code></td></tr>
<tr><td>Mode</td><td>{vis_txt}</td></tr>
{f'<tr><td>Pesan</td><td><i>{msg_text}</i></td></tr>' if msg_text else ''}
</table>

<tg-emoji emoji-id="5215659875962462292">📢</tg-emoji> Konfirmasi pesanan dan lanjut ke pembayaran."""
    text = premium_text(f"""
[verified] <b>KONFIRMASI ORDER GIFT</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[star] <b>Gift:</b> {emoji_html} {g_label}
[product] <b>Tujuan:</b> @{target}
[dolar] <b>Harga:</b> <code>{_fmt(price)}</code>
[panel] <b>Mode:</b> {vis_txt}{msg_preview}

[spikerbiru] Konfirmasi pesanan dan lanjut ke pembayaran.</blockquote>
""")
    kb = styled_inline_keyboard([
        [styled_button("💳 Bayar via QRIS", callback_data="gift_pay_qris", style="success", emoji_name="duitkarung")],
        [styled_button("💰 Bayar via Saldo", callback_data="gift_pay_saldo", style="primary", emoji_name="card")],
        [styled_button("Batal", callback_data="menu_page_2_back", style="danger", emoji_name="back")]
    ])

    q = update.callback_query
    if q:
        await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""[verified] <b>KONFIRMASI ORDER GIFT</b>
<hr/>
<ul><li>[star] <b>Gift:</b> {emoji_html} {g_label}</li><li>[product] <b>Tujuan:</b> @{target}</li><li>[dolar] <b>Harga:</b> <code>{_fmt(price)}</code></li><li>[panel] <b>Mode:</b> {vis_txt}{msg_preview}</li></ul>
<p>[spikerbiru] Konfirmasi pesanan dan lanjut ke pembayaran.</p>"""), log_label="AutoRich")
    else:
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id, rich_html, text,
            reply_markup=kb, log_label="GiftFinalConfirm",
        )


async def handle_gift_msg_skip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    gift_data = context.user_data.get("gift_pending", {})
    gift_data["message"] = ""
    context.user_data["gift_pending"] = gift_data
    await show_gift_final_confirm(update, context)


async def handle_gift_msg_write(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    context.user_data["current_menu_state"] = "gift_ask_message"
    text = premium_text("""
[catatan] <b>TULIS PESAN GIFT</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[product] Ketik pesan/ucapan yang mau ikut dikirim bareng gift ini.
[warning] Maksimal 200 karakter.</blockquote>
""")
    kb = styled_inline_keyboard([[styled_button("Batal", callback_data="menu_page_2_back", style="danger", emoji_name="back")]])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""[catatan] <b>TULIS PESAN GIFT</b>
<hr/>
<ul><li>[product] Ketik pesan/ucapan yang mau ikut dikirim bareng gift ini.</li><li>[warning] Maksimal 200 karakter.</li></ul>"""), log_label="AutoRich")


async def handle_gift_message_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Tangkap teks pesan custom dari chat (dipanggil dari handle_message)."""
    if context.user_data.get("current_menu_state") != "gift_ask_message":
        return False
    gift_data = context.user_data.get("gift_pending")
    if not gift_data:
        return False

    msg_text = update.message.text.strip()[:200]
    gift_data["message"] = msg_text
    context.user_data["gift_pending"] = gift_data

    await show_gift_final_confirm(update, context)
    return True

async def cancel_gift_qris(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q, "Dibatalkan!")
    uid = q.from_user.id

    gift_data = context.user_data.get("gift_pending", {})
    qr_path = gift_data.get("qr_path", "")
    if qr_path and os.path.exists(qr_path):
        try:
            os.remove(qr_path)
        except Exception:
            pass

    context.user_data.pop("gift_pending", None)

    # Hapus pesan QRIS foto terlebih dahulu
    try:
        await q.message.delete()
    except Exception:
        pass

    # Kirim menu utama DENGAN FOTO (pakai send_main_menu_new agar foto muncul)
    await send_main_menu_new(context, uid)

async def handle_gift_pay_qris(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dispatcher: pilih flow QRIS otomatis (Pakasir) atau manual (approve owner) sesuai mode payment aktif."""
    if get_payment_method() == "manual":
        await _handle_gift_pay_qris_manual(update, context)
    else:
        await _handle_gift_pay_qris_otomatis(update, context)


async def _handle_gift_pay_qris_otomatis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buat QRIS otomatis via Pakasir dan kirim foto QR ke user."""
    q = update.callback_query
    await safe_answer(q, "⏳ Membuat QRIS...")
    uid = q.from_user.id

    gift_data = context.user_data.get("gift_pending", {})
    if not gift_data:
        await safe_answer(q, "Sesi kadaluarsa. Ulangi dari menu Gift.", show_alert=True); return

    price     = gift_data.get("price",0)
    g_emoji   = gift_data.get("emoji","🎁")
    g_label   = gift_data.get("label","Gift")
    ceid      = gift_data.get("custom_emoji_id","")
    target    = gift_data.get("target_username","")
    from src.main_menu import _fmt
    emoji_html = f"<tg-emoji emoji-id='{ceid}'>{g_emoji}</tg-emoji>" if ceid else g_emoji

    # Buat QRIS via pakasir (fungsi yang sudah ada di project)
    qris_data = await create_qris(price)

    if not qris_data:
        await safe_answer(q, "❌ Gagal membuat QRIS. Coba lagi.", show_alert=True); return

    order_id = qris_data["id"]
    qr_path  = qris_data.get("qr_path","")
    gift_data["order_id"]    = order_id
    gift_data["qris_amount"] = price
    gift_data["qr_path"] = qr_path
    context.user_data["gift_pending"] = gift_data

    # ── FIX ROOT CAUSE: sebelumnya order gift TIDAK PERNAH dicatat ke tabel
    # pending_payments. Akibatnya, atomic guard di handle_gift_cek_payment
    # (UPDATE ... WHERE status='pending') selalu affect 0 baris karena baris
    # order_id-nya memang tidak ada — jadi walau pembayaran sudah sukses,
    # tombol "Cek Pembayaran" cuma muncul "sudah diproses sebelumnya" dan
    # gift TIDAK PERNAH benar-benar terkirim. Sama seperti flow QRIS lain
    # (session/stars/premium), order gift wajib didaftarkan di sini.
    expires_at = int(time.time()) + 900
    add_pending_payment(uid, order_id, price, qr_path, 0, expires_at)
    # ── FIX: simpan detail gift (target, gift_id, pesan, anon, dll) durably di
    # DB, bukan cuma di context.user_data. Kalau bot crash/restart persis
    # setelah pembayaran ke-mark 'paid' tapi SEBELUM gift beneran terkirim
    # (mis. gara-gara Bad Gateway di tengah proses), _recover_stuck_gift_orders()
    # butuh data ini buat nyelesein/notify order yang ke-stuck.
    try:
        import json as _json
        cursor.execute(
            "UPDATE pending_payments SET gift_json=? WHERE id=?",
            (_json.dumps({**gift_data, "buyer_id": uid}), order_id)
        )
        conn.commit()
    except Exception as _gje:
        print(f"[GiftJSON] Gagal simpan detail gift ke DB: {_gje}")

    text = premium_text(f"""
[duitkarung] <b>PEMBAYARAN QRIS — GIFT</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] <b>ID Order:</b> <code>{order_id}</code>
[star] <b>Gift:</b> {emoji_html} {g_label}
[product] <b>Tujuan:</b> @{target}
[dolar] <b>Total:</b> <code>{_fmt(price)}</code>

[spikerbiru] Scan QR di atas dan tekan <b>Cek Pembayaran</b> setelah transfer.
Bot akan memproses pengiriman gift otomatis setelah pembayaran terkonfirmasi.</blockquote>
""")
    kb = styled_inline_keyboard([
        [styled_button("✅ Cek Pembayaran", callback_data=f"gift_cek_{order_id}", style="success", emoji_name="verified")],
        [styled_button("Batalkan",          callback_data="cancel_gift_qris",       style="danger",  emoji_name="back")]
    ])

    # Kirim foto QR (fix utama — bukan URL)
    qr_message = None
    try:
        if qr_path and os.path.exists(qr_path):
            with open(qr_path, "rb") as f:
                photo_bytes = f.read()
            qr_message = await safe_send_photo(context, uid, photo=photo_bytes, caption=text, reply_markup=kb)
            try:
                await q.message.delete()
            except Exception:
                pass
            # ── FIX: auto-poll status bayar di background, supaya gift TETAP
            # terkirim otomatis walau user tidak menekan "Cek Pembayaran".
            asyncio.create_task(
                _auto_check_gift_qris(context, uid, order_id, price, dict(gift_data), context.bot, qr_message)
            )
            return
    except Exception as e:
        print(f"[Gift QRIS] Gagal kirim foto: {e}")

    # Fallback: edit pesan
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""[duitkarung] <b>PEMBAYARAN QRIS — GIFT</b>
<hr/>
<ul><li>[catatan] <b>ID Order:</b> <code>{order_id}</code></li><li>[star] <b>Gift:</b> {emoji_html} {g_label}</li><li>[product] <b>Tujuan:</b> @{target}</li><li>[dolar] <b>Total:</b> <code>{_fmt(price)}</code></li></ul>
<ul><li>[spikerbiru] Scan QR di atas dan tekan <b>Cek Pembayaran</b> setelah transfer.</li><li>Bot akan memproses pengiriman gift otomatis setelah pembayaran terkonfirmasi.</li></ul>"""), log_label="AutoRich")
    # ── FIX: tetap jalankan auto-poll walau fallback (bukan pakai foto) ──
    asyncio.create_task(
        _auto_check_gift_qris(context, uid, order_id, price, dict(gift_data), context.bot, None)
    )


async def _auto_check_gift_qris(context: ContextTypes.DEFAULT_TYPE, uid: int, order_id: str, amount: int,
                                 gift_data: dict, buyer_bot, qr_message=None):
    """Auto-poll status pembayaran QRIS gift setiap beberapa detik di background,
    supaya gift TETAP terkirim otomatis walau user tidak menekan tombol
    'Cek Pembayaran'. Aman dijalankan bareng flow manual (klik tombol) karena
    keduanya pakai atomic guard yang sama di DB (UPDATE ... WHERE status='pending'),
    jadi gift tidak akan pernah terkirim dobel.
    """
    max_attempts = 180  # 180 x 5 detik = ~15 menit (sesuaikan dgn masa berlaku QRIS)
    for _ in range(max_attempts):
        await asyncio.sleep(5)

        # Berhenti kalau order ini sudah dibatalkan / ditimpa order gift baru
        current = context.user_data.get("gift_pending", {})
        if current.get("order_id") != order_id:
            return

        try:
            is_paid = await check_payment_status(order_id, amount)
        except Exception:
            is_paid = False
        if not is_paid:
            continue

        # ── ATOMIC GUARD: sama persis dengan handle_gift_cek_payment, cegah
        # gift terkirim 2x kalau user KEBETULAN juga pencet tombol manual.
        try:
            import sqlite3 as _sq3
            _ac = _sq3.connect(DB_PATH)
            _ac.execute(
                "UPDATE pending_payments SET status='paid' WHERE (id=? OR order_id=?) AND status='pending'",
                (order_id, order_id)
            )
            _changed = _ac.execute("SELECT changes()").fetchone()[0]
            _ac.commit()
            _ac.close()
        except Exception as _ae:
            print(f"[Error Atomic Guard Gift Auto]: {_ae}")
            _changed = 0

        if not _changed:
            # Sudah lebih dulu diproses lewat klik manual user.
            return

        context.user_data.pop("gift_pending", None)

        qr_path = gift_data.get("qr_path", "")
        if qr_message is not None:
            try:
                await qr_message.delete()
            except Exception:
                pass
        if qr_path and os.path.exists(qr_path):
            try:
                os.remove(qr_path)
            except Exception:
                pass

        await _process_gift_delivery(context, uid, gift_data, order_id=order_id, paid_via="qris", buyer_bot=buyer_bot)
        return


async def _handle_gift_pay_qris_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mode manual: tampilkan QRIS foto yang diset owner (atau info rekening), minta user upload bukti TF."""
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id

    gift_data = context.user_data.get("gift_pending", {})
    if not gift_data:
        await safe_answer(q, "Sesi kadaluarsa. Ulangi dari menu Gift.", show_alert=True); return

    price     = gift_data.get("price", 0)
    g_emoji   = gift_data.get("emoji", "🎁")
    g_label   = gift_data.get("label", "Gift")
    ceid      = gift_data.get("custom_emoji_id", "")
    target    = gift_data.get("target_username", "")
    from src.main_menu import _fmt
    emoji_html = f"<tg-emoji emoji-id='{ceid}'>{g_emoji}</tg-emoji>" if ceid else g_emoji

    context.user_data["current_menu_state"] = "gift_wait_bukti"

    # Ambil info rekening non-QRIS yang sudah diset owner
    pay_lines = []
    for label, key, _ in PAYMENT_METHODS_LIST:
        if key == "qris":
            continue
        info = get_payment_info(key)
        if info:
            pay_lines.append(f"[panahijo] <b>{label}:</b> <code>{info}</code>")
    rekening_text = "\n".join(pay_lines) if pay_lines else ""
    qris_file_id = get_payment_info("qris")

    text = premium_text(f"""
[duitkarung] <b>PEMBAYARAN MANUAL — GIFT</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[star] <b>Gift:</b> {emoji_html} {g_label}
[product] <b>Tujuan:</b> @{target}
[dolar] <b>Total Transfer:</b> <code>{_fmt(price)}</code>

{"" if not rekening_text else rekening_text + chr(10)}{"[card] Scan QRIS di atas atau transfer ke rekening di atas." if qris_file_id and rekening_text else ("[card] Scan QRIS di atas untuk transfer." if qris_file_id else ("[warning] Info rekening belum diset oleh owner." if not rekening_text else ""))}

[warning] Transfer TEPAT <code>{_fmt(price)}</code> sesuai nominal.
[catatan] Setelah transfer, kirim <b>foto/screenshot bukti transfer</b> ke bot ini.
[shield] Gift akan diproses & dikirim setelah Owner menyetujui.</blockquote>
""")
    kb = styled_inline_keyboard([
        [styled_button("Batal", callback_data="cancel_gift_qris", style="danger", emoji_name="back")]
    ])

    if qris_file_id:
        try:
            try:
                await q.message.delete()
            except Exception:
                pass
            await context.bot.send_photo(
                chat_id=uid,
                photo=qris_file_id,
                caption=text,
                parse_mode="HTML",
                reply_markup=kb
            )
            return
        except Exception as e:
            print(f"[Gift QRIS Manual foto] {e}")

    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""[duitkarung] <b>PEMBAYARAN MANUAL — GIFT</b>
<hr/>
<ul><li>[star] <b>Gift:</b> {emoji_html} {g_label}</li><li>[product] <b>Tujuan:</b> @{target}</li><li>[dolar] <b>Total Transfer:</b> <code>{_fmt(price)}</code></li></ul>
<p>{"" if not rekening_text else rekening_text + chr(10)}{"[card] Scan QRIS di atas atau transfer ke rekening di atas." if qris_file_id and rekening_text else ("[card] Scan QRIS di atas untuk transfer." if qris_file_id else ("[warning] Info rekening belum diset oleh owner." if not rekening_text else ""))}</p>
<ul><li>[warning] Transfer TEPAT <code>{_fmt(price)}</code> sesuai nominal.</li><li>[catatan] Setelah transfer, kirim <b>foto/screenshot bukti transfer</b> ke bot ini.</li><li>[shield] Gift akan diproses & dikirim setelah Owner menyetujui.</li></ul>"""), log_label="AutoRich")


async def handle_gift_bukti_tf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Tangkap foto bukti TF gift (mode manual) → kirim ke owner untuk approve."""
    if context.user_data.get("current_menu_state") != "gift_wait_bukti":
        return False
    if not update.message or not update.message.photo:
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            '<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> Kirim <b>foto/screenshot</b> bukti transfer ya, bukan teks.',
            premium_text("[warning] <b>Format Tidak Sesuai</b>\n\n<blockquote>[catatan] Harap kirim <b>foto/screenshot</b> bukti transfer, bukan pesan teks.\n[panahijo] Ambil screenshot dari aplikasi banking/dompet kamu lalu kirim ke sini.</blockquote>"),
            log_label="BuktiTFGiftWrongType",
        )
        return True

    uid = update.effective_user.id
    uname = update.effective_user.username or str(uid)
    gift_data = context.user_data.get("gift_pending", {})
    if not gift_data:
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            '<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> Sesi gift kadaluarsa. Ulangi dari menu Gift.',
            premium_text("[warning] Sesi gift kadaluarsa. Ulangi dari menu Gift."),
            log_label="GiftSessionExpired",
        )
        context.user_data["current_menu_state"] = "main_menu"
        return True

    # Simpan gift_data ke dict global agar bisa diakses dari callback owner
    gift_manual_pending[uid] = gift_data
    context.user_data["current_menu_state"] = "main_menu"

    g_emoji = gift_data.get("emoji", "🎁")
    g_label = gift_data.get("label", "Gift")
    target  = gift_data.get("target_username", "")
    price   = gift_data.get("price", 0)
    from src.main_menu import _fmt

    try:
        target_owner = OWNER_ID[0] if isinstance(OWNER_ID, list) else OWNER_ID
        kb = styled_inline_keyboard([
            [
                styled_button("✅ Approve", callback_data=f"gift_approve_manual_{uid}", style="success", emoji_name="verified"),
                styled_button("❌ Tolak",   callback_data=f"gift_tolak_manual_{uid}",   style="danger",  emoji_name="batal"),
            ]
        ])
        caption = premium_text(f"""[duitkarung] <b>REQUEST GIFT MANUAL</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[card] <b>User:</b> @{uname} (<code>{uid}</code>)
[star] <b>Gift:</b> {g_emoji} {g_label}
[product] <b>Tujuan:</b> @{target}
[dolar] <b>Nominal:</b> <b>{_fmt(price)}</b></blockquote>""")
        await send_photo_to_owner(context, target_owner, update.message.photo[-1].file_id, caption, kb)
    except Exception as e:
        print(f"[Gift Bukti TF Owner] {e}")

    await notif.send_rich_message_to_chat(
        context.bot, update.effective_chat.id,
        f'<tg-emoji emoji-id="6298432564487001027">✅</tg-emoji> <b>BUKTI TRANSFER TERKIRIM</b>\n\n'
        f'<table bordered striped>\n<tr><th>Info</th><th>Detail</th></tr>\n'
        f'<tr><td>Status</td><td>Bukti transfer gift sudah dikirim ke Owner</td></tr>\n'
        f'<tr><td>Tujuan</td><td>@{target}</td></tr>\n</table>\n\n'
        f'<tg-emoji emoji-id="6093456762113888541">🕐</tg-emoji> Gift akan dikirim setelah disetujui.',
        premium_text(f"""[done] <b>BUKTI TRANSFER TERKIRIM</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Bukti transfer gift kamu sudah dikirim ke Owner.
[waktu] Gift akan dikirim ke @{target} setelah disetujui.</blockquote>"""),
        log_label="BuktiTerkirimGift",
    )
    return True


async def gift_approve_manual_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner approve gift manual → langsung proses kirim gift via MTProto."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q, "⏳ Memproses gift...")

    parts = q.data.split("_")
    target_uid = int(parts[-1])

    gift_data = gift_manual_pending.pop(target_uid, None)
    if not gift_data:
        await safe_answer(q, "Sesi gift ini sudah diproses/kadaluarsa.", show_alert=True)
        return

    try:
        await q.message.edit_caption(
            caption=premium_text(f"[done] <b>GIFT MANUAL DISETUJUI</b>\n<blockquote>User: <code>{target_uid}</code>\n[waktu] Sedang mengirim gift...</blockquote>"),
            parse_mode="HTML"
        )
    except Exception:
        pass

    # Bot ASAL order pembeli (pusat/clone) — semua notif proses/hasil kirim
    # gift harus lewat bot ini, bukan context.bot (bot pusat, karena Owner
    # approve dari pusat).
    buyer_bot = get_origin_bot(gift_data.get("origin_bot_token"), fallback_bot=context.bot)
    await _process_gift_delivery(context, target_uid, gift_data, order_id=f"MANUAL-{int(_time.time())}", paid_via="manual", buyer_bot=buyer_bot)


async def gift_tolak_manual_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner tolak gift manual."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q)

    parts = q.data.split("_")
    target_uid = int(parts[-1])

    gift_data = gift_manual_pending.pop(target_uid, None)
    g_label = gift_data.get("label", "Gift") if gift_data else "Gift"
    price   = gift_data.get("price", 0) if gift_data else 0
    # Bot ASAL order pembeli (pusat/clone) — notif tolak harus lewat bot ini,
    # bukan context.bot (bot pusat, karena Owner tolak dari pusat).
    buyer_bot = get_origin_bot(gift_data.get("origin_bot_token") if gift_data else None, fallback_bot=context.bot)
    from src.main_menu import _fmt

    try:
        rich_html = f"""\
<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>GIFT MANUAL DITOLAK</b>

<table bordered striped>
<tr><th>Info</th><th>Detail</th></tr>
<tr><td>Gift</td><td>{g_label}</td></tr>
<tr><td>Nominal</td><td>{_fmt(price)}</td></tr>
</table>

<tg-emoji emoji-id="5258500400918587241">📝</tg-emoji> Bukti transfer tidak valid atau tidak sesuai. Hubungi CS jika ada pertanyaan."""
        fallback = premium_text(f"""[warning] <b>GIFT MANUAL DITOLAK</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[star] <b>Gift:</b> {g_label}
[dolar] <b>Nominal:</b> {_fmt(price)}
[catatan] Bukti transfer tidak valid atau tidak sesuai. Hubungi CS jika ada pertanyaan.</blockquote>""")
        await notif.send_rich_message_to_chat(
            buyer_bot, target_uid, rich_html, fallback,
            log_label="GiftManualDitolak",
        )
    except Exception as e:
        print(f"[Notif Tolak Gift Manual] {e}")

    try:
        await q.message.edit_caption(
            caption=premium_text(f"[batal] <b>GIFT MANUAL DITOLAK</b>\n<blockquote>User: <code>{target_uid}</code></blockquote>"),
            parse_mode="HTML"
        )
    except Exception:
        pass


async def handle_gift_pay_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bayar gift pakai saldo (deposit_balance) — potong saldo dulu, lalu proses kirim gift."""
    q = update.callback_query
    uid = q.from_user.id

    gift_data = context.user_data.get("gift_pending", {})
    if not gift_data:
        await safe_answer(q, "Sesi kadaluarsa. Ulangi dari menu Gift.", show_alert=True); return

    price = gift_data.get("price", 0)
    user_data = get_user(uid)
    saldo = user_data[3] if user_data else 0
    from src.main_menu import _fmt

    if not user_data or saldo < price:
        await safe_answer(q, f"❌ Saldo tidak cukup! Saldo: {_fmt(saldo)}, dibutuhkan: {_fmt(price)}", show_alert=True)
        return

    await safe_answer(q, "⏳ Memotong saldo & memproses gift...")

    # Potong saldo dulu (sesuai keputusan: potong dulu, auto-refund jika gagal kirim)
    update_balance(uid, belance_delta=-price)

    try:
        await q.message.delete()
    except Exception:
        pass

    await _process_gift_delivery(context, uid, gift_data, order_id=f"SALDO-{int(_time.time())}", paid_via="saldo")


async def _process_gift_delivery(context: ContextTypes.DEFAULT_TYPE, uid: int, gift_data: dict, order_id: str, paid_via: str = "qris", buyer_bot=None):
    """Fungsi bersama: proses kirim gift via MTProto, catat DB, notif, dan handle refund jika gagal (khusus saldo).

    buyer_bot: bot yang dipakai untuk SEMUA pesan ke pembeli (loading, sukses,
    gagal) — default context.bot (sudah benar untuk alur bayar QRIS/saldo yang
    berjalan real-time di chat pembeli sendiri). WAJIB diisi eksplisit dengan
    bot ASAL order (lihat get_origin_bot()) untuk alur approve MANUAL, karena di
    situ context.bot adalah bot pusat (dipakai Owner approve), bukan bot tempat
    pembeli order.
    """
    if buyer_bot is None:
        buyer_bot = context.bot
    target    = gift_data.get("target_username", "")
    gift_id   = gift_data.get("gift_id", "")
    is_anon   = gift_data.get("anon", False)
    g_emoji   = gift_data.get("emoji", "🎁")
    g_label   = gift_data.get("label", "Gift")
    ceid      = gift_data.get("custom_emoji_id", "")
    price     = gift_data.get("price", 0)
    base_price = gift_data.get("base_price", price)  # Fallback ke price jika tidak ada
    custom_msg = gift_data.get("message", "")
    from src.main_menu import _fmt
    emoji_html = f"<tg-emoji emoji-id='{ceid}'>{g_emoji}</tg-emoji>" if ceid else g_emoji

    status_msg = await notif.send_rich_message_to_chat(
        buyer_bot, uid,
        premium_text(f"""
[waktu] <b>MENGHUBUNGKAN KE MTPROTO...</b>
<hr/>
<ul><li>[done] Pembayaran <code>{_fmt(price)}</code> berhasil diterima.</li><li>[star] Gift: {emoji_html} {g_label}</li><li>[product] Tujuan: @{target}</li><li>[waktu] Sedang memproses pengiriman gift, mohon tunggu...</li></ul>
"""),
        premium_text(f"""
[waktu] <b>MENGHUBUNGKAN KE MTPROTO...</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[done] Pembayaran <code>{_fmt(price)}</code> berhasil diterima.
[star] Gift: {emoji_html} {g_label}
[product] Tujuan: @{target}

[waktu] Sedang memproses pengiriman gift, mohon tunggu...</blockquote>
"""),
        log_label="GiftDeliveryLoading",
    )

    gift_ok = False
    gift_err = ""
    try:
        from src.gift_sender import send_star_gift_with_retry
        from config import API_ID, API_HASH
        await send_star_gift_with_retry(
            api_id=API_ID, api_hash=API_HASH,
            target_username=target, gift_id=gift_id,
            message_text=custom_msg, hide_name=is_anon, include_upgrade=False
        )
        gift_ok = True
    except Exception as e:
        gift_err = str(e)
        print(f"[Gift MTProto] Error: {e}")

    # Refund otomatis jika dibayar pakai saldo dan gagal kirim
    if not gift_ok and paid_via == "saldo":
        update_balance(uid, belance_delta=price)

    # Catat ke DB
    try:
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS gift_orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER, order_id TEXT,
                gift_emoji TEXT, gift_label TEXT, gift_custom_emoji_id TEXT,
                target_username TEXT, amount INTEGER, gift_id TEXT,
                is_anon INTEGER DEFAULT 0, recipient_msg TEXT,
                status TEXT DEFAULT 'pending', created_at INTEGER
            )
        """)
        st = "success" if gift_ok else "failed"
        cursor.execute(
            "INSERT INTO gift_orders (user_id, order_id, gift_emoji, gift_label, gift_custom_emoji_id, target_username, amount, gift_id, is_anon, recipient_msg, status, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (uid, order_id, g_emoji, g_label, ceid, target, price, gift_id, int(is_anon), custom_msg, st, int(_time.time()))
        )
        conn.commit()
    except Exception as e:
        print(f"[Gift DB] {e}")

    if context is not None:
        context.user_data["current_menu_state"] = "page2_gift"

    if gift_ok:
        text = premium_text(f"""
[done] <b>GIFT BERHASIL DIKIRIM! 🎉</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[star] <b>Gift:</b> {emoji_html} {g_label}
[product] <b>Dikirim ke:</b> @{target}
[dolar] <b>Pembayaran:</b> <code>{_fmt(price)}</code>

[star] Terima kasih sudah order gift di layanan kami! 🙏</blockquote>
""")
        try:
            # Pakai buyer_bot: kalau pembeli cuma pernah chat lewat bot clone
            # dan tidak pernah start bot pusat, get_chat via context.bot (pusat)
            # akan gagal (bot itu belum pernah "kenal" user tsb).
            user_obj = await buyer_bot.get_chat(uid)
            buyer_uname = user_obj.username or f"id{uid}"
        except Exception:
            buyer_uname = f"id{uid}"
        try:
            _ug = get_user(uid)
            _sisa_gift = _ug[3] if _ug and len(_ug) > 3 else None  # FIX: belance_balance (saldo yang bisa dipakai), bukan deposit_balance
            await notif.notif_pembelian_gift_channel(
                context.bot, uid, buyer_uname, target, g_label, g_emoji, order_id, price,
                saldo_sisa=_sisa_gift, gift_custom_emoji_id=ceid
            )
        except Exception as _e:
            print(f"[Notif Gift Channel] {_e}")
        try:
            # buyer_bot (bukan context.bot) supaya deteksi clone di dalam
            # process_transaction_commission() akurat — lihat penjelasan di
            # session_approve_manual_handler untuk alasan lengkapnya.
            await clone_system.process_transaction_commission(
                buyer_bot, DB_PATH, uid, order_id, "Confes Gift", price, base_price
            )
        except Exception as _ce:
            print(f"[CloneCommission] {_ce}")
    else:
        refund_note = "\n[shield] Saldo kamu sudah <b>direfund otomatis</b>." if paid_via == "saldo" else ""
        text = premium_text(f"""
[warning] <b>GIFT GAGAL TERKIRIM</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[dolar] Pembayaran sudah diterima (<code>{_fmt(price)}</code>).
Namun gift gagal terkirim ke @{target}.{refund_note}

[catatan] Error: <code>{gift_err[:200]}</code>

[spikerbiru] Silakan hubungi CS untuk refund atau pengiriman ulang.</blockquote>
""")
        try:
            owner_ids = OWNER_ID.all_ids if hasattr(OWNER_ID,"all_ids") else [OWNER_ID]
            owner_html = f"""\
<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>GIFT GAGAL TERKIRIM</b>

<table bordered striped>
<tr><th>Info</th><th>Detail</th></tr>
<tr><td>Order</td><td><code>{order_id}</code></td></tr>
<tr><td>User</td><td><code>{uid}</code></td></tr>
<tr><td>Target</td><td>@{target}</td></tr>
<tr><td>Gift</td><td>{f'<tg-emoji emoji-id="{ceid}">{g_emoji}</tg-emoji>' if ceid else g_emoji} {g_label}</td></tr>
</table>

<tg-emoji emoji-id="5258500400918587241">📝</tg-emoji> Error: <code>{gift_err[:300]}</code>"""
            owner_text = premium_text(f"""
[warning] <b>GIFT GAGAL TERKIRIM</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] <b>Order:</b> <code>{order_id}</code>
[product] <b>User:</b> <code>{uid}</code>
[product] <b>Target:</b> @{target}
[star] <b>Gift:</b> {g_emoji} {g_label}

[warning] Error: <code>{gift_err[:300]}</code></blockquote>
""")
            # Notif kegagalan ke Owner SELALU lewat bot pusat (sama seperti pola
            # owner_notify_bot()), supaya Owner tetap kebagian notif walau
            # transaksinya terjadi di clone bot manapun.
            _owner_bot = owner_notify_bot(context)
            for oid in owner_ids:
                await notif.send_rich_message_to_chat(_owner_bot, oid, owner_html, owner_text, log_label="GiftFailedOwner")
        except Exception:
            pass

    if context is not None:
        context.user_data.pop("gift_pending", None)

    kb = styled_inline_keyboard([
        [styled_button("🎁 Order Gift Lagi", callback_data="menu_page_2_back", style="success", emoji_name="star")],
        [styled_button("Menu Utama",          callback_data="menu_back",    style="danger",  emoji_name="back")]
    ])
    await notif.edit_rich_message(
        buyer_bot, uid, status_msg,
        text, text,
        reply_markup=kb, log_label="GiftDeliveryResult",
    )

async def handle_gift_cek_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cek status bayar QRIS otomatis (Pakasir) → jika lunas, proses kirim gift via MTProto."""
    q = update.callback_query
    uid = q.from_user.id

    gift_data = context.user_data.get("gift_pending", {})
    order_id  = q.data.replace("gift_cek_", "")
    amount    = gift_data.get("qris_amount", gift_data.get("price", 0))

    is_paid = await check_payment_status(order_id, amount)

    if not is_paid:
        await safe_answer(q, "❌ Pembayaran belum diterima. Tunggu lalu cek lagi.", show_alert=True)
        return

    # ── ATOMIC GUARD: cegah gift dikirim 2x jika user spam klik ─────────────
    try:
        import sqlite3 as _sq3
        _ac = _sq3.connect(DB_PATH)
        _ac.execute(
            "UPDATE pending_payments SET status='paid' WHERE (id=? OR order_id=?) AND status='pending'",
            (order_id, order_id)
        )
        _changed = _ac.execute("SELECT changes()").fetchone()[0]
        _ac.commit()
        _ac.close()
    except Exception as _ae:
        print(f"[Error Atomic Guard Gift]: {_ae}")
        _changed = 0

    if not _changed:
        # FIX: sebelumnya di sini SELALU cuma nampilin "sudah diproses
        # sebelumnya" tanpa ngecek apakah gift-nya BENERAN kekirim atau
        # malah stuck (paid tapi gagal/kepotong di tengah). Sekarang dicek
        # dulu ke gift_orders biar user dapet info yang akurat & ada arah
        # jelas, bukan jalan buntu.
        try:
            cursor.execute("SELECT status FROM gift_orders WHERE order_id=? ORDER BY id DESC LIMIT 1", (order_id,))
            _row = cursor.fetchone()
        except Exception:
            _row = None
        if _row and _row[0] == "success":
            await safe_answer(q, "✅ Order ini sudah berhasil diproses & gift sudah terkirim sebelumnya.", show_alert=True)
        elif _row and _row[0] == "failed":
            await safe_answer(q, "⚠️ Pembayaran sudah diterima tapi pengiriman gift sebelumnya gagal. Hubungi CS untuk pengiriman ulang/refund.", show_alert=True)
        else:
            # Belum ada baris gift_orders sama sekali → proses sebelumnya
            # kepotong (bot restart/error) sebelum sempat selesai. Coba
            # selesaikan sekarang juga alih-alih nyuruh user nunggu tanpa arah.
            await safe_answer(q, "⏳ Pembayaran sudah tercatat, menyelesaikan pengiriman gift...", show_alert=True)
            try:
                import json as _json
                cursor.execute("SELECT gift_json FROM pending_payments WHERE id=? OR order_id=?", (order_id, order_id))
                _gj_row = cursor.fetchone()
                _gd = _json.loads(_gj_row[0]) if _gj_row and _gj_row[0] else gift_data
            except Exception:
                _gd = gift_data
            await _process_gift_delivery(context, uid, _gd, order_id=order_id, paid_via="qris")
        return
    # ─────────────────────────────────────────────────────────────────────────

    await safe_answer(q, "⏳ Pembayaran diterima, mengirim gift...")

    # FIX: hapus foto QR dulu (biar gak nempel terus), ganti pesan baru full-text
    qr_path = gift_data.get("qr_path", "")
    try:
        await q.message.delete()
    except Exception:
        pass
    if qr_path and os.path.exists(qr_path):
        try:
            os.remove(qr_path)
        except Exception:
            pass

    # ── Pembayaran lunas → proses kirim gift via MTProto (fungsi bersama) ──
    await _process_gift_delivery(context, uid, gift_data, order_id=order_id, paid_via="qris")


async def show_gift_owner_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q)
    from src.main_menu import create_owner_gift_menu
    text = premium_text("""
[crown] <b>OWNER — GIFT MENU</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[gear] Kelola fitur Auto Order Gift:
[panahijo] Set Harga: ubah harga tiap gift
[panahijo] ON/OFF: aktifkan atau nonaktifkan gift per item</blockquote>
""")
    await fast_edit(q, text, reply_markup=create_owner_gift_menu(), parse_mode="HTML", rich_html=premium_text(f"""[crown] <b>OWNER — GIFT MENU</b>
<hr/>
<ul><li>[gear] Kelola fitur Auto Order Gift:</li><li>[panahijo] Set Harga: ubah harga tiap gift</li><li>[panahijo] ON/OFF: aktifkan atau nonaktifkan gift per item</li></ul>"""), log_label="AutoRich")


async def show_gift_owner_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid): await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q)
    from src.main_menu import create_owner_gift_toggle_menu
    text = premium_text("[gear] <b>ON/OFF GIFT</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Klik gift untuk toggle ON/OFF.\n🟢 = aktif | 🔴 = nonaktif</blockquote>")
    await fast_edit(q, text, reply_markup=create_owner_gift_toggle_menu(), parse_mode="HTML", rich_html=premium_text(f"""[gear] <b>ON/OFF GIFT</b>
<hr/>
<ul><li>Klik gift untuk toggle ON/OFF.</li><li>🟢 = aktif | 🔴 = nonaktif</li></ul>"""), log_label="AutoRich")


async def handle_gift_owner_toggle_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid): await safe_answer(q, "Owner only!", show_alert=True); return
    idx = int(q.data.split("_")[-1])
    from src.main_menu import toggle_gift_enabled, is_gift_enabled, GIFT_ITEMS
    toggle_gift_enabled(idx)
    st = "ON ✅" if is_gift_enabled(idx) else "OFF ❌"
    await safe_answer(q, f"{GIFT_ITEMS[idx]['emoji']} {GIFT_ITEMS[idx]['label']} → {st}")
    from src.main_menu import create_owner_gift_toggle_menu
    await q.edit_message_reply_markup(reply_markup=create_owner_gift_toggle_menu())


async def handle_gift_owner_toggle_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid): await safe_answer(q, "Owner only!", show_alert=True); return
    enable = (q.data == "gift_owner_toggle_onall")
    from src.main_menu import set_gift_enabled, GIFT_ITEMS, create_owner_gift_toggle_menu
    for i in range(len(GIFT_ITEMS)):
        set_gift_enabled(i, enable)
    await safe_answer(q, "✅ Semua gift ON" if enable else "❌ Semua gift OFF")
    await q.edit_message_reply_markup(reply_markup=create_owner_gift_toggle_menu())


async def show_gift_owner_setprice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid): await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q)
    from src.main_menu import create_owner_gift_setprice_menu
    text = premium_text("[dolar] <b>SET HARGA GIFT</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Pilih gift yang ingin diubah harganya.</blockquote>")
    await fast_edit(q, text, reply_markup=create_owner_gift_setprice_menu(), parse_mode="HTML", rich_html=premium_text(f"""[dolar] <b>SET HARGA GIFT</b>
<hr/>
<p>Pilih gift yang ingin diubah harganya.</p>"""), log_label="AutoRich")


async def show_gift_owner_price_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid): await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q)
    idx = int(q.data.split("_")[-1])
    from src.main_menu import GIFT_ITEMS, get_gift_price, _fmt, create_owner_gift_price_actions
    g = GIFT_ITEMS[idx]
    price = get_gift_price(idx)
    ceid = g.get("custom_emoji_id","")
    emoji_html = f"<tg-emoji emoji-id='{ceid}'>{g['emoji']}</tg-emoji>" if ceid else g['emoji']
    text = premium_text(f"""
[dolar] <b>DETAIL HARGA GIFT</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[star] Gift: {emoji_html} {g['label']}
[catatan] Gift ID: <code>{g['gift_id']}</code>
[dolar] Harga Saat Ini: <code>{_fmt(price)}</code>

Pilih aksi:</blockquote>
""")
    await fast_edit(q, text, reply_markup=create_owner_gift_price_actions(idx), parse_mode="HTML", rich_html=premium_text(f"""[dolar] <b>DETAIL HARGA GIFT</b>
<hr/>
<ul><li>[star] Gift: {emoji_html} {g['label']}</li><li>[catatan] Gift ID: <code>{g['gift_id']}</code></li><li>[dolar] Harga Saat Ini: <code>{_fmt(price)}</code></li></ul>
<p>Pilih aksi:</p>"""), log_label="AutoRich")


async def handle_gift_owner_price_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid): await safe_answer(q, "Owner only!", show_alert=True); return
    idx = int(q.data.split("_")[-1])
    from src.main_menu import reset_gift_price, GIFT_ITEMS
    reset_gift_price(idx)
    await safe_answer(q, f"✅ Harga {GIFT_ITEMS[idx]['emoji']} direset ke default.")
    await show_gift_owner_setprice(update, context)


async def handle_gift_owner_price_edit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid): await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q)
    idx = int(q.data.split("_")[-1])
    from src.main_menu import GIFT_ITEMS, get_gift_price, _fmt
    g = GIFT_ITEMS[idx]
    price = get_gift_price(idx)
    context.user_data["gift_owner_edit_idx"] = idx
    context.user_data["current_menu_state"]  = "gift_owner_wait_price"
    text = premium_text(f"""
[catatan] <b>UBAH HARGA GIFT</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[star] Gift: {g['emoji']} {g['label']}
[dolar] Harga Sekarang: <code>{_fmt(price)}</code>

Kirim angka harga baru (contoh: <code>10000</code>)</blockquote>
""")
    kb = styled_inline_keyboard([[styled_button("Batal", callback_data="gift_owner_setprice", style="danger", emoji_name="back")]])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""[catatan] <b>UBAH HARGA GIFT</b>
<hr/>
<ul><li>[star] Gift: {g['emoji']} {g['label']}</li><li>[dolar] Harga Sekarang: <code>{_fmt(price)}</code></li></ul>
<p>Kirim angka harga baru (contoh: <code>10000</code>)</p>"""), log_label="AutoRich")


async def handle_gift_owner_price_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Tangkap input harga baru dari owner."""
    uid = update.effective_user.id
    if not is_owner(uid): return False
    if context.user_data.get("current_menu_state") != "gift_owner_wait_price": return False

    raw = update.message.text.strip().replace(".", "").replace(",", "")
    if not raw.isdigit() or int(raw) < 100:
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            '<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> Harga tidak valid. Kirim angka saja (min. 100).',
            premium_text("[warning] Harga tidak valid. Kirim angka saja (min. 100)."),
            log_label="OwnerGiftPriceInvalid",
        )
        return True

    idx = context.user_data.get("gift_owner_edit_idx", -1)
    from src.main_menu import GIFT_ITEMS, set_gift_price, get_gift_price, _fmt
    if idx < 0 or idx >= len(GIFT_ITEMS):
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            '<tg-emoji emoji-id="5447644880824181073">⚠️</tg-emoji> <b>Gift tidak ditemukan.</b>',
            premium_text("[warning] <b>Gift tidak ditemukan.</b>"),
            log_label="OwnerGiftNotFound",
        )
        return True

    val = int(raw)
    set_gift_price(idx, val)
    context.user_data.pop("gift_owner_edit_idx", None)
    context.user_data["current_menu_state"] = "page2_gift"

    g = GIFT_ITEMS[idx]
    await notif.send_rich_message_to_chat(
        context.bot, update.effective_chat.id,
        f'<tg-emoji emoji-id="6298432564487001027">✅</tg-emoji> Harga <b>{g["emoji"]} {g["label"]}</b> berhasil diubah ke <code>{_fmt(val)}</code>',
        premium_text(f"[done] Harga <b>{g['emoji']} {g['label']}</b> berhasil diubah ke <code>{_fmt(val)}</code>"),
        log_label="OwnerGiftPriceUpdated",
    )
    return True


# Handler /gift_login untuk setup akun MTProto pengirim
async def cmd_gift_login(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            '<tg-emoji emoji-id="5879895758202735862">🔒</tg-emoji> <b>Owner only!</b>',
            premium_text("[gembok1] <b>Owner only!</b>"),
            log_label="GiftLoginOwnerOnly",
        )
        return
    from src.gift_sender import start_gift_login
    from config import API_ID, API_HASH
    msg = await start_gift_login(uid, API_ID, API_HASH)
    context.user_data["current_menu_state"] = "gift_login_wait_phone"
    await notif.send_rich_message_to_chat(
        context.bot, update.effective_chat.id,
        f'<tg-emoji emoji-id="6339081565200452504">⬇️</tg-emoji> <b>CONNECT MTPROTO / KURIR</b>\n\n{msg}',
        premium_text(f"[download] {msg}"),
        log_label="GiftLoginStart",
    )


async def handle_gift_owner_login_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Versi tombol dari cmd_gift_login — dipanggil dari menu Owner Gift, bukan command /gift_login."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q, "⏳ Memulai koneksi MTProto...")
    from src.gift_sender import start_gift_login
    from config import API_ID, API_HASH
    msg = await start_gift_login(uid, API_ID, API_HASH)
    context.user_data["current_menu_state"] = "gift_login_wait_phone"
    text = premium_text(f"""
[download] <b>CONNECT MTPROTO / KURIR</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>{msg}</blockquote>
""")
    kb = styled_inline_keyboard([[styled_button("Batal", callback_data="menu_owner", style="danger", emoji_name="back")]])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""[download] <b>CONNECT MTPROTO / KURIR</b>
<hr/>
<p>{msg}</p>"""), log_label="AutoRich")


async def handle_gift_login_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    if not is_owner(uid): return False
    state = context.user_data.get("current_menu_state","")
    if state not in ("gift_login_wait_phone","gift_login_wait_code","gift_login_wait_2fa"):
        return False

    from src.gift_sender import gift_login_phone, gift_login_code, gift_login_2fa, is_gift_login_pending
    from config import API_ID, API_HASH
    text_in = update.message.text.strip()

    if state == "gift_login_wait_phone":
        result = await gift_login_phone(uid, text_in)
        if "OTP" in result:
            context.user_data["current_menu_state"] = "gift_login_wait_code"
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            f'<tg-emoji emoji-id="5258500400918587241">📝</tg-emoji> {result}',
            premium_text(f"[catatan] {result}"), log_label="GiftLoginPhoneStep",
        )
        return True

    if state == "gift_login_wait_code":
        result = await gift_login_code(uid, text_in, API_ID, API_HASH)
        if "2FA" in result:
            context.user_data["current_menu_state"] = "gift_login_wait_2fa"
        elif "berhasil" in result.lower():
            context.user_data["current_menu_state"] = ""
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            f'<tg-emoji emoji-id="5258500400918587241">📝</tg-emoji> {result}',
            premium_text(f"[catatan] {result}"), log_label="GiftLoginCodeStep",
        )
        return True

    if state == "gift_login_wait_2fa":
        result = await gift_login_2fa(uid, text_in)
        if "berhasil" in result.lower():
            context.user_data["current_menu_state"] = ""
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            f'<tg-emoji emoji-id="5258500400918587241">📝</tg-emoji> {result}',
            premium_text(f"[catatan] {result}"), log_label="GiftLogin2FAStep",
        )
        return True

    return False


# ==================== WITHDRAW HANDLERS ====================

async def withdraw_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    user = get_user(uid)
    
    if not user or user[3] <= 0:
        await fast_edit(
            q, premium_text("[warning] <b>Balance Masih Kosong!</b>\n\n<blockquote>[catatan] Jual/simpan session dulu untuk mendapatkan saldo.</blockquote>"),
            reply_markup=create_back_button(), parse_mode="HTML",
            rich_html=f"{emoji('warning')} <b>Balance Masih Kosong!</b>\n<hr/>\n<p>{emoji('catatan')} Jual/simpan session dulu untuk mendapatkan saldo.</p>",
            log_label="WithdrawEmpty",
        )
        return
    
    text = f"""
[duitkarung] <b>WITHDRAW</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[dolar] Balance: <code>{format_currency(user[3])}</code>

[panahijo] Pilih metode pembayaran:</blockquote>
"""
    rich_html = f"""\
{emoji('duitkarung')} <b>WITHDRAW</b>
<hr/>
<p>{emoji('dolar')} Balance: <code>{format_currency(user[3])}</code></p>
<p>{emoji('panahijo')} Pilih metode pembayaran:</p>"""
    await fast_edit(q, premium_text(text), reply_markup=create_payment_methods_keyboard(), parse_mode="HTML", rich_html=rich_html, log_label="WithdrawMenu")

async def process_withdraw_method(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    method = q.data.split("_")[1].upper()
    
    context.user_data['withdraw_method'] = method
    user_states[uid] = {'action': 'withdraw_number', 'mode': 'withdraw'}
    
    text = f"""
[duitkarung] <b>WITHDRAW - {method}</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Masukkan nomor pembayaran:
Contoh: <code>0852xxxx</code> atau <code>08XXXX</code></blockquote>
"""
    rich_html = f"""\
{emoji('duitkarung')} <b>WITHDRAW - {method}</b>
<hr/>
<p>{emoji('catatan')} Masukkan nomor pembayaran:</p>
<p>Contoh: <code>0852xxxx</code> atau <code>08XXXX</code></p>"""
    await fast_edit(q, premium_text(text), reply_markup=create_cancel_button(), parse_mode="HTML", rich_html=rich_html, log_label="WithdrawMethod")

# ==================== OWNER PANEL (SEND NEW) ====================
async def send_owner_panel_new(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Kirim Owner Panel sebagai pesan baru — dari ReplyKeyboard Owner."""
    if not is_owner(user_id):
        return
    menu_html = """\
<tg-emoji emoji-id="5769547529993588669">👑</tg-emoji> <b>OWNER MENU</b>

<tg-emoji emoji-id="6298432564487001027">✅</tg-emoji> Selamat datang di panel kontrol owner.
<tg-emoji emoji-id="5258500400918587241">📝</tg-emoji> Silakan pilih fitur pengelolaan bot sesuai kebutuhan operasional."""
    text = premium_text("""
[crown] <b>OWNER MENU</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[verified] Selamat datang di panel kontrol owner.
[catatan] Silakan pilih fitur pengelolaan bot sesuai kebutuhan operasional.</blockquote>
""")
    await notif.send_rich_message_to_chat(
        context.bot, user_id, menu_html, text,
        reply_markup=create_owner_menu(context),
        log_label="OwnerPanel",
    )

# ==================== OWNER ADD SALDO MANUAL ====================
def create_owner_add_saldo_keyboard() -> InlineKeyboardMarkup:
    return styled_inline_keyboard([
        [styled_button("Kembali ke Owner Menu", callback_data="menu_owner", style="danger", emoji_name="back")]
    ])

async def owner_backup_data_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Backup session_stock.db + WAL files (fix: dulu cuma copy .db, sekarang lengkap 3 file)."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)

    await fast_edit(
        q,
        premium_text("""
[download] <b>BACKUP DATA</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[loading] Sedang menyiapkan file backup (db + shm + wal)...</blockquote>
"""),
        parse_mode="HTML",
        rich_html=f"{emoji('download')} <b>BACKUP DATA</b>\n<hr/>\n<p>{emoji('loading')} Sedang menyiapkan file backup (db + shm + wal)...</p>",
        log_label="OwnerBackupLoading",
    )

    try:
        await send_stock_backup(context, chat_id=q.message.chat_id, trigger="manual")

        await fast_edit(
            q,
            premium_text("""
[done] <b>BACKUP SELESAI</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] File backup (.zip berisi db+shm+wal) sudah dikirim ke chat ini.
[shield] Simpan baik-baik file tersebut.</blockquote>
"""),
            reply_markup=create_owner_menu(context), parse_mode="HTML",
            rich_html=f"{emoji('done')} <b>BACKUP SELESAI</b>\n<hr/>\n<p>{emoji('catatan')} File backup (.zip berisi db+shm+wal) sudah dikirim ke chat ini.</p>\n<p>{emoji('shield')} Simpan baik-baik file tersebut.</p>",
            log_label="OwnerBackupDone",
        )

    except Exception as e:
        err_msg = html.escape(str(e))
        await fast_edit(
            q,
            premium_text(f"""
[warning] <b>BACKUP GAGAL</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Error: <code>{err_msg}</code></blockquote>
"""),
            reply_markup=create_owner_menu(context), parse_mode="HTML",
            rich_html=f"{emoji('warning')} <b>BACKUP GAGAL</b>\n<hr/>\n<p>{emoji('catatan')} Error: <code>{err_msg}</code></p>",
            log_label="OwnerBackupFailed",
        )


async def owner_add_saldo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tampilkan form add saldo manual oleh owner."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)
    user_states[uid] = {'action': 'owner_add_saldo', 'mode': 'owner'}
    text = premium_text("""
[dolar] <b>ADD SALDO MANUAL</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Ketik perintah add saldo dengan format:
[panahijo] <code>@username jumlah</code>
[panahijo] <code>user_id jumlah</code>

Contoh:
<code>@pretygirrls 100000</code>
<code>@pretygirrls 100k</code>
<code>974468120 1.5jt</code>

[warning] Gunakan angka murni atau format singkat (k = ribu, jt/m = juta).</blockquote>
""")
    rich_html = f"""\
{emoji('dolar')} <b>ADD SALDO MANUAL</b>
<hr/>
<p>{emoji('catatan')} Ketik perintah add saldo dengan format:</p>
<ul>
<li><code>@username jumlah</code></li>
<li><code>user_id jumlah</code></li>
</ul>
<p><b>Contoh:</b></p>
<ul>
<li><code>@pretygirrls 100000</code></li>
<li><code>@pretygirrls 100k</code></li>
<li><code>974468120 1.5jt</code></li>
</ul>
<p>{emoji('warning')} Gunakan angka murni atau format singkat (k = ribu, jt/m = juta).</p>"""
    await fast_edit(q, text, reply_markup=create_owner_add_saldo_keyboard(), parse_mode="HTML", rich_html=rich_html, log_label="OwnerAddSaldo")

async def owner_kurangi_saldo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tampilkan form kurangi saldo manual oleh owner."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)
    user_states[uid] = {'action': 'owner_kurangi_saldo', 'mode': 'owner'}
    text = premium_text("""
[warning] <b>KURANGI SALDO MANUAL</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Ketik perintah kurangi saldo dengan format:
[panahijo] <code>@username jumlah</code>
[panahijo] <code>user_id jumlah</code>

Contoh:
<code>@pretygirrls 100000</code>
<code>@pretygirrls 100k</code>
<code>974468120 1.5jt</code>

[warning] <b>Saldo akan dikurangi dari belance_balance.</b>
[error] Pastikan saldo user mencukupi sebelum mengurangi.</blockquote>
""")
    rich_html = f"""\
{emoji('warning')} <b>KURANGI SALDO MANUAL</b>
<hr/>
<p>{emoji('catatan')} Ketik perintah kurangi saldo dengan format:</p>
<ul>
<li><code>@username jumlah</code></li>
<li><code>user_id jumlah</code></li>
</ul>
<p><b>Contoh:</b></p>
<ul>
<li><code>@pretygirrls 100000</code></li>
<li><code>@pretygirrls 100k</code></li>
<li><code>974468120 1.5jt</code></li>
</ul>
<p>{emoji('warning')} <b>Saldo akan dikurangi dari belance_balance.</b></p>
<p>{emoji('error')} Pastikan saldo user mencukupi sebelum mengurangi.</p>"""
    kb = styled_inline_keyboard([
        [styled_button("Kembali ke Owner Menu", callback_data="menu_owner", style="danger", emoji_name="back")]
    ])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=rich_html, log_label="OwnerKurangiSaldo")

async def _parse_nominal(raw: str) -> int:
    """Parse nominal dari string seperti 100k, 1.5jt, 50000, dll."""
    raw = raw.lower().strip().replace(",", ".").replace("rp", "").strip()
    multiplier = 1
    if raw.endswith("jt") or raw.endswith("m"):
        multiplier = 1_000_000
        raw = raw.rstrip("jt").rstrip("m").strip()
    elif raw.endswith("k"):
        multiplier = 1_000
        raw = raw.rstrip("k").strip()
    try:
        return int(float(raw) * multiplier)
    except:
        return 0

# ==================== OWNER HANDLERS ====================
async def owner_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    # Simpan halaman sebelumnya ke history
    push_nav(context, "owner_menu")
    
    text = premium_text("""
[crown] <b>OWNER MENU</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[verified] Selamat datang di panel kontrol owner.
[catatan] Silakan pilih fitur pengelolaan bot sesuai kebutuhan operasional.</blockquote>
""")
    await fast_edit(q, text, reply_markup=create_owner_menu(context), parse_mode="HTML", rich_html=premium_text(f"""[crown] <b>OWNER MENU</b>
<hr/>
<ul><li>[verified] Selamat datang di panel kontrol owner.</li><li>[catatan] Silakan pilih fitur pengelolaan bot sesuai kebutuhan operasional.</li></ul>"""), log_label="AutoRich")

async def owner_stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if uid != OWNER_ID:
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)
    
    stock = get_stock_count()
    sold = get_sold_count()
    income = get_total_income()
    users = get_total_users()
    
    text = premium_text(f"""
[grafik] <b>STATISTIK BOT</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[crown] <b>Total Pengguna:</b> <code>{users}</code>
[product] <b>Stok Tersedia:</b> <code>{stock}</code>
[done] <b>Total Terjual:</b> <code>{sold}</code>
[dolar] <b>Total Pendapatan:</b> <code>{format_currency(income)}</code></blockquote>
""")
    rich_html = f"""\
{emoji('grafik')} <b>STATISTIK BOT</b>

<table bordered striped>
<tr><th>Metrik</th><th>Nilai</th></tr>
<tr><td>Total Pengguna</td><td><code>{users}</code></td></tr>
<tr><td>Stok Tersedia</td><td><code>{stock}</code></td></tr>
<tr><td>Total Terjual</td><td><code>{sold}</code></td></tr>
<tr><td>Total Pendapatan</td><td><code>{format_currency(income)}</code></td></tr>
</table>"""
    await fast_edit(q, text, reply_markup=create_owner_menu(context), parse_mode="HTML", rich_html=rich_html, log_label="OwnerStats")

async def owner_clone_manage_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tampilkan list clone bot pending (menunggu approve) & aktif, dari Owner Panel."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)

    pending = clone_system.get_pending_clones(DB_PATH)
    active = clone_system.get_active_clones(DB_PATH)

    if not pending and not active:
        rich = premium_text("""\
[roket] <b>KELOLA CLONE BOT</b>
<hr/>
<p>[catatan] Belum ada permintaan clone bot sama sekali.</p>""")
        fallback = premium_text("""\
[roket] <b>KELOLA CLONE BOT</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Belum ada permintaan clone bot sama sekali.</blockquote>
""")
        await fast_edit(q, fallback, reply_markup=create_owner_menu(context), parse_mode="HTML", rich_html=rich, log_label="OwnerCloneManageEmpty")
        return

    pending_rows = "".join(
        f"<tr><td><code>{c['id']}</code></td><td><code>{c['owner_id']}</code></td><td>@{c['bot_username']}</td></tr>"
        for c in pending
    ) or "<tr><td colspan='3'>Tidak ada</td></tr>"

    active_rows = "".join(
        f"<tr><td><code>{c['id']}</code></td><td>@{c['bot_username']}</td><td>{c['komisi_persen']}%</td></tr>"
        for c in active
    ) or "<tr><td colspan='3'>Tidak ada</td></tr>"

    rich = premium_text(f"""\
[roket] <b>KELOLA CLONE BOT</b>
<hr/>
<p>[waktu] <b>Menunggu Persetujuan ({len(pending)})</b></p>
<table bordered striped>
<tr><th>ID</th><th>Owner</th><th>Bot</th></tr>
{pending_rows}
</table>
<hr/>
<p>[verified] <b>Clone Aktif ({len(active)})</b></p>
<table bordered striped>
<tr><th>ID</th><th>Bot</th><th>Komisi</th></tr>
{active_rows}
</table>
<p>[panahijo] Ketik <code>/approveclone [id] [komisi%]</code> untuk menyetujui.</p>
<p>[panahijo] Ketik <code>/rejectclone [id]</code> untuk menolak.</p>""")

    fallback = premium_text(f"""\
[roket] <b>KELOLA CLONE BOT</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>
Menunggu Persetujuan ({len(pending)}):
{chr(10).join(f"  ID {c['id']} — owner {c['owner_id']} — @{c['bot_username']}" for c in pending) or "  Tidak ada"}

Clone Aktif ({len(active)}):
{chr(10).join(f"  ID {c['id']} — @{c['bot_username']} — komisi {c['komisi_persen']}%" for c in active) or "  Tidak ada"}

Ketik /approveclone [id] [komisi%] untuk menyetujui.
Ketik /rejectclone [id] untuk menolak.</blockquote>
""")

    await fast_edit(q, fallback, reply_markup=create_owner_menu(context), parse_mode="HTML", rich_html=rich, log_label="OwnerCloneManage")


async def owner_wd_manage_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tampilkan list permintaan withdraw clone yang masih pending, dari Owner Panel."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)

    pending_wd = clone_system.get_pending_withdraws(DB_PATH)

    if not pending_wd:
        rich = premium_text("""\
[dolar] <b>KELOLA WITHDRAW CLONE</b>
<hr/>
<p>[catatan] Tidak ada permintaan withdraw yang menunggu diproses.</p>""")
        fallback = premium_text("""\
[dolar] <b>KELOLA WITHDRAW CLONE</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Tidak ada permintaan withdraw yang menunggu diproses.</blockquote>
""")
        await fast_edit(q, fallback, reply_markup=create_owner_menu(context), parse_mode="HTML", rich_html=rich, log_label="OwnerWDManageEmpty")
        return

    wd_rows = "".join(
        f"<tr><td><code>{w['id']}</code></td><td><code>{w['owner_id']}</code></td>"
        f"<td>Rp {w['amount']:,}</td><td>{html.escape(str(w['method']))}</td>"
        f"<td>{html.escape(str(w['payment_number']))}</td><td>{html.escape(str(w['account_name']))}</td></tr>"
        for w in pending_wd
    )

    rich = premium_text(f"""\
[dolar] <b>KELOLA WITHDRAW CLONE</b>
<hr/>
<table bordered striped>
<tr><th>ID</th><th>Owner</th><th>Nominal</th><th>Metode</th><th>Tujuan</th><th>A/N</th></tr>
{wd_rows}
</table>
<p>[panahijo] Setelah transfer manual selesai, ketik <code>/approvewd [id]</code>.</p>
<p>[panahijo] Untuk menolak, ketik <code>/rejectwd [id]</code>.</p>""")

    fallback_lines = "\n".join(
        f"  ID {w['id']} — owner {w['owner_id']} — Rp {w['amount']:,} via {w['method']} ke {w['payment_number']} a/n {w['account_name']}"
        for w in pending_wd
    )
    fallback = premium_text(f"""\
[dolar] <b>KELOLA WITHDRAW CLONE</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>
{fallback_lines}

Setelah transfer manual, ketik /approvewd [id].
Untuk menolak, ketik /rejectwd [id].</blockquote>
""")

    await fast_edit(q, fallback, reply_markup=create_owner_menu(context), parse_mode="HTML", rich_html=rich, log_label="OwnerWDManage")


async def owner_list_requests_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if uid != OWNER_ID:
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)
    
    cursor.execute("SELECT id, user_id, amount, method, payment_number, account_name, created_at FROM withdraw_requests WHERE status='pending' ORDER BY created_at DESC")
    rows = cursor.fetchall()
    
    if not rows:
        await fast_edit(
            q, premium_text("[warning] <b>Tidak ada permintaan withdraw yang menunggu.</b>"),
            reply_markup=create_owner_menu(context), parse_mode="HTML",
            rich_html=f"{emoji('warning')} <b>Tidak ada permintaan withdraw yang menunggu.</b>",
            log_label="OwnerListRequestsEmpty",
        )
        return
    
    text = "WITHDRAW REQUESTS\n\n"
    table_rows = ""
    for r in rows[:10]:
        rid, ruid, amount, method, payment_number, account_name, created = r
        tgl = datetime.fromtimestamp(created).strftime('%d/%m %H:%M')
        text += f"ID: {rid}\nUser: {ruid}\nAmount: {format_currency(amount)}\nMethod: {method}\nNumber: {payment_number}\nA/N: {account_name}\nDate: {tgl}\n\n"
        table_rows += (
            f"<tr><td><code>{rid}</code></td><td><code>{ruid}</code></td><td>{format_currency(amount)}</td>"
            f"<td>{html.escape(str(method))}</td><td>{html.escape(str(payment_number))}</td>"
            f"<td>{html.escape(str(account_name))}</td><td><code>{tgl}</code></td></tr>\n"
        )
    rich_html = f"""\
{emoji('catatan')} <b>WITHDRAW REQUESTS ({len(rows)} Pending)</b>

<table bordered striped>
<tr><th>ID</th><th>User</th><th>Jumlah</th><th>Metode</th><th>No. Rek/Wallet</th><th>A/N</th><th>Tanggal</th></tr>
{table_rows}</table>"""
    
    await fast_edit(q, premium_text(text), reply_markup=create_owner_menu(context), parse_mode="HTML", rich_html=rich_html, log_label="OwnerListRequests")


async def owner_approve_wd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tombol inline 'Approve' pada notif withdraw saldo (sistem lama, bukan clone).
    Menandai withdraw selesai — saldo user SUDAH dipotong saat request dibuat,
    di sini cuma update status + kirim notif ke user."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)

    wd_id = int(q.data.split("_")[-1])
    cursor.execute("SELECT id, user_id, amount, method, payment_number, account_name, status, origin_bot_token FROM withdraw_requests WHERE id=?", (wd_id,))
    row = cursor.fetchone()
    if not row or row[6] != "pending":
        await fast_edit(
            q, premium_text(f"[warning] WD #{wd_id} tidak ditemukan atau sudah diproses sebelumnya."),
            parse_mode="HTML", log_label="WDSaldoApproveGagal",
        )
        return

    _, wd_user_id, wd_amount, wd_method, wd_number, wd_name, _, wd_origin_token = row
    cursor.execute("UPDATE withdraw_requests SET status='selesai' WHERE id=?", (wd_id,))
    conn.commit()

    await fast_edit(
        q,
        premium_text(f"[done] <b>WD #{wd_id} SELESAI</b>\n<hr/>\n<p>User <code>{wd_user_id}</code> sudah ditandai selesai untuk penarikan {format_currency(wd_amount)}.</p>"),
        parse_mode="HTML",
        rich_html=f"{emoji('done')} <b>WD #{wd_id} SELESAI</b>\n<hr/>\n<p>User {wd_user_id} sudah ditandai selesai untuk penarikan {format_currency(wd_amount)}.</p>",
        log_label="WDSaldoApproveOK",
    )

    rich_user = premium_text(f"""\
[done] <b>WITHDRAW BERHASIL DIPROSES</b>
<hr/>
<table bordered striped>
<tr><th>Detail</th><th>Info</th></tr>
<tr><td>[dolar] Nominal</td><td><b>{format_currency(wd_amount)}</b></td></tr>
<tr><td>[card] Metode</td><td>{wd_method}</td></tr>
<tr><td>[WhatsApp] Nomor</td><td><code>{wd_number}</code></td></tr>
<tr><td>[verified] Status</td><td><b>SELESAI</b></td></tr>
</table>
<p>[sparkle] Dana sudah ditransfer oleh admin. Terima kasih!</p>""")
    fallback_user = premium_text(f"""\
[done] <b>WITHDRAW BERHASIL DIPROSES</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>Nominal : {format_currency(wd_amount)}
Metode  : {wd_method}
Nomor   : {wd_number}
Status  : SELESAI

Dana sudah ditransfer oleh admin. Terima kasih!</blockquote>
""")
    try:
        _wd_user_bot = get_origin_bot(wd_origin_token, fallback_bot=context.bot)
        await notif.send_rich_message_to_chat(
            _wd_user_bot, wd_user_id, rich_user, fallback_user,
            reply_markup=create_main_menu(wd_user_id), log_label="WDSaldoUserNotifSelesai",
        )
    except Exception as e:
        print(f"[WithdrawSaldo] Gagal notif user: {e}")


async def owner_reject_wd_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tombol inline 'Tolak' pada notif withdraw saldo (sistem lama, bukan clone).
    Mengembalikan saldo user karena permintaan ditolak."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)

    wd_id = int(q.data.split("_")[-1])
    cursor.execute("SELECT id, user_id, amount, method, payment_number, account_name, status, origin_bot_token FROM withdraw_requests WHERE id=?", (wd_id,))
    row = cursor.fetchone()
    if not row or row[6] != "pending":
        await fast_edit(
            q, premium_text(f"[warning] WD #{wd_id} tidak ditemukan atau sudah diproses sebelumnya."),
            parse_mode="HTML", log_label="WDSaldoRejectGagal",
        )
        return

    _, wd_user_id, wd_amount, wd_method, wd_number, wd_name, _, wd_origin_token = row
    cursor.execute("UPDATE withdraw_requests SET status='ditolak' WHERE id=?", (wd_id,))
    conn.commit()
    # Kembalikan saldo user karena WD ditolak
    update_balance(wd_user_id, belance_delta=wd_amount)

    await fast_edit(
        q,
        premium_text(f"[batal] <b>WD #{wd_id} DITOLAK</b>\n<hr/>\n<p>Saldo {format_currency(wd_amount)} sudah dikembalikan ke user <code>{wd_user_id}</code>.</p>"),
        parse_mode="HTML",
        rich_html=f"{emoji('batal')} <b>WD #{wd_id} DITOLAK</b>\n<hr/>\n<p>Saldo {format_currency(wd_amount)} sudah dikembalikan ke user {wd_user_id}.</p>",
        log_label="WDSaldoRejectOK",
    )

    rich_user = premium_text(f"""\
[batal] <b>WITHDRAW DITOLAK</b>
<hr/>
<p>[warning] Permintaan withdraw {format_currency(wd_amount)} ditolak oleh admin. Saldo kamu sudah dikembalikan.</p>
<p>[chat] Hubungi Customer Service untuk info lebih lanjut.</p>""")
    fallback_user = premium_text(f"""\
[batal] <b>WITHDRAW DITOLAK</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[warning] Permintaan withdraw {format_currency(wd_amount)} ditolak admin. Saldo kamu sudah dikembalikan.
[chat] Hubungi Customer Service untuk info lebih lanjut.</blockquote>
""")
    try:
        _wd_user_bot = get_origin_bot(wd_origin_token, fallback_bot=context.bot)
        await notif.send_rich_message_to_chat(
            _wd_user_bot, wd_user_id, rich_user, fallback_user,
            reply_markup=create_main_menu(wd_user_id), log_label="WDSaldoUserNotifDitolak",
        )
    except Exception as e:
        print(f"[WithdrawSaldo] Gagal notif user: {e}")



async def owner_list_users_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if uid != OWNER_ID:
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)
    
    cursor.execute("""
        SELECT user_id, username, deposit_balance, belance_balance, created_at
        FROM users
        WHERE deposit_balance > 0 OR belance_balance > 0
        ORDER BY deposit_balance DESC
        LIMIT 50
    """)
    rows = cursor.fetchall()

    if not rows:
        await fast_edit(
            q, premium_text("[warning] <b>Belum ada user yang memiliki saldo.</b>"),
            reply_markup=create_owner_menu(context), parse_mode="HTML",
            rich_html=f"{emoji('warning')} <b>Belum ada user yang memiliki saldo.</b>",
            log_label="OwnerListUsersEmpty",
        )
        return

    raw_text = f"""
[crown] <b>LIST USER BERSALDO</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Menampilkan {len(rows)} user dengan saldo aktif.</blockquote>
"""
    table_rows = ""
    for r in rows:
        u_id, username, deposit, belance, created = r
        uname_display = f"@{username}" if username else f"ID:{u_id}"
        tgl = datetime.fromtimestamp(created).strftime("%d/%m/%Y") if created else "-"
        raw_text += f"\n<b>{uname_display}</b> | <code>{u_id}</code>\n[duitkarung] Deposit: <code>{format_currency(deposit)}</code> | Belance: <code>{format_currency(belance)}</code>\n[waktu] Join: {tgl}\n"
        table_rows += (
            f"<tr><td>{html.escape(str(uname_display))}</td><td><code>{u_id}</code></td>"
            f"<td>{format_currency(deposit)}</td><td>{format_currency(belance)}</td><td><code>{tgl}</code></td></tr>\n"
        )

    text = premium_text(raw_text)
    rich_html = f"""\
{emoji('crown')} <b>LIST USER BERSALDO</b>
<p>{emoji('catatan')} Menampilkan {len(rows)} user dengan saldo aktif.</p>

<table bordered striped>
<tr><th>User</th><th>ID</th><th>Deposit</th><th>Belance</th><th>Join</th></tr>
{table_rows}</table>"""
    await fast_edit(q, text, reply_markup=create_owner_menu(context), parse_mode="HTML", rich_html=rich_html, log_label="OwnerListUsers")

async def owner_set_cooldown_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if uid != OWNER_ID:
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)
    
    user_states[uid] = {'action': 'set_cooldown', 'mode': 'owner'}
    text = premium_text("""
[waktu] <b>PENGATURAN COOLDOWN</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Masukkan durasi cooldown dalam satuan detik.
[panahijo] <code>60</code> = 1 menit
[panahijo] <code>300</code> = 5 menit
[panahijo] <code>0</code> = nonaktif</blockquote>
""")
    rich_html = f"""\
{emoji('waktu')} <b>PENGATURAN COOLDOWN</b>
<hr/>
<p>{emoji('catatan')} Masukkan durasi cooldown dalam satuan detik.</p>
<ul>
<li><code>60</code> = 1 menit</li>
<li><code>300</code> = 5 menit</li>
<li><code>0</code> = nonaktif</li>
</ul>"""
    await fast_edit(q, text, reply_markup=create_cancel_button(), parse_mode="HTML", rich_html=rich_html, log_label="OwnerSetCooldown")

async def owner_change_mode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    
    # 1. Cek Hak Akses
    if uid != OWNER_ID:
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    
    await safe_answer(q)
    
    # 2. Setup Keyboard Premium
    keyboard = [
        [
            InlineKeyboardButton(
                text=" NORMAL ", 
                callback_data="mode_normal", 
                style="success", 
                icon_custom_emoji_id="5319032622016400976"
            )
        ],
        [
            InlineKeyboardButton(
                text=" MAINTENANCE ", 
                callback_data="mode_maintenance", 
                style="primary", 
                icon_custom_emoji_id="5395483821569249369"
            )
        ],
        [
            InlineKeyboardButton(
                text=" KEMBALI ", 
                callback_data="menu_owner",
                style="danger",
                icon_custom_emoji_id="5215204871422093648" 
            )
        ]
    ]

    # 3. Setup Teks Premium
    text = (
        "<tg-emoji emoji-id='5895754654360277212'>🎁</tg-emoji> "
        "<b>CHANGE BOT MODE</b>\n\n"
        "Select bot operation mode:"
    )
    rich_html = (
        '<tg-emoji emoji-id="5895754654360277212">🎁</tg-emoji> <b>CHANGE BOT MODE</b>\n'
        '<hr/>\n'
        '<p>Select bot operation mode:</p>'
    )

    # 4. Update Message
    await fast_edit(q, premium_text(text), reply_markup=styled_inline_keyboard(keyboard), parse_mode="HTML", rich_html=rich_html, log_label="OwnerChangeMode")


# ══════════════════════════════════════════════════════════════
#   PAYMENT METHOD — SET & GANTI (OWNER PANEL)
# ══════════════════════════════════════════════════════════════

def get_payment_info(method: str) -> str:
    """Ambil info rekening/nomor dari DB untuk metode tertentu."""
    try:
        import sqlite3 as _sq3
        _c = _sq3.connect(DB_PATH)
        row = _c.execute("SELECT value FROM settings WHERE key=?", (f"payinfo_{method}",)).fetchone()
        _c.close()
        return row[0] if row else ""
    except:
        return ""

def set_payment_info(method: str, info: str):
    """Simpan info rekening ke DB."""
    try:
        import sqlite3 as _sq3
        _c = _sq3.connect(DB_PATH)
        _c.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (f"payinfo_{method}", info))
        _c.commit()
        _c.close()
    except Exception as e:
        print(f"[set_payment_info] {e}")

PAYMENT_METHODS_LIST = [
    ("QRIS",    "qris",    "dolar"),
    ("DANA",    "dana",    "duitkarung"),
    ("GoPay",   "gopay",   "duitkarung"),
    ("OVO",     "ovo",     "duitkarung"),
    ("Seabank", "seabank", "duitkarung"),
]

async def owner_set_payment_handler(update, context):
    """Tampilkan daftar metode payment yang bisa diset info rekeningnya."""
    q = update.callback_query
    uid = q.from_user.id
    if uid != OWNER_ID:
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)

    lines = []
    table_rows = ""
    for label, key, _ in PAYMENT_METHODS_LIST:
        info = get_payment_info(key)
        if key == "qris":
            status = "✅ <i>Foto QRIS sudah diset</i>" if info else "<i>Belum diset (upload foto)</i>"
        else:
            status = f"<code>{info}</code>" if info else "<i>Belum diset</i>"
        lines.append(f"[panahijo] <b>{label}:</b> {status}")
        table_rows += f"<tr><td>{html.escape(str(label))}</td><td>{status}</td></tr>\n"

    joined = "\n".join(lines)
    text = premium_text(
        "[dolar] <b>SET INFO PAYMENT</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<blockquote>" + joined + "\n\n"
        "[catatan] Pilih metode untuk mengisi info transfer.\n"
        "[card] <b>QRIS:</b> Upload foto gambar QR. Metode lain: ketik nomor rekening.</blockquote>"
    )
    rich_html = f"""\
{emoji('dolar')} <b>SET INFO PAYMENT</b>

<table bordered striped>
<tr><th>Metode</th><th>Status</th></tr>
{table_rows}</table>

<p>{emoji('catatan')} Pilih metode untuk mengisi info transfer.</p>
<p>{emoji('card')} <b>QRIS:</b> Upload foto gambar QR. Metode lain: ketik nomor rekening.</p>"""

    buttons = []
    for label, key, emoji_name in PAYMENT_METHODS_LIST:
        info = get_payment_info(key)
        if key == "qris":
            btn_label = f"✅ QRIS (Foto)" if info else "📷 QRIS (Upload Foto)"
        else:
            btn_label = f"✅ {label}" if info else label
        buttons.append([styled_button(btn_label, callback_data=f"owner_setpayinfo_{key}", style="primary", emoji_name=emoji_name)])
    buttons.append([styled_button("Kembali", callback_data="menu_owner", style="danger", emoji_name="back")])

    await fast_edit(q, text, reply_markup=styled_inline_keyboard(buttons), parse_mode="HTML", rich_html=rich_html, log_label="OwnerSetPayment")


async def owner_setpayinfo_handler(update, context):
    """Owner klik salah satu metode → minta input info rekening (QRIS = foto, lain = teks)."""
    q = update.callback_query
    uid = q.from_user.id
    if uid != OWNER_ID:
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)

    method_key = q.data.replace("owner_setpayinfo_", "")
    method_label = next((l for l, k, _ in PAYMENT_METHODS_LIST if k == method_key), method_key.upper())
    current = get_payment_info(method_key)

    context.user_data["owner_setpay_method"] = method_key
    context.user_data["owner_setpay_label"]  = method_label

    kb = styled_inline_keyboard([[styled_button("Batal", callback_data="owner_set_payment", style="danger", emoji_name="back")]])

    if method_key == "qris":
        # QRIS: owner upload foto gambar QRIS
        context.user_data["current_menu_state"] = "owner_wait_qris_photo"
        current_text = "\n[catatan] <b>QRIS saat ini sudah diset.</b> Upload foto baru untuk mengganti." if current else ""
        text = premium_text(
            f"[dolar] <b>SET FOTO QRIS</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<blockquote>[card] Upload <b>foto gambar QRIS</b> kamu ke sini.\n"
            f"Foto ini akan ditampilkan ke user saat deposit manual.{current_text}\n\n"
            f"[warning] Kirim foto (bukan dokumen), pastikan QR code jelas & terbaca.</blockquote>"
        )
    else:
        # Metode lain: ketik nomor/info
        context.user_data["current_menu_state"] = "owner_wait_payinfo"
        current_text = f"\n[catatan] <b>Info saat ini:</b> <code>{current}</code>" if current else ""
        text = premium_text(
            f"[dolar] <b>SET {method_label.upper()}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"<blockquote>Ketik nomor/info transfer untuk <b>{method_label}</b>.\n"
            f"Contoh: <code>08123456789 a/n Budi</code>{current_text}</blockquote>"
        )

    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""[dolar] <b>SET {method_label.upper()}</b>
<hr/>
<ul><li>Ketik nomor/info transfer untuk <b>{method_label}</b>.</li><li>Contoh: <code>08123456789 a/n Budi</code>{current_text}</li></ul>"""), log_label="AutoRich")


async def handle_owner_payinfo_input(update, context) -> bool:
    """Tangkap teks input info rekening dari owner."""
    if context.user_data.get("current_menu_state") != "owner_wait_payinfo":
        return False

    method_key   = context.user_data.get("owner_setpay_method", "")
    method_label = context.user_data.get("owner_setpay_label", method_key.upper())
    info         = update.message.text.strip()

    if not method_key or not info:
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id, premium_text("[warning] <b>INPUT TIDAK VALID</b>\n<hr/>\n<p>Input yang kamu kirim tidak valid.</p>"), premium_text("[warning] <b>INPUT TIDAK VALID</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Input yang kamu kirim tidak valid.</blockquote>"),
            log_label="OwnerMsg1",
        )
        return True

    set_payment_info(method_key, info)
    context.user_data["current_menu_state"] = "main_menu"

    await notif.send_rich_message_to_chat(
        context.bot, update.effective_chat.id, premium_text(f"[done] <b>INFO PEMBAYARAN TERSIMPAN</b>\n<hr/>\n<ul><li>Info {method_label} berhasil disimpan!</li><li><code>{info}</code></li></ul>"), premium_text(f"[done] <b>INFO PEMBAYARAN TERSIMPAN</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Info {method_label} berhasil disimpan!\n<code>{info}</code></blockquote>"),
        log_label="OwnerMsg2",
    )
    return True


async def owner_ganti_mt_payment_handler(update, context):
    """Toggle cepat antara otomatis <-> manual."""
    q = update.callback_query
    uid = q.from_user.id
    if uid != OWNER_ID:
        await safe_answer(q, "Owner only!", show_alert=True)
        return

    current = get_payment_method()
    new_method = "manual" if current == "otomatis" else "otomatis"
    set_payment_method(new_method)

    _gw_label = "Nevapedia" if get_active_gateway() == "nevapedia" else "Pakasir"
    label = "Manual (Bukti TF)" if new_method == "manual" else f"Otomatis (QRIS {_gw_label})"
    # NOTE: show_alert popup TIDAK bisa dipakai di sini — tombol "Ganti MT Payment"
    # sekarang ada di Reply Keyboard (bukan inline), jadi tidak ada callback_query
    # asli yang bisa dijawab dengan alert. Konfirmasi dikirim sebagai pesan teks biasa.
    await safe_answer(q)
    await notif.send_rich_message_to_chat(
        context.bot, uid, premium_text(f"[done] <b>METODE PEMBAYARAN DIGANTI</b>\n<hr/>\n<p>Metode pembayaran gift diganti ke: <b>{label}</b></p>"), premium_text(f"[done] <b>METODE PEMBAYARAN DIGANTI</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Metode pembayaran gift diganti ke: <b>{label}</b></blockquote>"),
        log_label="OwnerMsg48",
    )
    await send_owner_panel_new(context, uid)


async def owner_ganti_gateway_handler(update, context):
    """Toggle cepat gateway QRIS otomatis antara Pakasir <-> Nevapedia."""
    q = update.callback_query
    uid = q.from_user.id
    if uid != OWNER_ID:
        await safe_answer(q, "Owner only!", show_alert=True)
        return

    current = get_active_gateway()
    new_gateway = "nevapedia" if current == "pakasir" else "pakasir"
    set_active_gateway(new_gateway)

    label = "Nevapedia" if new_gateway == "nevapedia" else "Pakasir"
    # NOTE: sama seperti Ganti MT Payment — tombol ini di Reply Keyboard, jadi
    # show_alert popup tidak bisa dipakai, konfirmasi dikirim sebagai pesan biasa.
    await safe_answer(q)
    await notif.send_rich_message_to_chat(
        context.bot, uid, premium_text(f"[done] <b>GATEWAY PEMBAYARAN DIGANTI</b>\n<hr/>\n<p>Gateway QRIS otomatis (Deposit/Gift/Stars/TON) diganti ke: <b>{label}</b></p>"), premium_text(f"[done] <b>GATEWAY PEMBAYARAN DIGANTI</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Gateway QRIS otomatis (Deposit/Gift/Stars/TON) diganti ke: <b>{label}</b></blockquote>"),
        log_label="OwnerMsgGantiGateway",
    )
    await send_owner_panel_new(context, uid)


# ── HANDLER APPROVE/TOLAK DEPOSIT MANUAL (dari owner) ──────────
async def owner_approve_deposit_manual_handler(update, context):
    q = update.callback_query
    uid = q.from_user.id
    if uid != OWNER_ID:
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)

    # callback: owner_approve_dm_{user_id}_{amount}_{req_msg_id}
    parts = q.data.split("_")
    target_uid = int(parts[3])
    amount     = int(parts[4])
    req_msg_id = int(parts[5])

    import sqlite3
    conn = sqlite3.connect(DB_PATH)
    cur  = conn.cursor()
    cur.execute("SELECT user_id FROM users WHERE user_id=?", (target_uid,))
    if cur.fetchone():
        cur.execute("UPDATE users SET deposit_balance=deposit_balance+?, belance_balance=belance_balance+? WHERE user_id=?",
                    (amount, amount, target_uid))
    else:
        cur.execute("INSERT INTO users (user_id, deposit_balance, belance_balance) VALUES (?,?,?)",
                    (target_uid, amount, amount))
    conn.commit()
    conn.close()

    # Bot ASAL user kirim bukti (pusat/clone) — notif hasil approve harus lewat
    # bot ini, bukan context.bot (bot pusat, karena Owner approve dari pusat).
    buyer_bot = get_origin_bot(deposit_manual_origin.pop(target_uid, None), fallback_bot=context.bot)

    # Notif ke user
    try:
        await notif.send_rich_message_to_chat(
            buyer_bot, target_uid, premium_text(f"""[done] <b>DEPOSIT DISETUJUI</b>
<hr/>
<ul><li>[dolar] <b>Nominal:</b> <b>Rp {amount:,}</b></li><li>[verified] Saldo kamu sudah ditambahkan oleh Owner.</li></ul>"""), premium_text(f"""[done] <b>DEPOSIT DISETUJUI</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[dolar] <b>Nominal:</b> <b>Rp {amount:,}</b>
[verified] Saldo kamu sudah ditambahkan oleh Owner.</blockquote>"""),
            log_label="OwnerMsg49",
        )
    except Exception as e:
        print(f"[Notif Approve DM] {e}")

    # Notif deposit ke channel (sama seperti deposit QRIS otomatis) — tetap
    # lewat context.bot (bot pusat) karena channel notif memang punya channel tetap.
    try:
        order_id_dm = f"MANUAL-{req_msg_id}"
        _u = get_user(target_uid)
        _uname = _u[1] if _u and _u[1] else None
        _new_bal = _u[2] if _u and len(_u) > 2 else amount
        await notif.notif_deposit_channel(context.bot, target_uid, amount, order_id_dm, username=_uname, new_balance=_new_bal)
    except Exception as e:
        print(f"[Error Notif Channel Deposit Manual]: {e}")


    # Update pesan owner
    try:
        await q.message.edit_caption(
            caption=premium_text(f"[done] <b>DEPOSIT DISETUJUI</b>\n<blockquote>User: <code>{target_uid}</code>\nNominal: <b>Rp {amount:,}</b></blockquote>"),
            parse_mode="HTML"
        )
    except:
        try:
            await q.message.edit_text(
                text=premium_text(f"[done] <b>DEPOSIT DISETUJUI</b>\n<blockquote>User: <code>{target_uid}</code>\nNominal: <b>Rp {amount:,}</b></blockquote>"),
                parse_mode="HTML"
            )
        except:
            pass


async def owner_tolak_deposit_manual_handler(update, context):
    q = update.callback_query
    uid = q.from_user.id
    if uid != OWNER_ID:
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)

    parts = q.data.split("_")
    target_uid = int(parts[3])
    amount     = int(parts[4])

    # Bot ASAL user kirim bukti (pusat/clone) — notif tolak harus lewat bot
    # ini, bukan context.bot (bot pusat, karena Owner tolak dari pusat).
    buyer_bot = get_origin_bot(deposit_manual_origin.pop(target_uid, None), fallback_bot=context.bot)

    # Notif ke user
    try:
        await notif.send_rich_message_to_chat(
            buyer_bot, target_uid, premium_text(f"""[warning] <b>DEPOSIT DITOLAK</b>
<hr/>
<ul><li>[dolar] <b>Nominal:</b> <b>Rp {amount:,}</b></li><li>[catatan] Bukti transfer tidak valid atau tidak sesuai. Hubungi CS jika ada pertanyaan.</li></ul>"""), premium_text(f"""[warning] <b>DEPOSIT DITOLAK</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[dolar] <b>Nominal:</b> <b>Rp {amount:,}</b>
[catatan] Bukti transfer tidak valid atau tidak sesuai. Hubungi CS jika ada pertanyaan.</blockquote>"""),
            log_label="OwnerMsg50",
        )
    except Exception as e:
        print(f"[Notif Tolak DM] {e}")

    try:
        await q.message.edit_caption(
            caption=premium_text(f"[batal] <b>DEPOSIT DITOLAK</b>\n<blockquote>User: <code>{target_uid}</code>\nNominal: <b>Rp {amount:,}</b></blockquote>"),
            parse_mode="HTML"
        )
    except:
        try:
            await q.message.edit_text(
                text=premium_text(f"[batal] <b>DEPOSIT DITOLAK</b>\n<blockquote>User: <code>{target_uid}</code>\nNominal: <b>Rp {amount:,}</b></blockquote>"),
                parse_mode="HTML"
            )
        except:
            pass

async def owner_broadcast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    allowed_owners = OWNER_ID.all_ids if hasattr(OWNER_ID, 'all_ids') else OWNER_ID

    if uid not in allowed_owners:
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)

    if is_broadcasting:
        await fast_edit(
            q,
            premium_text("""
[warning] <b>Broadcast Sedang Berlangsung</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Sistem sedang mengirim pesan ke semua pengguna.
[panahijo] Harap tunggu hingga proses saat ini selesai sebelum mengirim broadcast baru.</blockquote>
"""),
            reply_markup=create_owner_menu(context), parse_mode="HTML",
            rich_html=f"{emoji('warning')} <b>Broadcast Sedang Berlangsung</b>\n<hr/>\n<p>{emoji('catatan')} Sistem sedang mengirim pesan ke semua pengguna. Harap tunggu hingga proses saat ini selesai.</p>",
            log_label="OwnerBroadcastBusy",
        )
        return

    user_states[uid] = {'action': 'broadcast', 'mode': 'owner'}

    text = premium_text("""
[spikerbiru] <b>MENU BROADCAST</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Kirim/ketik teks yang mau di-broadcast ke <b>semua user</b> yang pernah pakai bot ini.

[pin] Setelah dikirim, broadcast langsung jalan otomatis dengan live status (total target, berhasil, gagal).
[warning] User yang gagal dikirimi (blokir bot/akun nonaktif) otomatis <b>dihapus</b> dari database.</blockquote>
""")
    await fast_edit(q, text, reply_markup=create_cancel_button(), parse_mode="HTML", rich_html=premium_text(f"""[spikerbiru] <b>MENU BROADCAST</b>
<hr/>
<ul><li>[catatan] Kirim/ketik teks yang mau di-broadcast ke <b>semua user</b> yang pernah pakai bot ini.</li></ul>
<p>[pin] Setelah dikirim, broadcast langsung jalan otomatis dengan live status (total target, berhasil, gagal).</p>
<p>[warning] User yang gagal dikirimi (blokir bot/akun nonaktif) otomatis dihapus dari database.</p>"""), log_label="AutoRich")

#====MENU ADD STOCK====#
async def owner_backup_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tombol 'Backup User' — export isi tabel users (user_id, username, saldo)
    jadi satu file .json, format yang sama persis yang diterima fitur Restore User."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)

    try:
        import json
        cursor.execute("SELECT user_id, username, deposit_balance, belance_balance, created_at FROM users ORDER BY user_id ASC")
        rows = cursor.fetchall()

        users_export = [
            {
                "user_id": r[0],
                "username": r[1],
                "deposit_balance": r[2] or 0,
                "belance_balance": r[3] or 0,
                "created_at": r[4],
            }
            for r in rows
        ]

        json_bytes = json.dumps(users_export, ensure_ascii=False, indent=2).encode("utf-8")
        bio = io.BytesIO(json_bytes)
        fname = f"backup_user_{time.strftime('%Y%m%d_%H%M%S')}.json"
        bio.name = fname

        caption = premium_text(f"""
[done] <b>BACKUP USER SELESAI</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Total user: <b>{len(users_export)}</b>
[pin] Isi: user_id, username, deposit_balance, belance_balance
[shield] File ini bisa langsung dipakai lagi lewat tombol <b>Restore User</b>.</blockquote>
""")
        await context.bot.send_document(
            chat_id=uid, document=bio, filename=fname,
            caption=caption, parse_mode="HTML", reply_markup=create_owner_menu(context)
        )
    except Exception as e:
        err_msg = html.escape(str(e))
        await notif.send_rich_message_to_chat(
            context.bot, uid,
            premium_text(f"[warning] <b>BACKUP USER GAGAL</b>\n<hr/>\n<p>Error: <code>{err_msg}</code></p>"),
            premium_text(f"[warning] <b>BACKUP USER GAGAL</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Error: <code>{err_msg}</code></blockquote>"),
            reply_markup=create_owner_menu(context),
            log_label="OwnerBackupUserFailed",
        )


async def owner_restore_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tombol 'Restore User' — minta owner kirim file .json/.js berisi data user
    untuk dimasukkan/dikembalikan ke database users."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)

    user_states[uid] = {'action': 'restore_user', 'mode': 'owner'}

    text = premium_text("""
[download] <b>RESTORE USER</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Kirim file <code>.json</code> atau <code>.js</code> berisi data user yang mau di-restore ke database.

[pin] Bisa array object (lengkap dengan saldo/username), contoh:
<code>[
  {"user_id": 123456789, "username": "budi", "deposit_balance": 50000}
]</code>

[pin] Atau array angka polos (cuma user_id), contoh:
<code>[123456789, 987654321, 555555555]</code>

[catatan] Field wajib: <b>user_id</b>. Field lain opsional (default 0/kosong utk user baru, dibiarkan apa adanya utk user yang sudah ada).
[warning] User yang sudah ada di database akan di-<b>update</b> (cuma field yang disebut di file), user baru akan ditambahkan.
Maksimal ukuran file 5MB.</blockquote>
""")
    await fast_edit(
        q, text, reply_markup=create_cancel_button(), parse_mode="HTML",
        rich_html=f"""{emoji('download')} <b>RESTORE USER</b>
<hr/>
<ul><li>{emoji('catatan')} Kirim file <code>.json</code> atau <code>.js</code> berisi data user yang mau di-restore ke database.</li></ul>
<p>{emoji('pin')} Bisa array object (lengkap saldo/username) atau array angka polos berisi user_id saja, mis. <code>[123456789, 987654321]</code>.</p>
<p>{emoji('catatan')} Field wajib: <b>user_id</b>. Field lain opsional.</p>
<p>{emoji('warning')} User yang sudah ada di database akan di-update (cuma field yang disebut di file), user baru akan ditambahkan. Maksimal ukuran file 5MB.</p>""",
        log_label="OwnerRestoreUserPrompt",
    )


def _parse_restore_user_file(raw_text: str):
    """Ekstrak list dict user dari isi file .json/.js. Dukung file JSON murni
    maupun file .js gaya 'module.exports = [...]' / 'export default [...]' /
    'const users = [...]'. Return list of dict, atau None kalau gagal parse."""
    import json
    txt = raw_text.strip()
    # Buang BOM kalau ada
    if txt.startswith("\ufeff"):
        txt = txt[1:]

    # Coba parse langsung sebagai JSON dulu (paling umum: file .json murni)
    try:
        data = json.loads(txt)
    except Exception:
        data = None

    if data is None:
        # File .js: cari literal array/object setelah tanda '=' (module.exports = ..., const x = ..., dst)
        m = re.search(r'=\s*(\[.*\]|\{.*\})\s*;?\s*$', txt, re.DOTALL)
        if not m:
            # atau file yang langsung diawali [ atau { tanpa assignment
            m2 = re.search(r'(\[.*\]|\{.*\})', txt, re.DOTALL)
            candidate = m2.group(1) if m2 else None
        else:
            candidate = m.group(1)
        if not candidate:
            return None
        try:
            data = json.loads(candidate)
        except Exception:
            return None

    if isinstance(data, dict):
        if isinstance(data.get("users"), list):
            data = data["users"]
        else:
            # anggap dict tunggal = satu user
            data = [data]

    if not isinstance(data, list):
        return None

    return data


async def _owner_restore_user_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terima & proses file restore user dari owner."""
    uid = update.effective_user.id
    doc = update.message.document
    if not doc:
        return

    fname = (doc.file_name or "").lower()
    if not fname.endswith((".json", ".js")):
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            premium_text("[warning] <b>FORMAT FILE TIDAK DIDUKUNG</b>\n<hr/>\n<p>[catatan] Kirim file <code>.json</code> atau <code>.js</code> berisi array data user.</p>"),
            premium_text("[warning] <b>FORMAT FILE TIDAK DIDUKUNG</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>[catatan] Kirim file <code>.json</code> atau <code>.js</code> berisi array data user.</blockquote>"),
            reply_markup=create_cancel_button(),
            log_label="OwnerRestoreUserBadFormat",
        )
        return

    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            premium_text("[warning] <b>FILE TERLALU BESAR</b>\n<hr/>\n<p>[catatan] Maksimal 5MB.</p>"),
            premium_text("[warning] <b>FILE TERLALU BESAR</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>[catatan] Maksimal 5MB.</blockquote>"),
            reply_markup=create_cancel_button(),
            log_label="OwnerRestoreUserTooBig",
        )
        return

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        file_bytes = await tg_file.download_as_bytearray()
        raw_text = bytes(file_bytes).decode("utf-8", errors="ignore")
    except Exception as e:
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            premium_text(f"[error] <b>GAGAL MEMBACA FILE</b>\n<hr/>\n<p><code>{html.escape(str(e))}</code></p>"),
            premium_text(f"[error] <b>GAGAL MEMBACA FILE</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote><code>{html.escape(str(e))}</code></blockquote>"),
            reply_markup=create_cancel_button(),
            log_label="OwnerRestoreUserReadErr",
        )
        return

    entries = _parse_restore_user_file(raw_text)
    if entries is None:
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            premium_text("[warning] <b>FILE TIDAK VALID</b>\n<hr/>\n<p>[catatan] Isi file bukan array JSON yang valid. Pastikan formatnya benar lalu kirim ulang.</p>"),
            premium_text("[warning] <b>FILE TIDAK VALID</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>[catatan] Isi file bukan array JSON yang valid. Pastikan formatnya benar lalu kirim ulang.</blockquote>"),
            reply_markup=create_cancel_button(),
            log_label="OwnerRestoreUserParseErr",
        )
        return

    _MISSING = object()  # sentinel: bedain field "tidak ada di file" vs "sengaja diisi 0/kosong"

    inserted, updated, invalid = 0, 0, []
    for i, entry in enumerate(entries):
        # Dukung juga file yang isinya array angka/string polos (cuma user_id),
        # bukan array object -- misal [123456789, 987654321, ...]. Kalau begitu,
        # anggap itu id-nya langsung; username & saldo otomatis dibiarkan
        # apa adanya (kalau user sudah ada) atau default kosong/0 (kalau baru).
        if isinstance(entry, (int, str)):
            entry = {"id": entry}
        if not isinstance(entry, dict):
            invalid.append(f"#{i+1}: bukan object/angka")
            continue

        raw_uid = entry.get("user_id", entry.get("id", entry.get("uid")))
        try:
            u_id = int(raw_uid)
        except (TypeError, ValueError):
            invalid.append(f"#{i+1}: user_id tidak valid ({raw_uid!r})")
            continue

        # Ambil tiap field HANYA kalau memang ada di file (sentinel _MISSING),
        # supaya user lama yang direstore tanpa username/saldo gak ketimpa jadi
        # kosong/0 -- field yang gak disebut di file dibiarkan apa adanya.
        username_raw = _MISSING
        for k in ("username", "name"):
            if k in entry:
                username_raw = entry.get(k)
                break

        deposit_raw = _MISSING
        for k in ("deposit_balance", "deposit", "saldo_deposit"):
            if k in entry:
                deposit_raw = entry.get(k)
                break

        belance_raw = _MISSING
        for k in ("belance_balance", "belance", "balance", "saldo_bonus"):
            if k in entry:
                belance_raw = entry.get(k)
                break

        def _to_int_or_default(raw, default=0):
            if raw is _MISSING or raw is None:
                return default
            try:
                return int(raw)
            except (TypeError, ValueError):
                return default

        deposit_balance = _to_int_or_default(deposit_raw, 0)
        belance_balance = _to_int_or_default(belance_raw, 0)

        try:
            created_at = int(entry.get("created_at") or int(time.time()))
        except (TypeError, ValueError):
            created_at = int(time.time())

        try:
            existing = get_user(u_id)
            if existing:
                # Bangun UPDATE dinamis: cuma set kolom yang memang ada di file.
                set_clauses, params = [], []
                if username_raw is not _MISSING:
                    set_clauses.append("username = ?")
                    params.append(username_raw)
                if deposit_raw is not _MISSING:
                    set_clauses.append("deposit_balance = ?")
                    params.append(deposit_balance)
                if belance_raw is not _MISSING:
                    set_clauses.append("belance_balance = ?")
                    params.append(belance_balance)

                if set_clauses:
                    params.append(u_id)
                    cursor.execute(f"UPDATE users SET {', '.join(set_clauses)} WHERE user_id = ?", params)
                updated += 1
            else:
                username = username_raw if username_raw is not _MISSING else None
                cursor.execute(
                    "INSERT INTO users (user_id, username, deposit_balance, belance_balance, created_at) VALUES (?, ?, ?, ?, ?)",
                    (u_id, username, deposit_balance, belance_balance, created_at)
                )
                inserted += 1
        except Exception as e:
            invalid.append(f"#{i+1} (user_id {u_id}): {e}")

    conn.commit()

    if uid in user_states:
        del user_states[uid]

    ringkasan = f"[done] <b>RESTORE USER SELESAI</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>[catatan] Total data di file: <b>{len(entries)}</b>\n[panahijo] User baru ditambahkan: <b>{inserted}</b>\n[panahijo] User diperbarui: <b>{updated}</b>\n[warning] Dilewati/invalid: <b>{len(invalid)}</b>"
    if invalid:
        shown = invalid[:10]
        ringkasan += "\n\n" + "\n".join(f"- {html.escape(x)}" for x in shown)
        if len(invalid) > len(shown):
            ringkasan += f"\n... dan {len(invalid) - len(shown)} lagi."
    ringkasan += "</blockquote>"

    await notif.send_rich_message_to_chat(
        context.bot, update.effective_chat.id, premium_text(ringkasan), premium_text(ringkasan),
        reply_markup=create_owner_menu(context),
        log_label="OwnerRestoreUserDone",
    )


async def _add_stock_receive_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Terima file .txt/.csv berisi daftar nomor telepon untuk batch Add Stock."""
    uid = update.effective_user.id
    doc = update.message.document
    if not doc:
        return

    fname = (doc.file_name or "").lower()
    if not fname.endswith((".txt", ".csv")):
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id, premium_text("[warning] <b>FORMAT FILE TIDAK DIDUKUNG</b>\n<hr/>\n<p>[catatan] Kirim file <code>.txt</code> atau <code>.csv</code> berisi nomor telepon, satu nomor per baris (boleh juga dipisah koma).</p>"), premium_text("[warning] <b>FORMAT FILE TIDAK DIDUKUNG</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>[catatan] Kirim file <code>.txt</code> atau <code>.csv</code> berisi nomor telepon, satu nomor per baris (boleh juga dipisah koma).</blockquote>"),
            reply_markup=create_cancel_button(),
            log_label="OwnerMsg3",
        )
        return

    if doc.file_size and doc.file_size > 2 * 1024 * 1024:
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id, premium_text("[warning] <b>FILE TERLALU BESAR</b>\n<hr/>\n<p>[catatan] Maksimal 2MB.</p>"), premium_text("[warning] <b>FILE TERLALU BESAR</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>[catatan] Maksimal 2MB.</blockquote>"),
            reply_markup=create_cancel_button(),
            log_label="OwnerMsg4",
        )
        return

    try:
        tg_file = await context.bot.get_file(doc.file_id)
        file_bytes = await tg_file.download_as_bytearray()
        raw_text = bytes(file_bytes).decode("utf-8", errors="ignore")
    except Exception as e:
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id, premium_text(f"[error] <b>GAGAL MEMBACA FILE</b>\n<hr/>\n<p><code>{html.escape(str(e))}</code></p>"), premium_text(f"[error] <b>GAGAL MEMBACA FILE</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote><code>{html.escape(str(e))}</code></blockquote>"),
            reply_markup=create_cancel_button(),
            log_label="OwnerMsg5",
        )
        return

    raw_lines = re.split(r'[\n,;]+', raw_text)
    phones = []
    for line in raw_lines:
        p = line.replace("+", "").strip()
        if p and p not in phones:
            phones.append(p)

    if not phones:
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id, premium_text("[warning] <b>NOMOR VALID TIDAK DITEMUKAN</b>\n<hr/>\n<p>[catatan] Pastikan satu nomor per baris, contoh: 6281234567890</p>"), premium_text("[warning] <b>NOMOR VALID TIDAK DITEMUKAN</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>[catatan] Pastikan satu nomor per baris, contoh: 6281234567890</blockquote>"),
            reply_markup=create_cancel_button(),
            log_label="OwnerMsg6",
        )
        return

    await notif.send_rich_message_to_chat(
        context.bot, update.effective_chat.id, premium_text(f"[done] <b>FILE DITERIMA</b>\n<hr/>\n<p>[pin] Ditemukan <b>{len(phones)}</b> nomor. Memproses satu per satu, tunggu sebentar...</p>"), premium_text(f"[done] <b>FILE DITERIMA</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>[pin] Ditemukan <b>{len(phones)}</b> nomor. Memproses satu per satu, tunggu sebentar...</blockquote>"),
        log_label="OwnerMsg7",
    )

    stock_batch_queue[uid] = {"pending": phones, "done": 0, "failed": []}
    await advance_stock_queue(update, context, uid)

async def advance_stock_queue(update: Update, context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Proses antrian batch 'Add Stock'. Ambil nomor berikutnya, kirim OTP.
    Nomor yang invalid otomatis di-skip (dicatat sebagai gagal) lalu lanjut ke nomor
    berikutnya tanpa owner perlu mengulang dari awal. Kalau antrian habis, tampilkan
    ringkasan berapa yang berhasil & gagal."""
    queue = stock_batch_queue.get(user_id)
    if not queue:
        return

    while queue["pending"]:
        phone = queue["pending"].pop(0)
        client = TelegramClient(StringSession(), API_ID, API_HASH)
        await client.connect()

        try:
            await client.send_code_request(phone)
        except PhoneNumberInvalidError:
            try:
                await client.disconnect()
            except Exception:
                pass
            queue["failed"].append((phone, "Nomor tidak valid"))
            continue
        except (TypeError, ValueError):
            try:
                await client.disconnect()
            except Exception:
                pass
            queue["failed"].append((phone, "Format nomor tidak dikenali"))
            continue
        except FloodWaitError as e:
            try:
                await client.disconnect()
            except Exception:
                pass
            queue["failed"].append((phone, f"Flood wait {e.seconds}s, coba lagi nanti"))
            continue
        except Exception as e:
            try:
                await client.disconnect()
            except Exception:
                pass
            queue["failed"].append((phone, str(e)))
            continue

        # Kirim OTP sukses -> tunggu owner masukkan kode
        login_state[user_id] = {"phone": phone, "client": client, "step": "code"}
        user_states[user_id] = {"action": "input_otp_stock", "mode": "owner"}

        sisa = len(queue["pending"])
        sisa_line = f"\n[pin] Sisa antrian: <b>{sisa}</b> nomor lagi." if sisa else ""
        pesan_otp_rich = premium_text(f"""\
[lightning] <b>PERMINTAAN OTP BERHASIL</b>
<hr/>
<table bordered striped>
<tr><th>Detail OTP</th><th>Info</th></tr>
<tr><td>[WhatsApp] Nomor</td><td><code>{html.escape(phone)}</code></td></tr>
<tr><td>[done] Status</td><td><b>TERKIRIM</b></td></tr>
{f'<tr><td>[pin] Sisa Antrian</td><td><b>{sisa}</b> nomor</td></tr>' if sisa else ''}
</table>
<p>[catatan] <b>Silakan masukkan kode OTP di bawah ini:</b></p>""")
        pesan_otp_fb = premium_text(f"""\
[lightning] <b>PERMINTAAN OTP BERHASIL</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[WhatsApp] <b>Nomor:</b> <code>{html.escape(phone)}</code>
[done] Kode OTP telah dikirim oleh sistem Telegram.{sisa_line}</blockquote>

[catatan] <b>Silakan masukkan kode OTP di bawah ini:</b>""")
        await notif.send_rich_message_to_chat(
            context.bot, user_id, pesan_otp_rich, pesan_otp_fb,
            reply_markup=create_cancel_button(),
            log_label="OwnerMsg51",
        )
        return

    # Antrian habis, tampilkan ringkasan hasil batch
    done = queue["done"]
    failed = queue["failed"]
    ringkasan_rich = f"""\
[done] <b>PROSES ADD STOCK SELESAI</b>
<hr/>
<table bordered striped>
<tr><th>Ringkasan</th><th>Jumlah</th></tr>
<tr><td>[verified] Berhasil</td><td><b>{done}</b> akun</td></tr>
<tr><td>[warning] Gagal</td><td><b>{len(failed)}</b> akun</td></tr>
</table>"""
    ringkasan_fb = f"""[done] <b>PROSES ADD STOCK SELESAI</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[pin] Berhasil ditambahkan: <b>{done}</b> akun."""
    if failed:
        detail_gagal = "\n".join([f"• <code>{html.escape(p)}</code> — {html.escape(str(r))}" for p, r in failed])
        ringkasan_rich += f"\n<p>[warning] Detail gagal:</p>\n<ul>" + "".join(
            [f"<li><code>{html.escape(p)}</code> — {html.escape(str(r))}</li>" for p, r in failed]
        ) + "</ul>"
        ringkasan_fb += f"\n[warning] Gagal: <b>{len(failed)}</b> akun.\n{detail_gagal}"
    ringkasan_fb += "</blockquote>"

    await notif.send_rich_message_to_chat(
        context.bot, user_id, premium_text(ringkasan_rich), premium_text(ringkasan_fb),
        reply_markup=create_main_menu(user_id),
        log_label="OwnerMsg52",
    )

    if user_id in stock_batch_queue:
        del stock_batch_queue[user_id]
    if user_id in login_state:
        del login_state[user_id]
    if user_id in user_states:
        del user_states[user_id]

async def owner_add_stock_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if uid != OWNER_ID:
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)

    login_state[uid] = {'step': 'add_stock_phone'}
    user_states[uid] = {'action': 'add_stock_phone', 'mode': 'owner'}
    if uid in stock_batch_queue:
        del stock_batch_queue[uid]

    text = premium_text("""
[download] <b>ADD NEW STOCK</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[WhatsApp] <b>Masukkan nomor telepon akun:</b>
[catatan] Contoh format: <code>628XXXXX</code>

[pin] Bisa <b>lebih dari satu nomor sekaligus</b>, satu nomor per baris:
<code>6281111111111
6282222222222
6283333333333</code>

[warning] Pastikan nomor diawali kode negara tanpa spasi atau tanda plus.</blockquote>
""")
    await fast_edit(q, text, reply_markup=create_cancel_button(), parse_mode="HTML", rich_html=premium_text(f"""[download] <b>ADD NEW STOCK</b>
<hr/>
<ul><li>[WhatsApp] <b>Masukkan nomor telepon akun:</b></li><li>[catatan] Contoh format: <code>628XXXXX</code></li></ul>
<ul><li>[pin] Bisa <b>lebih dari satu nomor sekaligus</b>, satu nomor per baris:</li><li><code>6281111111111</li><li>6282222222222</li><li>6283333333333</code></li></ul>
<p>[warning] Pastikan nomor diawali kode negara tanpa spasi atau tanda plus.</p>"""), log_label="AutoRich")

async def owner_set_price_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if uid != OWNER_ID:
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)
    
    stock = get_available_stock()
    if not stock:
        await fast_edit(
            q, premium_text("[warning] <b>Stok Tidak Tersedia!</b>"), reply_markup=create_back_button(),
            parse_mode="HTML", rich_html=f"{emoji('warning')} <b>Stok Tidak Tersedia!</b>", log_label="NoStockAvailable"
        )
        return
    
    keyboard = []
    for s in stock:
        keyboard.append([styled_button(f"ID {s['account_id']} - {format_currency(s['price'])}", callback_data=f"setprice_{s['id']}", style="primary", emoji_name="dolar")])
    keyboard.append([styled_button("Kembali", callback_data="menu_owner", style="danger", emoji_name="back")])
    
    await fast_edit(q, premium_text("""[dolar] <b>SETTING HARGA</b>\n\n<blockquote>[catatan] Pilih session yang ingin diubah harganya.</blockquote>"""), reply_markup=styled_inline_keyboard(keyboard), parse_mode="HTML", rich_html=premium_text(f"""[dolar] <b>SETTING HARGA</b>
<hr/>
<p>[catatan] Pilih session yang ingin diubah harganya.</p>"""), log_label="AutoRich")

async def set_price_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if uid != OWNER_ID:
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)
    
    stock_id = int(q.data.split("_")[1])
    user_states[uid] = {'action': 'set_price', 'stock_id': stock_id, 'mode': 'owner'}
    await fast_edit(q, premium_text("""[dolar] <b>Masukkan harga baru.</b>

<blockquote>[catatan] Contoh: <code>10000</code></blockquote>"""), reply_markup=create_cancel_button(), parse_mode="HTML", rich_html=premium_text(f"""[dolar] <b>Masukkan harga baru.</b>
<hr/>
<p>[catatan] Contoh: <code>10000</code></p>"""), log_label="AutoRich")

async def owner_remove_stock_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if uid != OWNER_ID:
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)
    
    stock = get_available_stock()
    if not stock:
        await fast_edit(
            q, premium_text("[warning] <b>Stok Tidak Tersedia!</b>"), reply_markup=create_back_button(),
            parse_mode="HTML", rich_html=f"{emoji('warning')} <b>Stok Tidak Tersedia!</b>", log_label="NoStockAvailable"
        )
        return
    
    keyboard = []
    for s in stock:
        keyboard.append([styled_button(f"Hapus ID {s['account_id']} - {s['phone']}", callback_data=f"delstock_{s['id']}", style="danger", emoji_name="warning")])
    keyboard.append([styled_button("Kembali", callback_data="menu_owner", style="danger", emoji_name="back")])
    
    await fast_edit(q, premium_text("""[warning] <b>HAPUS STOCK</b>\n\n<blockquote>[catatan] Pilih session yang ingin dihapus dari database stok.</blockquote>"""), reply_markup=styled_inline_keyboard(keyboard), parse_mode="HTML", rich_html=premium_text(f"""[warning] <b>HAPUS STOCK</b>
<hr/>
<p>[catatan] Pilih session yang ingin dihapus dari database stok.</p>"""), log_label="AutoRich")

async def delete_stock_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if uid != OWNER_ID:
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)
    
    stock_id = int(q.data.split("_")[1])
    remove_stock(stock_id)
    await safe_answer(q, "Stock deleted!", show_alert=True)
    await fast_edit(q, premium_text("[done] <b>Stok berhasil dihapus dari database.</b>"), reply_markup=create_owner_menu(context), parse_mode="HTML", rich_html=premium_text(f"""[done] <b>Stok berhasil dihapus dari database.</b>"""), log_label="AutoRich")

# --- Handler untuk mode NORMAL ---
async def cancel_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler universal untuk tombol 'Batal' (cancel_input) di semua flow input
    (Add Stock, Set Price, Set Cooldown, Withdraw, dll). Sebelumnya tombol ini
    tidak punya handler sama sekali sehingga klik 'Batal' tidak melakukan apa-apa."""
    q = update.callback_query
    await safe_answer(q, "Dibatalkan!")
    uid = q.from_user.id

    mode = None
    if uid in user_states:
        mode = user_states[uid].get('mode')
        del user_states[uid]

    # Jika ada sesi login telethon yang masih nyantol (proses add stock), putuskan koneksinya.
    if uid in login_state:
        client = login_state[uid].get('client') if isinstance(login_state[uid], dict) else None
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass
        del login_state[uid]

    text = premium_text("[done] <b>Proses dibatalkan.</b>")

    if uid == OWNER_ID and mode == 'owner':
        await fast_edit(q, text, reply_markup=create_owner_menu(context), parse_mode="HTML", rich_html=premium_text(f"""[done] <b>Proses dibatalkan.</b>"""), log_label="AutoRich")
    else:
        # Untuk flow non-owner (withdraw, dll) atau bila tidak yakin mode-nya,
        # kembalikan langsung ke menu utama (Menu 1) agar user tidak terjebak
        # di state input, dengan teks yang sama seperti pertama kali /start.
        if q.message:
            await safe_delete_message(context.bot, q.message.chat_id, q.message.message_id)
        await send_root_menu_new(context, uid)

async def mode_normal_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id != OWNER_ID:
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    
    conn = sqlite3.connect(DB_PATH) 
    cursor = conn.cursor()
    cursor.execute("UPDATE settings SET value = ? WHERE key = ?", ("normal", "bot_mode"))
    conn.commit()
    conn.close()
    
    await safe_answer(q, "Mode changed to NORMAL!", show_alert=True)
    await fast_edit(q, premium_text("[done] <b>Mode bot berhasil diubah ke NORMAL.</b>"), reply_markup=create_owner_menu(context), parse_mode="HTML", rich_html=premium_text(f"""[done] <b>Mode bot berhasil diubah ke NORMAL.</b>"""), log_label="AutoRich")

# --- Handler untuk mode MAINTENANCE ---
# --- Handler untuk mode MAINTENANCE ---
async def mode_maintenance_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if uid != OWNER_ID:
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    
    # 1. Update ke DATABASE (Sekarang menggunakan DB_PATH yang benar)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("UPDATE settings SET value = ? WHERE key = ?", ("maintenance", "bot_mode"))
    conn.commit()
    conn.close()
    
    # 3. Langsung kasih feedback ke owner
    await safe_answer(q, "Mode changed to MAINTENANCE!", show_alert=True)
    await fast_edit(q, premium_text("""[warning] <b>Mode bot berhasil diubah ke MAINTENANCE.</b>

<blockquote>[catatan] Pengguna umum tidak dapat menggunakan bot sampai mode normal diaktifkan kembali.</blockquote>"""), reply_markup=create_owner_menu(context), parse_mode="HTML", rich_html=premium_text(f"""[warning] <b>Mode bot berhasil diubah ke MAINTENANCE.</b>
<hr/>
<p>[catatan] Pengguna umum tidak dapat menggunakan bot sampai mode normal diaktifkan kembali.</p>"""), log_label="AutoRich")

# === GERBANG UTAMA: HANDLE MESSAGE ===
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # === GUARD: TOLAK GRUP & BLOCKED USER ===
    if not await is_private_chat(update):
        return
    if update.effective_user and is_blocked(update.effective_user.id):
        return
    # ===========================================
    # Pastikan pesan yang masuk berbentuk teks
    if not update.message or not update.message.text:
        return

    user_id = update.effective_user.id
    
    # --- CEK STATUS MAINTENANCE DARI DB_PATH ---
    conn = sqlite3.connect(DB_PATH) # Gunakan DB_PATH yang sudah benar
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM settings WHERE key = 'bot_mode'")
    status = cursor.fetchone()
    conn.close() 
    
    if status and status[0] == "maintenance" and user_id != OWNER_ID:
        pesan = (
            "<tg-emoji emoji-id='5368806667297238348'>⚡️</tg-emoji> <b>BOT SEDANG MAINTENANCE</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n"
            "<blockquote>"
            "Maaf <tg-emoji emoji-id='5454010941479873740'>‼️</tg-emoji>, bot saat ini sedang dalam masa perbaikan demi kenyamanan bersama.\n\n"
            "Mohon tunggu sampai proses pemeliharaan selesai ya! <tg-emoji emoji-id='5395483821569249369'>⏯️</tg-emoji>"
            "</blockquote>"
        )
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id, premium_text(pesan), premium_text(pesan),
            log_label="OwnerMsg8",
        )
        return 
        
    text = update.message.text
    phone = text.replace("+", "").strip()

    # ===== CLONE SYSTEM: deteksi pesan forward berisi token BotFather =====
    if await clone_detect_forwarded_token(update, context):
        return

    # ===== CLONE SYSTEM: input alur withdraw dompet clone =====
    if await clone_handle_wd_input(update, context):
        return

    # ===== CLONE SYSTEM: input alur atur komisi (self-service pemilik clone) =====
    if await clone_handle_komisi_input(update, context):
        return

    # ===== HANDLER INPUT MANUAL PERSEN NEGO (OWNER PANEL) =====
    if context.user_data.get("current_menu_state") == "owner_wait_nego_persen":
        handled = await owner_nego_custom_input(update, context)
        if handled:
            return

    # ===== HANDLER CHAT NEGO HARGA (AI) — KHUSUS BUY NOKTEL =====
    if context.user_data.get("current_menu_state") == "nego_chat":
        handled = await handle_nego_chat_input(update, context)
        if handled:
            return

    # ===== HANDLER REPLY KEYBOARD (3 TOMBOL PINTASAN) =====
    if await handle_reply_keyboard_input(update, context):
        return

    # ===== HANDLER INPUT GIFT (USERNAME, HARGA OWNER, LOGIN) =====
    gift_states = ("gift_ask_username", "gift_ask_message", "gift_owner_wait_price",
                   "gift_login_wait_phone", "gift_login_wait_code", "gift_login_wait_2fa")
    if context.user_data.get("current_menu_state") in gift_states:
        if context.user_data.get("current_menu_state") == "gift_ask_username":
            handled = await handle_gift_username_input(update, context)
        elif context.user_data.get("current_menu_state") == "gift_ask_message":
            handled = await handle_gift_message_input(update, context)
        elif context.user_data.get("current_menu_state") == "gift_owner_wait_price":
            handled = await handle_gift_owner_price_input(update, context)
        else:
            handled = await handle_gift_login_input(update, context)
        if handled:
            return

    # ===== HANDLER INPUT TOPUP STARS (USERNAME, QTY) =====
    if context.user_data.get("current_menu_state") == "stars_ask_target":
        handled = await handle_stars_target_input(update, context)
        if handled:
            return
    if context.user_data.get("current_menu_state") == "stars_ask_qty":
        handled = await handle_stars_qty_input(update, context)
        if handled:
            return

    # ===== HANDLER INPUT TOPUP STARS BULK (TABEL: USERNAME + JUMLAH PER BARIS) =====
    if context.user_data.get("current_menu_state") == "bulk_stars_ask_table":
        handled = await handle_bulk_stars_table_input(update, context)
        if handled:
            return

    # ===== HANDLER INPUT TOPUP PREMIUM (USERNAME) =====
    if context.user_data.get("current_menu_state") == "premium_ask_target":
        handled = await handle_premium_target_input(update, context)
        if handled:
            return

    # ===== HANDLER INPUT TOPUP TON (ALAMAT WALLET, JUMLAH) =====
    if context.user_data.get("current_menu_state") == "ton_ask_address":
        handled = await handle_ton_address_input(update, context)
        if handled:
            return
    if context.user_data.get("current_menu_state") == "ton_ask_amount":
        handled = await handle_ton_amount_input(update, context)
        if handled:
            return

    # ===== HANDLER OWNER: STARS TOPUP SETTINGS =====
    if str(context.user_data.get("current_menu_state", "")).startswith("stars_owner_wait_"):
        handled = await handle_stars_owner_input(update, context)
        if handled:
            return

    # ===== HANDLER OWNER: PREMIUM TOPUP SETTINGS (harga dasar + FE) =====
    if str(context.user_data.get("current_menu_state", "")).startswith("premium_owner_wait_"):
        handled = await handle_premium_owner_input(update, context)
        if handled:
            return

    # ===== HANDLER OWNER: TON TOPUP SETTINGS (margin jual) =====
    if context.user_data.get("current_menu_state") == "ton_owner_wait_margin":
        uid_own = update.effective_user.id
        text_own = (update.message.text or "").strip()
        try:
            margin = float(text_own.replace(",", ".").replace("%", "").strip())
            assert margin >= 0
        except (ValueError, AssertionError):
            await context.bot.send_message(uid_own, premium_text("[warning] Masukkan angka yang valid, contoh: 5"), parse_mode="HTML")
            context.user_data["current_menu_state"] = "idle"
            return
        ton_topup.set_margin_persen(margin)
        context.user_data["current_menu_state"] = "idle"
        await context.bot.send_message(uid_own, premium_text(f"[done] Margin jual TON berhasil diupdate jadi {margin:g}%."), parse_mode="HTML")
        return

    if context.user_data.get("current_menu_state") == "ton_owner_wait_fee_flat":
        uid_own = update.effective_user.id
        text_own = (update.message.text or "").strip()
        try:
            fee_flat = float(text_own.replace(",", ".").replace("Rp", "").replace("rp", "").strip())
            assert fee_flat >= 0
        except (ValueError, AssertionError):
            await context.bot.send_message(uid_own, premium_text("[warning] Masukkan angka yang valid, contoh: 1500"), parse_mode="HTML")
            context.user_data["current_menu_state"] = "idle"
            return
        ton_topup.set_fee_flat_idr(fee_flat)
        context.user_data["current_menu_state"] = "idle"
        await context.bot.send_message(
            uid_own,
            premium_text(
                f"[done] Fee flat jual TON berhasil diupdate jadi Rp{fee_flat:,.0f}/TON."
                + (" Untung sekarang TETAP segitu per TON, gak ngikutin naik-turun rate." if fee_flat > 0
                   else " Fee flat di-set 0, jadi sekarang balik pakai mode margin persen (lama).")
            ),
            parse_mode="HTML",
        )
        return

    if context.user_data.get("current_menu_state") == "ton_owner_wait_apikey":
        uid_own = update.effective_user.id
        text_own = (update.message.text or "").strip()
        ton_topup.set_api_key(text_own)
        context.user_data["current_menu_state"] = "idle"
        await context.bot.send_message(uid_own, premium_text("[done] TON API Key (khusus Topup TON) berhasil diupdate."), parse_mode="HTML")
        return

    # ===== HANDLER INPUT GMAIL REPORT =====
    gmail_states = (
        "gmail_wait_email", "gmail_wait_apppass",
        "gmail_wait_add_target", "gmail_wait_subject",
        "gmail_wait_body", "gmail_wait_hit"
    )
    if context.user_data.get("current_menu_state") in gmail_states:
        handled = await gmail_handle_input(update, context)
        if handled:
            return

    # ===== HANDLER DEPOSIT NOMINAL MANUAL =====
    if context.user_data.get("current_menu_state") == "deposit_ask_manual":
        handled = await handle_deposit_manual_input(update, context)
        if handled:
            return

    # ===== HANDLER SET INFO PAYMENT (OWNER) =====
    if context.user_data.get("current_menu_state") == "owner_wait_payinfo":
        handled = await handle_owner_payinfo_input(update, context)
        if handled:
            return

    # ===== HANDLER BUKTI TRANSFER DEPOSIT MANUAL =====
    if context.user_data.get("current_menu_state") == "deposit_wait_bukti":
        handled = await handle_deposit_bukti_tf(update, context)
        if handled:
            return

    # ===== HANDLER BUKTI TRANSFER GIFT MANUAL =====
    if context.user_data.get("current_menu_state") == "gift_wait_bukti":
        handled = await handle_gift_bukti_tf(update, context)
        if handled:
            return

    # ===== HANDLER CV KONTAK PAGE 5 =====
    if context.user_data.get("current_menu_state") == "cv5_wait_nama_file":
        handled = await cv5_handle_nama_file(update, context)
        if handled:
            return
    if context.user_data.get("current_menu_state") == "cv5_wait_jumlah":
        handled = await cv5_handle_jumlah_text(update, context)
        if handled:
            return
    if context.user_data.get("current_menu_state") == "cv5_txt2vcf_wait_name":
        handled = await cv5_handle_txt2vcf_name(update, context)
        if handled:
            return
    if context.user_data.get("current_menu_state") == "cv5_txt2vcf_wait_filename":
        handled = await cv5_handle_txt2vcf_filename(update, context)
        if handled:
            return
    if context.user_data.get("current_menu_state") == "cv5_txt2vcf_wait_custom_qty":
        handled = await cv5_handle_txt2vcf_custom_qty(update, context)
        if handled:
            return
    if context.user_data.get("current_menu_state") == "cv5_adminnavy_wait_name":
        handled = await cv5_handle_adminnavy_name(update, context)
        if handled:
            return
    if context.user_data.get("current_menu_state") == "cv5_adminnavy_wait_filename":
        handled = await cv5_handle_adminnavy_filename(update, context)
        if handled:
            return
    if context.user_data.get("current_menu_state") == "cv5_adminnavy_wait_file":
        handled = await cv5_handle_adminnavy_phones_text(update, context)
        if handled:
            return
    if context.user_data.get("current_menu_state") == "cv5_renfile_wait_pattern":
        handled = await cv5_handle_renfile_pattern(update, context)
        if handled:
            return
    if context.user_data.get("current_menu_state") == "cv5_renkontak_wait_name":
        handled = await cv5_handle_renkontak_name(update, context)
        if handled:
            return
    if context.user_data.get("current_menu_state") == "cv5_text2file_wait_text":
        handled = await cv5_handle_text2file_text(update, context)
        if handled:
            return
    if context.user_data.get("current_menu_state") == "cv5_text2file_wait_name":
        handled = await cv5_handle_text2file_name(update, context)
        if handled:
            return

    # Handle owner actions
    if user_id in user_states:
        action = user_states[user_id].get('action')
        mode = user_states[user_id].get('mode')

        if action == 'owner_add_saldo' and mode == 'owner':
            # Format: @username jumlah  ATAU  user_id jumlah
            parts_input = text.strip().split()
            if len(parts_input) < 2:
                await notif.send_rich_message_to_chat(
                    context.bot, update.effective_chat.id, premium_text("[warning] <b>FORMAT SALAH</b>\n<hr/>\n<ul><li>Gunakan:</li><li><code>@username jumlah</code></li><li><code>user_id jumlah</code></li><li>Contoh: <code>@pretygirrls 100k</code> atau <code>974468120 100k</code></li></ul>"), premium_text("[warning] <b>FORMAT SALAH</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Gunakan:\n<code>@username jumlah</code>\n<code>user_id jumlah</code>\nContoh: <code>@pretygirrls 100k</code> atau <code>974468120 100k</code></blockquote>"),
                    log_label="OwnerMsg9",
                )
                return
            raw_target = parts_input[0].strip()
            amount = await _parse_nominal(parts_input[1])
            if amount <= 0:
                await notif.send_rich_message_to_chat(
                    context.bot, update.effective_chat.id, premium_text("[warning] <b>NOMINAL TIDAK VALID</b>\n<hr/>\n<p>Contoh: <code>100000</code> atau <code>100k</code> atau <code>1.5jt</code></p>"), premium_text("[warning] <b>NOMINAL TIDAK VALID</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Contoh: <code>100000</code> atau <code>100k</code> atau <code>1.5jt</code></blockquote>"),
                    log_label="OwnerMsg10",
                )
                return

            row_s = None
            # --- Cari by user_id (jika input angka) ---
            if raw_target.lstrip("-").isdigit():
                target_id = int(raw_target)
                try:
                    conn_s = sqlite3.connect(DB_PATH)
                    cur_s = conn_s.cursor()
                    cur_s.execute("SELECT user_id, username FROM users WHERE user_id = ?", (target_id,))
                    row_s = cur_s.fetchone()
                    conn_s.close()
                except Exception as e:
                    await notif.send_rich_message_to_chat(
                        context.bot, update.effective_chat.id, premium_text(f"[warning] <b>ERROR DATABASE</b>\n<hr/>\n<p><code>{e}</code></p>"), premium_text(f"[warning] <b>ERROR DATABASE</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote><code>{e}</code></blockquote>"),
                        log_label="OwnerMsg11",
                    )
                    return
                # Jika belum ada di DB, auto-register (misal user belum /start tapi owner tau ID-nya)
                if not row_s:
                    try:
                        tg_chat = await context.bot.get_chat(target_id)
                        uname_new = tg_chat.username or ""
                        create_user(target_id, uname_new)
                        conn_s2 = sqlite3.connect(DB_PATH)
                        cur_s2 = conn_s2.cursor()
                        cur_s2.execute("SELECT user_id, username FROM users WHERE user_id = ?", (target_id,))
                        row_s = cur_s2.fetchone()
                        conn_s2.close()
                    except Exception:
                        pass
            else:
                # --- Cari by username ---
                target_username = raw_target.lstrip("@")
                try:
                    conn_s = sqlite3.connect(DB_PATH)
                    cur_s = conn_s.cursor()
                    cur_s.execute("SELECT user_id, username FROM users WHERE LOWER(username) = LOWER(?)", (target_username,))
                    row_s = cur_s.fetchone()
                    conn_s.close()
                except Exception as e:
                    await notif.send_rich_message_to_chat(
                        context.bot, update.effective_chat.id, premium_text(f"[warning] <b>ERROR DATABASE</b>\n<hr/>\n<p><code>{e}</code></p>"), premium_text(f"[warning] <b>ERROR DATABASE</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote><code>{e}</code></blockquote>"),
                        log_label="OwnerMsg12",
                    )
                    return
                # Fallback: username mungkin berubah, cari via Telegram API
                if not row_s:
                    try:
                        tg_chat = await context.bot.get_chat(f"@{target_username}")
                        real_uid = tg_chat.id
                        conn_s2 = sqlite3.connect(DB_PATH)
                        cur_s2 = conn_s2.cursor()
                        cur_s2.execute("UPDATE users SET username = ? WHERE user_id = ?", (target_username, real_uid))
                        conn_s2.commit()
                        cur_s2.execute("SELECT user_id, username FROM users WHERE user_id = ?", (real_uid,))
                        row_s = cur_s2.fetchone()
                        conn_s2.close()
                    except Exception:
                        pass
                # Fallback: cek owner yang belum /start
                if not row_s:
                    owner_ids_list = OWNER_ID.all_ids if hasattr(OWNER_ID, "all_ids") else [OWNER_ID]
                    for oid in owner_ids_list:
                        try:
                            tg_user = await context.bot.get_chat(oid)
                            if tg_user.username and tg_user.username.lower() == target_username.lower():
                                create_user(oid, tg_user.username)
                                conn_s3 = sqlite3.connect(DB_PATH)
                                cur_s3 = conn_s3.cursor()
                                cur_s3.execute("SELECT user_id, username FROM users WHERE user_id = ?", (oid,))
                                row_s = cur_s3.fetchone()
                                conn_s3.close()
                                break
                        except Exception:
                            pass

            if not row_s:
                label = raw_target
                await notif.send_rich_message_to_chat(
                    context.bot, update.effective_chat.id, premium_text(f"[warning] <b>USER TIDAK DITEMUKAN</b>\n<hr/>\n<ul><li>User {label} tidak ditemukan di database.</li><li>[catatan] Pastikan user sudah pernah /start di bot ini, atau coba gunakan user_id langsung.</li></ul>"), premium_text(f"[warning] <b>USER TIDAK DITEMUKAN</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>User {label} tidak ditemukan di database.\n\n[catatan] Pastikan user sudah pernah /start di bot ini, atau coba gunakan user_id langsung.</blockquote>"),
                    log_label="OwnerMsg13",
                )
                return
            target_uid, target_uname = row_s
            update_balance(target_uid, deposit_delta=amount, belance_delta=amount)
            del user_states[user_id]
            await notif.send_rich_message_to_chat(
                context.bot, update.effective_chat.id, premium_text(f"""\
[done] <b>SALDO BERHASIL DITAMBAHKAN</b>
<hr/>
<table bordered striped>
<tr><th>Detail Transaksi</th><th>Info</th></tr>
<tr><td>[crown] Target</td><td>@{target_uname}</td></tr>
<tr><td>[card] ID User</td><td><code>{target_uid}</code></td></tr>
<tr><td>[dolar] Nominal</td><td><b>Rp {amount:,}</b></td></tr>
</table>
<p>[verified] Saldo berhasil dikreditkan ke akun user.</p>"""), premium_text(f"""\
[done] <b>SALDO BERHASIL DITAMBAHKAN</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[crown] <b>Target:</b> @{target_uname}
[card] <b>ID User:</b> <code>{target_uid}</code>
[dolar] <b>Nominal:</b> <code>Rp {amount:,}</code>
[verified] Saldo berhasil dikreditkan ke akun user.</blockquote>"""),
                reply_markup=create_owner_menu(context),
                log_label="OwnerMsg14",
            )
            try:
                await notif.send_rich_message_to_chat(
                    context.bot, target_uid, premium_text(f"""
[duitkarung] <b>SALDO DITAMBAHKAN OLEH OWNER</b>
<hr/>
<ul><li>[dolar] <b>Nominal:</b> <code>Rp {amount:,}</code></li><li>[done] Saldo kamu telah diisi oleh admin. Cek di menu Profil.</li></ul>
"""), premium_text(f"""
[duitkarung] <b>SALDO DITAMBAHKAN OLEH OWNER</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[dolar] <b>Nominal:</b> <code>Rp {amount:,}</code>
[done] Saldo kamu telah diisi oleh admin. Cek di menu Profil.</blockquote>
"""),
                    log_label="OwnerMsg53",
                )
            except Exception:
                pass
            return
        
        # --- KURANGI SALDO MANUAL ---
        elif action == 'owner_kurangi_saldo' and mode == 'owner':
            parts_input = text.strip().split()
            if len(parts_input) < 2:
                await notif.send_rich_message_to_chat(
                    context.bot, update.effective_chat.id, premium_text("[warning] <b>FORMAT SALAH</b>\n<hr/>\n<ul><li>Gunakan:</li><li><code>@username jumlah</code></li><li><code>user_id jumlah</code></li><li>Contoh: <code>@pretygirrls 100k</code> atau <code>974468120 100k</code></li></ul>"), premium_text("[warning] <b>FORMAT SALAH</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Gunakan:\n<code>@username jumlah</code>\n<code>user_id jumlah</code>\nContoh: <code>@pretygirrls 100k</code> atau <code>974468120 100k</code></blockquote>"),
                    log_label="OwnerMsg15",
                )
                return
            raw_target = parts_input[0].strip()
            amount = await _parse_nominal(parts_input[1])
            if amount <= 0:
                await notif.send_rich_message_to_chat(
                    context.bot, update.effective_chat.id, premium_text("[warning] <b>NOMINAL TIDAK VALID</b>\n<hr/>\n<p>Contoh: <code>100000</code> atau <code>100k</code> atau <code>1.5jt</code></p>"), premium_text("[warning] <b>NOMINAL TIDAK VALID</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Contoh: <code>100000</code> atau <code>100k</code> atau <code>1.5jt</code></blockquote>"),
                    log_label="OwnerMsg16",
                )
                return

            row_s = None
            if raw_target.lstrip("-").isdigit():
                target_id = int(raw_target)
                try:
                    conn_s = sqlite3.connect(DB_PATH)
                    cur_s = conn_s.cursor()
                    cur_s.execute("SELECT user_id, username, belance_balance FROM users WHERE user_id = ?", (target_id,))
                    row_s = cur_s.fetchone()
                    conn_s.close()
                except Exception as e:
                    await notif.send_rich_message_to_chat(
                        context.bot, update.effective_chat.id, premium_text(f"[warning] <b>ERROR DATABASE</b>\n<hr/>\n<p><code>{e}</code></p>"), premium_text(f"[warning] <b>ERROR DATABASE</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote><code>{e}</code></blockquote>"),
                        log_label="OwnerMsg17",
                    )
                    return
            else:
                target_username = raw_target.lstrip("@")
                try:
                    conn_s = sqlite3.connect(DB_PATH)
                    cur_s = conn_s.cursor()
                    cur_s.execute("SELECT user_id, username, belance_balance FROM users WHERE LOWER(username) = LOWER(?)", (target_username,))
                    row_s = cur_s.fetchone()
                    conn_s.close()
                except Exception as e:
                    await notif.send_rich_message_to_chat(
                        context.bot, update.effective_chat.id, premium_text(f"[warning] <b>ERROR DATABASE</b>\n<hr/>\n<p><code>{e}</code></p>"), premium_text(f"[warning] <b>ERROR DATABASE</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote><code>{e}</code></blockquote>"),
                        log_label="OwnerMsg18",
                    )
                    return
                if not row_s:
                    try:
                        tg_chat = await context.bot.get_chat(f"@{target_username}")
                        real_uid = tg_chat.id
                        conn_s2 = sqlite3.connect(DB_PATH)
                        cur_s2 = conn_s2.cursor()
                        cur_s2.execute("SELECT user_id, username, belance_balance FROM users WHERE user_id = ?", (real_uid,))
                        row_s = cur_s2.fetchone()
                        conn_s2.close()
                    except Exception:
                        pass

            if not row_s:
                label = raw_target
                await notif.send_rich_message_to_chat(
                    context.bot, update.effective_chat.id, premium_text(f"[warning] <b>USER TIDAK DITEMUKAN</b>\n<hr/>\n<ul><li>User {label} tidak ditemukan di database.</li><li>[catatan] Pastikan user sudah pernah /start di bot ini.</li></ul>"), premium_text(f"[warning] <b>USER TIDAK DITEMUKAN</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>User {label} tidak ditemukan di database.\n\n[catatan] Pastikan user sudah pernah /start di bot ini.</blockquote>"),
                    log_label="OwnerMsg19",
                )
                return

            target_uid, target_uname, current_belance = row_s
            current_belance = current_belance or 0

            if amount > current_belance:
                await notif.send_rich_message_to_chat(
                    context.bot, update.effective_chat.id, premium_text(f"""\
[warning] <b>SALDO USER TIDAK MENCUKUPI</b>
<hr/>
<table bordered striped>
<tr><th>Detail Saldo</th><th>Info</th></tr>
<tr><td>[card] Saldo saat ini</td><td><b>Rp {current_belance:,}</b></td></tr>
<tr><td>[error] Kurangi maks</td><td><b>Rp {current_belance:,}</b></td></tr>
</table>"""), premium_text(f"""\
[warning] <b>SALDO USER TIDAK MENCUKUPI</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[card] Saldo saat ini: <code>Rp {current_belance:,}</code>
[error] Kurangi maks: <code>Rp {current_belance:,}</code></blockquote>"""),
                    log_label="OwnerMsg20",
                )
                return

            update_balance(target_uid, belance_delta=-amount)
            del user_states[user_id]
            await notif.send_rich_message_to_chat(
                context.bot, update.effective_chat.id, premium_text(f"""\
[done] <b>SALDO BERHASIL DIKURANGI</b>
<hr/>
<table bordered striped>
<tr><th>Detail Transaksi</th><th>Info</th></tr>
<tr><td>[crown] Target</td><td>@{target_uname}</td></tr>
<tr><td>[card] ID User</td><td><code>{target_uid}</code></td></tr>
<tr><td>[warning] Dikurangi</td><td><b>Rp {amount:,}</b></td></tr>
<tr><td>[dolar] Sisa Saldo</td><td><b>Rp {current_belance - amount:,}</b></td></tr>
</table>
<p>[verified] Saldo berhasil dikurangi dari akun user.</p>"""), premium_text(f"""\
[done] <b>SALDO BERHASIL DIKURANGI</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[crown] <b>Target:</b> @{target_uname}
[card] <b>ID User:</b> <code>{target_uid}</code>
[warning] <b>Dikurangi:</b> <code>Rp {amount:,}</code>
[dolar] <b>Sisa Saldo:</b> <code>Rp {current_belance - amount:,}</code>
[verified] Saldo berhasil dikurangi dari akun user.</blockquote>"""),
                reply_markup=create_owner_menu(context),
                log_label="OwnerMsg21",
            )
            try:
                await notif.send_rich_message_to_chat(
                    context.bot, target_uid, premium_text(f"""
[warning] <b>SALDO DIKURANGI OLEH OWNER</b>
<hr/>
<ul><li>[dolar] <b>Nominal:</b> <code>Rp {amount:,}</code></li><li>[card] <b>Sisa Saldo:</b> <code>Rp {current_belance - amount:,}</code></li><li>[catatan] Hubungi admin jika ada pertanyaan.</li></ul>
"""), premium_text(f"""
[warning] <b>SALDO DIKURANGI OLEH OWNER</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[dolar] <b>Nominal:</b> <code>Rp {amount:,}</code>
[card] <b>Sisa Saldo:</b> <code>Rp {current_belance - amount:,}</code>
[catatan] Hubungi admin jika ada pertanyaan.</blockquote>
"""),
                    log_label="OwnerMsg54",
                )
            except Exception:
                pass
            return

        # --- BLOKIR USER ---
        elif action == 'owner_blokir_input' and mode == 'owner':
            raw_target = text.strip()
            target_uid_b = None
            target_uname_b = ""
            if raw_target.lstrip("-").isdigit():
                target_uid_b = int(raw_target)
                try:
                    tg = await context.bot.get_chat(target_uid_b)
                    target_uname_b = tg.username or ""
                except:
                    pass
            else:
                uname_clean = raw_target.lstrip("@")
                try:
                    tg = await context.bot.get_chat(f"@{uname_clean}")
                    target_uid_b = tg.id
                    target_uname_b = tg.username or uname_clean
                except:
                    pass
            if not target_uid_b:
                await notif.send_rich_message_to_chat(
                    context.bot, update.effective_chat.id, premium_text(f"[warning] <b>USER TIDAK DITEMUKAN</b>\n<hr/>\n<p>Target: <code>{raw_target}</code></p>"), premium_text(f"[warning] <b>USER TIDAK DITEMUKAN</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Target: <code>{raw_target}</code></blockquote>"),
                    log_label="OwnerMsg22",
                )
                return
            if is_owner(target_uid_b):
                await notif.send_rich_message_to_chat(
                    context.bot, update.effective_chat.id, premium_text("[warning] <b>TIDAK BISA BLOKIR SESAMA OWNER</b>\n<hr/>\n<p>Aksi ini tidak diizinkan.</p>"), premium_text("[warning] <b>TIDAK BISA BLOKIR SESAMA OWNER</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Aksi ini tidak diizinkan.</blockquote>"),
                    log_label="OwnerMsg23",
                )
                return
            block_user(target_uid_b, target_uname_b)
            del user_states[user_id]
            uname_disp = f"@{target_uname_b}" if target_uname_b else f"id{target_uid_b}"
            await notif.send_rich_message_to_chat(
                context.bot, update.effective_chat.id, premium_text(f"""\
[batal] <b>USER BERHASIL DIBLOKIR</b>
<hr/>
<table bordered striped>
<tr><th>Detail User</th><th>Info</th></tr>
<tr><td>[crown] User</td><td>{uname_disp}</td></tr>
<tr><td>[card] ID</td><td><code>{target_uid_b}</code></td></tr>
</table>
<p>[warning] User tidak bisa mengakses bot lagi.</p>"""), premium_text(f"""\
[batal] <b>USER BERHASIL DIBLOKIR</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[crown] <b>User:</b> {uname_disp}
[card] <b>ID:</b> <code>{target_uid_b}</code>
[warning] User tidak bisa mengakses bot lagi.</blockquote>"""),
                reply_markup=create_owner_menu(context),
                log_label="OwnerMsg24",
            )
            return

        # --- UNBLOKIR USER ---
        elif action == 'owner_unblokir_input' and mode == 'owner':
            raw_target = text.strip()
            target_uid_u = None
            target_uname_u = ""
            if raw_target.lstrip("-").isdigit():
                target_uid_u = int(raw_target)
            else:
                uname_clean = raw_target.lstrip("@")
                try:
                    tg = await context.bot.get_chat(f"@{uname_clean}")
                    target_uid_u = tg.id
                    target_uname_u = tg.username or uname_clean
                except:
                    # coba cari di tabel blocked_users
                    rows_b = get_blocked_list()
                    for rb in rows_b:
                        if rb[1] and rb[1].lower() == uname_clean.lower():
                            target_uid_u = rb[0]
                            target_uname_u = rb[1]
                            break
            if not target_uid_u:
                await notif.send_rich_message_to_chat(
                    context.bot, update.effective_chat.id, premium_text(f"[warning] <b>USER TIDAK DITEMUKAN</b>\n<hr/>\n<p>Target: <code>{raw_target}</code></p>"), premium_text(f"[warning] <b>USER TIDAK DITEMUKAN</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Target: <code>{raw_target}</code></blockquote>"),
                    log_label="OwnerMsg25",
                )
                return
            unblock_user(target_uid_u)
            del user_states[user_id]
            uname_disp = f"@{target_uname_u}" if target_uname_u else f"id{target_uid_u}"
            await notif.send_rich_message_to_chat(
                context.bot, update.effective_chat.id, premium_text(f"""\
[verified] <b>USER BERHASIL DIUNBLOKIR</b>
<hr/>
<table bordered striped>
<tr><th>Detail User</th><th>Info</th></tr>
<tr><td>[crown] User</td><td>{uname_disp}</td></tr>
<tr><td>[card] ID</td><td><code>{target_uid_u}</code></td></tr>
</table>
<p>[done] User sudah bisa mengakses bot kembali.</p>"""), premium_text(f"""\
[verified] <b>USER BERHASIL DIUNBLOKIR</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[crown] <b>User:</b> {uname_disp}
[card] <b>ID:</b> <code>{target_uid_u}</code>
[done] User sudah bisa mengakses bot kembali.</blockquote>"""),
                reply_markup=create_owner_menu(context),
                log_label="OwnerMsg26",
            )
            return

        # --- BROADCAST: full otomatis, tanpa command, dengan live progress ---
        elif action == 'broadcast' and mode == 'owner':
            global is_broadcasting
            broadcast_text_raw = text.strip()
            if not broadcast_text_raw:
                return  # pesan kosong (misal cuma stiker/foto lolos ke sini), biarkan owner kirim ulang teksnya

            del user_states[user_id]

            if is_broadcasting:
                await notif.send_rich_message_to_chat(
                    context.bot, update.effective_chat.id,
                    premium_text("[warning] <b>Broadcast Sedang Berlangsung</b>\n<hr/>\n<p>Ada proses broadcast lain yang masih jalan, coba lagi sebentar.</p>"),
                    premium_text("[warning] <b>Broadcast Sedang Berlangsung</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Ada proses broadcast lain yang masih jalan, coba lagi sebentar.</blockquote>"),
                    reply_markup=create_owner_menu(context),
                    log_label="OwnerBroadcastBusyMsg",
                )
                return

            users = get_all_user_ids()
            if not users:
                await notif.send_rich_message_to_chat(
                    context.bot, update.effective_chat.id,
                    premium_text("[warning] <b>Tidak Ada Pengguna</b>\n<hr/>\n<p>Database pengguna masih kosong. Belum ada yang menggunakan bot ini.</p>"),
                    premium_text("[warning] <b>Tidak Ada Pengguna</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Database pengguna masih kosong. Belum ada yang menggunakan bot ini.</blockquote>"),
                    reply_markup=create_owner_menu(context),
                    log_label="OwnerBroadcastEmpty",
                )
                return

            is_broadcasting = True
            start_text = premium_text(f"""
[spikerbiru] <b>BROADCAST DIMULAI</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[done] <b>Terkirim:</b> <code>0/{len(users)}</code>
[warning] <b>Gagal:</b> <code>0</code></blockquote>
""")
            status_message = await notif.send_rich_message_to_chat(
                context.bot, update.effective_chat.id, start_text, start_text,
                log_label="BroadcastLoading",
            )
            # run_broadcast yang jalan di background ini yang update live progress
            # (total target/berhasil/gagal) dan otomatis hapus user gagal dari DB.
            asyncio.create_task(run_broadcast(context.bot, update.effective_chat.id, status_message, users, broadcast_text_raw, mode="text"))
            return

        elif action == 'set_cooldown' and mode == 'owner':
            try:
                seconds = int(text)
                cooldown_config["duration"] = seconds
                await notif.send_rich_message_to_chat(
                    context.bot, update.effective_chat.id, premium_text(f"[waktu] <b>COOLDOWN DIATUR</b>\n<hr/>\n<p>Cooldown diatur ke <b>{seconds}</b> detik.</p>"), premium_text(f"[waktu] <b>COOLDOWN DIATUR</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Cooldown diatur ke <b>{seconds}</b> detik.</blockquote>"),
                    log_label="OwnerMsg30",
                )
            except:
                await notif.send_rich_message_to_chat(
                    context.bot, update.effective_chat.id, premium_text("[warning] <b>ANGKA TIDAK VALID</b>\n<hr/>\n<p>Masukkan angka yang valid.</p>"), premium_text("[warning] <b>ANGKA TIDAK VALID</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Masukkan angka yang valid.</blockquote>"),
                    log_label="OwnerMsg31",
                )
            del user_states[user_id]
            return
        
        elif action == 'set_price' and mode == 'owner':
            try:
                new_price = int(text)
                stock_id = user_states[user_id].get('stock_id')
                update_stock_price(stock_id, new_price)
                await notif.send_rich_message_to_chat(
                    context.bot, update.effective_chat.id, premium_text(f"[dolar] <b>HARGA DIUBAH</b>\n<hr/>\n<p>Harga diubah ke <b>{format_currency(new_price)}</b>!</p>"), premium_text(f"[dolar] <b>HARGA DIUBAH</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Harga diubah ke <b>{format_currency(new_price)}</b>!</blockquote>"),
                    log_label="OwnerMsg32",
                )
            except:
                await notif.send_rich_message_to_chat(
                    context.bot, update.effective_chat.id, premium_text("[warning] <b>HARGA TIDAK VALID</b>\n<hr/>\n<p>Masukkan harga yang valid.</p>"), premium_text("[warning] <b>HARGA TIDAK VALID</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Masukkan harga yang valid.</blockquote>"),
                    log_label="OwnerMsg33",
                )
            del user_states[user_id]
            return
        
        elif action == 'add_stock_phone' and mode == 'owner':
            raw_lines = re.split(r'[\n,]+', text)
            phones = []
            for line in raw_lines:
                p = line.replace("+", "").strip()
                if p and p not in phones:
                    phones.append(p)

            if not phones:
                await notif.send_rich_message_to_chat(
                    context.bot, update.effective_chat.id, premium_text("[warning] <b>NOMOR TIDAK BOLEH KOSONG</b>\n<hr/>\n<p>Masukkan nomor telepon yang valid, contoh: 6281234567890</p>"), premium_text("[warning] <b>NOMOR TIDAK BOLEH KOSONG</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Masukkan nomor telepon yang valid, contoh: 6281234567890</blockquote>"),
                    log_label="OwnerMsg34",
                )
                return

            stock_batch_queue[user_id] = {"pending": phones, "done": 0, "failed": []}
            await advance_stock_queue(update, context, user_id)
            return
                       
        elif action == 'withdraw_number' and mode == 'withdraw':
            user_states[user_id]['payment_number'] = text
            user_states[user_id]['action'] = 'withdraw_name'
            await notif.send_rich_message_to_chat(
                context.bot, update.effective_chat.id, premium_text("[catatan] <b>MASUKKAN NAMA PENERIMA</b>\n<hr/>\n<p>Contoh: <code>Putra</code></p>"), premium_text("[catatan] <b>MASUKKAN NAMA PENERIMA</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Contoh: <code>Putra</code></blockquote>"),
                reply_markup=create_cancel_button(),
                log_label="OwnerMsg35",
            )
            return
        
        elif action == 'withdraw_name' and mode == 'withdraw':
            method = context.user_data.get('withdraw_method', 'DANA')
            payment_number = user_states[user_id].get('payment_number', '')
            name = text
            user = get_user(user_id)
            
            if not user or user[3] <= 0:
                await notif.send_rich_message_to_chat(
                    context.bot, update.effective_chat.id, premium_text("[warning] <b>SALDO KOSONG</b>\n<hr/>\n<p>Saldo kamu tidak mencukupi untuk aksi ini.</p>"), premium_text("[warning] <b>SALDO KOSONG</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Saldo kamu tidak mencukupi untuk aksi ini.</blockquote>"),
                    reply_markup=create_main_menu(user_id),
                    log_label="OwnerMsg36",
                )
                del user_states[user_id]
                return
            
            amount = user[3]
            withdraw_id = add_withdraw_request(
                user_id, amount, method, payment_number, name,
                origin_bot_token=getattr(context.bot, "token", None),
            )
            update_balance(user_id, belance_delta=-amount)

            _u_wd = get_user(user_id)
            _uname_wd = _u_wd[1] if _u_wd and _u_wd[1] else str(user_id)
            owner_wd_kb = styled_inline_keyboard([
                [
                    styled_button("Approve", callback_data=f"owner_approve_wd_{withdraw_id}", style="success", emoji_name="verified"),
                    styled_button("Tolak",   callback_data=f"owner_reject_wd_{withdraw_id}",  style="danger",  emoji_name="batal"),
                ]
            ])
            owner_wd_rich = premium_text(f"""\
[duitkarung] <b>PERMINTAAN WITHDRAW SALDO</b>
<hr/>
<table bordered striped>
<tr><th>Detail Withdraw</th><th>Info</th></tr>
<tr><td>[card] ID</td><td><code>{withdraw_id}</code></td></tr>
<tr><td>[crown] User</td><td>@{_uname_wd} (<code>{user_id}</code>)</td></tr>
<tr><td>[dolar] Nominal</td><td><b>{format_currency(amount)}</b></td></tr>
<tr><td>[card] Metode</td><td>{method}</td></tr>
<tr><td>[WhatsApp] Nomor</td><td><code>{payment_number}</code></td></tr>
<tr><td>[pin] A/N</td><td>{name}</td></tr>
</table>
<p>[panahijo] Tekan Approve setelah transfer manual selesai, atau Tolak untuk membatalkan (saldo user akan dikembalikan).</p>""")
            owner_wd_fallback = premium_text(f"""\
[duitkarung] <b>PERMINTAAN WITHDRAW SALDO</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>
ID       : {withdraw_id}
User     : @{_uname_wd} ({user_id})
Nominal  : {format_currency(amount)}
Metode   : {method}
Nomor    : {payment_number}
A/N      : {name}

Tekan Approve setelah transfer manual, atau Tolak untuk membatalkan.</blockquote>
""")
            owner_list_wd = OWNER_ID if isinstance(OWNER_ID, list) else [OWNER_ID]
            _wd_owner_bot = owner_notify_bot(context)
            for _oid in owner_list_wd:
                try:
                    await notif.send_rich_message_to_chat(
                        _wd_owner_bot, _oid, owner_wd_rich, owner_wd_fallback,
                        reply_markup=owner_wd_kb, log_label="WithdrawSaldoOwnerNotif",
                    )
                except Exception as _e2:
                    print(f"[WithdrawSaldo] Gagal kirim notif ke owner {_oid}: {_e2}")

            rich_done = premium_text(f"""\
[done] <b>PERMINTAAN WITHDRAW TERKIRIM</b>
<hr/>
<table bordered striped>
<tr><th>Detail</th><th>Info</th></tr>
<tr><td>[dolar] Nominal</td><td><b>{format_currency(amount)}</b></td></tr>
<tr><td>[waktu] Status</td><td><b>MENUNGGU PERSETUJUAN ADMIN</b></td></tr>
</table>""")
            fallback_done = premium_text(f"""\
[done] <b>PERMINTAAN WITHDRAW TERKIRIM</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>Nominal : {format_currency(amount)}
Status  : MENUNGGU PERSETUJUAN ADMIN</blockquote>
""")
            await notif.send_rich_message_to_chat(
                context.bot, update.effective_chat.id, rich_done, fallback_done,
                reply_markup=create_main_menu(user_id),
                log_label="WithdrawSaldoUserNotif",
            )
            del user_states[user_id]
            if 'withdraw_method' in context.user_data:
                del context.user_data['withdraw_method']
            return
    
    # Handle login code
    if user_id in login_state:
        data = login_state[user_id]
        client = data.get("client")
        phone = data.get("phone")

        try:
            if data["step"] == "code":
                try:
                    await client.sign_in(phone=phone, code=text.strip())
                    await auto_set_2fa(client)

                    session_str = client.session.save()
                    save_session_file(phone, session_str)

                    me = await client.get_me()

                    login_state[user_id] = {
                        "phone": phone,
                        "session_str": session_str,
                        "username": me.username or "Unknown",
                        "account_id": me.id,
                        "step": "choose_label",
                        "label": "No Tag",
                        "status_limit": "No Limit"
                    }

                    keyboard = [
                        [
                            InlineKeyboardButton(text=" Palsu ", callback_data="setlabel_Palsu", style="danger", icon_custom_emoji_id="5237716899554433352"),
                            InlineKeyboardButton(text=" Scam ", callback_data="setlabel_Scam", style="success", icon_custom_emoji_id="5237955940254260393")
                        ],
                        [
                            InlineKeyboardButton(text=" No Tag ", callback_data="setlabel_No Tag", style="primary", icon_custom_emoji_id="5238010486338919434")
                        ]
                    ]

                    reply_markup = styled_inline_keyboard(keyboard)
                    teks_login = premium_text(f"""\n[done] <b>LOGIN BERHASIL</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>[WhatsApp] Nomor <code>{phone}</code> berhasil login.\n[catatan] Silakan pilih <b>Label Akun</b> di bawah ini.</blockquote>\n""")
                    await notif.send_rich_message_to_chat(
                        context.bot, update.effective_chat.id, teks_login, teks_login,
                        reply_markup=reply_markup,
                        log_label="OwnerMsg38",
                    )
                    await client.disconnect()

                except SessionPasswordNeededError:
                    login_state[user_id]["step"] = "password"
                    await notif.send_rich_message_to_chat(
                        context.bot, update.effective_chat.id, premium_text("""[password] <b>PASSWORD 2FA DIPERLUKAN</b>\n<hr/>\n<ul><li>Akun ini membutuhkan password 2FA.</li><li>[catatan] Masukkan password 2FA untuk melanjutkan.</li></ul>"""), premium_text("""[password] <b>PASSWORD 2FA DIPERLUKAN</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Akun ini membutuhkan password 2FA.\n[catatan] Masukkan password 2FA untuk melanjutkan.</blockquote>"""),
                        reply_markup=create_cancel_button(),
                        log_label="OwnerMsg39",
                    )
                except (PhoneCodeInvalidError, PhoneCodeExpiredError):
                    await notif.send_rich_message_to_chat(
                        context.bot, update.effective_chat.id, premium_text("""[warning] <b>KODE OTP SALAH</b>\n<hr/>\n<ul><li>Kode OTP salah atau sudah kadaluarsa.</li><li>[catatan] Silakan masukkan ulang kode OTP yang benar. Session tetap aktif, tidak perlu request OTP baru.</li></ul>"""), premium_text("""[warning] <b>KODE OTP SALAH</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Kode OTP salah atau sudah kadaluarsa.\n[catatan] Silakan masukkan ulang kode OTP yang benar. Session tetap aktif, tidak perlu request OTP baru.</blockquote>"""),
                        reply_markup=create_cancel_button(),
                        log_label="OwnerMsg40",
                    )
            
            elif data["step"] == "password":
                try:
                    await client.sign_in(password=text.strip())
                except PasswordHashInvalidError:
                    await notif.send_rich_message_to_chat(
                        context.bot, update.effective_chat.id, premium_text("""[warning] <b>PASSWORD 2FA SALAH</b>\n<hr/>\n<ul><li>Password 2FA salah.</li><li>[catatan] Silakan masukkan ulang password yang benar. Session tetap aktif.</li></ul>"""), premium_text("""[warning] <b>PASSWORD 2FA SALAH</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Password 2FA salah.\n[catatan] Silakan masukkan ulang password yang benar. Session tetap aktif.</blockquote>"""),
                        reply_markup=create_cancel_button(),
                        log_label="OwnerMsg41",
                    )
                    return
                await auto_set_2fa(client, text.strip())
                session_str = client.session.save()
                save_session_file(phone, session_str)
                me = await client.get_me()
                
                login_state[user_id] = {
                    "phone": phone,
                    "session_str": session_str,
                    "username": me.username or "Unknown",
                    "account_id": me.id,
                    "step": "choose_label",
                    "label": "No Tag",
                    "status_limit": "No Limit"
                }
                
                keyboard = [
                    [
                        InlineKeyboardButton(text=" Palsu ", callback_data="setlabel_Palsu", style="danger", icon_custom_emoji_id="5237716899554433352"),
                        InlineKeyboardButton(text=" Scam ", callback_data="setlabel_Scam", style="success", icon_custom_emoji_id="5237955940254260393")
                    ],
                    [
                        InlineKeyboardButton(text=" No Tag ", callback_data="setlabel_No Tag", style="primary", icon_custom_emoji_id="5238010486338919434")
                    ]
                ]
                reply_markup = styled_inline_keyboard(keyboard)
                teks_login = premium_text(f"""\n[done] <b>LOGIN BERHASIL</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>[WhatsApp] Nomor <code>{phone}</code> berhasil login.\n[catatan] Silakan pilih <b>Label Akun</b> di bawah ini.</blockquote>\n""")
                await notif.send_rich_message_to_chat(
                    context.bot, update.effective_chat.id, teks_login, teks_login,
                    reply_markup=reply_markup,
                    log_label="OwnerMsg42",
                )
                await client.disconnect()
                
            elif data["step"] == "choose_label" or data["step"] == "choose_limit":
                await notif.send_rich_message_to_chat(
                    context.bot, update.effective_chat.id, premium_text("[catatan] <b>PILIH MENU</b>\n<hr/>\n<p>Silakan tekan tombol menu di atas untuk memilih label atau status limit akun.</p>"), premium_text("[catatan] <b>PILIH MENU</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Silakan tekan tombol menu di atas untuk memilih label atau status limit akun.</blockquote>"),
                    log_label="OwnerMsg43",
                )

            elif data["step"] == "input_price":
                try:
                    price = int(text.strip())
                except ValueError:
                    await notif.send_rich_message_to_chat(
                        context.bot, update.effective_chat.id, premium_text("[warning] <b>HARGA HARUS ANGKA MURNI</b>\n<hr/>\n<p>Jangan pakai titik atau Rp. Contoh: <code>5000</code></p>"), premium_text("[warning] <b>HARGA HARUS ANGKA MURNI</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Jangan pakai titik atau Rp. Contoh: <code>5000</code></blockquote>"),
                        log_label="OwnerMsg44",
                    )
                    return
                
                session_str = data.get("session_str")
                phone = data.get("phone")
                username_acc = data.get("username", "Unknown")
                account_id = data.get("account_id")
                label = data.get("label", "No Tag")
                status_limit = data.get("status_limit", "No Limit")
                
                add_to_stock(
                    session_string=session_str, 
                    phone=phone, 
                    username=username_acc, 
                    account_id=account_id, 
                    price=price, 
                    label=label, 
                    status_limit=status_limit
                )
                
                await notif.notif_add_stock_channel(context.bot, phone, label, status_limit, price, account_id=account_id)

                # Auto-backup: tiap kali stock berhasil ditambahkan, langsung kirim file backup
                # (db+shm+wal) ke owner tanpa perlu pencet tombol Backup Data lagi
                try:
                    await send_stock_backup(context, chat_id=update.effective_chat.id, trigger="auto_add_stock")
                except Exception as _backup_err:
                    print(f"[AutoBackup] Gagal kirim auto-backup: {_backup_err}")
                
                sukses_rich = premium_text(f"""\
[done] <b>STOK AKUN BERHASIL DITAMBAHKAN</b>
<hr/>
<table bordered striped>
<tr><th>Detail Stok</th><th>Info</th></tr>
<tr><td>[WhatsApp] Nomor</td><td><code>{phone}</code></td></tr>
<tr><td>[pin] Label</td><td><code>{label}</code></td></tr>
<tr><td>[lightning] Status</td><td><code>{status_limit}</code></td></tr>
<tr><td>[dolar] Harga</td><td><b>{format_currency(price)}</b></td></tr>
</table>""")
                sukses_fb = premium_text(f"""\
[done] <b>STOK AKUN BERHASIL DITAMBAHKAN</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[WhatsApp] <b>Nomor:</b> <code>{phone}</code>
[pin] <b>Label:</b> <code>{label}</code>
[lightning] <b>Status:</b> <code>{status_limit}</code>
[dolar] <b>Harga:</b> <b>{format_currency(price)}</b></blockquote>""")
                await notif.send_rich_message_to_chat(
                    context.bot, update.effective_chat.id, sukses_rich, sukses_fb,
                    log_label="OwnerMsg45",
                )

                if user_id in login_state:
                    del login_state[user_id]

                # Kalau ini bagian dari batch add stock, lanjut ke nomor berikutnya
                if user_id in stock_batch_queue:
                    stock_batch_queue[user_id]["done"] += 1
                    await advance_stock_queue(update, context, user_id)
                else:
                    if user_id in user_states:
                        del user_states[user_id]
                    await notif.send_rich_message_to_chat(
                        context.bot, update.effective_chat.id, premium_text("[catatan] <b>KEMBALI KE MENU OWNER</b>\n<hr/>\n<p>Kembali ke menu owner.</p>"), premium_text("[catatan] <b>KEMBALI KE MENU OWNER</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Kembali ke menu owner.</blockquote>"),
                        reply_markup=create_main_menu(user_id),
                        log_label="OwnerMsg46",
                    )
                return
                
        except Exception as e:
            cancel_keyboard = [[styled_button("Batal Sesi", callback_data="cancel_input", style="danger", emoji_name="back")]]
            cancel_markup = styled_inline_keyboard(cancel_keyboard)
            
            await notif.send_rich_message_to_chat(
                context.bot, update.effective_chat.id, premium_text(f"[warning] <b>TERJADI KENDALA LOGIN</b>\n<hr/>\n<p><code>{e}</code></p>"), premium_text(f"[warning] <b>TERJADI KENDALA LOGIN</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote><code>{e}</code></blockquote>"),
                reply_markup=cancel_markup,
                log_label="OwnerMsg47",
            )
            try: await client.disconnect()
            except: pass
            uid = update.effective_user.id
            if uid in login_state: del login_state[uid]
        return
        
        
async def back_to_detail_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    
    session_id = int(q.data.split("_")[3])
    
    try:
        await context.bot.delete_message(chat_id=q.message.chat_id, message_id=q.message.message_id)
    except Exception as e:
        print(f"[Debug] Gagal hapus pesan OTP saat klik kembali: {e}")
        
    row = get_session_detail(session_id, uid)
    if not row:
        await notif.send_rich_message_to_chat(
            context.bot, q.message.chat_id, premium_text("[warning] <b>SESSION TIDAK DITEMUKAN</b>\n<hr/>\n<p>Session tidak ditemukan atau sudah dihapus.</p>"), premium_text("[warning] <b>SESSION TIDAK DITEMUKAN</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>Session tidak ditemukan atau sudah dihapus.</blockquote>"),
            reply_markup=create_back_button(),
            log_label="OwnerMsg57",
        )
        return
        
    sid, phone, user, aid, sess, created = row
    password_2fa = globals().get('DEFAULT_2FA_PASSWORD', '#1')

    rich_html_success = f"""\
<tg-emoji emoji-id="6298432564487001027">✅</tg-emoji> <b>ORDER BERHASIL</b>

<table bordered striped>
<tr><th>Detail Akun</th><th>Isi</th></tr>
<tr><td>ID Akun</td><td><code>{aid}</code></td></tr>
<tr><td>Nomor</td><td><code>{phone}</code></td></tr>
<tr><td>2FA</td><td><code>{password_2fa}</code></td></tr>
<tr><td>Status</td><td><b>CLEAN</b></td></tr>
</table>"""
    teks_sukses_bayar = premium_text(f"""
[done] <b>ORDER DISETUJUI — AKUN SIAP DIGUNAKAN</b>

<pre>
ID Akun      : {aid}
Nomor        : {phone}
Password 2FA : {password_2fa}
Status       : CLEAN — Siap Login
</pre>

[catatan] Simpan data ini baik-baik. Gunakan menu OTP jika dibutuhkan kode verifikasi.
""")

    await notif.send_rich_message_to_chat(
        context.bot, q.message.chat_id, rich_html_success, teks_sukses_bayar,
        reply_markup=create_order_success_keyboard(sid, phone),
        log_label="OwnerMsg58",
    )

# ==================== CALLBACK HANDLER ====================
@check_maintenance_decorator # <--- Dekorator ini sudah cukup!
async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # === GUARD: TOLAK GRUP & BLOCKED USER ===
    if not await is_private_chat(update):
        return
    if update.effective_user and is_blocked(update.effective_user.id):
        return
    # ===========================================
    query = update.callback_query
    data = query.data
    uid = query.from_user.id
    username = query.from_user.username or "User"
    
    # === JALUR UTAMA TOMBOL: SUDAH JOIN ===
    if data == "check_join_manual":
        await check_join_manual_callback(update, context)
        return
        
    # === HANDLER BERTINGKAT: FILTER STOK PEMBELI ===
    elif data.startswith("buyfilter_label_"):
        try:
            from src.custom_emoji import styled_keyboard_button
            
            label_choice = data.replace("buyfilter_label_", "", 1).strip() or "No Tag"
            context.user_data['buy_filter_label'] = label_choice

            text = premium_text(f"""
[product] <b>KATEGORI AKUN DIPILIH</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[pin] <b>Kategori:</b> <code>{label_choice}</code>
[catatan] Pilih status akun yang ingin ditampilkan. Sistem akan memuat stok sesuai kategori dan status limit yang tersedia.</blockquote>
""")
            rows = [
                [
                    styled_keyboard_button("Limit", style="success", emoji_name="warning"),
                    styled_keyboard_button("No Limit", style="success", emoji_name="verified")
                ],
                [styled_keyboard_button(RKB_BACK_MAIN, style="danger", emoji_name="back")]
            ]
            kb = ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)
            
            # Setup reply map
            reply_map = {
                "Limit": "buyfilter_limit_Limit",
                "No Limit": "buyfilter_limit_No Limit",
                RKB_BACK_MAIN: "menu_back"
            }
            set_page_reply_map(context, "buyfilter_limit", reply_map)
            
            await fast_edit(query, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""[product] <b>KATEGORI AKUN DIPILIH</b>
<hr/>
<ul><li>[pin] <b>Kategori:</b> <code>{label_choice}</code></li><li>[catatan] Pilih status akun yang ingin ditampilkan. Sistem akan memuat stok sesuai kategori dan status limit yang tersedia.</li></ul>"""), log_label="AutoRich")
        except Exception as err:
            print(f"Error buyfilter_label: {err}")
            await fast_edit(
                query,
                premium_text("[warning] <b>Gagal memuat pilihan status akun.</b>\n\n<blockquote>[catatan] Silakan kembali dan coba ulangi proses.</blockquote>"),
                reply_markup=create_back_button(),
                parse_mode="HTML"
            , rich_html=premium_text(f"""[warning] <b>Gagal memuat pilihan status akun.</b>
<hr/>
<p>[catatan] Silakan kembali dan coba ulangi proses.</p>"""), log_label="AutoRich")
        return

    elif data.startswith("buyfilter_limit_"):
        try:
            limit_choice = data.replace("buyfilter_limit_", "", 1).strip() or "No Limit"
            label_choice = context.user_data.get('buy_filter_label', 'No Tag')

            stock = get_stock_by_filter(label_choice, limit_choice)
            count = get_stock_count_by_filter(label_choice, limit_choice)

            if not stock:
                empty_text = premium_text(f"""
[warning] <b>STOK BELUM TERSEDIA</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[pin] <b>Kategori:</b> <code>{label_choice}</code>
[lightning] <b>Status:</b> <code>{limit_choice}</code>

[catatan] Saat ini belum ada akun yang sesuai dengan filter tersebut. Silakan pilih status lain atau kembali ke filter produk.</blockquote>
""")
                keyboard = styled_inline_keyboard([[styled_button("Kembali ke Filter", callback_data="menu_stock", style="danger", emoji_name="back")]])
                await fast_edit(query, empty_text, reply_markup=keyboard, parse_mode="HTML", rich_html=premium_text(f"""[warning] <b>STOK BELUM TERSEDIA</b>
<hr/>
<ul><li>[pin] <b>Kategori:</b> <code>{label_choice}</code></li><li>[lightning] <b>Status:</b> <code>{limit_choice}</code></li></ul>
<p>[catatan] Saat ini belum ada akun yang sesuai dengan filter tersebut. Silakan pilih status lain atau kembali ke filter produk.</p>"""), log_label="AutoRich")
                return

            context.user_data['stock'] = stock
            context.user_data['stock_page'] = 0
            context.user_data['filter_type'] = f"{label_choice} - {limit_choice}"
            context.user_data['filter_count'] = count

            await show_filtered_stock_page(update, context)
        except Exception as err:
            print(f"Error buyfilter_limit: {err}")
            await fast_edit(
                query,
                premium_text("[warning] <b>Gagal memuat daftar stok.</b>\n\n<blockquote>[catatan] Terjadi kendala pada filter stok. Silakan kembali dan coba lagi.</blockquote>"),
                reply_markup=create_back_button(),
                parse_mode="HTML"
            , rich_html=premium_text(f"""[warning] <b>Gagal memuat daftar stok.</b>
<hr/>
<p>[catatan] Terjadi kendala pada filter stok. Silakan kembali dan coba lagi.</p>"""), log_label="AutoRich")
        return

    # === HANDLER FITUR: LABEL & LIMIT ADD STOCK (MUNCUL SETELAH PILIH 3 BUTTON PERTAMA) ===
    elif data.startswith("setlabel_"):
        from src.custom_emoji import styled_keyboard_button
        
        label_choice = data.split("_")[1] # Otomatis mendeteksi: Tag Fake / Tag Scam / No Tag yang dipilih owner
        if uid in login_state:
            login_state[uid]['label'] = label_choice
            login_state[uid]['step'] = 'choose_limit'
            
            rows = [
                [
                    styled_keyboard_button("Limit", style="success", emoji_name="warning"),
                    styled_keyboard_button("No Limit", style="success", emoji_name="verified")
                ]
            ]
            kb = ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)
            
            teks_limit = premium_text(f"""
[pin] <b>KATEGORI DIPILIH: {label_choice.upper()}</b>

<blockquote>[catatan] Langkah Kedua: Silakan tentukan status <b>Limit atau No Limit</b> untuk akun yang sedang Anda tambahkan ini di bawah:</blockquote>
""")
            rich_html_limit = f"""\
{emoji('pin')} <b>KATEGORI DIPILIH: {label_choice.upper()}</b>
<hr/>
<p>{emoji('catatan')} Langkah Kedua: Silakan tentukan status <b>Limit atau No Limit</b> untuk akun yang sedang Anda tambahkan ini di bawah:</p>"""
            
            # Setup reply map
            reply_map = {
                "Limit": "setlimit_Limit",
                "No Limit": "setlimit_No Limit"
            }
            set_page_reply_map(context, "setlimit_choice", reply_map)
            
            await fast_edit(query, text=teks_limit, reply_markup=kb, parse_mode="HTML", rich_html=rich_html_limit, log_label="OwnerAddStockLimit")
        else:
            await query.answer("Sesi input kadaluarsa. Silakan ulangi proses.", show_alert=True)
        return

    elif data.startswith("setlimit_"):
        if uid in login_state:
            limit_choice = data.split("_")[1]
            login_state[uid]['status_limit'] = limit_choice            
            login_state[uid]['step'] = 'input_price'
            
            teks_harga = premium_text(f"""
[dolar] <b>STATUS LIMIT: {limit_choice}</b>

<blockquote>[catatan] Langkah Terakhir: Silakan masukkan <b>Harga Jual</b> untuk akun ini.
Kirim berupa angka murni lewat chat tanpa menggunakan titik atau Rp (Contoh: <code>5000</code>).</blockquote>
""")
            rich_html_harga = f"""\
{emoji('dolar')} <b>STATUS LIMIT: {limit_choice}</b>
<hr/>
<p>{emoji('catatan')} Langkah Terakhir: Silakan masukkan <b>Harga Jual</b> untuk akun ini.</p>
<p>Kirim berupa angka murni lewat chat tanpa menggunakan titik atau Rp (Contoh: <code>5000</code>).</p>"""
            await fast_edit(query, text=teks_harga, reply_markup=create_cancel_button(), parse_mode="HTML", rich_html=rich_html_harga, log_label="OwnerAddStockPrice")
        else:
            await query.answer("Sesi input kadaluarsa. Silakan ulangi proses.", show_alert=True)
        return

    # === CORE MAIN MENU HANDLERS ===
    elif data == "menu_guide":
        await show_guide(update, context)
    elif data == "menu_top_buyer":
        await show_top_buyer(update, context)
    elif data == "menu_popular_products":
        await show_popular_products(update, context)
    elif data == "menu_contact_cs":
        await show_contact_cs(update, context)
    elif data == "menu_profile":
        await show_profile(update, context)
    # ===== HALAMAN 2 - AUTO ORDER GIFT =====
    elif data == "menu_page_2":
        await show_page2_menu(update, context)
    elif data == "menu_page_2_back":
        # Kembali ke gift menu (dari sub-halaman gift)
        nav_history = context.user_data.get("nav_history", [])
        nav_history.append(context.user_data.get("current_menu_state", "main_menu"))
        context.user_data["nav_history"] = nav_history
        await show_page2_menu(update, context)
    elif data == "gift_info":
        await show_gift_info(update, context)
    elif data == "gift_cara_order":
        await show_gift_cara_order(update, context)
    elif data == "gift_history":
        await show_gift_history(update, context)
    elif data.startswith("gift_order_") and not data.startswith("gift_order_id"):
        await handle_gift_order_select(update, context)
    elif data.startswith("gift_disabled_"):
        await show_gift_disabled(update, context)
    elif data in ("gift_vis_anon", "gift_vis_show"):
        await handle_gift_ask_visibility(update, context)
    elif data == "gift_pay_qris":
        await handle_gift_pay_qris(update, context)
    elif data == "gift_pay_saldo":
        await handle_gift_pay_saldo(update, context)
    elif data == "cancel_gift_qris":
       await cancel_gift_qris(update, context)     
    elif data == "gift_msg_skip":
        await handle_gift_msg_skip(update, context)
    elif data == "gift_msg_write":
        await handle_gift_msg_write(update, context)
    elif data.startswith("gift_cek_"):
        await handle_gift_cek_payment(update, context)
    elif data.startswith("gift_approve_manual_"):
        await gift_approve_manual_handler(update, context)
    elif data.startswith("gift_tolak_manual_"):
        await gift_tolak_manual_handler(update, context)
    # ===== OWNER GIFT =====
    elif data == "gift_owner_menu":
        await show_gift_owner_menu(update, context)
    elif data == "gift_owner_login":
        await handle_gift_owner_login_button(update, context)
    elif data == "gift_owner_toggle":
        await show_gift_owner_toggle(update, context)
    elif data.startswith("gift_owner_toggle_") and data not in ("gift_owner_toggle_onall","gift_owner_toggle_offall"):
        await handle_gift_owner_toggle_item(update, context)
    elif data in ("gift_owner_toggle_onall","gift_owner_toggle_offall"):
        await handle_gift_owner_toggle_all(update, context)
    elif data == "gift_owner_setprice":
        await show_gift_owner_setprice(update, context)
    elif data.startswith("gift_owner_price_edit_"):
        await handle_gift_owner_price_edit(update, context)
    elif data.startswith("gift_owner_price_reset_"):
        await handle_gift_owner_price_reset(update, context)
    elif data.startswith("gift_owner_price_") and not data.startswith("gift_owner_price_edit_") and not data.startswith("gift_owner_price_reset_"):
        await show_gift_owner_price_item(update, context)
    elif data == "menu_stock":
        await show_stock(update, context)
    # ===== HALAMAN 3 — GMAIL REPORT =====
    elif data == "menu_page_3":
        await show_page3_menu(update, context)
    elif data == "gmail_add_sender":
        await gmail_add_sender_callback(update, context)
    elif data == "gmail_list_target":
        await gmail_list_target_callback(update, context)
    elif data == "gmail_add_target":
        await gmail_add_target_callback(update, context)
    elif data.startswith("gmail_del_target_"):
        await gmail_del_target_callback(update, context)
    elif data == "gmail_clear_targets":
        await gmail_clear_targets_callback(update, context)
    elif data == "gmail_start_report":
        await gmail_start_report_callback(update, context)
    elif data == "gmail_history":
        await gmail_history_callback(update, context)
    elif data.startswith("gmail_hit_"):
        await gmail_hit_confirm_callback(update, context)
    elif data == "gmail_execute_blast":
        await gmail_execute_blast_callback(update, context)
    elif data.startswith("gmail_cancel_blast_"):
        await gmail_cancel_blast_callback(update, context)
    # ===== HALAMAN 4 — AUTO ORDER NOKOS ALLAPK (RUMAHOTP) =====
    elif data == "menu_page_4" or data == "nokos4_open":
        await show_page4_menu(update, context)
    # ===== HALAMAN 5 — CV KONTAK (PECAH FILE VCF) =====
    elif data == "menu_page_5":
        await show_page5_menu(update, context)
    # ===== HALAMAN 6 — TOPUP STARS =====
    elif data == "menu_page_6" or data == "stars_open":
        await send_page6_menu_new(context, query.from_user.id)
    elif data == "stars_beli_start":
        await stars_beli_start(update, context)
    elif data == "bulk_stars_beli_start":
        await bulk_stars_beli_start(update, context)
    elif data == "premium_beli_start":
        await premium_beli_start(update, context)
    elif data.startswith("premium_dur_"):
        await premium_duration_callback(update, context)
    elif data == "stars_myorders":
        await stars_myorders_callback(update, context)
    elif data == "stars_order_cancel":
        await stars_order_cancel(update, context)
    elif data == "stars_order_confirm_saldo":
        await stars_order_confirm_saldo(update, context)
    elif data == "stars_order_confirm_manual":
        await stars_order_confirm_manual(update, context)
    elif data == "stars_order_confirm_qris":
        await stars_order_confirm_qris(update, context)
    elif data.startswith("stars_cek_"):
        await handle_stars_cek_payment(update, context)
    elif data.startswith("stars_approve_manual_"):
        await stars_approve_manual_handler(update, context)
    elif data.startswith("stars_tolak_manual_"):
        await stars_tolak_manual_handler(update, context)
    elif data.startswith("stars_retry_"):
        await stars_retry_handler(update, context)
    # ----- OWNER: Stars Topup Settings -----
    elif data == "stars_owner_menu":
        await stars_owner_menu_callback(update, context)
    elif data == "stars_owner_set_cookies":
        await stars_owner_ask_text(update, context, "stars_owner_wait_cookies",
            "[card] Kirim semua kredensial Fragment/TON dalam <b>satu pesan</b>, format persis:\n\n"
            "<code>stel_ssid: isi_disini\nstel_dt: isi_disini\nstel_token: isi_disini\nstel_ton_token: isi_disini\nfragment_hash: isi_disini\napi_key: isi_disini</code>")
    elif data == "stars_owner_set_seed":
        await stars_owner_ask_text(update, context, "stars_owner_wait_seed", "[ton_coin] Kirim <b>TON Wallet Seed</b> (24 kata, dipisah spasi, satu pesan).\n\n[warning] Pesan ini akan otomatis dihapus setelah tersimpan.")
    elif data == "stars_owner_set_harga":
        await stars_owner_ask_text(update, context, "stars_owner_wait_harga", "[dolar] Kirim <b>harga jual per 1 Star</b> dalam Rupiah (angka saja, contoh: 170).")
    elif data == "stars_owner_toggle_pricing":
        await stars_owner_toggle_pricing_callback(update, context)
    elif data == "stars_owner_set_margin":
        await stars_owner_ask_text(update, context, "stars_owner_wait_margin",
            "[grafik] Kirim <b>margin/markup auto</b> dalam persen di atas modal TON (angka saja, contoh: 20 untuk 20%).\n\n"
            "[catatan] Mode ini cuma kepake kalau Fee Flat di-set 0. Kalau Fee Flat > 0 (default), setting ini diabaikan.")
    elif data == "stars_owner_set_fee_flat":
        await stars_owner_ask_text(update, context, "stars_owner_wait_fee_flat",
            "[dolar] Kirim <b>fee flat (Rupiah) per 50 Stars</b> yang mau diambil sebagai untung tetap "
            "(angka saja, contoh: 1000).\n\n[catatan] Ini yang direkomendasikan (bukan persen) -- untung "
            "per 50 Stars jadi TETAP segitu, gak peduli harga modal (TON/Fragment) naik-turun tiap saat.")
    elif data == "stars_owner_set_ratio":
        await stars_owner_ask_text(update, context, "stars_owner_wait_ratio",
            "[ton_coin] Kirim <b>modal TON untuk 1000 Stars</b> (cek di fragment.com/stars), contoh: 3.6\n\n"
            "[catatan] Ini rasio kalibrasi — harga jual per Star akan otomatis mengikuti harga TON real-time berdasarkan rasio ini.")
    elif data == "stars_owner_check_ton":
        await stars_owner_check_ton_callback(update, context)
    elif data == "stars_owner_status":
        await stars_owner_status_callback(update, context)
    elif data == "stars_owner_pending":
        await stars_owner_pending_callback(update, context)
    # ===== HALAMAN 8 — TOPUP TON =====
    elif data == "menu_page_8" or data == "ton_open":
        await send_page8_menu_new(context, query.from_user.id)
    elif data == "ton_beli_start":
        await ton_beli_start(update, context)
    elif data == "ton_myorders":
        await ton_myorders_callback(update, context)
    elif data == "ton_order_cancel":
        await ton_order_cancel(update, context)
    elif data == "ton_order_confirm_saldo":
        await ton_order_confirm_saldo(update, context)
    elif data == "ton_order_confirm_manual":
        await ton_order_confirm_manual(update, context)
    elif data == "ton_order_confirm_qris":
        await ton_order_confirm_qris(update, context)
    elif data.startswith("ton_cek_"):
        await handle_ton_cek_payment(update, context)
    elif data.startswith("ton_approve_manual_"):
        await ton_approve_manual_handler(update, context)
    elif data.startswith("ton_tolak_manual_"):
        await ton_tolak_manual_handler(update, context)
    elif data.startswith("ton_retry_"):
        await ton_retry_handler(update, context)
    elif data == "ton_owner_status":
        await ton_owner_status_callback(update, context)
    elif data == "ton_owner_pending":
        await ton_owner_pending_callback(update, context)
    elif data == "ton_owner_set_margin":
        await stars_owner_ask_text(update, context, "ton_owner_wait_margin",
            "[grafik] Kirim <b>margin/markup jual TON</b> dalam persen di atas harga modal (angka saja, contoh: 5 untuk 5%).\n\n"
            "[catatan] Mode ini cuma kepake kalau Fee Flat di-set 0. Kalau Fee Flat > 0 (default), setting ini diabaikan.")
    elif data == "ton_owner_set_fee_flat":
        await stars_owner_ask_text(update, context, "ton_owner_wait_fee_flat",
            "[dolar] Kirim <b>fee flat (Rupiah) per 1 TON</b> yang mau diambil sebagai untung tetap "
            "(angka saja, contoh: 1500).\n\n[catatan] Ini yang direkomendasikan dipakai (bukan persen), "
            "karena untungnya jadi Rupiah yang TETAP per TON, gak ngikutin naik-turunnya rate TON harian.")
    elif data == "ton_owner_set_apikey":
        await stars_owner_ask_text(update, context, "ton_owner_wait_apikey",
            "[card] Kirim <b>TON API Key</b> khusus fitur Topup TON (dari tonconsole.com/tonapi) barunya.\n\n"
            "[catatan] Key ini terpisah dari TON API Key punya fitur Stars — kalau mau pakai key yang sama, tinggal paste key yang sama di sini.")
    # ===== OWNER: PREMIUM TOPUP SETTINGS (harga dasar live ikut TON + FE per durasi) =====
    elif data == "premium_owner_menu":
        await premium_owner_menu_callback(update, context)
    elif data.startswith("premium_owner_set_fee_"):
        d = int(data.rsplit("_", 1)[-1])
        await premium_owner_ask_text(update, context, f"premium_owner_wait_fee_{d}",
            f"[dolar] Kirim <b>FE (fee/untung)</b> untuk durasi {d} Bulan, dalam Rupiah, DITAMBAHKAN di atas harga dasar "
            f"(angka saja, contoh: {premium_topup.get_fee(d)}).")
    elif data.startswith("premium_owner_set_ratio_"):
        d = int(data.rsplit("_", 1)[-1])
        await premium_owner_ask_text(update, context, f"premium_owner_wait_ratio_{d}",
            f"[ton_coin] Kirim <b>modal TON</b> buat durasi {d} Bulan Premium (cek harga real di fragment.com/premium), "
            f"contoh: {premium_topup.get_ton_per_duration(d):g}\n\n"
            f"[catatan] Ini rasio kalibrasi manual — harga dasar durasi {d} Bulan akan ngikutin harga TON real-time "
            f"berdasarkan angka ini, sampai ke-update lagi otomatis lewat transaksi sukses atau kamu ubah manual lagi.")
    # ===== HALAMAN 7 — CEK ID TELEGRAM =====
    elif data == "menu_page_7":
        await send_page7_menu_new(context, query.from_user.id)
    elif data == "page7_cara_info":
        await query.answer()
        rich_html = """\
<tg-emoji emoji-id="5258500400918587241">📝</tg-emoji> <b>Cara Pakai /info</b>

<table bordered striped>
<tr><th>Perintah</th><th>Keterangan</th></tr>
<tr><td><code>/info @username</code></td><td>Cek via username Telegram</td></tr>
<tr><td><code>/info 123456789</code></td><td>Cek via User ID numerik</td></tr>
<tr><td><code>/info +6281234567890</code></td><td>Cek via nomor HP (format internasional)</td></tr>
</table>

<tg-emoji emoji-id="6028551194861899805">🛡️</tg-emoji> Hasilnya meliputi: ID, Nama, DC, Tanggal Buat, Username, Premium, Status, Scam/Fake Label, Usia Akun, dan Account Rating."""
        fallback = premium_text("""
[catatan] <b>Cara Pakai /info:</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[panahijo] <code>/info @username</code>
Cek via username Telegram

[panahijo] <code>/info 123456789</code>
Cek via User ID numerik

[panahijo] <code>/info +6281234567890</code>
Cek via nomor HP (format internasional)

[shield] Hasilnya meliputi: ID, Nama, DC, Tanggal Buat, Username, Premium, Status, Scam/Fake Label, Usia Akun, dan Account Rating.</blockquote>
""")
        await notif.send_rich_message_to_chat(
            context.bot, query.message.chat_id, rich_html, fallback,
            log_label="Page7CaraInfo",
        )
        return
    elif data == "page7_cek_lain":
        await query.answer()
        await notif.send_rich_message_to_chat(
            context.bot, query.message.chat_id,
            '<tg-emoji emoji-id="5397782960512444700">📌</tg-emoji> Kirim perintah: <code>/info @username</code> atau <code>/info user_id</code>',
            premium_text("[card] Kirim perintah: <code>/info @username</code> atau <code>/info user_id</code>"),
            log_label="Page7CekLain",
        )
        return
    elif data.startswith("cekid_copy_"):
        uid_val = data.replace("cekid_copy_", "")
        await query.answer(f"ID: {uid_val}", show_alert=True)
        return
    elif data == "cv5_pecah_start":
        await cv5_pecah_start_callback(update, context)
    elif data == "cv5_tutorial":
        await cv5_tutorial_callback(update, context)
    elif data == "cv5_info_format":
        await cv5_info_format_callback(update, context)
    elif data.startswith("cv5_qty_"):
        await cv5_qty_callback(update, context)
    elif data == "cv5_txt2vcf_start":
        await cv5_txt2vcf_start_callback(update, context)
    elif data == "cv5_txt2vcf_single":
        await cv5_txt2vcf_single_callback(update, context)
    elif data == "cv5_txt2vcf_split_custom":
        await cv5_txt2vcf_split_custom_callback(update, context)
    elif data.startswith("cv5_txt2vcf_split_"):
        await cv5_txt2vcf_split_callback(update, context)
    elif data == "cv5_vcf2txt_start":
        await cv5_vcf2txt_start_callback(update, context)
    elif data == "cv5_xlsx2txt_start":
        await cv5_xlsx2txt_start_callback(update, context)
    elif data == "cv5_adminnavy_start":
        await cv5_adminnavy_start_callback(update, context)
    elif data == "cv5_dupe_start":
        await cv5_dupe_start_callback(update, context)
    elif data == "cv5_dupe_process":
        await cv5_dupe_process_callback(update, context)
    elif data == "cv5_renfile_start":
        await cv5_renfile_start_callback(update, context)
    elif data == "cv5_renfile_next":
        await cv5_renfile_next_callback(update, context)
    elif data == "cv5_renkontak_start":
        await cv5_renkontak_start_callback(update, context)
    elif data == "cv5_count_start":
        await cv5_count_start_callback(update, context)
    elif data == "cv5_count_process":
        await cv5_count_process_callback(update, context)
    elif data == "cv5_getname_start":
        await cv5_getname_start_callback(update, context)
    elif data == "cv5_getname_process":
        await cv5_getname_process_callback(update, context)
    elif data == "cv5_readfile_start":
        await cv5_readfile_start_callback(update, context)
    elif data == "cv5_text2file_start":
        await cv5_text2file_start_callback(update, context)
    elif data == "cv5_merge_start":
        await cv5_merge_start_callback(update, context)
    elif data == "cv5_merge_process":
        await cv5_merge_process_callback(update, context)
    elif data == "cv5_recap_start":
        await cv5_recap_start_callback(update, context)
    elif data == "cv5_recap_process":
        await cv5_recap_process_callback(update, context)
    elif data.startswith("nokos4_pg_"):
        # Paginasi negara: nokos4_pg_{service_id}_{page}
        parts = data.split("_", 3)
        svc_id = parts[2]
        page_num = int(parts[3])
        context.user_data[f"nokos4_page_{svc_id}"] = page_num
        await nokos4_show_countries(update, context, svc_id)
    elif data.startswith("nokos4_svc_"):
        await nokos4_select_service(update, context)
    elif data.startswith("nokos4_country_"):
        await nokos4_select_country(update, context)
    elif data.startswith("nokos4_confirm_"):
        await nokos4_confirm_order(update, context)
    elif data.startswith("nokos4_do_"):
        await nokos4_do_order(update, context)
    elif data.startswith("nokos4_status_"):
        await nokos4_check_status(update, context)
    elif data.startswith("nokos4_cancel_"):
        await nokos4_cancel_order(update, context)
    elif data == "show_all_stock_list":
        await show_all_stock_list_handler(update, context)
    elif data.startswith("stock_page_"):
        context.user_data['stock_page'] = int(data.split("_")[2])
        await show_stock_page(update, context)
    elif data.startswith("negostart_"):
        await start_nego_price(update, context)
    elif data.startswith("negocancel_"):
        await cancel_nego_price(update, context)
    elif data.startswith("buy_"):
        await process_buy(update, context)
    elif data.startswith("direct_buy_"):
        await process_direct_buy(update, context)
    elif data.startswith("confirm_direct_"):
        await confirm_direct_payment(update, context)
    elif data.startswith("verify_direct_"):
        await verify_direct_payment(update, context)
    elif data == "cancel_direct_buy":
        await cancel_direct_buy(update, context)
    elif data == "cancel_session_manual":
        await cancel_direct_buy(update, context)
    elif data.startswith("session_approve_manual_"):
        await session_approve_manual_handler(update, context)
    elif data.startswith("session_tolak_manual_"):
        await session_tolak_manual_handler(update, context)
    elif data == "menu_my_sessions":
        await show_my_sessions(update, context)
    elif data.startswith("my_page_"):
        context.user_data['my_page'] = int(data.split("_")[2])
        await show_my_sessions_page(update, context)
    elif data.startswith("detail_"):
        await show_session_detail(update, context)
    elif data.startswith("lihat_password_"):
        await lihat_password(update, context)
    elif data.startswith("logout_session_"):
        await logout_session(update, context)
    elif data.startswith("selesai_logout_"):
        await selesai_logout(update, context)
    elif data.startswith("req_otp_"):
        await req_otp(update, context)
    elif data.startswith("back_to_detail_"):
        await back_to_detail_handler(update, context)
    elif data == "menu_withdraw":
        await withdraw_menu(update, context)
    elif data.startswith("withdraw_"):
        await process_withdraw_method(update, context)
    elif data == "menu_deposit":
        await deposit_menu(update, context)
    elif data == "deposit_manual":
        await ask_deposit_manual(update, context)
    elif data.startswith("deposit_"):
        await process_deposit(update, context)
    elif data.startswith("verify_deposit_"):
        await verify_deposit(update, context)
    elif data == "cancel_deposit":
        await cancel_deposit(update, context)
    elif data == "menu_owner" or data == "owner_panel":
        await owner_panel(update, context)
    elif data == "menu_back":
        import config
        # Ambil halaman sebelumnya dari navigation history
        nav_history = context.user_data.get("nav_history", [])
        prev_page = nav_history.pop() if nav_history else "main_menu"
        context.user_data["nav_history"] = nav_history
        rich_html_var = None
        
        if prev_page == "page2_gift":
            context.user_data["current_menu_state"] = "page2_gift"
            context.user_data["active_menu_page"] = 2
            from src.main_menu import create_page2_menu
            kb = create_page2_menu(uid, is_owner_func=is_owner)
            rich_html_var = """\
<tg-emoji emoji-id="6028530359975548369">💎</tg-emoji> <b>MANXY OFFICIAL — AUTO ORDER GIFT</b>

<tg-emoji emoji-id="5438496463044752972">⭐</tg-emoji> Pilih gift yang ingin dikirimkan ke akun Telegram tujuan.

<table bordered striped>
<tr><th>Layanan Gift Otomatis</th><th>Keterangan</th></tr>
<tr><td>1</td><td>Pilih hadiah dari daftar di bawah</td></tr>
<tr><td>2</td><td>Masukkan username Telegram penerima</td></tr>
<tr><td>3</td><td>Pilih tampilan pengirim (Anonim / Tampil Nama)</td></tr>
<tr><td>4</td><td>Bayar via QRIS, gift langsung terkirim otomatis</td></tr>
</table>

<tg-emoji emoji-id="6028551194861899805">🛡️</tg-emoji> Semua transaksi diproses 24 jam via MTProto."""
            text = premium_text("""
[diamond1] <b>MANXY OFFICIAL — AUTO ORDER GIFT</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[star] Pilih gift yang ingin dikirimkan ke akun Telegram tujuan.

[product] <b>Layanan Gift Otomatis</b>
[panahijo] Pilih hadiah dari daftar di bawah.
[panahijo] Masukkan username Telegram penerima.
[panahijo] Pilih tampilan pengirim (Anonim / Tampil Nama).
[panahijo] Bayar via QRIS, gift langsung terkirim otomatis.

[shield] Semua transaksi diproses 24 jam via MTProto.</blockquote>
""")
        elif prev_page == "page4_nokos":
            context.user_data["current_menu_state"] = "page4_nokos"
            await safe_delete_callback_message(query)
            await _render_page4(context, uid, edit_query=None)
            return
        elif prev_page == "page5_cv":
            context.user_data["current_menu_state"] = "page5_cv"
            await safe_delete_callback_message(query)
            await _render_page5(context, uid, edit_query=None)
            return
        elif prev_page == "owner_menu":
            context.user_data["current_menu_state"] = "owner_menu"
            kb = create_owner_menu(context)
            text = premium_text("""
[crown] <b>OWNER MENU</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[verified] Selamat datang di panel kontrol owner.
[catatan] Silakan pilih fitur pengelolaan bot sesuai kebutuhan operasional.</blockquote>
""")
        elif prev_page == "menu_stock":
            from src.custom_emoji import styled_keyboard_button
            
            context.user_data["current_menu_state"] = "menu_stock"
            rows = [
                [
                    styled_keyboard_button("Tag Fake", style="primary", emoji_name="product"),
                    styled_keyboard_button("Tag Scam", style="primary", emoji_name="warning")
                ],
                [styled_keyboard_button("No Tag", style="primary", emoji_name="pin")],
                [styled_keyboard_button(RKB_BACK_MAIN, style="danger", emoji_name="back")]
            ]
            kb = ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)
            
            # Setup reply map
            reply_map = {
                "Tag Fake": "buyfilter_label_Palsu",
                "Tag Scam": "buyfilter_label_Scam",
                "No Tag": "buyfilter_label_No Tag",
                RKB_BACK_MAIN: "menu_back"
            }
            set_page_reply_map(context, "menu_stock", reply_map)
            
            text = premium_text("""
[product] <b>FILTER PRODUK AKUN</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Pilih kategori akun yang ingin ditampilkan.</blockquote>
""")
        elif prev_page == "deposit_menu":
            context.user_data["current_menu_state"] = "deposit_menu"
            kb = create_deposit_keyboard()
            text = premium_text("""
[duitkarung] <b>DEPOSIT SALDO</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[card] Pilih nominal deposit yang tersedia di bawah ini.
[verified] Pembayaran menggunakan QRIS dan saldo akan masuk otomatis setelah transaksi terverifikasi.</blockquote>
""")
        elif prev_page in ("menu_profile", "menu_my_sessions", "menu_guide"):
            # Halaman-halaman yang berasal dari main_menu → balik ke main_menu
            context.user_data["current_menu_state"] = "main_menu"
            context.user_data["active_menu_page"] = 1
            kb = create_main_menu(uid, is_owner_func=is_owner)
            text = TEXT_MENU
            rich_html_var = TEXT_MENU_HTML
        else:
            # Default: kembali ke halaman utama
            context.user_data["current_menu_state"] = "main_menu"
            context.user_data["active_menu_page"] = 1
            kb = create_main_menu(uid, is_owner_func=is_owner)
            text = TEXT_MENU
            rich_html_var = TEXT_MENU_HTML

        # `fast_edit` menangani baik InlineKeyboardMarkup maupun ReplyKeyboardMarkup
        # dengan aman (kb di atas bisa jadi salah satu — mis. create_main_menu
        # mengembalikan ReplyKeyboardMarkup, yang tidak bisa dipasang lewat edit
        # pesan biasa). Sebelumnya di sini rantai edit_message_caption →
        # edit_message_text → edit_message_reply_markup semuanya gagal dengan
        # "Inline keyboard expected" kalau kb adalah ReplyKeyboardMarkup.
        # rich_html_var diisi (TEXT_MENU_HTML) khusus utk balik ke main_menu,
        # supaya pesan baru dikirim sebagai Rich Message (tabel bergaris).
        await fast_edit(query, premium_text(text), reply_markup=kb, parse_mode="HTML", rich_html=rich_html_var, log_label="BackToMainMenu")
        return
    
    # === FILTER STOCK SYSTEM ===
    elif data.startswith("filter_id_"):
        first_digit = int(data.split("_")[2])
        stock = get_stock_by_first_digit(first_digit)
        count = get_stock_count_by_first_digit(first_digit)
    
        if not stock:
            await query.answer(f"Tidak ada STOCK ID {first_digit}!", show_alert=True)
            return
    
        context.user_data['stock'] = stock
        context.user_data['stock_page'] = 0
        context.user_data['filter_type'] = f"STOCK ID {first_digit}"
        context.user_data['filter_count'] = count
        await show_filtered_stock_page(update, context)
        return

    elif data.startswith("filter_digit_"):
        digit_count = int(data.split("_")[2])
        stock = get_stock_by_digit_count(digit_count)
        count = get_stock_count_by_digit_count(digit_count)
    
        if not stock:
            await query.answer(f"Tidak ada STOCK {digit_count} DIGIT!", show_alert=True)
            return
    
        context.user_data['stock'] = stock
        context.user_data['stock_page'] = 0
        context.user_data['filter_type'] = f"{digit_count} DIGIT"
        context.user_data['filter_count'] = count
        await show_filtered_stock_page(update, context)
        return

    elif data == "filter_all":
        stock = get_stock_all()
        count = len(stock)
    
        if not stock:
            await query.answer("Tidak ada stock tersedia!", show_alert=True)
            return
    
        context.user_data['stock'] = stock
        context.user_data['stock_page'] = 0
        context.user_data['filter_type'] = "SEMUA STOCK"
        context.user_data['filter_count'] = count
        await show_filtered_stock_page(update, context)
        return
    
    # === OWNER PANEL MENU CALLBACKS ===
    elif data == "owner_stats":
        await owner_stats_handler(update, context)
    elif data == "owner_list_requests":
        await owner_list_requests_handler(update, context)
    elif data == "owner_list_users":
        await owner_list_users_handler(update, context)
    elif data == "owner_set_cooldown":
        await owner_set_cooldown_handler(update, context)
    elif data == "owner_change_mode":
        await owner_change_mode_handler(update, context)
    elif data == "owner_broadcast":
        await owner_broadcast_handler(update, context)
    elif data == "owner_add_stock":
        await owner_add_stock_handler(update, context)
    elif data == "owner_add_saldo":
        await owner_add_saldo_handler(update, context)
    elif data == "owner_kurangi_saldo":
        await owner_kurangi_saldo_handler(update, context)
    elif data == "owner_backup_data":
        await owner_backup_data_handler(update, context)
    elif data == "owner_backup_user":
        await owner_backup_user_handler(update, context)
    elif data == "owner_restore_user":
        await owner_restore_user_handler(update, context)
    elif data == "owner_clone_manage":
        await owner_clone_manage_handler(update, context)
    elif data == "owner_wd_manage":
        await owner_wd_manage_handler(update, context)
    elif data == "owner_blokir_user":
        await owner_blokir_user_handler(update, context)
    elif data == "owner_list_blokir":
        await owner_list_blokir_handler(update, context)
    elif data == "owner_unblokir_user":
        await owner_unblokir_user_handler(update, context)
    elif data == "owner_set_payment":
        await owner_set_payment_handler(update, context)
    elif data == "owner_ganti_mt_payment":
        await owner_ganti_mt_payment_handler(update, context)
    elif data == "owner_ganti_gateway":
        await owner_ganti_gateway_handler(update, context)
    elif data.startswith("owner_setpayinfo_"):
        await owner_setpayinfo_handler(update, context)
    elif data.startswith("owner_approve_dm_"):
        await owner_approve_deposit_manual_handler(update, context)
    elif data.startswith("owner_tolak_dm_"):
        await owner_tolak_deposit_manual_handler(update, context)
    elif data.startswith("clone_wd_approve_"):
        await clone_wd_approve_callback(update, context)
    elif data.startswith("clone_wd_reject_"):
        await clone_wd_reject_callback(update, context)
    elif data.startswith("miniwd_ok_"):
        await miniapp_wd_approve_handler(update, context)
    elif data.startswith("miniwd_no_"):
        await miniapp_wd_reject_handler(update, context)
    elif data.startswith("miniord_ok_"):
        await miniapp_order_approve_handler(update, context)
    elif data.startswith("miniord_no_"):
        await miniapp_order_reject_handler(update, context)
    elif data.startswith("clone_approve_"):
        await clone_approve_callback(update, context)
    elif data.startswith("clone_reject_"):
        await clone_reject_callback(update, context)
    elif data.startswith("owner_approve_wd_"):
        await owner_approve_wd_handler(update, context)
    elif data.startswith("owner_reject_wd_"):
        await owner_reject_wd_handler(update, context)
    elif data == "owner_nego_settings":
        await owner_nego_settings_handler(update, context)
    elif data in ("nego_toggle_on", "nego_toggle_off"):
        await owner_nego_toggle_handler(update, context)
    elif data.startswith("nego_persen_"):
        await owner_nego_persen_handler(update, context)
    elif data == "owner_set_price":
        await owner_set_price_handler(update, context)
    elif data.startswith("setprice_"):
        await set_price_handler(update, context)
    elif data == "owner_remove_stock":
        await owner_remove_stock_handler(update, context)
    elif data.startswith("delstock_"):
        await delete_stock_handler(update, context)
    elif data == "mode_normal":
        await mode_normal_handler(update, context)
    elif data == "mode_maintenance":
        await mode_maintenance_handler(update, context)
    elif data == "cancel_input":
        await cancel_input_handler(update, context)

# ════════════════════════════════════════════════════
# ════════════════════════════════════════════════════════════════
#   HALAMAN 3 — MENU REPORT GMAIL (BLAST EMAIL KE TARGET USER)
# ════════════════════════════════════════════════════════════════

# ── state keys ──
# gmail_wait_email         → tunggu input Gmail sender
# gmail_wait_apppass       → tunggu App Password
# gmail_wait_add_target    → tunggu input email tujuan (bisa multi)
# gmail_wait_subject       → tunggu input subject email
# gmail_wait_body          → tunggu input isi/pesan email
# gmail_wait_hit           → (tidak dipakai, pakai inline button)


# ─────────────────────────────────────────
#   HELPER INTERNAL
# ─────────────────────────────────────────
def _bar(sent: int, total: int, w: int = 20) -> str:
    filled = int((sent / total) * w) if total else 0
    return "█" * filled + "░" * (w - filled)

def _pct_label(pct: int) -> str:
    """Tampilkan label persentase bertahap kelipatan 10: 0%→10%→20%→...→100%"""
    step = (pct // 10) * 10
    return f"{step}%"


# ─────────────────────────────────────────
#   MENU UTAMA PAGE 3 (callback & reply KB)
# ─────────────────────────────────────────
async def show_page3_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    await _render_page3(context, uid, edit_query=q)


async def send_page3_menu_new(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Kirim page 3 sebagai pesan baru (dari reply keyboard)."""
    await _render_page3(context, user_id, edit_query=None)


async def _render_page3(context, uid: int, edit_query=None):
    from src.custom_emoji import styled_keyboard_button
    sender  = gmail_reporter.get_sender(uid)
    targets = gmail_reporter.get_targets(uid)
    sender_info = f"<code>{sender[0]}</code>" if sender else "<i>Belum diatur</i>"
    target_count = len(targets)

    text = premium_text(f"""
<u><b>[crown] MENU REPORT GMAIL [gmail]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[roket] <b>Sender Aktif  :</b> {sender_info}
[target] <b>Target Email :</b> <code>{target_count}</code> alamat tersimpan

[lightning] <b>Fitur Tersedia:</b>
[tambah] <b>Add Sender</b>    — Daftarkan Gmail pengirim
[gmail] <b>Email Tujuan</b>  — Kelola daftar target
[roket] <b>Mulai Report</b>  — Blast email custom
[catatan] <b>Riwayat</b>       — History blast kamu</blockquote>
""")
    rich_html = f"""\
{emoji('crown')} <b>MENU REPORT GMAIL</b> {emoji('gmail')}

<table bordered striped>
<tr><th>Status Akun</th><th>Detail</th></tr>
<tr><td>Sender Aktif</td><td>{sender_info}</td></tr>
<tr><td>Target Email</td><td><code>{target_count}</code> alamat tersimpan</td></tr>
</table>
<hr/>
<table bordered striped>
<tr><th>{emoji('lightning')} Fitur Tersedia</th><th>Keterangan</th></tr>
<tr><td>Add Sender</td><td>Daftarkan Gmail pengirim</td></tr>
<tr><td>Email Tujuan</td><td>Kelola daftar target</td></tr>
<tr><td>Mulai Report</td><td>Blast email custom</td></tr>
<tr><td>Riwayat</td><td>History blast kamu</td></tr>
</table>"""
    kb = ReplyKeyboardMarkup([
        [styled_keyboard_button("Add Sender", style="primary", emoji_name="tambah"),
         styled_keyboard_button("Email Tujuan", style="success", emoji_name="gmail")],
        [styled_keyboard_button("Mulai Report", style="danger", emoji_name="roket")],
        [styled_keyboard_button("Cek Riwayat", style="primary", emoji_name="catatan")],
        [styled_keyboard_button(RKB_BACK_MAIN, style="danger", emoji_name="back")],
    ], resize_keyboard=True, is_persistent=True)
    set_page_reply_map(context, "page3_gmail", {
        "Add Sender": "gmail_add_sender",
        "Email Tujuan": "gmail_list_target",
        "Mulai Report": "gmail_start_report",
        "Cek Riwayat": "gmail_history",
    })

    if edit_query:
        await fast_edit(edit_query, text, reply_markup=kb, parse_mode="HTML", rich_html=rich_html, log_label="Page3GmailMenuCB")
    else:
        # NOTE: sendRichMessage Bot API belum dukung kirim sebagai photo caption,
        # jadi tampilan menu Gmail dikirim sebagai rich text message (tanpa foto thumbnail),
        # sama seperti pola Page 2 Gift.
        await notif.send_rich_message_to_chat(
            context.bot, uid, rich_html, text,
            reply_markup=kb,
            log_label="Page3GmailMenuNew",
        )


# ─────────────────────────────────────────
#   ADD SENDER
# ─────────────────────────────────────────
async def gmail_add_sender_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    context.user_data["current_menu_state"] = "gmail_wait_email"

    current = gmail_reporter.get_sender(uid)
    info = f"\n<blockquote>🔄 <b>Sender lama:</b> <code>{current[0]}</code> akan diganti.</blockquote>" if current else ""

    text = premium_text(f"""
<u><b>[crown] DAFTARKAN SENDER GMAIL [gmail]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>📌 <b>Langkah 1 dari 2</b>

[gmail] Kirim alamat <b>Gmail</b> kamu:
Contoh: <code>emailkamu@gmail.com</code>{info}

⚠️ Wajib aktifkan <b>2-Step Verification</b> dulu
lalu buat <b>App Password</b> di:
[gembok] Google Account → Security → App Passwords</blockquote>
""")
    kb = styled_inline_keyboard([[styled_button("❌ Batal", callback_data="menu_page_3", style="danger", emoji_name="back")]])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""<u><b>[crown] DAFTARKAN SENDER GMAIL [gmail]</b></u>
<hr/>
<p>📌 <b>Langkah 1 dari 2</b></p>
<ul><li>[gmail] Kirim alamat <b>Gmail</b> kamu:</li><li>Contoh: <code>emailkamu@gmail.com</code>{info}</li></ul>
<ul><li>⚠️ Wajib aktifkan <b>2-Step Verification</b> dulu</li><li>lalu buat <b>App Password</b> di:</li><li>[gembok] Google Account → Security → App Passwords</li></ul>"""), log_label="AutoRich")


# ─────────────────────────────────────────
#   KELOLA EMAIL TUJUAN
# ─────────────────────────────────────────
async def gmail_list_target_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    await _render_target_list(q, uid)


async def _render_target_list(q, uid: int):
    targets = gmail_reporter.get_targets(uid)

    if not targets:
        body = "⚠️ Belum ada email tujuan. Tambahkan dulu!"
        table_rows = "<tr><td colspan=\"2\">Belum ada email tujuan. Tambahkan dulu!</td></tr>"
    else:
        lines = [f"<code>{e}</code>" for _, e in targets]
        body  = "\n".join(f"{i+1}. {l}" for i, l in enumerate(lines))
        table_rows = "".join(f"<tr><td>#{i+1}</td><td><code>{html.escape(e)}</code></td></tr>\n" for i, (_, e) in enumerate(targets))

    text = premium_text(f"""
<u><b>[gmail] DAFTAR EMAIL TUJUAN [target]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>📋 Total: <b>{len(targets)}</b> email tersimpan

{body}</blockquote>
""")
    rich_html = f"""\
{emoji('gmail')} <b>DAFTAR EMAIL TUJUAN</b> {emoji('target')}
Total: <b>{len(targets)}</b> email tersimpan

<table bordered striped>
<tr><th>No</th><th>Email Tujuan</th></tr>
{table_rows}</table>"""

    rows = []
    # Tombol hapus per email
    for tid, email in targets[:10]:
        rows.append([styled_button(f"🗑 {email}", callback_data=f"gmail_del_target_{tid}", style="danger", emoji_name="warning")])

    rows.append([styled_button("Tambah Email Tujuan", callback_data="gmail_add_target", style="success", emoji_name="tambah")])
    rows.append([styled_button("Hapus Semua",         callback_data="gmail_clear_targets", style="danger",  emoji_name="warning")])
    rows.append([styled_button("Kembali",                callback_data="menu_page_3",          style="primary", emoji_name="back")])

    kb = styled_inline_keyboard(rows)
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=rich_html, log_label="GmailTargetList")


async def gmail_add_target_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await safe_answer(q)
    context.user_data["current_menu_state"] = "gmail_wait_add_target"

    text = premium_text("""
<u><b>[tambah] TAMBAH EMAIL TUJUAN [gmail]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Kirim satu atau lebih email tujuan, pisahkan dengan koma:

Contoh:
<code>mozarofficial@gmail.com, sksjsm@gmail.com, target3@gmail.com</code>

✅ Bot akan menyimpan semua email valid secara otomatis! </blockquote>
""")
    kb = styled_inline_keyboard([[styled_button("Batal", callback_data="gmail_list_target", style="danger", emoji_name="back")]])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""<u><b>[tambah] TAMBAH EMAIL TUJUAN [gmail]</b></u>
<hr/>
<p>[catatan] Kirim satu atau lebih email tujuan, pisahkan dengan koma:</p>
<ul><li>Contoh:</li><li><code>mozarofficial@gmail.com, sksjsm@gmail.com, target3@gmail.com</code></li></ul>
<p>✅ Bot akan menyimpan semua email valid secara otomatis!</p>"""), log_label="AutoRich")


async def gmail_del_target_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    tid = int(q.data.replace("gmail_del_target_", ""))
    gmail_reporter.remove_target(tid, uid)
    await _render_target_list(q, uid)


async def gmail_clear_targets_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await safe_answer(q, "Semua email tujuan dihapus!")
    uid = q.from_user.id
    targets = gmail_reporter.get_targets(uid)
    for tid, _ in targets:
        gmail_reporter.remove_target(tid, uid)
    await _render_target_list(q, uid)


# ─────────────────────────────────────────
#   MULAI REPORT — FLOW STEP BY STEP
# ─────────────────────────────────────────
async def gmail_start_report_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id

    sender  = gmail_reporter.get_sender(uid)
    targets = gmail_reporter.get_targets(uid)

    if not sender:
        text = premium_text("""
<u><b>⚠️ SENDER BELUM DIATUR ❗</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>❌ Kamu belum daftarkan Gmail pengirim!
📌 Klik <b>Add Sender</b> terlebih dahulu ya.</blockquote>
""")
        kb = styled_inline_keyboard([
            [styled_button("Add Sender", callback_data="gmail_add_sender", style="primary", emoji_name="tambah")],
            [styled_button("Kembali",       callback_data="menu_page_3",      style="danger",  emoji_name="back")],
        ])
        await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""<u><b>⚠️ SENDER BELUM DIATUR ❗</b></u>
<hr/>
<ul><li>❌ Kamu belum daftarkan Gmail pengirim!</li><li>📌 Klik <b>Add Sender</b> terlebih dahulu ya.</li></ul>"""), log_label="AutoRich")
        return

    if not targets:
        text = premium_text("""
<u><b>⚠️ BELUM ADA EMAIL TUJUAN</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>❌ Kamu belum menambahkan email tujuan!
[gmail] Klik <b>Email Tujuan</b> untuk menambahkan.</blockquote>
""")
        kb = styled_inline_keyboard([
            [styled_button("Email Tujuan", callback_data="gmail_list_target", style="success", emoji_name="gmail")],
            [styled_button("Kembali",          callback_data="menu_page_3",       style="danger",  emoji_name="back")],
        ])
        await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""<u><b>⚠️ BELUM ADA EMAIL TUJUAN</b></u>
<hr/>
<ul><li>❌ Kamu belum menambahkan email tujuan!</li><li>[gmail] Klik <b>Email Tujuan</b> untuk menambahkan.</li></ul>"""), log_label="AutoRich")
        return

    # Simpan data ke session, minta subject
    context.user_data["gmail_sender_email"]   = sender[0]
    context.user_data["gmail_sender_pass"]    = sender[1]
    context.user_data["gmail_targets_list"]   = [e for _, e in targets]
    context.user_data["current_menu_state"]   = "gmail_wait_subject"

    target_preview = "\n".join(f"  └ <code>{e}</code>" for e in [e for _, e in targets][:5])
    more = f"\n  └ <i>+{len(targets)-5} lainnya...</i>" if len(targets) > 5 else ""

    text = premium_text(f"""
<u><b>[roket] MULAI BLAST REPORT [roket]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[gembok1] <b>Sender  :</b> <code>{sender[0]}</code>
[target] <b>Target  :</b> {len(targets)} email
{target_preview}{more}

📌 <b>Langkah 1 dari 3</b>
[catatan] Kirim <b>nama/subject</b> email laporan kamu:</blockquote>
""")
    kb = styled_inline_keyboard([[styled_button("❌ Batal", callback_data="menu_page_3", style="danger", emoji_name="back")]])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""<u><b>[roket] MULAI BLAST REPORT [roket]</b></u>
<hr/>
<ul><li>[gembok1] <b>Sender  :</b> <code>{sender[0]}</code></li><li>[target] <b>Target  :</b> {len(targets)} email</li><li>{target_preview}{more}</li></ul>
<ul><li>📌 <b>Langkah 1 dari 3</b></li><li>[catatan] Kirim <b>nama/subject</b> email laporan kamu:</li></ul>"""), log_label="AutoRich")


async def gmail_history_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    rows = gmail_reporter.get_user_reports(uid)

    if not rows:
        text = premium_text("""
<u><b>[catatan] RIWAYAT BLAST [catatan]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>⚠️ Belum ada riwayat blast nih!
[roket] Gunakan <b>Mulai Report</b> untuk memulai.</blockquote>
""")
        rich_html = f"""\
{emoji('catatan')} <b>RIWAYAT BLAST</b>

{emoji('warning')} Belum ada riwayat blast nih! Gunakan <b>Mulai Report</b> untuk memulai."""
    else:
        lines = []
        table_rows = ""
        for i, (subj, tgts, hit, total, sent, failed, st, ts) in enumerate(rows, 1):
            tgl  = datetime.fromtimestamp(ts).strftime("%d/%m %H:%M") if ts else "-"
            icon = "✅" if st == "success" else "❌"
            status_label = "Sukses" if st == "success" else "Gagal"
            t_count = len(tgts.split(",")) if tgts else 0
            lines.append(
                f"{icon} <b>#{i}</b> <i>{subj[:30]}</i>\n"
                f"   [target] {t_count} target | [roket] {total} blast | ✅ {sent} | <code>{tgl}</code>"
            )
            table_rows += (
                f"<tr><td>#{i}</td><td>{html.escape(str(subj)[:30])}</td><td>{t_count}</td>"
                f"<td>{sent}/{total}</td><td>{status_label}</td><td><code>{tgl}</code></td></tr>\n"
            )
        body = "\n\n".join(lines)
        text = premium_text(f"""
<u><b>📋 RIWAYAT BLAST [catatan] (10 Terakhir)</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>{body}</blockquote>
""")
        rich_html = f"""\
{emoji('catatan')} <b>RIWAYAT BLAST (10 Terakhir)</b>

<table bordered striped>
<tr><th>Subject</th><th>Target</th><th>Terkirim</th><th>Status</th><th>Waktu</th></tr>
{table_rows}</table>"""

    kb = styled_inline_keyboard([[styled_button("Kembali", callback_data="menu_page_3", style="danger", emoji_name="back")]])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=rich_html, log_label="GmailHistory")


# ─────────────────────────────────────────
#   PILIH JUMLAH HIT (setelah body masuk)
# ─────────────────────────────────────────
async def gmail_ask_hit_count(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Minta user pilih berapa kali kirim ke tiap target."""
    targets = context.user_data.get("gmail_targets_list", [])
    subject = context.user_data.get("gmail_subject", "-")
    body    = context.user_data.get("gmail_body", "-")

    rich_html = f"""\
<tg-emoji emoji-id="6235646232883107337">💥</tg-emoji> <b>PILIH JUMLAH HIT PER TARGET</b>

<table bordered striped>
<tr><th>Info</th><th>Detail</th></tr>
<tr><td>Subject</td><td><i>{subject[:50]}</i></td></tr>
<tr><td>Target</td><td>{len(targets)} email</td></tr>
<tr><td>Pesan</td><td><i>{body[:60]}...</i></td></tr>
</table>

<tg-emoji emoji-id="6298432564487001027">✅</tg-emoji> Pilih berapa kali email dikirim ke <b>setiap</b> target:"""
    text = premium_text(f"""
<u><b>[boom] PILIH JUMLAH HIT PER TARGET [target]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] <b>Subject :</b> <i>{subject[:50]}</i>
[target] <b>Target  :</b> {len(targets)} email
[pesan] <b>Pesan   :</b> <i>{body[:60]}...</i>

[done] Pilih berapa kali email dikirim ke <b>setiap</b> target:</blockquote>
""")
    kb = styled_inline_keyboard([
        [
            styled_button("1x",   callback_data="gmail_hit_1",   style="primary", emoji_name="done"),
            styled_button("5x",   callback_data="gmail_hit_5",   style="success", emoji_name="done"),
            styled_button("10x",   callback_data="gmail_hit_10",  style="danger",  emoji_name="done"),
        ],
        [
            styled_button("20x",  callback_data="gmail_hit_20",  style="primary", emoji_name="done"),
            styled_button("50x",  callback_data="gmail_hit_50",  style="danger",  emoji_name="warning"),
        ],
        [styled_button("❌ Batal", callback_data="menu_page_3", style="danger", emoji_name="back")],
    ])

    _hit_rich_html = f"""\
{emoji('boom')} <b>PILIH JUMLAH HIT PER TARGET</b> {emoji('target')}

<table bordered striped>
<tr><th>Info</th><th>Detail</th></tr>
<tr><td>Subject</td><td><i>{subject[:50]}</i></td></tr>
<tr><td>Target</td><td>{len(targets)} email</td></tr>
<tr><td>Pesan</td><td><i>{body[:60]}...</i></td></tr>
</table>

{emoji('done')} Pilih berapa kali email dikirim ke <b>setiap</b> target:"""
    if update.callback_query:
        await fast_edit(update.callback_query, text, reply_markup=kb, parse_mode="HTML", rich_html=_hit_rich_html, log_label="AutoRich")
    else:
        await notif.send_rich_message_to_chat(
            context.bot, update.message.chat_id, _hit_rich_html, text,
            reply_markup=kb, log_label="GmailAskHit",
        )


# ─────────────────────────────────────────
#   HANDLE TOMBOL HIT → KONFIRMASI
# ─────────────────────────────────────────
async def gmail_hit_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    await safe_answer(q)
    hit = int(q.data.replace("gmail_hit_", ""))
    context.user_data["gmail_hit_count"] = hit

    targets = context.user_data.get("gmail_targets_list", [])
    subject = context.user_data.get("gmail_subject", "-")
    body    = context.user_data.get("gmail_body", "-")
    sender  = context.user_data.get("gmail_sender_email", "-")
    total   = len(targets) * hit

    target_list = "\n".join(f"  └ <code>{e}</code>" for e in targets[:5])
    more = f"\n  └ <i>+{len(targets)-5} lainnya</i>" if len(targets) > 5 else ""

    text = premium_text(f"""
<u><b>[done] KONFIRMASI BLAST [boom]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>🔐 <b>Sender     :</b> <code>{sender}</code>
[pesan] <b>Subject    :</b> <i>{subject[:50]}</i>
[target] <b>Target     :</b> {len(targets)} email
{target_list}{more}
[boom] <b>Hit/Target :</b> {hit}x
[gmail] <b>Total Blast:</b> {total} email

❓ Yakin mau blast sekarang? [roket]</blockquote>
""")
    kb = styled_inline_keyboard([
        [styled_button("YA, BLAST SEKARANG!", callback_data="gmail_execute_blast", style="danger",  emoji_name="roket")],
        [styled_button("❌ Batal",                callback_data="menu_page_3",         style="primary", emoji_name="back")],
    ])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""<u><b>[done] KONFIRMASI BLAST [boom]</b></u>
<hr/>
<ul><li>🔐 <b>Sender     :</b> <code>{sender}</code></li><li>[pesan] <b>Subject    :</b> <i>{subject[:50]}</i></li><li>[target] <b>Target     :</b> {len(targets)} email</li><li>{target_list}{more}</li><li>[boom] <b>Hit/Target :</b> {hit}x</li><li>[gmail] <b>Total Blast:</b> {total} email</li></ul>
<p>❓ Yakin mau blast sekarang? [roket]</p>"""), log_label="AutoRich")


# ─────────────────────────────────────────
#   EKSEKUSI BLAST + LIVE PROGRESS
# ─────────────────────────────────────────
_blast_cancel_flags: dict = {}   # uid -> True jika user batalkan

async def gmail_execute_blast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Execute Gmail blast dengan progress update yang benar + formatted bagus"""
    q   = update.callback_query
    try:
        await safe_answer(q, "⏳ Memulai blast...")
    except Exception:
        pass
    uid = q.from_user.id

    gmail    = context.user_data.get("gmail_sender_email", "")
    app_pass = context.user_data.get("gmail_sender_pass", "")
    targets  = context.user_data.get("gmail_targets_list", [])
    subject  = context.user_data.get("gmail_subject", "")
    body     = context.user_data.get("gmail_body", "")
    hit      = context.user_data.get("gmail_hit_count", 1)

    if not gmail or not targets or not subject or not body:
        await fast_edit(q, premium_text("⚠️ <b>Data tidak lengkap. Ulangi dari awal.</b>"),
                        reply_markup=styled_inline_keyboard([[styled_button("Kembali", callback_data="menu_page_3", style="danger", emoji_name="back")]]),
                        parse_mode="HTML", rich_html=premium_text(f"""⚠️ <b>Data tidak lengkap. Ulangi dari awal.</b>"""), log_label="AutoRich")
        return

    total = len(targets) * hit
    _blast_cancel_flags[uid] = False
    
    # ✅ CAPTURE CHAT ID SEBELUM EXECUTOR (CRITICAL!)
    blast_chat_id = q.message.chat_id
    safe_gmail = html.escape(str(gmail))
    
    # ✅ SEND INITIAL STATUS MESSAGE
    init_text = premium_text(f"""
<u><b>[roket] BLAST DIMULAI [roket]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

<b>📋 INFORMASI BLAST</b>
[email] <b>Sender:</b> <code>{safe_gmail}</code>
[target] <b>Target:</b> <b>{len(targets)} Email</b>
[boom] <b>Hit/Email:</b> <b>{hit}x</b>
[gmail] <b>Total:</b> <b>{total} Email</b>

<b>📊 STATUS PENGIRIMAN</b>
[bars] <b>Progress:</b> [          ] <b>0%</b>
[verified] <b>Terkirim:</b> <b>0/{total}</b>
[cross] <b>Gagal:</b> <b>0</b>

[loading] <b>Status:</b> <i>INITIALIZING...</i>""")
    
    status_msg = await context.bot.send_message(
        chat_id=blast_chat_id,
        text=init_text,
        parse_mode="HTML"
    )
    
    status_msg_id = status_msg.message_id
    success_count = 0
    failed_count = 0
    last_pct_shown = -1
    
    # ✅ PROGRESS CALLBACK - FORMATTED BAGUS
    async def _progress(sent: int, failed: int, current_target: str, total_blast: int):
        nonlocal success_count, failed_count, last_pct_shown
        
        success_count = sent
        failed_count = failed
        pct = int((sent / total_blast) * 100) if total_blast else 0
        
        # Only update if percentage changed
        if pct == last_pct_shown:
            return
        
        last_pct_shown = pct
        
        # Build fancy progress bar
        filled = int(pct / 5)  # 0-20 chars
        bar = "█" * filled + "░" * (20 - filled)
        safe_target = html.escape(str(current_target))[:35]
        
        # Determine status emoji based on progress
        if pct < 25:
            status_emoji = "loading"
        elif pct < 50:
            status_emoji = "hourglass"
        elif pct < 75:
            status_emoji = "work"
        else:
            status_emoji = "done"
        
        progress_text = premium_text(f"""
<u><b>[roket] BLAST SEDANG BERJALAN [roket]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

<b>📋 INFORMASI BLAST</b>
[email] <b>Sender:</b> <code>{safe_gmail}</code>
[target] <b>Target:</b> <b>{len(targets)} Email</b>
[boom] <b>Hit/Email:</b> <b>{hit}x</b>
[gmail] <b>Total:</b> <b>{total_blast} Email</b>

<b>📊 STATUS PENGIRIMAN</b>
[bars] <b>Progress:</b> [{bar}] <b>{pct}%</b>
[verified] <b>Terkirim:</b> <b>{sent}/{total_blast}</b>
[cross] <b>Gagal:</b> <b>{failed}</b>

<b>⏳ SEDANG DIPROSES</b>
[target] {safe_target}
[{status_emoji}] <i>Tunggu sebentar...</i>""")
        
        try:
            await context.bot.edit_message_text(
                chat_id=blast_chat_id,
                message_id=status_msg_id,
                text=progress_text,
                parse_mode="HTML"
            )
        except Exception:
            # Silent - blast tetap jalan
            pass

    try:
        result = await gmail_reporter.send_blast(
            gmail, app_pass, targets, subject, body, hit,
            progress_callback=_progress,
            cancel_check=lambda: _blast_cancel_flags.get(uid, False)
        )
        success_count = result["success"]
        failed_count  = result["failed"]
        was_cancelled = result.get("cancelled", False)
        st = "cancelled" if was_cancelled else ("success" if result["success"] > 0 else "failed")
        
        # ✅ FORCE FINAL 100% UPDATE
        if not was_cancelled and success_count > 0:
            last_pct_shown = -1
            await _progress(success_count, failed_count, "[COMPLETED]", total)
    except Exception as e:
        st = "failed"
        was_cancelled = False
        failed_count = total
        success_count = 0

    _blast_cancel_flags.pop(uid, None)

    # Bersihkan context
    for k in ["gmail_sender_email","gmail_sender_pass","gmail_targets_list",
              "gmail_subject","gmail_body","gmail_hit_count"]:
        context.user_data.pop(k, None)
    context.user_data["current_menu_state"] = ""

    gmail_reporter.save_report(uid, gmail, subject, targets, hit, total,
                               success_count, failed_count, st)

    pct  = int((success_count / total) * 100) if total else 0
    bar  = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))

    if was_cancelled:
        final_text = premium_text(f"""
<u><b>[batal] BLAST DIBATALKAN [batal]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

<b>📋 INFORMASI BLAST</b>
[email] <b>Sender:</b> <code>{safe_gmail}</code>
[target] <b>Target:</b> <b>{len(targets)} Email</b>
[boom] <b>Hit/Email:</b> <b>{hit}x</b>
[gmail] <b>Total:</b> <b>{total} Email</b>

<b>📊 HASIL AKHIR</b>
[bars] <b>Progress:</b> [{bar}] <b>{pct}%</b>
[verified] <b>Terkirim:</b> <b>{success_count}/{total}</b>
[cross] <b>Gagal:</b> <b>{failed_count}</b>
[warning] <b>Status:</b> <i>DIBATALKAN OLEH USER</i>""")
    else:
        final_text = premium_text(f"""
<u><b>[done] BLAST SELESAI [done]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━━━━━

<b>📋 INFORMASI BLAST</b>
[email] <b>Sender:</b> <code>{safe_gmail}</code>
[target] <b>Target:</b> <b>{len(targets)} Email</b>
[boom] <b>Hit/Email:</b> <b>{hit}x</b>
[gmail] <b>Total:</b> <b>{total} Email</b>

<b>📊 HASIL AKHIR</b>
[bars] <b>Progress:</b> [{bar}] <b>{pct}%</b>
[verified] <b>Terkirim:</b> <b>{success_count}/{total}</b>
[cross] <b>Gagal:</b> <b>{failed_count}</b>
[trophy] <b>Success Rate:</b> <b>{pct}%</b>

[download] Laporan telah disimpan.""")

    # ✅ FINAL MESSAGE UPDATE
    try:
        await context.bot.edit_message_text(
            chat_id=blast_chat_id,
            message_id=status_msg_id,
            text=final_text,
            parse_mode="HTML"
        )
    except Exception:
        pass

    # ✅ SEND NOTIFICATION TO CHANNEL
    if success_count > 0 and not was_cancelled:
        try:
            sender_label = update.effective_user.username if update.effective_user and update.effective_user.username else uid
            notif_text = premium_text(f"""
[roket] <b>BLAST GMAIL BERHASIL</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[card] User: @{sender_label} (<code>{uid}</code>)
[target] Total Target: {len(targets)}
[done] Terkirim: {success_count}
[batal] Gagal: {failed_count}</blockquote>
""")
            notif_html = f"""\
<tg-emoji emoji-id="5420226647125148933">🚀</tg-emoji> <b>BLAST GMAIL BERHASIL</b>

<table bordered striped>
<tr><th>Informasi Blast</th><th>Detail</th></tr>
<tr><td><tg-emoji emoji-id="5769547529993588669">👑</tg-emoji> User</td><td>@{sender_label} (<code>{uid}</code>)</td></tr>
<tr><td><tg-emoji emoji-id="5310278924616356636">🎯</tg-emoji> Total Target</td><td>{len(targets)} Email</td></tr>
<tr><td><tg-emoji emoji-id="6235646232883107337">💥</tg-emoji> Hit Per Gmail</td><td>{hit}x</td></tr>
<tr><td><tg-emoji emoji-id="6102907940727950594">💌</tg-emoji> Total Blast</td><td>{total} Email</td></tr>
<tr><td><tg-emoji emoji-id="5212932275376759608">✅</tg-emoji> Terkirim</td><td><b>{success_count}</b></td></tr>
<tr><td><tg-emoji emoji-id="5846210329700217522">❌</tg-emoji> Gagal</td><td>{failed_count}</td></tr>
<tr><td><tg-emoji emoji-id="5364265190353286344">📈</tg-emoji> Progress</td><td>[{bar}] {pct}%</td></tr>
</table>"""
            await notify_success_channel(context, "LINK_CH_NOTIF_GMAIL", notif_text, html_content=notif_html)
        except Exception:
            pass

    # ✅ FINAL MESSAGE UPDATE
    try:
        await context.bot.edit_message_text(
            chat_id=blast_chat_id,
            message_id=status_msg_id,
            text=final_text,
            parse_mode="HTML"
        )
    except Exception:
        pass


async def gmail_cancel_blast_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User tekan batalkan saat blast berjalan."""
    q   = update.callback_query
    try:
        await safe_answer(q, "🛑 Membatalkan blast...")
    except Exception:
        pass
    parts = q.data.split("_")
    uid   = int(parts[-1])
    _blast_cancel_flags[uid] = True

    text = premium_text("""
<u><b>[batal] PERMINTAAN BATAL DITERIMA [batal]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>⚠️ Proses akan berhenti setelah pengiriman email saat ini selesai.
[proces] Mohon tunggu beberapa saat, ringkasan akhir akan dikirim otomatis.</blockquote>
""")
    try:
        await notif.edit_rich_message(
            context.bot, q.message.chat_id, q.message.message_id,
            text, text, log_label="GmailBlastCancelAck",
        )
    except Exception:
        pass


# ─────────────────────────────────────────
#   HANDLE MESSAGE INPUT GMAIL
# ─────────────────────────────────────────
async def gmail_handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    uid   = update.effective_user.id
    state = context.user_data.get("current_menu_state", "")

    # ── Input Gmail sender ──
    if state == "gmail_wait_email":
        raw = update.message.text.strip()
        if "@" not in raw or "." not in raw:
            await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""⚠️ Format email tidak valid. Contoh: <code>emailkamu@gmail.com</code>"""), premium_text("⚠️ Format email tidak valid.\nContoh: <code>emailkamu@gmail.com</code>"), log_label="AutoRich2")
            return True
        context.user_data["gmail_temp_email"]   = raw
        context.user_data["current_menu_state"] = "gmail_wait_apppass"

        text = premium_text(f"""
<u><b>[gembok] APP PASSWORD</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[done] Gmail: <code>{raw}</code>

📌 <b>Langkah 2 dari 2</b>
[gembok] Kirim <b>App Password</b> Gmail kamu.
Format: <code>xxxx xxxx xxxx xxxx</code>

[catatan] Cara dapat App Password:
Google Account → Security → 2-Step Verification → App Passwords</blockquote>
""")
        _apppass_rich = f"""\
{emoji('gembok')} <b>APP PASSWORD</b>

<table bordered striped>
<tr><th>Info</th><th>Detail</th></tr>
<tr><td>Gmail</td><td><code>{html.escape(raw)}</code></td></tr>
<tr><td>Langkah</td><td>2 dari 2</td></tr>
</table>

{emoji('gembok')} Kirim <b>App Password</b> Gmail kamu.
Format: <code>xxxx xxxx xxxx xxxx</code>

{emoji('catatan')} Cara dapat App Password:
Google Account → Security → 2-Step Verification → App Passwords"""
        kb = styled_inline_keyboard([[styled_button("❌ Batal", callback_data="menu_page_3", style="danger", emoji_name="back")]])
        await notif.send_rich_message_to_chat(
            context.bot, update.message.chat_id, _apppass_rich, text,
            reply_markup=kb, log_label="GmailWaitAppPass",
        )
        return True

    # ── Input App Password ──
    if state == "gmail_wait_apppass":
        raw = update.message.text.strip().replace(" ", "")
        if len(raw) < 12:
            await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""⚠️ App Password tidak valid (min. 12 karakter tanpa spasi)."""), premium_text("⚠️ App Password tidak valid (min. 12 karakter tanpa spasi)."), log_label="AutoRich2")
            return True
        gmail    = context.user_data.pop("gmail_temp_email", "")
        gmail_reporter.save_sender(uid, gmail, raw)
        context.user_data["current_menu_state"] = ""

        text = premium_text(f"""
<u><b>[gmail] SENDER BERHASIL DISIMPAN [done]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[gmail] <b>Gmail   :</b> <code>{gmail}</code>
[gembok1] <b>Password:</b> Tersimpan aman [done]

[roket] Sekarang tambahkan <b>Email Tujuan</b> lalu mulai blast!</blockquote>
""")
        _sender_saved_rich = f"""\
{emoji('gmail')} <b>SENDER BERHASIL DISIMPAN</b> {emoji('done')}

<table bordered striped>
<tr><th>Info</th><th>Detail</th></tr>
<tr><td>Gmail</td><td><code>{html.escape(gmail)}</code></td></tr>
<tr><td>Password</td><td>Tersimpan aman {emoji('done')}</td></tr>
</table>

{emoji('roket')} Sekarang tambahkan <b>Email Tujuan</b> lalu mulai blast!"""
        kb = styled_inline_keyboard([
            [styled_button("Tambah Email Tujuan", callback_data="gmail_add_target",   style="success", emoji_name="tambah")],
            [styled_button("Kembali ke Menu",         callback_data="menu_page_3",        style="danger",  emoji_name="back")],
        ])
        await notif.send_rich_message_to_chat(
            context.bot, update.message.chat_id, _sender_saved_rich, text,
            reply_markup=kb, log_label="GmailSenderSaved",
        )
        return True

    # ── Input email tujuan (bisa banyak dipisah koma) ──
    if state == "gmail_wait_add_target":
        raw    = update.message.text.strip()
        emails = [e.strip() for e in raw.replace("\n", ",").split(",") if "@" in e.strip()]
        if not emails:
            await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""⚠️ Tidak ada email valid ditemukan. Pisahkan dengan koma, contoh: <code>a@gmail.com, b@gmail.com</code>"""), premium_text("⚠️ Tidak ada email valid ditemukan.\nPisahkan dengan koma, contoh: <code>a@gmail.com, b@gmail.com</code>"), log_label="AutoRich2")
            return True

        added = 0
        dupe  = 0
        for e in emails:
            ok = gmail_reporter.add_target(uid, e)
            if ok:
                added += 1
            else:
                dupe += 1

        context.user_data["current_menu_state"] = ""
        targets = gmail_reporter.get_targets(uid)
        text = premium_text(f"""
<u><b>[done] EMAIL TUJUAN DIPERBARUI [target]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[tambah] Ditambahkan      : {added} email baru 
⚠️ Duplikat        : {dupe} email diabaikan
📊 Total Tersimpan : {len(targets)} email 🗂️</blockquote>
""")
        _target_added_rich = f"""\
{emoji('done')} <b>EMAIL TUJUAN DIPERBARUI</b> {emoji('target')}

<table bordered striped>
<tr><th>Status</th><th>Jumlah</th></tr>
<tr><td>Ditambahkan</td><td><b>{added}</b> email baru</td></tr>
<tr><td>Duplikat diabaikan</td><td>{dupe} email</td></tr>
<tr><td>Total Tersimpan</td><td><b>{len(targets)}</b> email</td></tr>
</table>"""
        kb = styled_inline_keyboard([
            [styled_button("📧 Lihat Daftar", callback_data="gmail_list_target",  style="success", emoji_name="gmail")],
            [styled_button("Kembali",         callback_data="menu_page_3",         style="danger",  emoji_name="back")],
        ])
        await notif.send_rich_message_to_chat(
            context.bot, update.message.chat_id, _target_added_rich, text,
            reply_markup=kb, log_label="GmailTargetAdded",
        )
        return True

    # ── Input Subject email ──
    if state == "gmail_wait_subject":
        subj = update.message.text.strip()
        if len(subj) < 3:
            await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""⚠️ Subject terlalu pendek (min. 3 karakter)."""), premium_text("⚠️ Subject terlalu pendek (min. 3 karakter)."), log_label="AutoRich2")
            return True
        context.user_data["gmail_subject"]       = subj
        context.user_data["current_menu_state"] = "gmail_wait_body"

        text = premium_text(f"""
<u><b>[pesan] ISI PESAN EMAIL [catatan]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[done] Subject: <i>{subj}</i>

📌 <b>Langkah 2 dari 3</b>
💬 Kirim <b>isi/pesan</b> email yang akan dikirim ke target.

[catatan] (Boleh panjang, multi-baris, bebas format)</blockquote>
""")
        _subject_rich = f"""\
{emoji('pesan')} <b>ISI PESAN EMAIL</b> {emoji('catatan')}

<table bordered striped>
<tr><th>Info</th><th>Detail</th></tr>
<tr><td>Subject</td><td><i>{html.escape(subj)}</i></td></tr>
<tr><td>Langkah</td><td>2 dari 3</td></tr>
</table>

💬 Kirim <b>isi/pesan</b> email yang akan dikirim ke target.
{emoji('catatan')} Boleh panjang, multi-baris, bebas format."""
        kb = styled_inline_keyboard([[styled_button("❌ Batal", callback_data="menu_page_3", style="danger", emoji_name="back")]])
        await notif.send_rich_message_to_chat(
            context.bot, update.message.chat_id, _subject_rich, text,
            reply_markup=kb, log_label="GmailWaitBody",
        )
        return True

    # ── Input Body email ──
    if state == "gmail_wait_body":
        body = update.message.text.strip()
        if len(body) < 5:
            await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""⚠️ Isi pesan terlalu pendek (min. 5 karakter)."""), premium_text("⚠️ Isi pesan terlalu pendek (min. 5 karakter)."), log_label="AutoRich2")
            return True
        context.user_data["gmail_body"]          = body
        context.user_data["current_menu_state"] = "gmail_wait_hit"

        # Langsung tampilkan pilihan hit count (step 3)
        await gmail_ask_hit_count(update, context)
        return True

    return False

# ==================== MAIN ====================
async def auto_cleanup_cache_loop():
    """Loop background: bersih-bersih otomatis biar disk container gak penuh
    (penyebab error 'No space left on device' yang bikin pip gagal install &
    module kayak qrcode jadi 'ModuleNotFoundError'). Jalan tiap 15 menit,
    membersihkan 3 hal:
      1) File QR di QR_DIR yang lebih tua dari 30 menit — transaksi normal
         sudah paid/expired jauh sebelum itu (expired default 5 menit), jadi
         file yang masih nyangkut di sini pasti residu (mis. bot sempat
         restart di tengah transaksi) dan aman dihapus.
      2) Folder cache pip (~/.cache/pip) — numpuk gigabytean tiap kali ada
         auto-restart/redeploy yang install ulang requirements.txt.
      3) Folder __pycache__ bawaan Python di seluruh project.
    """
    await asyncio.sleep(30)  # tunggu bot stabil duluan sebelum mulai bersih-bersih
    import shutil
    while True:
        try:
            now = time.time()

            # 1) Residu file QR lama
            if os.path.isdir(QR_DIR):
                for fname in os.listdir(QR_DIR):
                    fpath = os.path.join(QR_DIR, fname)
                    try:
                        if os.path.isfile(fpath) and (now - os.path.getmtime(fpath)) > 1800:
                            os.remove(fpath)
                    except Exception:
                        pass

            # 2) Cache pip
            try:
                pip_cache_dir = os.path.expanduser("~/.cache/pip")
                if os.path.isdir(pip_cache_dir):
                    shutil.rmtree(pip_cache_dir, ignore_errors=True)
            except Exception:
                pass

            # 3) __pycache__ di seluruh project
            try:
                for root, dirs, _files in os.walk(BASE_DIR):
                    if "__pycache__" in dirs:
                        shutil.rmtree(os.path.join(root, "__pycache__"), ignore_errors=True)
                        dirs.remove("__pycache__")
            except Exception:
                pass

        except Exception as e:
            print(f"[AutoCleanupCache] {e}")
        await asyncio.sleep(900)  # ulangi tiap 15 menit


async def check_dead_sessions_loop(app):
    """Cek berkala semua session_stock yang masih 'available'.
    Kalau session-nya udah mati/logout/banned, otomatis hapus dari listing produk (status -> 'dead')."""
    await asyncio.sleep(15)  # tunggu bot stabil duluan sebelum mulai cek
    while True:
        try:
            conn2 = sqlite3.connect(DB_PATH)
            cur2 = conn2.cursor()
            cur2.execute("SELECT id, session_string, account_id FROM session_stock WHERE status='available'")
            rows = cur2.fetchall()
            conn2.close()

            for stock_id, session_str, account_id in rows:
                # Skip jika session_string kosong, None, atau terlalu pendek (bukan session valid)
                if not session_str or not isinstance(session_str, str) or len(session_str.strip()) < 20:
                    continue

                is_alive = False
                try:
                    client = TelegramClient(StringSession(session_str.strip()), API_ID, API_HASH)
                    await client.connect()
                    is_alive = await client.is_user_authorized()
                    await client.disconnect()
                except Exception:
                    is_alive = False

                if not is_alive:
                    try:
                        conn3 = sqlite3.connect(DB_PATH)
                        cur3 = conn3.cursor()
                        cur3.execute("UPDATE session_stock SET status='dead' WHERE id=?", (stock_id,))
                        conn3.commit()
                        conn3.close()

                        owner_ids = OWNER_ID.all_ids if hasattr(OWNER_ID, "all_ids") else [OWNER_ID]
                        rich_dead = f"""\
{emoji('warning')} <b>SESI MATI TERDETEKSI</b>
<hr/>
<p>{emoji('catatan')} Stock ID: <code>{stock_id}</code></p>
<p>{emoji('product')} Account ID: <code>{account_id}</code></p>
<p>Otomatis dihapus dari listing produk karena sesi sudah tidak aktif (logout/banned).</p>"""
                        fallback_dead = premium_text(f"""
[warning] <b>SESI MATI TERDETEKSI</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Stock ID: <code>{stock_id}</code>
[product] Account ID: <code>{account_id}</code>

Otomatis dihapus dari listing produk karena sesi sudah tidak aktif (logout/banned).</blockquote>
""")
                        for oid in owner_ids:
                            try:
                                await notif.send_rich_message_to_chat(
                                    app.bot, oid, rich_dead, fallback_dead,
                                    log_label="SesiMatiTerdeteksi",
                                )
                            except Exception:
                                pass
                    except Exception:
                        pass

                await asyncio.sleep(2)  # jeda antar cek biar gak flood/rate-limit

        except Exception:
            pass

        await asyncio.sleep(1800)  # cek ulang tiap 30 menit


# ═══════════════════════════════════════════════════════════
#   HALAMAN 4 — AUTO ORDER NOKOS ALLAPK (API RUMAHOTP)
# ═══════════════════════════════════════════════════════════
_nokos4_orders: dict = {}   # order_id -> {"uid":, "price":, "task": asyncio.Task}
_nokos4_busy: set = set()   # uid -> sedang proses (anti double-tap / anti race condition)

def nokos_get_pending_order(uid):
    """Cari order 'pending' milik uid ini di DB. Return (order_id, price) atau None."""
    try:
        c = sqlite3.connect(DB_PATH)
        row = c.execute(
            "SELECT order_id, price FROM nokos_orders WHERE user_id=? AND status='pending' ORDER BY created_at DESC LIMIT 1",
            (uid,)
        ).fetchone()
        c.close()
        return row
    except Exception as e:
        print(f"[nokos_get_pending_order] {e}")
        return None

def nokos_order_create(order_id, uid, price):
    """Catat order baru ke DB dengan status pending."""
    try:
        c = sqlite3.connect(DB_PATH)
        c.execute(
            "INSERT OR IGNORE INTO nokos_orders (order_id, user_id, price, status) VALUES (?, ?, ?, 'pending')",
            (str(order_id), uid, price)
        )
        c.commit()
        c.close()
    except Exception as e:
        print(f"[nokos_order_create] {e}")

def nokos_order_complete(order_id):
    """Tandai order sebagai completed (OTP sudah diterima)."""
    try:
        c = sqlite3.connect(DB_PATH)
        c.execute(
            "UPDATE nokos_orders SET status='completed' WHERE order_id=? AND status='pending'",
            (str(order_id),)
        )
        changed = c.execute("SELECT changes()").fetchone()[0]
        c.commit()
        c.close()
        return changed > 0
    except Exception as e:
        print(f"[nokos_order_complete] {e}")
        return False

def nokos_order_refund(order_id):
    """Atomic: set status=refunded HANYA jika masih pending. Return True jika berhasil."""
    try:
        c = sqlite3.connect(DB_PATH)
        c.execute(
            "UPDATE nokos_orders SET status='refunded' WHERE order_id=? AND status='pending'",
            (str(order_id),)
        )
        changed = c.execute("SELECT changes()").fetchone()[0]
        c.commit()
        c.close()
        return changed > 0  # False jika sudah completed/refunded sebelumnya
    except Exception as e:
        print(f"[nokos_order_refund] {e}")
        return False


async def send_page4_menu_new(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Kirim halaman 4 sebagai pesan baru (dari reply keyboard)."""
    context.user_data["current_menu_state"] = "page4_nokos"
    await _render_page4(context, user_id, edit_query=None)


async def show_page4_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await safe_answer(q)
    except Exception:
        pass
    uid = q.from_user.id
    # Track nav history kalau masuk dari halaman lain
    prev = context.user_data.get("current_menu_state", "main_menu")
    if prev not in ("page4_nokos",):
        nav_history = context.user_data.get("nav_history", [])
        nav_history.append(prev if prev else "main_menu")
        context.user_data["nav_history"] = nav_history
    context.user_data["current_menu_state"] = "page4_nokos"
    await _render_page4(context, uid, edit_query=q)


async def _render_page4(context, uid: int, edit_query=None):
    user_data = get_user(uid)
    saldo = user_data[3] if user_data else 0  # FIX: belance_balance, bukan deposit_balance

    loading_text = premium_text("""
<u><b>[globe] AUTO ORDER NOKOS ALLAPK [lightning]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[loading] Mengambil daftar layanan terbaru dari server...</blockquote>
""")
    if edit_query:
        await fast_edit(edit_query, loading_text, parse_mode="HTML", rich_html=premium_text(f"""<u><b>[globe] AUTO ORDER NOKOS ALLAPK [lightning]</b></u>
<hr/>
<p>[loading] Mengambil daftar layanan terbaru dari server...</p>"""), log_label="AutoRich")

    result = await rumahotp.get_services()
    services = (result.get("data") or [])[:8] if result.get("success") else []

    text = premium_text(f"""
<u><b>[globe] AUTO ORDER NOKOS ALLAPK [lightning]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[card] <b>Saldo Kamu  :</b> <code>{format_currency(saldo)}</code>
[product] <b>Sistem      :</b> Auto Order — nomor & OTP otomatis didapat dalam hitungan detik

[lightning] <b>Cara Pakai:</b>
[panahijo] Pilih aplikasi/layanan di bawah ini
[panahijo] Pilih negara yang tersedia
[panahijo] Sistem otomatis carikan nomor termurah
[panahijo] Tunggu OTP masuk otomatis ke chat ini</blockquote>
""")
    rich_html = f"""\
{emoji('globe')} <b>AUTO ORDER NOKOS ALLAPK</b> {emoji('lightning')}

<table bordered striped>
<tr><th>Info</th><th>Detail</th></tr>
<tr><td>Saldo Kamu</td><td><code>{format_currency(saldo)}</code></td></tr>
<tr><td>Sistem</td><td>Auto Order — nomor &amp; OTP otomatis didapat dalam hitungan detik</td></tr>
</table>
<hr/>
<table bordered striped>
<tr><th>{emoji('lightning')} Cara Pakai</th><th>Langkah</th></tr>
<tr><td>1</td><td>Pilih aplikasi/layanan di bawah ini</td></tr>
<tr><td>2</td><td>Pilih negara yang tersedia</td></tr>
<tr><td>3</td><td>Sistem otomatis carikan nomor termurah</td></tr>
<tr><td>4</td><td>Tunggu OTP masuk otomatis ke chat ini</td></tr>
</table>"""

    from src.custom_emoji import styled_keyboard_button, clean_button_label
    rows = []
    reply_map = {}
    if services:
        used_labels = set()
        for svc in services:
            sid = svc.get("service_code") or svc.get("id")
            name = svc.get("service_name") or svc.get("name") or "Layanan"
            label = clean_button_label(name[:16])
            # Hindari tabrakan label kembar — Reply Keyboard cuma bawa teks,
            # jadi tiap label yang ditampilkan wajib unik biar bisa di-route.
            base_label, n = label, 2
            while label in used_labels:
                label = f"{base_label} #{n}"
                n += 1
            used_labels.add(label)
            reply_map[label] = f"nokos4_svc_{sid}"
        for i in range(0, len(services), 2):
            chunk = list(reply_map.items())[i:i + 2]
            rows.append([styled_keyboard_button(lbl, style="primary", emoji_name="panahijo") for lbl, _ in chunk])
    else:
        err_msg = html.escape(str(result.get("message", "Tidak ada respons dari server")))
        text = premium_text(f"""
<u><b>[globe] AUTO ORDER NOKOS ALLAPK [lightning]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[error] Gagal mengambil daftar layanan dari server RumahOTP.
[proses] Detail: <code>{err_msg}</code></blockquote>
""")
        rich_html = f"""\
{emoji('globe')} <b>AUTO ORDER NOKOS ALLAPK</b> {emoji('lightning')}

<table bordered striped>
<tr><th>Status</th><th>Detail</th></tr>
<tr><td>Error</td><td>Gagal mengambil daftar layanan dari server RumahOTP</td></tr>
<tr><td>Detail</td><td><code>{err_msg}</code></td></tr>
</table>"""

    rows.append([styled_keyboard_button("Refresh", style="success", emoji_name="refresh")])
    reply_map["Refresh"] = "nokos4_open"
    rows.append([styled_keyboard_button(RKB_BACK_MAIN, style="danger", emoji_name="back")])
    kb = ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)
    set_page_reply_map(context, "page4_nokos", reply_map)

    if edit_query:
        await fast_edit(edit_query, text, reply_markup=kb, parse_mode="HTML", rich_html=rich_html, log_label="Page4NokosMenuCB")
    else:
        # NOTE: sendRichMessage Bot API belum dukung kirim sebagai photo caption,
        # jadi tampilan menu Nokos AllApk dikirim sebagai rich text message
        # (tanpa foto thumbnail), sama seperti pola Page 2 Gift & Page 3 Gmail.
        await notif.send_rich_message_to_chat(
            context.bot, uid, rich_html, text,
            reply_markup=kb,
            log_label="Page4NokosMenuNew",
        )


async def nokos4_show_countries(update_or_q, context, service_id: str):
    """Render daftar negara dengan paginasi. Bisa dipanggil dari handler manapun."""
    q = update_or_q.callback_query if hasattr(update_or_q, 'callback_query') else update_or_q

    await fast_edit(q, premium_text("""
<u><b>[globe] MENCARI NEGARA [loading]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[loading] Mengambil daftar negara tersedia...</blockquote>
"""), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[globe] MENCARI NEGARA [loading]</b></u>
<hr/>
<p>[loading] Mengambil daftar negara tersedia...</p>"""), log_label="AutoRich")

    result = await rumahotp.get_countries(service_id)
    countries = result.get("data") or [] if result.get("success") else []

    if not countries:
        await fast_edit(q, premium_text("""
<u><b>[globe] TIDAK ADA NEGARA [error]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[error] Layanan ini sedang tidak memiliki stok negara.
[proses] Silakan pilih layanan lain.</blockquote>
"""), reply_markup=styled_inline_keyboard([[styled_button("Kembali", callback_data="nokos4_open", style="danger", emoji_name="back")]]), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[globe] TIDAK ADA NEGARA [error]</b></u>
<hr/>
<ul><li>[error] Layanan ini sedang tidak memiliki stok negara.</li><li>[proses] Silakan pilih layanan lain.</li></ul>"""), log_label="AutoRich")
        return

    rows = []
    valid_countries = []
    for c in countries:
        avail_list = [p for p in (c.get("pricelist") or []) if p.get("available", True) and p.get("stock", 1) > 0]
        if avail_list:
            valid_countries.append((c, avail_list))

    if not valid_countries:
        await fast_edit(q, premium_text("""
<u><b>[globe] STOK SEDANG HABIS</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[error] Semua provider untuk layanan ini sedang kehabisan stok.
[proses] Coba lagi beberapa saat lagi, atau pilih layanan lain.</blockquote>
"""), reply_markup=styled_inline_keyboard([[styled_button("Kembali", callback_data="nokos4_open", style="danger", emoji_name="back")]]), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[globe] STOK SEDANG HABIS</b></u>
<hr/>
<ul><li>[error] Semua provider untuk layanan ini sedang kehabisan stok.</li><li>[proses] Coba lagi beberapa saat lagi, atau pilih layanan lain.</li></ul>"""), log_label="AutoRich")
        return

    # Urutkan dari termurah ke termahal
    valid_countries.sort(key=lambda x: min(p.get("price", 0) for p in x[1]))

    # Paginasi — 20 negara per halaman
    PER_PAGE = 20
    page = int(context.user_data.get(f"nokos4_page_{service_id}", 0))
    total_pages = max(1, (len(valid_countries) + PER_PAGE - 1) // PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    paged = valid_countries[page * PER_PAGE:(page + 1) * PER_PAGE]

    for i in range(0, len(paged), 2):
        chunk = paged[i:i + 2]
        row = []
        for c, avail_list in chunk:
            number_id = c.get("number_id")
            name = c.get("name") or "Negara"
            lowest = min(p.get("price", 0) for p in avail_list)
            final_price = rumahotp.apply_markup(lowest)
            flag_emoji = get_premium_country_flag(name)
            label = f"{name[:12]} ({format_currency(final_price)})"
            row.append(styled_button(label, callback_data=f"nokos4_country_{service_id}_{number_id}", style="success", emoji_name=flag_emoji))
        rows.append(row)

    nav = []
    if page > 0:
        nav.append(styled_button(f"◀ Prev ({page}/{total_pages})", callback_data=f"nokos4_pg_{service_id}_{page-1}", style="primary", emoji_name="back"))
    if page < total_pages - 1:
        nav.append(styled_button(f"Next ({page+2}/{total_pages}) ▶", callback_data=f"nokos4_pg_{service_id}_{page+1}", style="primary", emoji_name="panahijo"))
    if nav:
        rows.append(nav)

    rows.append([styled_button("Kembali", callback_data="nokos4_open", style="danger", emoji_name="back")])

    text = premium_text(f"""
<u><b>[globe] PILIH NEGARA [verified]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[panahijo] Harga sudah termasuk biaya layanan. Halaman <b>{page+1}/{total_pages}</b> — {len(valid_countries)} negara tersedia</blockquote>
""")
    rich_html = f"""\
{emoji('globe')} <b>PILIH NEGARA</b> {emoji('verified')}

<table bordered striped>
<tr><th>Info</th><th>Detail</th></tr>
<tr><td>Halaman</td><td><code>{page+1}/{total_pages}</code></td></tr>
<tr><td>Negara Tersedia</td><td><code>{len(valid_countries)}</code></td></tr>
<tr><td>Catatan</td><td>Harga sudah termasuk biaya layanan</td></tr>
</table>"""
    await fast_edit(q, text, reply_markup=styled_inline_keyboard(rows), parse_mode="HTML", rich_html=rich_html, log_label="NokosCountryList")


async def nokos4_select_service(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if uid in _nokos4_busy:
        try:
            await safe_answer(q, "⏳ Masih memproses permintaan sebelumnya, tunggu sebentar...", show_alert=True)
        except Exception:
            pass
        return
    _nokos4_busy.add(uid)
    try:
        await safe_answer(q)
    except Exception:
        pass
    try:
        service_id = q.data.split("_", 2)[2]
        await nokos4_show_countries(update, context, service_id)
    finally:
        _nokos4_busy.discard(uid)


async def nokos4_select_country(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await safe_answer(q)
    except Exception:
        pass
    uid = q.from_user.id
    if uid in _nokos4_busy:
        try:
            await safe_answer(q, "⏳ Masih memproses permintaan sebelumnya, tunggu sebentar...", show_alert=True)
        except Exception:
            pass
        return
    _nokos4_busy.add(uid)
    _, _, service_id, number_id = q.data.split("_", 3)

    await fast_edit(q, premium_text("""
<u><b>[lightning] MENYIAPKAN ORDER [loading]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[loading] Mengecek harga & ketersediaan provider...</blockquote>
"""), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[lightning] MENYIAPKAN ORDER [loading]</b></u>
<hr/>
<p>[loading] Mengecek harga & ketersediaan provider...</p>"""), log_label="AutoRich")

    result = await rumahotp.get_countries(service_id)
    countries = result.get("data") or [] if result.get("success") else []
    country = next((c for c in countries if str(c.get("number_id")) == str(number_id)), None)

    avail_list = [p for p in (country.get("pricelist") or []) if p.get("available", True) and p.get("stock", 1) > 0] if country else []

    if not country or not avail_list:
        await fast_edit(q, premium_text("""
<u><b>[error] STOK HABIS</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[error] Provider untuk negara ini baru saja kehabisan stok.
[proses] Silakan pilih negara lain.</blockquote>
"""), reply_markup=styled_inline_keyboard([[styled_button("Kembali", callback_data=f"nokos4_svc_{service_id}", style="danger", emoji_name="back")]]), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[error] STOK HABIS</b></u>
<hr/>
<ul><li>[error] Provider untuk negara ini baru saja kehabisan stok.</li><li>[proses] Silakan pilih negara lain.</li></ul>"""), log_label="AutoRich")
        _nokos4_busy.discard(uid)
        return

    # Urutkan semua provider dari termurah ke termahal
    avail_list_sorted = sorted(avail_list, key=lambda p: p.get("price", 0))

    user_data = get_user(uid)
    saldo = user_data[3] if user_data else 0  # FIX: belance_balance, bukan deposit_balance
    country_name = country.get("name", "Negara")

    text = premium_text(f"""
<u><b>[globe] PILIH HARGA — {html.escape(country_name)} [card]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[card] <b>Saldo Kamu :</b> <code>{format_currency(saldo)}</code>

[panahijo] Pilih nominal harga yang kamu mau.
[warning] Jika OTP tidak masuk dalam 5 menit, order dibatalkan otomatis & saldo direfund.</blockquote>
""")

    price_rows = []
    for p in avail_list_sorted:
        base_price = p.get("price", 0)
        final_price = rumahotp.apply_markup(base_price)
        prov_id = p.get("provider_id")
        stok = p.get("stock", "?")
        label = f"Rp {final_price:,}".replace(",", ".") + f"  (stok: {stok}{'⚠️' if isinstance(stok, int) and stok <= 2 else ''})"
        price_rows.append([styled_button(label, callback_data=f"nokos4_confirm_{service_id}_{number_id}_{prov_id}_{final_price}", style="success", emoji_name="verified")])

    price_rows.append([styled_button("Kembali", callback_data=f"nokos4_svc_{service_id}", style="danger", emoji_name="back")])
    await fast_edit(q, text, reply_markup=styled_inline_keyboard(price_rows), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[globe] PILIH HARGA — {html.escape(country_name)} [card]</b></u>
<hr/>
<p>[card] <b>Saldo Kamu :</b> <code>{format_currency(saldo)}</code></p>
<ul><li>[panahijo] Pilih nominal harga yang kamu mau.</li><li>[warning] Jika OTP tidak masuk dalam 5 menit, order dibatalkan otomatis & saldo direfund.</li></ul>"""), log_label="AutoRich")
    _nokos4_busy.discard(uid)


async def nokos4_confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    try:
        await safe_answer(q)
    except Exception:
        pass
    uid = q.from_user.id
    _, _, service_id, number_id, provider_id, price_str = q.data.split("_", 5)
    final_price = int(price_str)

    if not check_cooldown(uid):
        try:
            await safe_answer(q, "Cooldown! Tunggu sebentar.", show_alert=True)
        except Exception:
            pass
        return

    user_data = get_user(uid)
    saldo = user_data[3] if user_data else 0  # FIX: belance_balance, bukan deposit_balance
    if not user_data or saldo < final_price:
        await fast_edit(q, premium_text(f"""
<u><b>[error] SALDO TIDAK CUKUP</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[card] Saldo Kamu : <code>{format_currency(saldo)}</code>
[product] Dibutuhkan : <code>{format_currency(final_price)}</code></blockquote>
"""), reply_markup=styled_inline_keyboard([
            [styled_button("Kembali", callback_data=f"nokos4_svc_{service_id}", style="danger", emoji_name="back")],
        ]), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[error] SALDO TIDAK CUKUP</b></u>
<hr/>
<ul><li>[card] Saldo Kamu : <code>{format_currency(saldo)}</code></li><li>[product] Dibutuhkan : <code>{format_currency(final_price)}</code></li></ul>"""), log_label="AutoRich")
        return

    await fast_edit(q, premium_text(f"""
<u><b>[verified] KONFIRMASI ORDER [card]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[card] <b>Saldo Kamu :</b> <code>{format_currency(saldo)}</code>
[product] <b>Bayar     :</b> <code>{format_currency(final_price)}</code>

[lightning] Nomor & OTP akan otomatis didapat setelah order dibuat.
[warning] Jika OTP tidak masuk dalam 5 menit, order dibatalkan otomatis & saldo direfund.</blockquote>
"""), reply_markup=styled_inline_keyboard([
        [styled_button(f"✅ YA, BAYAR {format_currency(final_price)}", callback_data=f"nokos4_do_{service_id}_{number_id}_{provider_id}_{final_price}", style="success", emoji_name="verified")],
        [styled_button("Kembali", callback_data=f"nokos4_country_{service_id}_{number_id}", style="danger", emoji_name="back")],
    ]), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[verified] KONFIRMASI ORDER [card]</b></u>
<hr/>
<ul><li>[card] <b>Saldo Kamu :</b> <code>{format_currency(saldo)}</code></li><li>[product] <b>Bayar     :</b> <code>{format_currency(final_price)}</code></li></ul>
<ul><li>[lightning] Nomor & OTP akan otomatis didapat setelah order dibuat.</li><li>[warning] Jika OTP tidak masuk dalam 5 menit, order dibatalkan otomatis & saldo direfund.</li></ul>"""), log_label="AutoRich")


async def nokos4_do_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler konfirmasi final — benar-benar eksekusi order ke RumahOTP."""
    q = update.callback_query
    try:
        await safe_answer(q)
    except Exception:
        pass
    uid = q.from_user.id
    _, _, service_id, number_id, provider_id, price_str = q.data.split("_", 5)
    final_price = int(price_str)

    if not check_cooldown(uid):
        try:
            await safe_answer(q, "Cooldown! Tunggu sebentar.", show_alert=True)
        except Exception:
            pass
        return

    if uid in _nokos4_busy:
        try:
            await safe_answer(q, "⏳ Order sebelumnya masih diproses, jangan tap berkali-kali.", show_alert=True)
        except Exception:
            pass
        return

    # Cegah order dobel: kalau user masih punya order pending (belum dapat OTP/dibatalkan),
    # jangan izinkan bikin order baru — ini yang bikin order "nyangkut" tanpa tombol cancel.
    pending = nokos_get_pending_order(uid)
    if pending:
        p_order_id, p_price = pending
        await fast_edit(q, premium_text(f"""
<u><b>[warning] MASIH ADA ORDER AKTIF</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[error] Kamu masih punya order nokos yang belum selesai (Order ID: <code>{html.escape(str(p_order_id))}</code>).
[proses] Cek status atau batalkan dulu order itu sebelum membuat order baru.</blockquote>
"""), reply_markup=styled_inline_keyboard([
            [styled_button("Cek Status", callback_data=f"nokos4_status_{p_order_id}_{uid}_{p_price}", style="primary", emoji_name="refresh")],
            [styled_button("Batalkan & Refund", callback_data=f"nokos4_cancel_{p_order_id}_{uid}_{p_price}", style="danger", emoji_name="warning")],
        ]), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[warning] MASIH ADA ORDER AKTIF</b></u>
<hr/>
<ul><li>[error] Kamu masih punya order nokos yang belum selesai (Order ID: <code>{html.escape(str(p_order_id))}</code>).</li><li>[proses] Cek status atau batalkan dulu order itu sebelum membuat order baru.</li></ul>"""), log_label="AutoRich")
        return

    _nokos4_busy.add(uid)

    await fast_edit(q, premium_text("""
<u><b>[lightning] MEMBUAT ORDER... [loading]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[loading] Sedang menghubungi server RumahOTP, mohon tunggu...</blockquote>
"""), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[lightning] MEMBUAT ORDER... [loading]</b></u>
<hr/>
<p>[loading] Sedang menghubungi server RumahOTP, mohon tunggu...</p>"""), log_label="AutoRich")

    order_result = await rumahotp.create_order(number_id, provider_id, 1)

    # Stok provider pilihan bisa berubah cepat — kalau gagal, fetch ulang & coba SEMUA provider lain
    if not order_result.get("success"):
        await fast_edit(q, premium_text("""\
<u><b>[lightning] MENCOBA PROVIDER LAIN... [loading]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[proses] Provider pertama kehabisan stok, sedang mencoba provider alternatif...</blockquote>
"""), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[lightning] MENCOBA PROVIDER LAIN... [loading]</b></u>
<hr/>
<p>[proses] Provider pertama kehabisan stok, sedang mencoba provider alternatif...</p>"""), log_label="AutoRich")
        fresh = await rumahotp.get_countries(service_id)
        fresh_countries = fresh.get("data") or [] if fresh.get("success") else []
        fresh_country = next((c for c in fresh_countries if str(c.get("number_id")) == str(number_id)), None)
        alt_providers = []
        if fresh_country:
            alt_providers = sorted(
                [p for p in (fresh_country.get("pricelist") or [])
                 if p.get("available", True) and p.get("stock", 1) > 0 and str(p.get("provider_id")) != str(provider_id)],
                key=lambda p: p.get("price", 0)
            )
        # Coba SEMUA provider yang tersedia, bukan cuma 2
        for alt in alt_providers:
            order_result = await rumahotp.create_order(number_id, alt.get("provider_id"), 1)
            if order_result.get("success"):
                provider_id = str(alt.get("provider_id"))  # update provider_id ke yang berhasil
                break

    if not order_result.get("success"):
        err = (order_result.get("error") or {}).get("message") or order_result.get("message") or "Unknown error"
        await fast_edit(q, premium_text(f"""\
<u><b>[error] GAGAL MEMBUAT ORDER</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[error] Error: <code>{html.escape(str(err))}</code>
[proses] Semua provider untuk negara ini sedang kehabisan stok. Saldo kamu tidak terpotong.
[warning] Coba pilih negara lain atau ulangi beberapa saat lagi.</blockquote>
"""), reply_markup=styled_inline_keyboard([
            [styled_button("Coba Negara Lain", callback_data=f"nokos4_svc_{service_id}", style="success", emoji_name="globe")],
            [styled_button("Kembali ke Layanan", callback_data="nokos4_open", style="danger", emoji_name="back")],
        ]), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[error] GAGAL MEMBUAT ORDER</b></u>
<hr/>
<ul><li>[error] Error: <code>{html.escape(str(err))}</code></li><li>[proses] Semua provider untuk negara ini sedang kehabisan stok. Saldo kamu tidak terpotong.</li><li>[warning] Coba pilih negara lain atau ulangi beberapa saat lagi.</li></ul>"""), log_label="AutoRich")
        _nokos4_busy.discard(uid)
        return

    # Potong saldo HANYA setelah order sukses dibuat
    # FIX: potong belance_balance (saldo yang bisa dipakai), bukan deposit_balance
    update_balance(uid, belance_delta=-final_price)
    update_user_stats(uid)

    data = order_result.get("data") or {}
    order_id = data.get("order_id")

    # Catat order ke DB — kunci anti double-refund yang persist setelah restart
    nokos_order_create(order_id, uid, final_price)
    phone_number = data.get("phone_number", "-")

    # Notif pembelian RumahOTP ke channel DIPINDAH ke saat OTP benar-benar masuk
    # (lihat _nokos4_poll_otp), supaya tidak ada notif palsu untuk order yang belum dapat kode.

    text = premium_text(f"""
<u><b>[verified] ORDER BERHASIL [done]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[product] <b>Nomor   :</b> <code>{phone_number}</code>
[card] <b>Harga   :</b> <code>{format_currency(final_price)}</code>
[lightning] <b>Status  :</b> Menunggu OTP...

[loading] Sistem otomatis cek OTP setiap 10 detik. Halaman ini akan update otomatis begitu OTP masuk.</blockquote>
""")
    kb = styled_inline_keyboard([
        [styled_button("Cek Status", callback_data=f"nokos4_status_{order_id}_{uid}_{final_price}", style="primary", emoji_name="refresh")],
        [styled_button("Batalkan & Refund", callback_data=f"nokos4_cancel_{order_id}_{uid}_{final_price}", style="danger", emoji_name="warning")],
    ])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""<u><b>[verified] ORDER BERHASIL [done]</b></u>
<hr/>
<ul><li>[product] <b>Nomor   :</b> <code>{phone_number}</code></li><li>[card] <b>Harga   :</b> <code>{format_currency(final_price)}</code></li><li>[lightning] <b>Status  :</b> Menunggu OTP...</li></ul>
<p>[loading] Sistem otomatis cek OTP setiap 10 detik. Halaman ini akan update otomatis begitu OTP masuk.</p>"""), log_label="AutoRich")

    # Hitung base_price (harga asli sebelum markup rumahotp)
    # final_price = base_price × (1 + PROFIT_PERCENT/100)
    # Jadi: base_price = final_price / (1 + PROFIT_PERCENT/100)
    base_price_rumahotp = int(final_price / (1 + rumahotp.PROFIT_PERCENT / 100))
    
    task = asyncio.create_task(_nokos4_poll_otp(context, q.message.chat_id, q.message.message_id, order_id, uid, final_price, phone_number, base_price_rumahotp))
    _nokos4_orders[str(order_id)] = {"uid": uid, "price": final_price, "base_price": base_price_rumahotp, "task": task}
    _nokos4_busy.discard(uid)


async def _nokos4_poll_otp(context, chat_id, message_id, order_id, uid, final_price, phone_number, base_price=None):
    """Auto cek OTP tiap 10 detik selama maksimal 5 menit, auto-refund jika gagal.
    
    base_price: harga asli rumahotp sebelum markup (untuk kalkulasi komisi yang akurat)
    """
    max_checks = 30  # 30 x 10s = 5 menit
    for _ in range(max_checks):
        await asyncio.sleep(10)
        if str(order_id) not in _nokos4_orders:
            return  # sudah dibatalkan manual / selesai

        status_result = await rumahotp.get_order_status(order_id)
        if rumahotp.has_otp(status_result):
            otp_code = status_result["data"]["otp_code"]
            _nokos4_orders.pop(str(order_id), None)
            nokos_order_complete(order_id)  # tandai completed di DB, cegah refund dobel

            # === PERBAIKAN: notif channel baru dikirim SETELAH otp benar-benar masuk ===
            try:
                _ur = get_user(uid)
                _uname_r = _ur[1] if _ur and _ur[1] else None
                _sisa_r = _ur[3] if _ur and len(_ur) > 3 else None
                await notif.notif_pembelian_rumahotp_channel(
                    context.bot, uid, phone_number, order_id, final_price,
                    username=_uname_r, saldo_sisa=_sisa_r
                )
            except Exception as _e:
                print(f"[Notif RumahOTP] {_e}")
            try:
                await clone_system.process_transaction_commission(
                    context.bot, DB_PATH, uid, order_id, "RumahOTP Nokos", final_price, base_price
                )
            except Exception as _ce:
                print(f"[CloneCommission] {_ce}")

            fallback_otp = premium_text(f"""
<u><b>[done] OTP DITERIMA [verified]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[product] <b>Nomor    :</b> <code>{phone_number}</code>
[lightning] <b>Kode OTP :</b> <code>{otp_code}</code>

[panahijo] Segera gunakan kode di atas sebelum kadaluwarsa.</blockquote>
""")
            rich_otp = f"""\
<u><b>{emoji('done')} OTP DITERIMA {emoji('verified')}</b></u>
<hr/>
<p>{emoji('product')} <b>Nomor    :</b> <code>{phone_number}</code></p>
<p>{emoji('lightning')} <b>Kode OTP :</b> <code>{otp_code}</code></p>
<p>{emoji('panahijo')} Segera gunakan kode di atas sebelum kadaluwarsa.</p>"""
            try:
                await notif.edit_rich_message(
                    context.bot, chat_id, message_id, rich_otp, fallback_otp,
                    reply_markup=styled_inline_keyboard([[styled_button("Order Lagi", callback_data="nokos4_open", style="success", emoji_name="roket")]]),
                    log_label="Nokos4OtpDiterima",
                )
            except Exception:
                pass
            return

    # Timeout — auto refund jika masih ada di _nokos4_orders (belum ditangani manual)
    if str(order_id) in _nokos4_orders:
        _nokos4_orders.pop(str(order_id), None)
        # ── ATOMIC DB CHECK — aman bahkan setelah bot restart ────────────────
        refund_ok = nokos_order_refund(order_id)
        if not refund_ok:
            return  # sudah di-refund/completed sebelumnya, jangan dobel
        # ────────────────────────────────────────────────────────────────────
        try:
            await rumahotp.cancel_order(order_id)
        except Exception:
            pass
        update_balance(uid, belance_delta=final_price)  # 100% aman — DB sudah lock
        fallback_refund = premium_text(f"""
<u><b>[warning] OTP TIDAK MASUK — AUTO REFUND</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[product] <b>Nomor  :</b> <code>{phone_number}</code>
[card] <b>Refund :</b> <code>{format_currency(final_price)}</code> sudah dikembalikan ke saldo kamu.</blockquote>
""")
        rich_refund = f"""\
<u><b>{emoji('warning')} OTP TIDAK MASUK — AUTO REFUND</b></u>
<hr/>
<p>{emoji('product')} <b>Nomor  :</b> <code>{phone_number}</code></p>
<p>{emoji('card')} <b>Refund :</b> <code>{format_currency(final_price)}</code> sudah dikembalikan ke saldo kamu.</p>"""
        try:
            await notif.edit_rich_message(
                context.bot, chat_id, message_id, rich_refund, fallback_refund,
                reply_markup=styled_inline_keyboard([[styled_button("Order Lagi", callback_data="nokos4_open", style="success", emoji_name="roket")]]),
                log_label="Nokos4AutoRefund",
            )
        except Exception:
            pass


async def nokos4_check_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    # FIX: order_id dari RumahOTP bisa mengandung underscore, jadi split("_", 4) dari kiri
    # bisa salah potong dan bikin int(price_str) meledak (tombol jadi kelihatan "gak ngapa-ngapain").
    # rsplit dari kanan aman karena uid & price selalu digit murni di posisi paling belakang.
    rest = q.data.split("nokos4_status_", 1)[1]
    order_id, uid_str, price_str = rest.rsplit("_", 2)
    final_price = int(price_str)
    uid = int(uid_str)

    try:
        await safe_answer(q, "🔄 Mengecek status...")
    except Exception:
        pass

    status_result = await rumahotp.get_order_status(order_id)
    if rumahotp.has_otp(status_result):
        otp_code = status_result["data"]["otp_code"]
        phone_number = status_result["data"].get("phone_number", "-")
        _nokos4_orders.pop(str(order_id), None)
        nokos_order_complete(order_id)  # tandai completed di DB, cegah auto-refund dobel
        text = premium_text(f"""
<u><b>[done] OTP DITERIMA [verified]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[product] <b>Nomor    :</b> <code>{phone_number}</code>
[lightning] <b>Kode OTP :</b> <code>{otp_code}</code></blockquote>
""")
        await fast_edit(q, text, reply_markup=styled_inline_keyboard([[styled_button("Order Lagi", callback_data="nokos4_open", style="success", emoji_name="roket")]]), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[done] OTP DITERIMA [verified]</b></u>
<hr/>
<ul><li>[product] <b>Nomor    :</b> <code>{phone_number}</code></li><li>[lightning] <b>Kode OTP :</b> <code>{otp_code}</code></li></ul>"""), log_label="AutoRich")
        return

    try:
        await safe_answer(q, "⏳ OTP belum masuk, coba lagi sebentar.", show_alert=True)
    except Exception:
        pass


async def nokos4_cancel_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    # FIX: sama seperti nokos4_check_status — rsplit dari kanan biar order_id
    # yang mengandung underscore gak bikin parsing salah & tombol "gak ngapa-ngapain".
    rest = q.data.split("nokos4_cancel_", 1)[1]
    order_id, uid_str, price_str = rest.rsplit("_", 2)
    final_price = int(price_str)
    uid = int(uid_str)

    try:
        await safe_answer(q, "🛑 Membatalkan order...")
    except Exception:
        pass

    info = _nokos4_orders.pop(str(order_id), None)
    if info and info.get("task"):
        info["task"].cancel()

    # ── ATOMIC DB CHECK: satu-satunya guard yang aman setelah restart ────────
    refund_ok = nokos_order_refund(order_id)
    if not refund_ok:
        # Sudah di-refund / completed sebelumnya (bahkan setelah bot restart)
        try:
            await safe_answer(q, "⚠️ Order ini sudah dibatalkan atau OTP sudah diterima sebelumnya.", show_alert=True)
        except Exception:
            pass
        return
    # ─────────────────────────────────────────────────────────────────────────

    try:
        await rumahotp.cancel_order(order_id)
    except Exception:
        pass

    # FIX: refund ke belance_balance (saldo yang bisa dipakai), bukan deposit_balance
    update_balance(uid, belance_delta=final_price)  # 100% aman — DB sudah lock

    text = premium_text(f"""
<u><b>[cancel] ORDER DIBATALKAN</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[card] Saldo <code>{format_currency(final_price)}</code> sudah dikembalikan ke akun kamu.</blockquote>
""")
    await fast_edit(q, text, reply_markup=styled_inline_keyboard([[styled_button("Order Lagi", callback_data="nokos4_open", style="success", emoji_name="roket")]]), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[cancel] ORDER DIBATALKAN</b></u>
<hr/>
<p>[card] Saldo <code>{format_currency(final_price)}</code> sudah dikembalikan ke akun kamu.</p>"""), log_label="AutoRich")


# ═══════════════════════════════════════════════════════════════════════════════
#   PAGE 5 — MENU CV KONTAK (PECAH FILE VCF)
# ═══════════════════════════════════════════════════════════════════════════════

import os
import io
import tempfile

async def send_page5_menu_new(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Kirim halaman CV Kontak sebagai pesan baru — dipakai dari Reply Keyboard."""
    context.user_data["current_menu_state"] = "page5_cv"
    context.user_data["active_menu_page"] = 5
    await _render_page5(context, user_id, edit_query=None)


async def show_page5_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buka halaman 5 — CV Kontak dari callback."""
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    push_nav(context, "page5_cv")
    context.user_data["current_menu_state"] = "page5_cv"
    context.user_data["active_menu_page"] = 5
    await _render_page5(context, uid, edit_query=q)


async def _render_page5(context, uid: int, edit_query=None):
    text = premium_text(f"""\
<u><b>[WhatsApp] MENU CV KONTAK [card]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[diamond] <b>Fitur Lengkap:</b>

[panahijo] Konversi TXT/VCF/XLSX
[panahijo] CV Admin/Navy & Cek Duplikat
[panahijo] Ganti Nama File & Kontak
[panahijo] Hitung/Baca Isi File
[panahijo] Gabung & Pecah File

[lightning] Pilih fitur di bawah ini [WhatsApp]</blockquote>
""")
    rich_html = f"""\
{emoji('WhatsApp')} <b>MENU CV KONTAK</b> {emoji('card')}

<table bordered striped>
<tr><th>{emoji('diamond')} Fitur Lengkap</th><th>Keterangan</th></tr>
<tr><td>Konversi File</td><td>TXT / VCF / XLSX antar format</td></tr>
<tr><td>CV Admin/Navy</td><td>Convert &amp; cek duplikat kontak</td></tr>
<tr><td>Ganti Nama</td><td>Ganti nama file & kontak</td></tr>
<tr><td>Hitung/Baca Isi File</td><td>Cek jumlah &amp; isi kontak dalam file</td></tr>
<tr><td>Gabung &amp; Pecah File</td><td>Merge atau split file kontak</td></tr>
</table>

{emoji('lightning')} Pilih fitur di bawah ini {emoji('WhatsApp')}"""
    from src.custom_emoji import styled_keyboard_button
    kb = ReplyKeyboardMarkup([
        [
            styled_keyboard_button("TXT ke VCF", style="primary", emoji_name="WhatsApp"),
            styled_keyboard_button("VCF ke TXT", style="primary", emoji_name="catatan"),
        ],
        [
            styled_keyboard_button("XLSX ke TXT", style="primary", emoji_name="patkotak"),
        ],
        [
            styled_keyboard_button("CV Admin/Navy", style="success", emoji_name="card"),
            styled_keyboard_button("Cek Duplikat", style="success", emoji_name="pin"),
        ],
        [
            styled_keyboard_button("Ganti Nama File", style="primary", emoji_name="catatan"),
            styled_keyboard_button("Ganti Nama Kontak", style="primary", emoji_name="catatan"),
        ],
        [
            styled_keyboard_button("Hitung Isi File", style="success", emoji_name="patkotak"),
            styled_keyboard_button("Ambil Nama File", style="success", emoji_name="pin"),
        ],
        [
            styled_keyboard_button("Baca Isi File", style="primary", emoji_name="catatan"),
            styled_keyboard_button("Teks ke File", style="primary", emoji_name="catatan"),
        ],
        [
            styled_keyboard_button("Gabung File", style="success", emoji_name="WhatsApp"),
            styled_keyboard_button("Pecah File", style="success", emoji_name="WhatsApp"),
        ],
        [
            styled_keyboard_button("Rekap File", style="success", emoji_name="diamond"),
        ],
        [
            styled_keyboard_button("Tutorial", style="danger", emoji_name="catatan"),
            styled_keyboard_button("Info Format", style="danger", emoji_name="pin"),
        ],
        [styled_keyboard_button(RKB_BACK_MAIN, style="danger", emoji_name="back")],
    ], resize_keyboard=True, is_persistent=True)
    set_page_reply_map(context, "page5_cv", {
        "TXT ke VCF": "cv5_txt2vcf_start",
        "VCF ke TXT": "cv5_vcf2txt_start",
        "XLSX ke TXT": "cv5_xlsx2txt_start",
        "CV Admin/Navy": "cv5_adminnavy_start",
        "Cek Duplikat": "cv5_dupe_start",
        "Ganti Nama File": "cv5_renfile_start",
        "Ganti Nama Kontak": "cv5_renkontak_start",
        "Hitung Isi File": "cv5_count_start",
        "Ambil Nama File": "cv5_getname_start",
        "Baca Isi File": "cv5_readfile_start",
        "Teks ke File": "cv5_text2file_start",
        "Gabung File": "cv5_merge_start",
        "Pecah File": "cv5_pecah_start",
        "Rekap File": "cv5_recap_start",
        "Tutorial": "cv5_tutorial",
        "Info Format": "cv5_info_format",
    })
    if edit_query:
        await fast_edit(edit_query, text, reply_markup=kb, parse_mode="HTML", rich_html=rich_html, log_label="Page5CvMenuCB")
    else:
        # NOTE: sendRichMessage Bot API belum dukung kirim sebagai photo caption,
        # jadi tampilan menu CV Kontak dikirim sebagai rich text message
        # (tanpa foto thumbnail), sama seperti pola Page 2/3/4.
        await notif.send_rich_message_to_chat(
            context.bot, uid, rich_html, text,
            reply_markup=kb,
            log_label="Page5CvMenuNew",
        )


# ─── TUTORIAL ───────────────────────────────────────────────────────────────
async def cv5_tutorial_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    text = premium_text("""\
<u><b>[catatan] TUTORIAL PECAH VCF [lightning]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[TopOne] Klik tombol <b>Pecah File VCF</b> di menu utama
[TopTwo] Bot akan minta kamu <b>kirim file .vcf</b> yang isinya banyak kontak
[TopThree] Setelah file diterima, ketik <b>nama file output</b> yang kamu inginkan (tanpa .vcf)
[patkotak] Lalu ketik <b>berapa kontak per file</b> (contoh: <code>50</code>)
[done] Bot otomatis <b>split & kirim</b> semua file hasil pecahan ke kamu!

[star] <b>Contoh:</b>
File input: <code>kontak_brazil.vcf</code> isi 500 kontak
Nama file: <code>BRAZIBISMILLAH</code>
Kontak per file: <code>50</code>
Hasil: <code>BRAZIBISMILLAH_001.vcf</code> s/d <code>BRAZIBISMILLAH_010.vcf</code>
masing-masing isi 50 kontak</blockquote>
""")
    rich_html = f"""\
{emoji('catatan')} <b>TUTORIAL PECAH VCF</b> {emoji('lightning')}

<table bordered striped>
<tr><th>Langkah</th><th>Keterangan</th></tr>
<tr><td>1</td><td>Klik tombol <b>Pecah File VCF</b> di menu utama</td></tr>
<tr><td>2</td><td>Bot akan minta kamu <b>kirim file .vcf</b> yang isinya banyak kontak</td></tr>
<tr><td>3</td><td>Setelah file diterima, ketik <b>nama file output</b> yang kamu inginkan (tanpa .vcf)</td></tr>
<tr><td>4</td><td>Lalu ketik <b>berapa kontak per file</b> (contoh: <code>50</code>)</td></tr>
<tr><td>5</td><td>Bot otomatis <b>split &amp; kirim</b> semua file hasil pecahan ke kamu!</td></tr>
</table>
<hr/>
<table bordered striped>
<tr><th>{emoji('star')} Contoh</th><th>Nilai</th></tr>
<tr><td>File input</td><td><code>kontak_brazil.vcf</code> isi 500 kontak</td></tr>
<tr><td>Nama file</td><td><code>BRAZIBISMILLAH</code></td></tr>
<tr><td>Kontak per file</td><td><code>50</code></td></tr>
<tr><td>Hasil</td><td><code>BRAZIBISMILLAH_001.vcf</code> s/d <code>BRAZIBISMILLAH_010.vcf</code>, masing-masing isi 50 kontak</td></tr>
</table>"""
    kb = styled_inline_keyboard([[styled_button("Kembali", callback_data="menu_page_5", style="danger", emoji_name="back")]])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=rich_html, log_label="Cv5Tutorial")


# ─── INFO FORMAT ────────────────────────────────────────────────────────────
async def cv5_info_format_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    text = premium_text("""\
<u><b>[pin] INFO FORMAT VCF [WhatsApp]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] <b>Format VCF yang didukung:</b>
<code>BEGIN:VCARD
VERSION:3.0
FN:NAMA KONTAK 01
TEL;TYPE=CELL:+6281234567890
END:VCARD</code>

[diamond] <b>Tips Penggunaan:</b>
[panahijo] Pastikan file berekstensi <code>.vcf</code>
[panahijo] 1 file bisa berisi ratusan/ribuan kontak
[panahijo] Nama kontak akan otomatis dinomori sesuai urutan
[panahijo] Nomor telepon asli tetap dipertahankan

[WhatsApp] File hasil bisa langsung <b>import ke WhatsApp</b> lewat menu Kontak HP kamu</blockquote>
""")
    rich_html = f"""\
{emoji('pin')} <b>INFO FORMAT VCF</b> {emoji('WhatsApp')}

<p>{emoji('catatan')} <b>Format VCF yang didukung:</b></p>
<p><code>BEGIN:VCARD
VERSION:3.0
FN:NAMA KONTAK 01
TEL;TYPE=CELL:+6281234567890
END:VCARD</code></p>

<table bordered striped>
<tr><th>{emoji('diamond')} Tips Penggunaan</th><th>Keterangan</th></tr>
<tr><td>Ekstensi File</td><td>Pastikan file berekstensi <code>.vcf</code></td></tr>
<tr><td>Kapasitas</td><td>1 file bisa berisi ratusan/ribuan kontak</td></tr>
<tr><td>Penamaan</td><td>Nama kontak akan otomatis dinomori sesuai urutan</td></tr>
<tr><td>Nomor Telepon</td><td>Nomor telepon asli tetap dipertahankan</td></tr>
</table>

{emoji('WhatsApp')} File hasil bisa langsung <b>import ke WhatsApp</b> lewat menu Kontak HP kamu"""
    kb = styled_inline_keyboard([[styled_button("Kembali", callback_data="menu_page_5", style="danger", emoji_name="back")]])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=rich_html, log_label="Cv5InfoFormat")


# ─── MULAI PECAH FILE — STEP 1: Minta upload file VCF ───────────────────────
async def cv5_pecah_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    context.user_data["current_menu_state"] = "cv5_wait_file"
    context.user_data["cv5_data"] = {}

    text = premium_text("""\
<u><b>[WhatsApp] PECAH FILE VCF [lightning]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] <b>Langkah 1 dari 3</b>

[roket] Kirim file <b>.vcf</b> kamu sekarang!
File bisa berisi ratusan atau ribuan kontak WhatsApp.

[warning] Pastikan format file adalah <code>.vcf</code> (VCard)</blockquote>
""")
    kb = styled_inline_keyboard([[styled_button("Batal", callback_data="menu_page_5", style="danger", emoji_name="batal")]])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""<u><b>[WhatsApp] PECAH FILE VCF [lightning]</b></u>
<hr/>
<p>[catatan] <b>Langkah 1 dari 3</b></p>
<ul><li>[roket] Kirim file <b>.vcf</b> kamu sekarang!</li><li>File bisa berisi ratusan atau ribuan kontak WhatsApp.</li></ul>
<p>[warning] Pastikan format file adalah <code>.vcf</code> (VCard)</p>"""), log_label="AutoRich")


async def cv5_handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Router dokumen pusat untuk semua fitur CV Kontak (page 5)."""
    # === GUARD: TOLAK GRUP & BLOCKED USER ===
    if not await is_private_chat(update):
        return
    if update.effective_user and is_blocked(update.effective_user.id):
        return
    # ===========================================
    uid = update.effective_user.id
    state = context.user_data.get("current_menu_state", "")

    # === Add Stock via file (owner sedang di step input nomor) ===
    if (
        uid == OWNER_ID
        and user_states.get(uid, {}).get("action") == "add_stock_phone"
        and user_states.get(uid, {}).get("mode") == "owner"
    ):
        await _add_stock_receive_file(update, context)
        return

    # === Restore User via file (owner sedang di step upload file .json/.js) ===
    if (
        uid == OWNER_ID
        and user_states.get(uid, {}).get("action") == "restore_user"
        and user_states.get(uid, {}).get("mode") == "owner"
    ):
        await _owner_restore_user_receive_file(update, context)
        return

    if state == "cv5_wait_file":
        await _cv5_pecah_receive_vcf(update, context)
        return
    if state == "cv5_txt2vcf_wait_file":
        await _cv5_txt2vcf_receive(update, context)
        return
    if state == "cv5_vcf2txt_wait_file":
        await _cv5_vcf2txt_receive(update, context)
        return
    if state == "cv5_xlsx2txt_wait_file":
        await _cv5_xlsx2txt_receive(update, context)
        return
    if state == "cv5_adminnavy_wait_file":
        await _cv5_adminnavy_receive(update, context)
        return
    if state == "cv5_dupe_wait_files":
        await _cv5_dupe_receive(update, context)
        return
    if state == "cv5_renfile_wait_files":
        await _cv5_renfile_receive(update, context)
        return
    if state == "cv5_renkontak_wait_file":
        await _cv5_renkontak_receive(update, context)
        return
    if state == "cv5_count_wait_files":
        await _cv5_count_receive(update, context)
        return
    if state == "cv5_getname_wait_files":
        await _cv5_getname_receive(update, context)
        return
    if state == "cv5_readfile_wait_file":
        await _cv5_readfile_receive(update, context)
        return
    if state == "cv5_merge_wait_files":
        await _cv5_merge_receive(update, context)
        return
    if state == "cv5_recap_wait_files":
        await _cv5_recap_receive(update, context)
        return
    # bukan bagian dari alur CV Kontak manapun
    return


def _parse_vcf_cards(vcf_text: str) -> list:
    """Parse VCF teks jadi list of card strings."""
    cards = []
    current = []
    for line in vcf_text.splitlines(keepends=True):
        current.append(line)
        if line.strip().upper() == "END:VCARD":
            cards.append("".join(current))
            current = []
    return cards


def _extract_phones_from_vcard(card: str) -> list:
    """Ambil semua nomor telepon dari satu vcard."""
    phones = []
    for line in card.splitlines():
        l = line.strip()
        if l.upper().startswith("TEL"):
            if ":" in l:
                phones.append(l.split(":", 1)[1].strip())
    return phones


def _extract_name_from_vcard(card: str) -> str:
    """Ambil nama (FN) dari satu vcard."""
    for line in card.splitlines():
        l = line.strip()
        if l.upper().startswith("FN:"):
            return l.split(":", 1)[1].strip()
    return ""


def _normalize_phone(p: str) -> str:
    """Normalisasi nomor telepon untuk perbandingan duplikat (hilangkan spasi/strip, +/00 prefix disamakan)."""
    digits = re.sub(r'[^\d+]', '', p)
    if digits.startswith("+"):
        digits = digits[1:]
    if digits.startswith("00"):
        digits = digits[2:]
    return digits


def _txt_lines_to_phones(txt: str) -> list:
    """Ambil daftar nomor dari file TXT (1 nomor per baris, baris kosong/komentar diabaikan)."""
    out = []
    for line in txt.splitlines():
        l = line.strip()
        if not l or l.startswith("#"):
            continue
        out.append(l)
    return out


def _build_vcf_from_phones(phones: list, name_prefix: str, start_num: int = 1) -> str:
    """Bangun isi file VCF dari daftar nomor + prefix nama (Admin 01, Admin 02, dst)."""
    out = []
    for i, phone in enumerate(phones, start=start_num):
        nama = f"{name_prefix} {i:02d}"
        out.append(f"BEGIN:VCARD\nVERSION:3.0\nFN:{nama}\nTEL;TYPE=CELL:{phone}\nEND:VCARD\n")
    return "".join(out)


async def _cv5_download_doc_bytes(context, doc) -> bytes:
    tg_file = await context.bot.get_file(doc.file_id)
    buf = io.BytesIO()
    await tg_file.download_to_memory(buf)
    return buf.getvalue()


def _cv5_safe_filename(name: str) -> str:
    """Ubah nama jadi aman untuk nama file (huruf/angka/underscore/strip saja)."""
    return re.sub(r'[^\w\-]', '_', name)


def _cv5_back_kb():
    return styled_inline_keyboard([[styled_button("Kembali", callback_data="menu_page_5", style="danger", emoji_name="back")]])


def _cv5_cancel_kb():
    return styled_inline_keyboard([[styled_button("Batal", callback_data="menu_page_5", style="danger", emoji_name="batal")]])


# ─── PECAH FILE VCF — STEP 2: Terima file VCF dari user ─────────────────────
async def _cv5_pecah_receive_vcf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        return

    fname = doc.file_name or ""
    if not fname.lower().endswith(".vcf"):
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] <b>File harus berformat .vcf!</b> Coba kirim ulang file yang benar."""), premium_text("[warning] <b>File harus berformat .vcf!</b>\nCoba kirim ulang file yang benar."), log_label="AutoRich2")
        return

    try:
        vcf_bytes = await _cv5_download_doc_bytes(context, doc)
    except Exception as e:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[error] Gagal download file: <code>{e}</code>"""), premium_text(f"[error] Gagal download file: <code>{e}</code>"), log_label="AutoRich2")
        return

    vcf_text = vcf_bytes.decode("utf-8", errors="replace")
    cards = _parse_vcf_cards(vcf_text)
    total = len(cards)

    if total == 0:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] <b>File VCF kosong atau format tidak dikenali!</b> Pastikan file berisi kontak yang valid."""), premium_text("[warning] <b>File VCF kosong atau format tidak dikenali!</b>\nPastikan file berisi kontak yang valid."), log_label="AutoRich2")
        return

    context.user_data["cv5_data"] = {
        "cards": cards,
        "total": total,
        "orig_name": fname,
    }
    context.user_data["current_menu_state"] = "cv5_wait_nama_file"

    text = premium_text(f"""\
<u><b>[done] FILE DITERIMA [verified]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] <b>Langkah 2 dari 3</b>

[WhatsApp] <b>File:</b> <code>{html.escape(fname)}</code>
[patkotak] <b>Total Kontak:</b> <code>{total:,}</code> kontak terdeteksi

[roket] Sekarang ketik <b>nama file output</b> yang kamu inginkan.
(Tanpa .vcf — contoh: <code>BRAZIBISMILLAH</code>)
Nama kontak di tiap file akan otomatis menyesuaikan.</blockquote>
""")
    kb = _cv5_cancel_kb()
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


# ─── STEP 3: Terima nama file, lalu minta jumlah kontak per file ─────────────
async def cv5_handle_nama_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Dipanggil dari handle_message jika state == cv5_wait_nama_file."""
    uid = update.effective_user.id
    nama = update.message.text.strip()

    # Sanitasi nama file
    import re as _re
    nama_safe = _re.sub(r'[^\w\-]', '_', nama)[:50]
    if not nama_safe:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] Nama file tidak valid! Coba lagi."""), premium_text("[warning] Nama file tidak valid! Coba lagi."), log_label="AutoRich2")
        return True

    context.user_data["cv5_data"]["nama_file"] = nama_safe
    context.user_data["current_menu_state"] = "cv5_wait_jumlah"

    total = context.user_data["cv5_data"].get("total", 0)
    text = premium_text(f"""\
<u><b>[patkotak] ATUR JUMLAH KONTAK [lightning]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] <b>Langkah 3 dari 3</b>

[done] <b>Nama file:</b> <code>{html.escape(nama_safe)}</code>
[WhatsApp] <b>Total kontak:</b> <code>{total:,}</code>

[roket] Sekarang ketik <b>jumlah kontak per file</b>.
Contoh: <code>50</code> → tiap file isi 50 kontak

[star] Saran:
<code>50</code> — ringan & cepat import
<code>100</code> — standar
<code>200</code> — file lebih besar</blockquote>
""")
    kb = styled_inline_keyboard([
        [
            styled_button("50 / file",  callback_data="cv5_qty_50",  style="success", emoji_name="TopOne"),
            styled_button("100 / file", callback_data="cv5_qty_100", style="primary", emoji_name="TopTwo"),
            styled_button("200 / file", callback_data="cv5_qty_200", style="primary", emoji_name="TopThree"),
        ],
        [styled_button("Batal", callback_data="menu_page_5", style="danger", emoji_name="batal")],
    ])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    return True


async def cv5_handle_jumlah_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Dipanggil dari handle_message jika state == cv5_wait_jumlah dan user ketik angka."""
    teks = update.message.text.strip()
    if not teks.isdigit():
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] Ketik angka saja! Contoh: <code>50</code>"""), premium_text("[warning] Ketik angka saja! Contoh: <code>50</code>"), log_label="AutoRich2")
        return True
    per_file = int(teks)
    if per_file < 1 or per_file > 1000:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] Jumlah harus antara <code>1</code> sampai <code>1000</code>!"""), premium_text("[warning] Jumlah harus antara <code>1</code> sampai <code>1000</code>!"), log_label="AutoRich2")
        return True
    context.user_data["cv5_data"]["per_file"] = per_file
    await _cv5_proses_pecah(update, context)
    return True


# ─── Callback tombol cepat qty ───────────────────────────────────────────────
async def cv5_qty_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    per_file = int(q.data.split("_")[2])
    context.user_data["cv5_data"]["per_file"] = per_file
    context.user_data["current_menu_state"] = "page5_cv"
    # Kirim loading
    await fast_edit(q, premium_text(f"""\
<u><b>[roket] MEMPROSES FILE [loading]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[loading] Sedang memecah file VCF...
[patkotak] <b>{per_file} kontak</b> per file

Harap tunggu sebentar [waktu]</blockquote>
"""), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[roket] MEMPROSES FILE [loading]</b></u>
<hr/>
<ul><li>[loading] Sedang memecah file VCF...</li><li>[patkotak] <b>{per_file} kontak</b> per file</li></ul>
<p>Harap tunggu sebentar [waktu]</p>"""), log_label="AutoRich")
    await _cv5_proses_pecah_from_query(q, context)


async def _cv5_proses_pecah_from_query(q, context):
    """Proses pecah dari callback query."""
    uid = q.from_user.id
    data = context.user_data.get("cv5_data", {})
    cards = data.get("cards", [])
    per_file = data.get("per_file", 50)
    nama_file = data.get("nama_file", "KONTAK")
    total = len(cards)

    if not cards:
        await fast_edit(q, premium_text("[error] Data hilang! Mulai ulang proses."), parse_mode="HTML",
                        reply_markup=styled_inline_keyboard([[styled_button("Kembali", callback_data="menu_page_5", style="danger", emoji_name="back")]]), rich_html=premium_text(f"""[error] Data hilang! Mulai ulang proses."""), log_label="AutoRich")
        return

    chunks = [cards[i:i+per_file] for i in range(0, total, per_file)]
    total_file = len(chunks)

    # Edit pesan jadi status kirim
    await fast_edit(q, premium_text(f"""\
<u><b>[roket] MENGIRIM FILE [lightning]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[loading] Mengirim <b>{total_file} file</b>...
[patkotak] Tiap file berisi <b>{per_file} kontak</b>
[WhatsApp] <b>Total kontak:</b> <code>{total:,}</code>

[waktu] Harap tunggu, file dikirim satu per satu...</blockquote>
"""), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[roket] MENGIRIM FILE [lightning]</b></u>
<hr/>
<ul><li>[loading] Mengirim <b>{total_file} file</b>...</li><li>[patkotak] Tiap file berisi <b>{per_file} kontak</b></li><li>[WhatsApp] <b>Total kontak:</b> <code>{total:,}</code></li></ul>
<p>[waktu] Harap tunggu, file dikirim satu per satu...</p>"""), log_label="AutoRich")

    await _cv5_send_chunks(context, uid, chunks, nama_file, per_file, total, total_file, status_msg=q.message, chat_id=q.message.chat_id)


async def _cv5_proses_pecah(update, context):
    """Proses pecah dari message input (ketik angka)."""
    uid = update.effective_user.id
    data = context.user_data.get("cv5_data", {})
    cards = data.get("cards", [])
    per_file = data.get("per_file", 50)
    nama_file = data.get("nama_file", "KONTAK")
    total = len(cards)
    context.user_data["current_menu_state"] = "page5_cv"

    if not cards:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[error] Data hilang! Mulai ulang proses."""), premium_text("[error] Data hilang! Mulai ulang proses."), log_label="AutoRich2")
        return

    chunks = [cards[i:i+per_file] for i in range(0, total, per_file)]
    total_file = len(chunks)

    status_msg = await notif.send_rich_message_to_chat(
        context.bot, update.message.chat_id,
        premium_text(f"""\
<u><b>[roket] MEMPROSES FILE [loading]</b></u>
<hr/>
<ul><li>[loading] Memecah & mengirim <b>{total_file} file</b>...</li><li>[WhatsApp] Total kontak: <code>{total:,}</code></li></ul>
"""),
        premium_text(f"""\
<u><b>[roket] MEMPROSES FILE [loading]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[loading] Memecah & mengirim <b>{total_file} file</b>...
[WhatsApp] Total kontak: <code>{total:,}</code></blockquote>
"""),
        log_label="Cv5PecahLoading",
    )
    await _cv5_send_chunks(context, uid, chunks, nama_file, per_file, total, total_file, status_msg=status_msg, chat_id=update.message.chat_id)


async def _cv5_send_chunks(context, uid, chunks, nama_file, per_file, total, total_file, status_msg=None, chat_id=None):
    """Kirim semua file hasil pecahan ke user."""
    berhasil = 0
    for idx, chunk in enumerate(chunks, start=1):
        # Generate nama kontak otomatis sesuai nama_file + nomor urut
        vcf_lines = []
        global_num = (idx - 1) * per_file
        for i, card in enumerate(chunk, start=1):
            nomor_global = global_num + i
            # Rebuild card dengan nama kontak baru
            rebuilt = _rebuild_card_with_name(card, nama_file, nomor_global)
            vcf_lines.append(rebuilt)

        vcf_content = "".join(vcf_lines)
        fname_out = f"{nama_file}_{idx:03d}.vcf"

        try:
            vcf_bytes = vcf_content.encode("utf-8")
            bio = io.BytesIO(vcf_bytes)
            bio.name = fname_out

            await context.bot.send_document(
                chat_id=uid,
                document=bio,
                filename=fname_out,
            )
            berhasil += 1
        except Exception as e:
            print(f"[CV5] Gagal kirim file {fname_out}: {e}")

        # Delay kecil biar tidak flood
        if idx % 5 == 0:
            import asyncio as _aio
            await _aio.sleep(0.5)

    # Kirim summary
    kb = styled_inline_keyboard([
        [styled_button("Pecah File Lagi", callback_data="cv5_pecah_start", style="success", emoji_name="WhatsApp")],
        [styled_button("Kembali ke Menu", callback_data="menu_page_5",    style="primary", emoji_name="back")],
    ])
    nama_file_safe = html.escape(nama_file)
    summary = premium_text(f"""\
<u><b>[done] PROSES SELESAI [verified]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[star] <b>Ringkasan Hasil:</b>

[WhatsApp] <b>Nama file   :</b> <code>{nama_file_safe}_XXX.vcf</code>
[patkotak] <b>Kontak/file :</b> <code>{per_file}</code>
[catatan] <b>Total file  :</b> <code>{berhasil}/{total_file}</code> berhasil dikirim
[diamond] <b>Total kontak:</b> <code>{total:,}</code>

[panahijo] Semua file siap diimport ke HP / WhatsApp kamu!</blockquote>
""")
    summary_rich = f"""\
{emoji('done')} <b>PROSES SELESAI</b>

<table bordered striped>
<tr><th>Ringkasan Hasil</th><th>Detail</th></tr>
<tr><td>Nama file</td><td><code>{nama_file_safe}_XXX.vcf</code></td></tr>
<tr><td>Kontak/file</td><td><code>{per_file}</code></td></tr>
<tr><td>Total file</td><td><code>{berhasil}/{total_file}</code> berhasil dikirim</td></tr>
<tr><td>Total kontak</td><td><code>{total:,}</code></td></tr>
</table>

{emoji('panahijo')} Semua file siap diimport ke HP / WhatsApp kamu!"""
    if status_msg is not None:
        await notif.edit_rich_message(
            context.bot, chat_id or uid, status_msg,
            summary_rich, summary, reply_markup=kb, log_label="Cv5PecahResult",
        )
    else:
        await notif.send_rich_message_to_chat(
            context.bot, uid, summary_rich, summary, reply_markup=kb, log_label="Cv5PecahResult",
        )


def _rebuild_card_with_name(card: str, prefix: str, nomor: int) -> str:
    """Rebuild satu vcard dengan nama kontak baru berformat PREFIX + nomor."""
    nama_baru = f"{prefix} {nomor:02d}"
    lines = card.splitlines(keepends=True)
    out = []
    for line in lines:
        if line.upper().startswith("FN:"):
            out.append(f"FN:{nama_baru}\n")
        elif line.upper().startswith("N:"):
            out.append(f"N:{nama_baru};;;;\n")
        else:
            out.append(line)
    return "".join(out)


# ══════════════════════════════════════════════════════════════════════════
# FITUR BARU MENU CV KONTAK (12 FITUR TAMBAHAN)
# ══════════════════════════════════════════════════════════════════════════

# ─── 1) TXT KE VCF ───────────────────────────────────────────────────────────
async def cv5_txt2vcf_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    context.user_data["current_menu_state"] = "cv5_txt2vcf_wait_file"
    context.user_data["cv5_data"] = {}
    text = premium_text("""\
<u><b>[catatan] TXT KE VCF [WhatsApp]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[roket] Kirim file <b>.txt</b> berisi daftar nomor (1 nomor per baris).
Bot akan ubah jadi file <b>.vcf</b> siap import ke WhatsApp.</blockquote>
""")
    await fast_edit(q, text, reply_markup=_cv5_cancel_kb(), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[catatan] TXT KE VCF [WhatsApp]</b></u>
<hr/>
<ul><li>[roket] Kirim file <b>.txt</b> berisi daftar nomor (1 nomor per baris).</li><li>Bot akan ubah jadi file <b>.vcf</b> siap import ke WhatsApp.</li></ul>"""), log_label="AutoRich")


async def _cv5_txt2vcf_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or not (doc.file_name or "").lower().endswith(".txt"):
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] File harus berformat <code>.txt</code>!"""), premium_text("[warning] File harus berformat <code>.txt</code>!"), log_label="AutoRich2")
        return
    raw = await _cv5_download_doc_bytes(context, doc)
    phones = _txt_lines_to_phones(raw.decode("utf-8", errors="replace"))
    if not phones:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] File TXT kosong / tidak ada nomor terdeteksi."""), premium_text("[warning] File TXT kosong / tidak ada nomor terdeteksi."), log_label="AutoRich2")
        return
    context.user_data["cv5_data"] = {"phones": phones, "orig_name": doc.file_name}
    context.user_data["current_menu_state"] = "cv5_txt2vcf_wait_name"
    text = premium_text(f"""\
<u><b>[done] FILE DITERIMA [verified]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[patkotak] <b>Total nomor:</b> <code>{len(phones):,}</code>
[roket] Ketik <b>nama kontak</b> yang diinginkan (akan dinomori otomatis).
Contoh: <code>USER</code> → USER 01, USER 02, dst.
[catatan] Nama file output akan ditanyakan di langkah berikutnya.</blockquote>
""")
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=_cv5_cancel_kb())


async def cv5_handle_txt2vcf_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    nama = update.message.text.strip()
    nama_safe = re.sub(r'[^\w\-\s]', '', nama)[:50].strip()
    if not nama_safe:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] Nama tidak valid! Coba lagi."""), premium_text("[warning] Nama tidak valid! Coba lagi."), log_label="AutoRich2")
        return True
    data = context.user_data.setdefault("cv5_data", {})
    data["nama_kontak"] = nama_safe
    context.user_data["current_menu_state"] = "cv5_txt2vcf_wait_filename"
    total = len(data.get("phones", []))
    text = premium_text(f"""\
<u><b>[catatan] NAMA FILE OUTPUT [tambah]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[done] <b>Nama kontak:</b> <code>{html.escape(nama_safe)}</code>
[catatan] <b>Total nomor:</b> <code>{total:,}</code>

[roket] Sekarang ketik <b>nama file</b> output (tanpa .vcf).
Contoh: <code>DATA_BRAZIL</code></blockquote>
""")
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=_cv5_cancel_kb())
    return True


async def cv5_handle_txt2vcf_filename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    nama_file = update.message.text.strip()
    nama_file_safe = _cv5_safe_filename(nama_file)[:50]
    if not nama_file_safe:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] Nama file tidak valid! Coba lagi."""), premium_text("[warning] Nama file tidak valid! Coba lagi."), log_label="AutoRich2")
        return True
    data = context.user_data.setdefault("cv5_data", {})
    data["nama_file"] = nama_file_safe
    context.user_data["current_menu_state"] = "cv5_txt2vcf_wait_mode"
    total = len(data.get("phones", []))
    text = premium_text(f"""\
<u><b>[patkotak] PILIH MODE OUTPUT [WhatsApp]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[done] <b>Nama kontak:</b> <code>{html.escape(data.get('nama_kontak',''))}</code>
[done] <b>Nama file:</b> <code>{html.escape(nama_file_safe)}.vcf</code>
[catatan] <b>Total nomor:</b> <code>{total:,}</code>

[roket] Mau hasilnya jadi <b>1 file saja</b>, atau <b>dipecah</b> jadi beberapa file?</blockquote>
""")
    kb = styled_inline_keyboard([
        [styled_button("1 File Saja", callback_data="cv5_txt2vcf_single", style="success", emoji_name="verified")],
        [
            styled_button("Pecah 50/file",  callback_data="cv5_txt2vcf_split_50",  style="primary", emoji_name="TopOne"),
            styled_button("Pecah 100/file", callback_data="cv5_txt2vcf_split_100", style="primary", emoji_name="TopTwo"),
            styled_button("Pecah 200/file", callback_data="cv5_txt2vcf_split_200", style="primary", emoji_name="TopThree"),
        ],
        [styled_button("Pecah — Jumlah Lain", callback_data="cv5_txt2vcf_split_custom", style="primary", emoji_name="patkotak")],
        [styled_button("Batal", callback_data="menu_page_5", style="danger", emoji_name="batal")],
    ])
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
    return True


async def _cv5_txt2vcf_send_single(query_or_msg, context, send_func):
    data = context.user_data.get("cv5_data", {})
    phones = data.get("phones", [])
    nama_kontak = data.get("nama_kontak", "KONTAK")
    nama_file = data.get("nama_file") or nama_kontak
    vcf_content = _build_vcf_from_phones(phones, nama_kontak)
    fname_out = f"{_cv5_safe_filename(nama_file)}.vcf"
    bio = io.BytesIO(vcf_content.encode("utf-8"))
    bio.name = fname_out
    context.user_data["current_menu_state"] = "page5_cv"
    caption = premium_text(f"[done] <b>{html.escape(fname_out)}</b>\n<blockquote>[patkotak] Total kontak: <code>{len(phones):,}</code></blockquote>")
    await send_func(document=bio, filename=fname_out, caption=caption, parse_mode="HTML", reply_markup=_cv5_back_kb())


async def cv5_txt2vcf_single_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id

    async def send_func(**kwargs):
        await context.bot.send_document(chat_id=uid, **kwargs)

    await fast_edit(q, premium_text("[loading] Memproses file VCF..."), parse_mode="HTML", rich_html=premium_text(f"""[loading] Memproses file VCF..."""), log_label="AutoRich")
    await _cv5_txt2vcf_send_single(q, context, send_func)


async def _cv5_txt2vcf_send_split(uid, context, per_file, status_msg=None, chat_id=None):
    data = context.user_data.get("cv5_data", {})
    phones = data.get("phones", [])
    nama_kontak = data.get("nama_kontak", "KONTAK")
    nama_file = data.get("nama_file") or nama_kontak
    total = len(phones)
    chunks = [phones[i:i + per_file] for i in range(0, total, per_file)]
    total_file = len(chunks)
    context.user_data["current_menu_state"] = "page5_cv"

    berhasil = 0
    for idx, chunk in enumerate(chunks, start=1):
        start_num = (idx - 1) * per_file + 1
        vcf_content = _build_vcf_from_phones(chunk, nama_kontak, start_num=start_num)
        fname_out = f"{_cv5_safe_filename(nama_file)}_{idx:03d}.vcf"
        bio = io.BytesIO(vcf_content.encode("utf-8"))
        bio.name = fname_out
        try:
            await context.bot.send_document(chat_id=uid, document=bio, filename=fname_out)
            berhasil += 1
        except Exception as e:
            print(f"[TXT2VCF split] Gagal kirim {fname_out}: {e}")
        if idx % 5 == 0:
            await asyncio.sleep(0.5)

    nama_file_safe = html.escape(nama_file)
    summary = premium_text(f"""\
<u><b>[done] PROSES SELESAI [verified]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[WhatsApp] <b>Nama file   :</b> <code>{nama_file_safe}_XXX.vcf</code>
[patkotak] <b>Kontak/file :</b> <code>{per_file}</code>
[catatan] <b>Total file  :</b> <code>{berhasil}/{total_file}</code> berhasil dikirim
[diamond] <b>Total kontak:</b> <code>{total:,}</code></blockquote>
""")
    summary_rich = f"""\
{emoji('done')} <b>PROSES SELESAI</b>

<table bordered striped>
<tr><th>Ringkasan Hasil</th><th>Detail</th></tr>
<tr><td>Nama file</td><td><code>{nama_file_safe}_XXX.vcf</code></td></tr>
<tr><td>Kontak/file</td><td><code>{per_file}</code></td></tr>
<tr><td>Total file</td><td><code>{berhasil}/{total_file}</code> berhasil dikirim</td></tr>
<tr><td>Total kontak</td><td><code>{total:,}</code></td></tr>
</table>"""
    if status_msg is not None:
        await notif.edit_rich_message(
            context.bot, chat_id or uid, status_msg,
            summary_rich, summary, reply_markup=_cv5_back_kb(), log_label="Cv5Txt2VcfResult",
        )
    else:
        await notif.send_rich_message_to_chat(
            context.bot, uid, summary_rich, summary, reply_markup=_cv5_back_kb(), log_label="Cv5Txt2VcfResult",
        )


async def cv5_txt2vcf_split_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    per_file = int(q.data.split("_")[-1])
    await fast_edit(q, premium_text(f"[loading] Memecah jadi file isi {per_file} kontak/file..."), parse_mode="HTML", rich_html=premium_text(f"""[loading] Memecah jadi file isi {per_file} kontak/file..."""), log_label="AutoRich")
    await _cv5_txt2vcf_send_split(q.from_user.id, context, per_file, status_msg=q.message, chat_id=q.message.chat_id)


async def cv5_txt2vcf_split_custom_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    context.user_data["current_menu_state"] = "cv5_txt2vcf_wait_custom_qty"
    text = premium_text("""\
<u><b>[patkotak] JUMLAH PER FILE [lightning]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[roket] Ketik jumlah kontak per file yang kamu mau.
Contoh: <code>75</code></blockquote>
""")
    await fast_edit(q, text, reply_markup=_cv5_cancel_kb(), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[patkotak] JUMLAH PER FILE [lightning]</b></u>
<hr/>
<ul><li>[roket] Ketik jumlah kontak per file yang kamu mau.</li><li>Contoh: <code>75</code></li></ul>"""), log_label="AutoRich")


async def cv5_handle_txt2vcf_custom_qty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    teks = update.message.text.strip()
    if not teks.isdigit():
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] Ketik angka saja! Contoh: <code>75</code>"""), premium_text("[warning] Ketik angka saja! Contoh: <code>75</code>"), log_label="AutoRich2")
        return True
    per_file = int(teks)
    if per_file < 1 or per_file > 1000:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] Jumlah harus antara <code>1</code> sampai <code>1000</code>!"""), premium_text("[warning] Jumlah harus antara <code>1</code> sampai <code>1000</code>!"), log_label="AutoRich2")
        return True
    status_msg = await notif.send_rich_message_to_chat(
        context.bot, update.message.chat_id,
        premium_text(f"[loading] Memecah jadi file isi {per_file} kontak/file..."),
        premium_text(f"[loading] Memecah jadi file isi {per_file} kontak/file..."),
        log_label="Cv5Txt2VcfLoading",
    )
    await _cv5_txt2vcf_send_split(update.effective_user.id, context, per_file, status_msg=status_msg, chat_id=update.message.chat_id)
    return True


# ─── 2) VCF KE TXT ───────────────────────────────────────────────────────────
async def cv5_vcf2txt_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    context.user_data["current_menu_state"] = "cv5_vcf2txt_wait_file"
    text = premium_text("""\
<u><b>[catatan] VCF KE TXT [WhatsApp]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[roket] Kirim file <b>.vcf</b>. Bot akan ekstrak semua nomor telepon
jadi file <b>.txt</b> (1 nomor per baris).</blockquote>
""")
    await fast_edit(q, text, reply_markup=_cv5_cancel_kb(), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[catatan] VCF KE TXT [WhatsApp]</b></u>
<hr/>
<ul><li>[roket] Kirim file <b>.vcf</b>. Bot akan ekstrak semua nomor telepon</li><li>jadi file <b>.txt</b> (1 nomor per baris).</li></ul>"""), log_label="AutoRich")


async def _cv5_vcf2txt_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or not (doc.file_name or "").lower().endswith(".vcf"):
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] File harus berformat <code>.vcf</code>!"""), premium_text("[warning] File harus berformat <code>.vcf</code>!"), log_label="AutoRich2")
        return
    raw = await _cv5_download_doc_bytes(context, doc)
    cards = _parse_vcf_cards(raw.decode("utf-8", errors="replace"))
    phones = []
    for c in cards:
        phones.extend(_extract_phones_from_vcard(c))
    if not phones:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] Tidak ada nomor terdeteksi di file VCF ini."""), premium_text("[warning] Tidak ada nomor terdeteksi di file VCF ini."), log_label="AutoRich2")
        return
    base = os.path.splitext(doc.file_name or "kontak")[0]
    fname_out = f"{base}.txt"
    bio = io.BytesIO("\n".join(phones).encode("utf-8"))
    bio.name = fname_out
    context.user_data["current_menu_state"] = "page5_cv"
    caption = premium_text(f"[done] <b>{html.escape(fname_out)}</b>\n<blockquote>[patkotak] Total nomor: <code>{len(phones):,}</code></blockquote>")
    await update.message.reply_document(document=bio, filename=fname_out, caption=caption, parse_mode="HTML",
                                         reply_markup=_cv5_back_kb())


# ─── 3) XLSX KE TXT ──────────────────────────────────────────────────────────
async def cv5_xlsx2txt_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    context.user_data["current_menu_state"] = "cv5_xlsx2txt_wait_file"
    text = premium_text("""\
<u><b>[patkotak] XLSX KE TXT [catatan]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[roket] Kirim file <b>.xlsx</b>. Bot akan ambil semua nomor dari kolom
pertama tiap baris dan ubah jadi file <b>.txt</b>.</blockquote>
""")
    await fast_edit(q, text, reply_markup=_cv5_cancel_kb(), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[patkotak] XLSX KE TXT [catatan]</b></u>
<hr/>
<ul><li>[roket] Kirim file <b>.xlsx</b>. Bot akan ambil semua nomor dari kolom</li><li>pertama tiap baris dan ubah jadi file <b>.txt</b>.</li></ul>"""), log_label="AutoRich")


async def _cv5_xlsx2txt_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or not (doc.file_name or "").lower().endswith((".xlsx", ".xls")):
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] File harus berformat <code>.xlsx</code>!"""), premium_text("[warning] File harus berformat <code>.xlsx</code>!"), log_label="AutoRich2")
        return
    raw = await _cv5_download_doc_bytes(context, doc)
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
        ws = wb.active
        phones = []
        for row in ws.iter_rows(values_only=True):
            for cell in row:
                if cell is None:
                    continue
                val = str(cell).strip()
                if val:
                    phones.append(val)
                break  # hanya kolom pertama tiap baris
    except Exception as e:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[error] Gagal baca file XLSX: <code>{html.escape(str(e))}</code>"""), premium_text(f"[error] Gagal baca file XLSX: <code>{html.escape(str(e))}</code>"), log_label="AutoRich2")
        return
    if not phones:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] Tidak ada data terdeteksi di file XLSX ini."""), premium_text("[warning] Tidak ada data terdeteksi di file XLSX ini."), log_label="AutoRich2")
        return
    base = os.path.splitext(doc.file_name or "data")[0]
    fname_out = f"{base}.txt"
    bio = io.BytesIO("\n".join(phones).encode("utf-8"))
    bio.name = fname_out
    context.user_data["current_menu_state"] = "page5_cv"
    caption = premium_text(f"[done] <b>{html.escape(fname_out)}</b>\n<blockquote>[patkotak] Total baris: <code>{len(phones):,}</code></blockquote>")
    await update.message.reply_document(document=bio, filename=fname_out, caption=caption, parse_mode="HTML",
                                         reply_markup=_cv5_back_kb())


# ─── 4) CV ADMIN/NAVY (user input nomor manual / file, lalu nama sendiri) ───
async def cv5_adminnavy_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    context.user_data["current_menu_state"] = "cv5_adminnavy_wait_file"
    context.user_data["cv5_data"] = {}
    text = premium_text("""\
<u><b>[card] CV ADMIN/NAVY [WhatsApp]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] <b>Langkah 1 dari 2</b>

[roket] Kirim daftar nomor dengan salah satu cara:
[panahijo] Kirim file <b>.txt</b> (1 nomor per baris), <i>atau</i>
[panahijo] Ketik langsung nomor (boleh banyak baris dalam 1 pesan)</blockquote>
""")
    await fast_edit(q, text, reply_markup=_cv5_cancel_kb(), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[card] CV ADMIN/NAVY [WhatsApp]</b></u>
<hr/>
<p>[catatan] <b>Langkah 1 dari 2</b></p>
<ul><li>[roket] Kirim daftar nomor dengan salah satu cara:</li><li>[panahijo] Kirim file <b>.txt</b> (1 nomor per baris), <i>atau</i></li><li>[panahijo] Ketik langsung nomor (boleh banyak baris dalam 1 pesan)</li></ul>"""), log_label="AutoRich")


async def _cv5_adminnavy_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or not (doc.file_name or "").lower().endswith(".txt"):
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] File harus berformat <code>.txt</code>!"""), premium_text("[warning] File harus berformat <code>.txt</code>!"), log_label="AutoRich2")
        return
    raw = await _cv5_download_doc_bytes(context, doc)
    phones = _txt_lines_to_phones(raw.decode("utf-8", errors="replace"))
    await _cv5_adminnavy_got_phones(update, context, phones)


async def cv5_handle_adminnavy_phones_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    phones = _txt_lines_to_phones(update.message.text)
    await _cv5_adminnavy_got_phones(update, context, phones)
    return True


async def _cv5_adminnavy_got_phones(update, context, phones):
    if not phones:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] Tidak ada nomor terdeteksi! Coba lagi."""), premium_text("[warning] Tidak ada nomor terdeteksi! Coba lagi."), log_label="AutoRich2")
        return
    context.user_data["cv5_data"] = {"phones": phones}
    context.user_data["current_menu_state"] = "cv5_adminnavy_wait_name"
    text = premium_text(f"""\
<u><b>[done] NOMOR DITERIMA [verified]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] <b>Langkah 2 dari 2</b>

[patkotak] <b>Total nomor:</b> <code>{len(phones):,}</code>
[roket] Ketik nama kontak yang kamu mau (misal: <code>Admin</code> atau <code>Navy</code>).
Hasil otomatis dinomori: Admin 01, Admin 02, dst.</blockquote>
""")
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=_cv5_cancel_kb())


async def cv5_handle_adminnavy_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    nama = update.message.text.strip()
    nama_safe = re.sub(r'[^\w\-\s]', '', nama)[:50].strip()
    if not nama_safe:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] Nama tidak valid! Coba lagi."""), premium_text("[warning] Nama tidak valid! Coba lagi."), log_label="AutoRich2")
        return True
    data = context.user_data.setdefault("cv5_data", {})
    data["nama_kontak"] = nama_safe
    context.user_data["current_menu_state"] = "cv5_adminnavy_wait_filename"
    text = premium_text(f"""\
<u><b>[catatan] NAMA FILE OUTPUT [tambah]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[done] <b>Nama kontak:</b> <code>{html.escape(nama_safe)}</code>
[roket] Sekarang ketik <b>nama file</b> output (tanpa .vcf).
Contoh: <code>DATA_ADMIN</code></blockquote>
""")
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=_cv5_cancel_kb())
    return True


async def cv5_handle_adminnavy_filename(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    nama_file = update.message.text.strip()
    nama_file_safe = _cv5_safe_filename(nama_file)[:50]
    if not nama_file_safe:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] Nama file tidak valid! Coba lagi."""), premium_text("[warning] Nama file tidak valid! Coba lagi."), log_label="AutoRich2")
        return True
    data = context.user_data.get("cv5_data", {})
    phones = data.get("phones", [])
    nama_kontak = data.get("nama_kontak", "KONTAK")
    vcf_content = _build_vcf_from_phones(phones, nama_kontak)
    fname_out = f"{nama_file_safe}.vcf"
    bio = io.BytesIO(vcf_content.encode("utf-8"))
    bio.name = fname_out
    context.user_data["current_menu_state"] = "page5_cv"
    caption = premium_text(f"[done] <b>{html.escape(fname_out)}</b>\n<blockquote>[patkotak] Total kontak: <code>{len(phones):,}</code></blockquote>")
    await update.message.reply_document(document=bio, filename=fname_out, caption=caption, parse_mode="HTML",
                                         reply_markup=_cv5_back_kb())
    return True


# ─── 5) CEK DUPLIKAT (bandingkan banyak file, cari nomor yang sama) ─────────
async def cv5_dupe_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    context.user_data["current_menu_state"] = "cv5_dupe_wait_files"
    context.user_data["cv5_data"] = {"files": []}
    text = premium_text("""\
<u><b>[pin] CEK DUPLIKAT [patkotak]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[roket] Kirim <b>2 atau lebih</b> file <code>.txt</code>/<code>.vcf</code> satu per satu.
Bot akan bandingkan dan cari nomor yang muncul di lebih dari 1 file.

[catatan] File terkirim: <code>0</code>
Setelah selesai kirim semua file, tekan <b>Proses Cek</b>.</blockquote>
""")
    kb = styled_inline_keyboard([
        [styled_button("Proses Cek", callback_data="cv5_dupe_process", style="success", emoji_name="verified")],
        [styled_button("Batal", callback_data="menu_page_5", style="danger", emoji_name="batal")],
    ])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""<u><b>[pin] CEK DUPLIKAT [patkotak]</b></u>
<hr/>
<ul><li>[roket] Kirim <b>2 atau lebih</b> file <code>.txt</code>/<code>.vcf</code> satu per satu.</li><li>Bot akan bandingkan dan cari nomor yang muncul di lebih dari 1 file.</li></ul>
<ul><li>[catatan] File terkirim: <code>0</code></li><li>Setelah selesai kirim semua file, tekan <b>Proses Cek</b>.</li></ul>"""), log_label="AutoRich")


def _cv5_phones_from_file(fname: str, raw: bytes) -> list:
    if fname.lower().endswith(".vcf"):
        cards = _parse_vcf_cards(raw.decode("utf-8", errors="replace"))
        phones = []
        for c in cards:
            phones.extend(_extract_phones_from_vcard(c))
        return phones
    return _txt_lines_to_phones(raw.decode("utf-8", errors="replace"))


async def _cv5_dupe_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    fname = doc.file_name or ""
    if not fname.lower().endswith((".txt", ".vcf")):
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] Hanya menerima file <code>.txt</code> atau <code>.vcf</code>!"""), premium_text("[warning] Hanya menerima file <code>.txt</code> atau <code>.vcf</code>!"), log_label="AutoRich2")
        return
    raw = await _cv5_download_doc_bytes(context, doc)
    phones = _cv5_phones_from_file(fname, raw)
    data = context.user_data.setdefault("cv5_data", {"files": []})
    data["files"].append({"name": fname, "phones": phones})
    total_files = len(data["files"])
    kb = styled_inline_keyboard([
        [styled_button("Proses Cek", callback_data="cv5_dupe_process", style="success", emoji_name="verified")],
        [styled_button("Batal", callback_data="menu_page_5", style="danger", emoji_name="batal")],
    ])
    await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[done] <b>{html.escape(fname)}</b> diterima ({len(phones)} nomor). [catatan] Total file terkirim: <code>{total_files}</code>"""), premium_text(f"[done] <b>{html.escape(fname)}</b> diterima ({len(phones)} nomor).\n[catatan] Total file terkirim: <code>{total_files}</code>"), reply_markup=kb, log_label="AutoRich2")


async def cv5_dupe_process_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    data = context.user_data.get("cv5_data", {})
    files = data.get("files", [])
    if len(files) < 2:
        await safe_answer(q, "⚠️ Minimal kirim 2 file dulu!", show_alert=True)
        return

    seen = {}  # normalized phone -> list of file names
    for f in files:
        norm_set = set()
        for p in f["phones"]:
            norm = _normalize_phone(p)
            if norm in norm_set:
                continue
            norm_set.add(norm)
            seen.setdefault(norm, []).append(f["name"])

    dupes = {k: v for k, v in seen.items() if len(v) > 1}
    context.user_data["current_menu_state"] = "page5_cv"

    if not dupes:
        await fast_edit(q, premium_text(f"""\
<u><b>[done] HASIL CEK DUPLIKAT [verified]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[star] Tidak ada nomor duplikat di antara <code>{len(files)}</code> file yang dicek.</blockquote>
"""), reply_markup=_cv5_back_kb(), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[done] HASIL CEK DUPLIKAT [verified]</b></u>
<hr/>
<p>[star] Tidak ada nomor duplikat di antara <code>{len(files)}</code> file yang dicek.</p>"""), log_label="AutoRich")
        return

    lines = [f"{phone}  →  muncul di: {', '.join(names)}" for phone, names in dupes.items()]
    result_txt = "\n".join(lines)
    bio = io.BytesIO(result_txt.encode("utf-8"))
    bio.name = "hasil_duplikat.txt"
    caption = premium_text(f"""\
<u><b>[done] HASIL CEK DUPLIKAT [verified]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[patkotak] File dicek: <code>{len(files)}</code>
[warning] Nomor duplikat ditemukan: <code>{len(dupes):,}</code></blockquote>
""")
    await context.bot.send_document(chat_id=q.from_user.id, document=bio, filename="hasil_duplikat.txt",
                                     caption=caption, parse_mode="HTML", reply_markup=_cv5_back_kb())


# ─── 6) GANTI NAMA FILE (banyak file sekaligus) ─────────────────────────────
async def cv5_renfile_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    context.user_data["current_menu_state"] = "cv5_renfile_wait_files"
    context.user_data["cv5_data"] = {"files": []}
    text = premium_text("""\
<u><b>[catatan] GANTI NAMA FILE [tambah]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[roket] Kirim file (boleh banyak sekaligus / satu per satu, format apa saja).
Setelah selesai kirim semua, tekan <b>Lanjut</b>.

[catatan] File terkirim: <code>0</code></blockquote>
""")
    kb = styled_inline_keyboard([
        [styled_button("Lanjut", callback_data="cv5_renfile_next", style="success", emoji_name="verified")],
        [styled_button("Batal", callback_data="menu_page_5", style="danger", emoji_name="batal")],
    ])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""<u><b>[catatan] GANTI NAMA FILE [tambah]</b></u>
<hr/>
<ul><li>[roket] Kirim file (boleh banyak sekaligus / satu per satu, format apa saja).</li><li>Setelah selesai kirim semua, tekan <b>Lanjut</b>.</li></ul>
<p>[catatan] File terkirim: <code>0</code></p>"""), log_label="AutoRich")


async def _cv5_renfile_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc:
        return
    raw = await _cv5_download_doc_bytes(context, doc)
    data = context.user_data.setdefault("cv5_data", {"files": []})
    data["files"].append({"name": doc.file_name or "file", "bytes": raw})
    total_files = len(data["files"])
    kb = styled_inline_keyboard([
        [styled_button("Lanjut", callback_data="cv5_renfile_next", style="success", emoji_name="verified")],
        [styled_button("Batal", callback_data="menu_page_5", style="danger", emoji_name="batal")],
    ])
    await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[done] <b>{html.escape(doc.file_name or '')}</b> diterima. [catatan] Total file: <code>{total_files}</code>"""), premium_text(f"[done] <b>{html.escape(doc.file_name or '')}</b> diterima.\n[catatan] Total file: <code>{total_files}</code>"), reply_markup=kb, log_label="AutoRich2")


async def cv5_renfile_next_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    data = context.user_data.get("cv5_data", {})
    if not data.get("files"):
        await safe_answer(q, "⚠️ Belum ada file dikirim!", show_alert=True)
        return
    context.user_data["current_menu_state"] = "cv5_renfile_wait_pattern"
    total = len(data["files"])
    text = premium_text(f"""\
<u><b>[catatan] NAMA BARU [tambah]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[patkotak] Total file: <code>{total}</code>
[roket] Ketik nama dasar untuk semua file (akan dinomori otomatis bila lebih dari 1 file).
Contoh: <code>DATA_BARU</code> → DATA_BARU_001.ext, DATA_BARU_002.ext, dst.
Ekstensi asli tiap file tetap dipertahankan.</blockquote>
""")
    await fast_edit(q, text, reply_markup=_cv5_cancel_kb(), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[catatan] NAMA BARU [tambah]</b></u>
<hr/>
<ul><li>[patkotak] Total file: <code>{total}</code></li><li>[roket] Ketik nama dasar untuk semua file (akan dinomori otomatis bila lebih dari 1 file).</li><li>Contoh: <code>DATA_BARU</code> → DATA_BARU_001.ext, DATA_BARU_002.ext, dst.</li><li>Ekstensi asli tiap file tetap dipertahankan.</li></ul>"""), log_label="AutoRich")


async def cv5_handle_renfile_pattern(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    nama = update.message.text.strip()
    nama_safe = re.sub(r'[^\w\-]', '_', nama)[:50]
    if not nama_safe:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] Nama tidak valid! Coba lagi."""), premium_text("[warning] Nama tidak valid! Coba lagi."), log_label="AutoRich2")
        return True
    data = context.user_data.get("cv5_data", {})
    files = data.get("files", [])
    context.user_data["current_menu_state"] = "page5_cv"
    multi = len(files) > 1
    for idx, f in enumerate(files, start=1):
        ext = os.path.splitext(f["name"])[1] or ""
        new_name = f"{nama_safe}_{idx:03d}{ext}" if multi else f"{nama_safe}{ext}"
        bio = io.BytesIO(f["bytes"])
        bio.name = new_name
        await update.message.reply_document(document=bio, filename=new_name,
                                             caption=premium_text(f"[done] <code>{html.escape(new_name)}</code>"),
                                             parse_mode="HTML")
    await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[verified] Selesai! <code>{len(files)}</code> file berhasil diganti nama."""), premium_text(f"[verified] Selesai! <code>{len(files)}</code> file berhasil diganti nama."), reply_markup=_cv5_back_kb(), log_label="AutoRich2")
    return True


# ─── 7) GANTI NAMA KONTAK (di dalam file VCF) ───────────────────────────────
async def cv5_renkontak_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    context.user_data["current_menu_state"] = "cv5_renkontak_wait_file"
    context.user_data["cv5_data"] = {}
    text = premium_text("""\
<u><b>[catatan] GANTI NAMA KONTAK [tambah]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[roket] Kirim file <b>.vcf</b> yang ingin diganti nama kontaknya.</blockquote>
""")
    await fast_edit(q, text, reply_markup=_cv5_cancel_kb(), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[catatan] GANTI NAMA KONTAK [tambah]</b></u>
<hr/>
<p>[roket] Kirim file <b>.vcf</b> yang ingin diganti nama kontaknya.</p>"""), log_label="AutoRich")


async def _cv5_renkontak_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc or not (doc.file_name or "").lower().endswith(".vcf"):
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] File harus berformat <code>.vcf</code>!"""), premium_text("[warning] File harus berformat <code>.vcf</code>!"), log_label="AutoRich2")
        return
    raw = await _cv5_download_doc_bytes(context, doc)
    cards = _parse_vcf_cards(raw.decode("utf-8", errors="replace"))
    if not cards:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] File VCF kosong / tidak valid."""), premium_text("[warning] File VCF kosong / tidak valid."), log_label="AutoRich2")
        return
    context.user_data["cv5_data"] = {"cards": cards, "orig_name": doc.file_name}
    context.user_data["current_menu_state"] = "cv5_renkontak_wait_name"
    text = premium_text(f"""\
<u><b>[done] FILE DITERIMA [verified]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[patkotak] Total kontak: <code>{len(cards):,}</code>
[roket] Ketik nama kontak baru (akan dinomori otomatis).
Contoh: <code>USER</code> → USER 01, USER 02, dst.</blockquote>
""")
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=_cv5_cancel_kb())


async def cv5_handle_renkontak_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    nama = update.message.text.strip()
    nama_safe = re.sub(r'[^\w\-\s]', '', nama)[:50].strip()
    if not nama_safe:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] Nama tidak valid! Coba lagi."""), premium_text("[warning] Nama tidak valid! Coba lagi."), log_label="AutoRich2")
        return True
    data = context.user_data.get("cv5_data", {})
    cards = data.get("cards", [])
    orig_name = data.get("orig_name", "kontak.vcf")
    rebuilt = [_rebuild_card_with_name(c, nama_safe, i) for i, c in enumerate(cards, start=1)]
    vcf_content = "".join(rebuilt)
    bio = io.BytesIO(vcf_content.encode("utf-8"))
    bio.name = orig_name
    context.user_data["current_menu_state"] = "page5_cv"
    caption = premium_text(f"[done] <b>{html.escape(orig_name)}</b>\n<blockquote>[patkotak] Total kontak diganti: <code>{len(cards):,}</code></blockquote>")
    await update.message.reply_document(document=bio, filename=orig_name, caption=caption, parse_mode="HTML",
                                         reply_markup=_cv5_back_kb())
    return True


# ─── 8) HITUNG ISI FILE (banyak file sekaligus) ─────────────────────────────
async def cv5_count_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    context.user_data["current_menu_state"] = "cv5_count_wait_files"
    context.user_data["cv5_data"] = {"files": []}
    text = premium_text("""\
<u><b>[patkotak] HITUNG ISI FILE [catatan]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[roket] Kirim file <code>.txt</code> atau <code>.vcf</code> (boleh banyak).
Bot akan hitung jumlah baris/kontak tiap file.
Setelah selesai, tekan <b>Lihat Hasil</b>.</blockquote>
""")
    kb = styled_inline_keyboard([
        [styled_button("Lihat Hasil", callback_data="cv5_count_process", style="success", emoji_name="verified")],
        [styled_button("Batal", callback_data="menu_page_5", style="danger", emoji_name="batal")],
    ])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""<u><b>[patkotak] HITUNG ISI FILE [catatan]</b></u>
<hr/>
<ul><li>[roket] Kirim file <code>.txt</code> atau <code>.vcf</code> (boleh banyak).</li><li>Bot akan hitung jumlah baris/kontak tiap file.</li><li>Setelah selesai, tekan <b>Lihat Hasil</b>.</li></ul>"""), log_label="AutoRich")


async def _cv5_count_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    fname = doc.file_name or ""
    raw = await _cv5_download_doc_bytes(context, doc)
    if fname.lower().endswith(".vcf"):
        count = len(_parse_vcf_cards(raw.decode("utf-8", errors="replace")))
    else:
        count = len(_txt_lines_to_phones(raw.decode("utf-8", errors="replace")))
    data = context.user_data.setdefault("cv5_data", {"files": []})
    data["files"].append({"name": fname, "count": count})
    kb = styled_inline_keyboard([
        [styled_button("Lihat Hasil", callback_data="cv5_count_process", style="success", emoji_name="verified")],
        [styled_button("Batal", callback_data="menu_page_5", style="danger", emoji_name="batal")],
    ])
    await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[done] <b>{html.escape(fname)}</b>: <code>{count:,}</code> baris/kontak."""), premium_text(f"[done] <b>{html.escape(fname)}</b>: <code>{count:,}</code> baris/kontak."), reply_markup=kb, log_label="AutoRich2")


async def cv5_count_process_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    data = context.user_data.get("cv5_data", {})
    files = data.get("files", [])
    context.user_data["current_menu_state"] = "page5_cv"
    if not files:
        await safe_answer(q, "⚠️ Belum ada file dikirim!", show_alert=True)
        return
    lines = [f"[patkotak] <code>{html.escape(f['name'])}</code> → <b>{f['count']:,}</b>" for f in files]
    total = sum(f["count"] for f in files)
    body = "\n".join(lines)
    text = premium_text(f"""\
<u><b>[done] HASIL HITUNG ISI FILE [verified]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>{body}

[diamond] <b>Total semua file:</b> <code>{total:,}</code></blockquote>
""")
    await fast_edit(q, text, reply_markup=_cv5_back_kb(), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[done] HASIL HITUNG ISI FILE [verified]</b></u>
<hr/>
<p>{body}</p>
<p>[diamond] <b>Total semua file:</b> <code>{total:,}</code></p>"""), log_label="AutoRich")


# ─── 9) AMBIL NAMA FILE ──────────────────────────────────────────────────────
async def cv5_getname_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    context.user_data["current_menu_state"] = "cv5_getname_wait_files"
    context.user_data["cv5_data"] = {"names": []}
    text = premium_text("""\
<u><b>[pin] AMBIL NAMA FILE [catatan]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[roket] Kirim file apa saja (boleh banyak). Bot akan kumpulkan
semua nama filenya jadi satu daftar.
Setelah selesai, tekan <b>Lihat Hasil</b>.</blockquote>
""")
    kb = styled_inline_keyboard([
        [styled_button("Lihat Hasil", callback_data="cv5_getname_process", style="success", emoji_name="verified")],
        [styled_button("Batal", callback_data="menu_page_5", style="danger", emoji_name="batal")],
    ])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""<u><b>[pin] AMBIL NAMA FILE [catatan]</b></u>
<hr/>
<ul><li>[roket] Kirim file apa saja (boleh banyak). Bot akan kumpulkan</li><li>semua nama filenya jadi satu daftar.</li><li>Setelah selesai, tekan <b>Lihat Hasil</b>.</li></ul>"""), log_label="AutoRich")


async def _cv5_getname_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    data = context.user_data.setdefault("cv5_data", {"names": []})
    data["names"].append(doc.file_name or "file")
    kb = styled_inline_keyboard([
        [styled_button("Lihat Hasil", callback_data="cv5_getname_process", style="success", emoji_name="verified")],
        [styled_button("Batal", callback_data="menu_page_5", style="danger", emoji_name="batal")],
    ])
    await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[done] Tercatat: <code>{html.escape(doc.file_name or '')}</code> [catatan] Total: <code>{len(data['names'])}</code>"""), premium_text(f"[done] Tercatat: <code>{html.escape(doc.file_name or '')}</code>\n[catatan] Total: <code>{len(data['names'])}</code>"), reply_markup=kb, log_label="AutoRich2")


async def cv5_getname_process_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    data = context.user_data.get("cv5_data", {})
    names = data.get("names", [])
    context.user_data["current_menu_state"] = "page5_cv"
    if not names:
        await safe_answer(q, "⚠️ Belum ada file dikirim!", show_alert=True)
        return
    result_txt = "\n".join(names)
    bio = io.BytesIO(result_txt.encode("utf-8"))
    bio.name = "daftar_nama_file.txt"
    caption = premium_text(f"[done] <b>Daftar Nama File</b>\n<blockquote>[patkotak] Total file: <code>{len(names):,}</code></blockquote>")
    await context.bot.send_document(chat_id=q.from_user.id, document=bio, filename="daftar_nama_file.txt",
                                     caption=caption, parse_mode="HTML", reply_markup=_cv5_back_kb())


# ─── 10) BACA ISI FILE ───────────────────────────────────────────────────────
async def cv5_readfile_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    context.user_data["current_menu_state"] = "cv5_readfile_wait_file"
    text = premium_text("""\
<u><b>[catatan] BACA ISI FILE [pin]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[roket] Kirim file <code>.txt</code> atau <code>.vcf</code>.
Bot akan tampilkan isinya langsung di chat (dipotong bila terlalu panjang).</blockquote>
""")
    await fast_edit(q, text, reply_markup=_cv5_cancel_kb(), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[catatan] BACA ISI FILE [pin]</b></u>
<hr/>
<ul><li>[roket] Kirim file <code>.txt</code> atau <code>.vcf</code>.</li><li>Bot akan tampilkan isinya langsung di chat (dipotong bila terlalu panjang).</li></ul>"""), log_label="AutoRich")


async def _cv5_readfile_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    fname = doc.file_name or ""
    if not fname.lower().endswith((".txt", ".vcf")):
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] Hanya mendukung file <code>.txt</code> atau <code>.vcf</code>!"""), premium_text("[warning] Hanya mendukung file <code>.txt</code> atau <code>.vcf</code>!"), log_label="AutoRich2")
        return
    raw = await _cv5_download_doc_bytes(context, doc)
    isi = raw.decode("utf-8", errors="replace")
    context.user_data["current_menu_state"] = "page5_cv"

    MAX_CHARS = 3500
    potongan = isi[:MAX_CHARS]
    terpotong = len(isi) > MAX_CHARS
    escaped = html.escape(potongan)
    text = premium_text(f"""\
<u><b>[catatan] ISI FILE: {html.escape(fname)} [pin]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote><code>{escaped}</code></blockquote>
{f"[warning] <i>Ditampilkan sebagian ({MAX_CHARS} karakter pertama). Kirim ulang sebagai dokumen untuk isi lengkap.</i>" if terpotong else ""}
""")
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=_cv5_back_kb())


# ─── 11) TEKS KE FILE ────────────────────────────────────────────────────────
async def cv5_text2file_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    context.user_data["current_menu_state"] = "cv5_text2file_wait_text"
    text = premium_text("""\
<u><b>[catatan] TEKS KE FILE [tambah]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[roket] Ketik / paste teks yang ingin dijadikan file <code>.txt</code>.</blockquote>
""")
    await fast_edit(q, text, reply_markup=_cv5_cancel_kb(), parse_mode="HTML", rich_html=premium_text(f"""<u><b>[catatan] TEKS KE FILE [tambah]</b></u>
<hr/>
<p>[roket] Ketik / paste teks yang ingin dijadikan file <code>.txt</code>.</p>"""), log_label="AutoRich")


async def cv5_handle_text2file_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    isi = update.message.text
    context.user_data["cv5_data"] = {"isi": isi}
    context.user_data["current_menu_state"] = "cv5_text2file_wait_name"
    await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[catatan] Sekarang ketik nama file (tanpa ekstensi). Contoh: <code>CATATAN</code>"""), premium_text("[catatan] Sekarang ketik nama file (tanpa ekstensi). Contoh: <code>CATATAN</code>"), reply_markup=_cv5_cancel_kb(), log_label="AutoRich2")
    return True


async def cv5_handle_text2file_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    nama = update.message.text.strip()
    nama_safe = re.sub(r'[^\w\-]', '_', nama)[:50]
    if not nama_safe:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] Nama tidak valid! Coba lagi."""), premium_text("[warning] Nama tidak valid! Coba lagi."), log_label="AutoRich2")
        return True
    isi = context.user_data.get("cv5_data", {}).get("isi", "")
    fname_out = f"{nama_safe}.txt"
    bio = io.BytesIO(isi.encode("utf-8"))
    bio.name = fname_out
    context.user_data["current_menu_state"] = "page5_cv"
    await update.message.reply_document(document=bio, filename=fname_out,
                                         caption=premium_text(f"[done] <code>{html.escape(fname_out)}</code>"),
                                         parse_mode="HTML", reply_markup=_cv5_back_kb())
    return True


# ─── 12) GABUNG FILE ─────────────────────────────────────────────────────────
async def cv5_merge_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    context.user_data["current_menu_state"] = "cv5_merge_wait_files"
    context.user_data["cv5_data"] = {"files": []}
    text = premium_text("""\
<u><b>[WhatsApp] GABUNG FILE [tambah]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[roket] Kirim <b>2 atau lebih</b> file <code>.txt</code> atau <code>.vcf</code> (harus format sama semua).
Setelah selesai, tekan <b>Gabungkan</b>.

[catatan] File terkirim: <code>0</code></blockquote>
""")
    kb = styled_inline_keyboard([
        [styled_button("Gabungkan", callback_data="cv5_merge_process", style="success", emoji_name="verified")],
        [styled_button("Batal", callback_data="menu_page_5", style="danger", emoji_name="batal")],
    ])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""<u><b>[WhatsApp] GABUNG FILE [tambah]</b></u>
<hr/>
<ul><li>[roket] Kirim <b>2 atau lebih</b> file <code>.txt</code> atau <code>.vcf</code> (harus format sama semua).</li><li>Setelah selesai, tekan <b>Gabungkan</b>.</li></ul>
<p>[catatan] File terkirim: <code>0</code></p>"""), log_label="AutoRich")


async def _cv5_merge_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    fname = doc.file_name or ""
    if not fname.lower().endswith((".txt", ".vcf")):
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] Hanya menerima file <code>.txt</code> atau <code>.vcf</code>!"""), premium_text("[warning] Hanya menerima file <code>.txt</code> atau <code>.vcf</code>!"), log_label="AutoRich2")
        return
    raw = await _cv5_download_doc_bytes(context, doc)
    ext = os.path.splitext(fname)[1].lower()
    data = context.user_data.setdefault("cv5_data", {"files": []})
    existing_exts = {os.path.splitext(f["name"])[1].lower() for f in data["files"]}
    if existing_exts and ext not in existing_exts:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] Semua file harus format yang sama (semua .txt atau semua .vcf)!"""), premium_text("[warning] Semua file harus format yang sama (semua .txt atau semua .vcf)!"), log_label="AutoRich2")
        return
    data["files"].append({"name": fname, "bytes": raw})
    kb = styled_inline_keyboard([
        [styled_button("Gabungkan", callback_data="cv5_merge_process", style="success", emoji_name="verified")],
        [styled_button("Batal", callback_data="menu_page_5", style="danger", emoji_name="batal")],
    ])
    await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[done] <b>{html.escape(fname)}</b> diterima. [catatan] Total file: <code>{len(data['files'])}</code>"""), premium_text(f"[done] <b>{html.escape(fname)}</b> diterima.\n[catatan] Total file: <code>{len(data['files'])}</code>"), reply_markup=kb, log_label="AutoRich2")


async def cv5_merge_process_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    data = context.user_data.get("cv5_data", {})
    files = data.get("files", [])
    context.user_data["current_menu_state"] = "page5_cv"
    if len(files) < 2:
        await safe_answer(q, "⚠️ Minimal kirim 2 file dulu!", show_alert=True)
        return
    ext = os.path.splitext(files[0]["name"])[1].lower() or ".txt"
    merged = b"\n".join(f["bytes"].rstrip(b"\n") for f in files) if ext == ".txt" else b"".join(f["bytes"] for f in files)
    fname_out = f"GABUNGAN{ext}"
    bio = io.BytesIO(merged)
    bio.name = fname_out
    caption = premium_text(f"[done] <b>{fname_out}</b>\n<blockquote>[patkotak] Total file digabung: <code>{len(files)}</code></blockquote>")
    await context.bot.send_document(chat_id=q.from_user.id, document=bio, filename=fname_out,
                                     caption=caption, parse_mode="HTML", reply_markup=_cv5_back_kb())


# ─── 13) REKAP FILE ──────────────────────────────────────────────────────────
async def cv5_recap_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    context.user_data["current_menu_state"] = "cv5_recap_wait_files"
    context.user_data["cv5_data"] = {"files": []}
    text = premium_text("""\
<u><b>[diamond] REKAP FILE [patkotak]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[roket] Kirim file <code>.txt</code>/<code>.vcf</code> (boleh banyak).
Bot akan bikin rekap: nama file, jumlah isi, dan total keseluruhan.
Setelah selesai, tekan <b>Buat Rekap</b>.</blockquote>
""")
    kb = styled_inline_keyboard([
        [styled_button("Buat Rekap", callback_data="cv5_recap_process", style="success", emoji_name="verified")],
        [styled_button("Batal", callback_data="menu_page_5", style="danger", emoji_name="batal")],
    ])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=premium_text(f"""<u><b>[diamond] REKAP FILE [patkotak]</b></u>
<hr/>
<ul><li>[roket] Kirim file <code>.txt</code>/<code>.vcf</code> (boleh banyak).</li><li>Bot akan bikin rekap: nama file, jumlah isi, dan total keseluruhan.</li><li>Setelah selesai, tekan <b>Buat Rekap</b>.</li></ul>"""), log_label="AutoRich")


async def _cv5_recap_receive(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    fname = doc.file_name or ""
    raw = await _cv5_download_doc_bytes(context, doc)
    if fname.lower().endswith(".vcf"):
        count = len(_parse_vcf_cards(raw.decode("utf-8", errors="replace")))
    else:
        count = len(_txt_lines_to_phones(raw.decode("utf-8", errors="replace")))
    data = context.user_data.setdefault("cv5_data", {"files": []})
    data["files"].append({"name": fname, "count": count})
    kb = styled_inline_keyboard([
        [styled_button("Buat Rekap", callback_data="cv5_recap_process", style="success", emoji_name="verified")],
        [styled_button("Batal", callback_data="menu_page_5", style="danger", emoji_name="batal")],
    ])
    await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[done] <b>{html.escape(fname)}</b> tercatat ({count:,} isi)."""), premium_text(f"[done] <b>{html.escape(fname)}</b> tercatat ({count:,} isi)."), reply_markup=kb, log_label="AutoRich2")


async def cv5_recap_process_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    data = context.user_data.get("cv5_data", {})
    files = data.get("files", [])
    context.user_data["current_menu_state"] = "page5_cv"
    if not files:
        await safe_answer(q, "⚠️ Belum ada file dikirim!", show_alert=True)
        return
    lines = [f"{f['name']} : {f['count']} kontak/baris" for f in files]
    total = sum(f["count"] for f in files)
    lines.append(f"\nTOTAL FILE: {len(files)}")
    lines.append(f"TOTAL ISI : {total}")
    result_txt = "\n".join(lines)
    bio = io.BytesIO(result_txt.encode("utf-8"))
    bio.name = "rekap_file.txt"
    caption = premium_text(f"""\
<u><b>[done] REKAP FILE [verified]</b></u>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[patkotak] Total file: <code>{len(files):,}</code>
[diamond] Total isi: <code>{total:,}</code></blockquote>
""")
    await context.bot.send_document(chat_id=q.from_user.id, document=bio, filename="rekap_file.txt",
                                     caption=caption, parse_mode="HTML", reply_markup=_cv5_back_kb())


# ─── Register Page 5 callbacks ke handle_callback ────────────────────────────


# ════════════════════════════════════════════════════════════════
#   OWNER — BLOKIR / UNBLOKIR USER
# ════════════════════════════════════════════════════════════════

async def owner_blokir_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)
    user_states[uid] = {"action": "owner_blokir_input", "mode": "owner"}
    text = premium_text("""
[batal] <b>BLOKIR USER</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Kirim <b>user_id</b> atau <b>@username</b> user yang ingin diblokir.
[warning] User yang diblokir tidak bisa akses bot sama sekali.</blockquote>
""")
    await fast_edit(q, text, reply_markup=create_cancel_button(), parse_mode="HTML", rich_html=premium_text(f"""[batal] <b>BLOKIR USER</b>
<hr/>
<ul><li>[catatan] Kirim <b>user_id</b> atau <b>@username</b> user yang ingin diblokir.</li><li>[warning] User yang diblokir tidak bisa akses bot sama sekali.</li></ul>"""), log_label="AutoRich")


async def owner_list_blokir_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)
    rows = get_blocked_list()
    if not rows:
        await fast_edit(q, premium_text("[verified] <b>Tidak ada user yang diblokir.</b>"), reply_markup=create_owner_menu(context), parse_mode="HTML", rich_html=premium_text(f"""[verified] <b>Tidak ada user yang diblokir.</b>"""), log_label="AutoRich")
        return

    raw = "[batal] <b>LIST USER DIBLOKIR</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>"
    table_rows = ""
    for r in rows:
        u_id, uname, blocked_at, reason = r
        tgl = datetime.fromtimestamp(blocked_at).strftime("%d/%m/%Y %H:%M") if blocked_at else "-"
        uname_str = f"@{uname}" if uname else f"id{u_id}"
        raw += f"\n[warning] <b>{uname_str}</b> | <code>{u_id}</code>\n[waktu] Diblokir: {tgl}"
        reason_display = html.escape(str(reason)) if reason else "-"
        if reason:
            raw += f"\n[catatan] Alasan: {reason}"
        raw += "\n"
        table_rows += f"<tr><td>{html.escape(str(uname_str))}</td><td><code>{u_id}</code></td><td><code>{tgl}</code></td><td>{reason_display}</td></tr>\n"
    raw += "</blockquote>"
    rich_html = f"""\
{emoji('batal')} <b>LIST USER DIBLOKIR</b>

<table bordered striped>
<tr><th>User</th><th>ID</th><th>Diblokir</th><th>Alasan</th></tr>
{table_rows}</table>"""

    kb = styled_inline_keyboard([
        [styled_button("Unblokir User", callback_data="owner_unblokir_user", style="success", emoji_name="verified")],
        [styled_button("Kembali", callback_data="owner_panel", style="danger", emoji_name="back")],
    ])
    await fast_edit(q, premium_text(raw), reply_markup=kb, parse_mode="HTML", rich_html=rich_html, log_label="OwnerListBlokir")


async def owner_unblokir_user_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True)
        return
    await safe_answer(q)
    user_states[uid] = {"action": "owner_unblokir_input", "mode": "owner"}
    text = premium_text("""
[verified] <b>UNBLOKIR USER</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Kirim <b>user_id</b> atau <b>@username</b> user yang ingin diunblokir.</blockquote>
""")
    await fast_edit(q, text, reply_markup=create_cancel_button(), parse_mode="HTML", rich_html=premium_text(f"""[verified] <b>UNBLOKIR USER</b>
<hr/>
<p>[catatan] Kirim <b>user_id</b> atau <b>@username</b> user yang ingin diunblokir.</p>"""), log_label="AutoRich")


async def kirim_rekap_saldo(bot):
    """Kirim file rekap saldo semua user ke owner, dipanggil tiap 12 jam."""
    import io
    from datetime import datetime
    try:
        cursor.execute(
            "SELECT user_id, username, deposit_balance, belance_balance FROM users ORDER BY belance_balance DESC"
        )
        rows = cursor.fetchall()
    except Exception as e:
        print(f"[Rekap Saldo] Gagal ambil data: {e}")
        return

    now_str = datetime.now().strftime("%d-%m-%Y %H:%M")
    lines = [
        f"REKAP SALDO USER - {now_str}",
        "=" * 40,
        f"{'No':<4} {'Username':<20} {'User ID':<14} {'Total Deposit':<16} {'Saldo Aktif'}",
        "-" * 72,
    ]
    total_deposit_all = 0
    total_saldo_all = 0
    for i, row in enumerate(rows, 1):
        user_id, username, deposit_balance, belance_balance = row
        dep = deposit_balance or 0
        sal = belance_balance or 0
        total_deposit_all += dep
        total_saldo_all += sal
        uname = f"@{username}" if username else f"id{user_id}"
        lines.append(f"{i:<4} {uname:<20} {str(user_id):<14} Rp {dep:>12,}   Rp {sal:>12,}")

    lines += [
        "-" * 72,
        f"{'TOTAL':<4} {'':<20} {'':<14} Rp {total_deposit_all:>12,}   Rp {total_saldo_all:>12,}",
        "=" * 40,
        f"Total user: {len(rows)}",
    ]

    content = "\n".join(lines)
    bio = io.BytesIO(content.encode("utf-8"))
    bio.name = f"rekap_saldo_{datetime.now().strftime('%d%m%Y_%H%M')}.txt"

    owner_ids = OWNER_ID if isinstance(OWNER_ID, list) else [OWNER_ID]
    caption = (
        f"📊 <b>REKAP SALDO USER</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🕐 <b>Waktu:</b> {now_str}\n"
        f"👥 <b>Total User:</b> {len(rows)}\n"
        f"💰 <b>Total Saldo Aktif:</b> Rp {total_saldo_all:,}"
    )
    for oid in owner_ids:
        try:
            bio.seek(0)
            await bot.send_document(
                chat_id=oid,
                document=bio,
                filename=bio.name,
                caption=caption,
                parse_mode="HTML"
            )
            print(f"[Rekap Saldo] Terkirim ke owner {oid}")
        except Exception as e:
            print(f"[Rekap Saldo] Gagal kirim ke {oid}: {e}")


async def rekap_saldo_loop(bot):
    """Loop rekap saldo tiap 12 jam."""
    import asyncio
    await asyncio.sleep(60)  # tunggu bot stabil dulu 1 menit setelah start
    while True:
        await kirim_rekap_saldo(bot)
        await asyncio.sleep(12 * 3600)  # 12 jam


async def kirim_backup_user_json(bot):
    """Bikin file JSON backup user (user_id, username, deposit_balance,
    belance_balance) & kirim ke semua owner. Format-nya sengaja sama persis
    dengan yang diterima fitur Restore User, jadi file ini bisa langsung
    dipakai lagi buat restore kalau data hilang. Dipanggil dari tombol
    'Backup User' (manual) maupun auto_backup_user_loop (otomatis tiap 40 menit)."""
    import json as _json
    try:
        cursor.execute(
            "SELECT user_id, username, deposit_balance, belance_balance, created_at FROM users ORDER BY user_id ASC"
        )
        rows = cursor.fetchall()
    except Exception as e:
        print(f"[Backup User JSON] Gagal ambil data: {e}")
        return

    users_export = [
        {
            "user_id": r[0],
            "username": r[1],
            "deposit_balance": r[2] or 0,
            "belance_balance": r[3] or 0,
            "created_at": r[4],
        }
        for r in rows
    ]

    json_bytes = _json.dumps(users_export, ensure_ascii=False, indent=2).encode("utf-8")
    bio = io.BytesIO(json_bytes)
    fname = f"backup_user_{time.strftime('%Y%m%d_%H%M%S')}.json"
    bio.name = fname

    caption = premium_text(f"""
[done] <b>AUTO BACKUP USER</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] Total user: <b>{len(users_export)}</b>
[pin] Isi: user_id, username, deposit_balance, belance_balance
[shield] File ini bisa langsung dipakai lagi lewat tombol <b>Restore User</b>.</blockquote>
""")

    owner_ids = OWNER_ID.all_ids if hasattr(OWNER_ID, 'all_ids') else (OWNER_ID if isinstance(OWNER_ID, list) else [OWNER_ID])
    for oid in owner_ids:
        try:
            bio.seek(0)
            await bot.send_document(
                chat_id=oid, document=bio, filename=fname,
                caption=caption, parse_mode="HTML"
            )
        except Exception as e:
            print(f"[Backup User JSON] Gagal kirim ke {oid}: {e}")


async def auto_backup_user_loop(bot):
    """Loop auto-backup user (JSON) tiap 40 menit — file dikirim ke semua owner,
    dan formatnya siap dipakai balik lewat tombol Restore User kalau sewaktu-waktu
    data users hilang/corrupt."""
    import asyncio
    await asyncio.sleep(120)  # tunggu bot stabil dulu 2 menit setelah start
    while True:
        await kirim_backup_user_json(bot)
        await asyncio.sleep(40 * 60)  # 40 menit


# ═══════════════════════════════════════════════════════════════════════════════
#   PAGE 7 — MENU CEK ID TELEGRAM
# ═══════════════════════════════════════════════════════════════════════════════

async def send_page7_menu_new(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Kirim halaman Cek ID sebagai pesan baru — dipakai dari Reply Keyboard."""
    context.user_data["current_menu_state"] = "page7_cekid"
    context.user_data["active_menu_page"] = 7

    text = premium_text("""
[card] <b>CEK PROFIL TELEGRAM</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[verified] Gunakan perintah di bawah ini untuk mengecek informasi lengkap akun Telegram:

[panahijo] <code>/info @username</code> — cek via username
[panahijo] <code>/info 123456789</code> — cek via User ID
[panahijo] <code>/info +6281234567890</code> — cek via nomor HP

[shield] Informasi yang ditampilkan: ID, Nama, DC, Tanggal Buat, Username, Premium, Status, Scam/Fake Label, Usia Akun, dan Rating.</blockquote>
""")
    rich_html = f"""\
{emoji('card')} <b>CEK PROFIL TELEGRAM</b>

<table bordered striped>
<tr><th>Perintah</th><th>Keterangan</th></tr>
<tr><td><code>/info @username</code></td><td>Cek via username</td></tr>
<tr><td><code>/info 123456789</code></td><td>Cek via User ID</td></tr>
<tr><td><code>/info +6281234567890</code></td><td>Cek via nomor HP</td></tr>
</table>

{emoji('shield')} Informasi yang ditampilkan: ID, Nama, DC, Tanggal Buat, Username, Premium, Status, Scam/Fake Label, Usia Akun, dan Rating."""

    from src.custom_emoji import styled_keyboard_button
    keyboard = ReplyKeyboardMarkup([
        [styled_keyboard_button("Cara Pakai /info", style="primary", emoji_name="catatan")],
        [styled_keyboard_button(RKB_BACK_MAIN, style="danger", emoji_name="back")],
    ], resize_keyboard=True, is_persistent=True)
    set_page_reply_map(context, "page7_cekid", {"Cara Pakai /info": "page7_cara_info"})

    # NOTE: sendRichMessage Bot API belum dukung kirim sebagai photo caption,
    # jadi tampilan menu Cek ID dikirim sebagai rich text message
    # (tanpa foto thumbnail), sama seperti pola Page 2/3/4/5.
    await notif.send_rich_message_to_chat(
        context.bot, user_id, rich_html, text,
        reply_markup=keyboard,
        log_label="Page7CekIdMenuNew",
    )



# ═══════════════════════════════════════════════════════════════════════════════
#   PAGE 6 — MENU TOPUP STARS (beli Telegram Stars via Fragment.com, on-chain)
# ═══════════════════════════════════════════════════════════════════════════════
from src import stars_topup
from src import premium_topup


async def _stars_resolve_username(username: str):
    """Cek eksistensi @username via MTProto (pakai session gift_sender yang
    sudah ada di project) — ringan, cuma buat verifikasi sebelum order.
    Return (display_name, real_username, user_id, photo_bytes) kalau ketemu,
    atau None kalau gagal/tidak ada. photo_bytes bisa None kalau target gak
    punya foto profil publik / gagal diunduh."""
    try:
        from src.gift_sender import get_gift_client, GIFT_SESSION_FILE
        if not os.path.exists(GIFT_SESSION_FILE):
            return None
        client = await get_gift_client(API_ID, API_HASH)
        if not client or not client.is_connected():
            return None
        entity = await client.get_entity(username.lstrip("@"))
        name = " ".join(filter(None, [getattr(entity, "first_name", "") or "", getattr(entity, "last_name", "") or ""])).strip() or username
        real_username = getattr(entity, "username", None) or username
        real_id = getattr(entity, "id", None)

        photo_bytes = None
        try:
            if getattr(entity, "photo", None):
                buf = io.BytesIO()
                await client.download_profile_photo(entity, file=buf, download_big=True)
                buf.seek(0)
                raw = buf.read()
                photo_bytes = raw if raw else None
        except Exception:
            photo_bytes = None

        return (name, real_username, real_id, photo_bytes)
    except Exception:
        return None


def _build_stars_found_card_image(username: str, user_id, photo_bytes: bytes = None) -> "io.BytesIO":
    """Render kartu 'USERNAME FOUND' sebagai gambar buat alur beli Stars —
    avatar target di kiri (kalau ada), badge status, username besar, ID akun,
    dan lencana verified di kanan. Full digambar via PIL, gak butuh aset
    template eksternal supaya gampang dirawat."""
    from PIL import Image, ImageDraw, ImageOps

    W, H = 1000, 420
    bg = Image.new("RGB", (W, H), (13, 17, 23))
    draw = ImageDraw.Draw(bg)

    # Panel utama, rounded rectangle dengan border tipis
    panel = (30, 30, W - 30, H - 30)
    draw.rounded_rectangle(panel, radius=24, fill=(22, 27, 34), outline=(42, 47, 58), width=2)

    # Garis pembatas vertikal antara area avatar & area teks
    divider_x = 330
    draw.line([(divider_x, 70), (divider_x, H - 70)], fill=(42, 47, 58), width=2)

    # ── Avatar di kiri ──────────────────────────────────────────────
    avatar_size = 200
    avatar_pos = (95, (H - avatar_size) // 2)
    ring_color = (90, 170, 255, 255)
    if photo_bytes:
        try:
            pp = Image.open(io.BytesIO(photo_bytes)).convert("RGBA")
            pp = ImageOps.fit(pp, (avatar_size, avatar_size))
            mask = Image.new("L", (avatar_size, avatar_size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, avatar_size, avatar_size), fill=255)
            bg_rgba = bg.convert("RGBA")
            ring = Image.new("RGBA", (avatar_size + 8, avatar_size + 8), (0, 0, 0, 0))
            ImageDraw.Draw(ring).ellipse((0, 0, avatar_size + 8, avatar_size + 8), outline=ring_color, width=4)
            bg_rgba.paste(ring, (avatar_pos[0] - 4, avatar_pos[1] - 4), ring)
            bg_rgba.paste(pp, avatar_pos, mask)
            bg = bg_rgba.convert("RGB")
            draw = ImageDraw.Draw(bg)
        except Exception:
            photo_bytes = None
    if not photo_bytes:
        # Fallback: lingkaran solid dengan inisial username
        cx, cy = avatar_pos[0] + avatar_size // 2, avatar_pos[1] + avatar_size // 2
        draw.ellipse((avatar_pos[0], avatar_pos[1], avatar_pos[0] + avatar_size, avatar_pos[1] + avatar_size),
                      fill=(35, 42, 54), outline=ring_color[:3], width=4)
        initial = (username[:1] or "?").upper()
        f_initial = _cekid_font(_CEKID_FONT_BOLD, 80)
        draw.text((cx, cy), initial, font=f_initial, fill=(150, 190, 240), anchor="mm")

    # ── Badge "USERNAME FOUND" ─────────────────────────────────────
    f_badge = _cekid_font(_CEKID_FONT_BOLD, 20)
    badge_x, badge_y = divider_x + 40, 70
    badge_text = "USERNAME FOUND"
    badge_w = f_badge.getlength(badge_text) + 40
    draw.rounded_rectangle((badge_x, badge_y, badge_x + badge_w, badge_y + 40), radius=20, fill=(20, 60, 40), outline=(60, 200, 130), width=2)
    draw.text((badge_x + badge_w / 2, badge_y + 20), badge_text, font=f_badge, fill=(90, 220, 150), anchor="mm")

    # ── Username besar ───────────────────────────────────────────────
    f_username = _cekid_font(_CEKID_FONT_BOLD, 46)
    uname_disp = f"@{username}" if not username.startswith("@") else username
    draw.text((divider_x + 40, badge_y + 70), uname_disp, font=f_username, fill=(235, 238, 242))

    # ── ID Account ───────────────────────────────────────────────────
    f_label = _cekid_font(_CEKID_FONT_BOLD, 16)
    f_value = _cekid_font(_CEKID_FONT_REG, 26)
    id_y = badge_y + 150
    draw.text((divider_x + 40, id_y), "ID ACCOUNT", font=f_label, fill=(122, 138, 156))
    draw.text((divider_x + 40, id_y + 26), str(user_id) if user_id else "—", font=f_value, fill=(110, 180, 255))

    # ── Lencana verified di kanan atas ────────────────────────────────
    vsize = 64
    vx, vy = W - 110, 75
    draw.ellipse((vx - vsize // 2, vy - vsize // 2, vx + vsize // 2, vy + vsize // 2), fill=(30, 130, 76))
    f_check = _cekid_font(_CEKID_FONT_BOLD, 34)
    draw.text((vx, vy), "\u2713", font=f_check, fill=(255, 255, 255), anchor="mm")
    f_vlabel = _cekid_font(_CEKID_FONT_REG, 14)
    draw.text((vx, vy + vsize // 2 + 18), "Verified", font=f_vlabel, fill=(122, 138, 156), anchor="mm")

    out = io.BytesIO()
    bg.save(out, format="JPEG", quality=92)
    out.seek(0)
    return out


async def send_page6_menu_new(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Kirim halaman Topup Stars sebagai pesan baru — dipakai dari Reply Keyboard."""
    context.user_data["current_menu_state"] = "page6_stars"
    context.user_data["active_menu_page"] = 6
    context.user_data.pop("stars_pending", None)

    user_data = get_user(user_id)
    saldo = user_data[3] if user_data else 0  # FIX: belance_balance, bukan deposit_balance
    harga = await stars_topup.get_harga_jual_per_star()
    mode_label = "🔄 Real-time (ikut harga TON)" if stars_topup.get_pricing_mode() == "auto" else "✍️ Manual"

    text = premium_text(f"""
[stars_ico] <b>TOPUP TELEGRAM STARS</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[card] <b>Saldo Kamu :</b> <code>{format_currency(saldo)}</code>
[dolar] <b>Harga per Star :</b> <code>{format_currency(harga)}</code> ({mode_label})

[lightning] <b>Cara Pakai:</b>
[panahijo] Tekan "Beli Stars"
[panahijo] Masukkan username Telegram tujuan
[panahijo] Masukkan jumlah Stars yang mau dibeli
[panahijo] Pilih metode bayar (Saldo / Transfer Manual)
[panahijo] Stars terkirim otomatis ke tujuan</blockquote>
""")
    rich_html = f"""\
{emoji('stars_ico')} <b>TOPUP TELEGRAM STARS</b>

<table bordered striped>
<tr><th>Info</th><th>Detail</th></tr>
<tr><td>Saldo Kamu</td><td><code>{format_currency(saldo)}</code></td></tr>
<tr><td>Harga per Star</td><td><code>{format_currency(harga)}</code> ({mode_label})</td></tr>
</table>
<hr/>
<table bordered striped>
<tr><th>{emoji('lightning')} Cara Pakai</th><th>Langkah</th></tr>
<tr><td>1</td><td>Tekan "Beli Stars"</td></tr>
<tr><td>2</td><td>Masukkan username Telegram tujuan</td></tr>
<tr><td>3</td><td>Masukkan jumlah Stars yang mau dibeli</td></tr>
<tr><td>4</td><td>Pilih metode bayar (Saldo / Transfer Manual)</td></tr>
<tr><td>5</td><td>Stars terkirim otomatis ke tujuan</td></tr>
</table>"""

    from src.custom_emoji import styled_keyboard_button
    rows = [
        [styled_keyboard_button("Beli Stars", style="success", emoji_name="miniapp_stars")],
        [styled_keyboard_button("Cek Order Saya", style="primary", emoji_name="card")],
        [styled_keyboard_button(RKB_BACK_MAIN, style="danger", emoji_name="back")],
    ]
    kb = ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)
    set_page_reply_map(context, "page6_stars", {
        "Beli Stars": "stars_beli_start",
        "Cek Order Saya": "stars_myorders",
    })

    await notif.send_rich_message_to_chat(
        context.bot, user_id, rich_html, text,
        reply_markup=kb,
        log_label="Page6StarsMenuNew",
    )


async def stars_beli_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mulai alur beli Stars — tanya username tujuan."""
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    push_nav(context, "stars_ask_target")
    context.user_data["stars_pending"] = {}
    context.user_data["current_menu_state"] = "stars_ask_target"

    from src.custom_emoji import styled_keyboard_button
    kb = ReplyKeyboardMarkup([[styled_keyboard_button("Batal", style="danger", emoji_name="back")]], resize_keyboard=True, is_persistent=True)
    set_page_reply_map(context, "stars_ask_target", {"Batal": "stars_order_cancel"})

    await notif.send_rich_message_to_chat(
        context.bot, uid,
        f'{emoji("stars_ico")} <b>Masukkan username Telegram tujuan</b>\n\n'
        f'<table bordered striped>\n'
        f'<tr><th>No</th><th>Contoh</th></tr>\n'
        f'<tr><td>1</td><td>@Durov</td></tr>\n'
        f'<tr><td>2</td><td>@pmgue</td></tr>\n'
        f'</table>',
        premium_text('[stars_ico] Masukkan <b>username Telegram tujuan</b> (contoh: @username):'),
        reply_markup=kb,
        log_label="StarsAskTarget",
    )


async def handle_stars_target_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if context.user_data.get("current_menu_state") != "stars_ask_target":
        return False
    uid = update.effective_user.id
    raw = (update.message.text or "").strip().lstrip("@")

    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", raw or ""):
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            f'{emoji("warning")} Format username salah. Username Telegram: 5-32 karakter, huruf/angka/underscore, tidak diawali angka. Contoh: @username',
            premium_text("[warning] Format username salah.\nUsername Telegram: 5-32 karakter, huruf/angka/underscore, tidak diawali angka.\nContoh: @username"),
            log_label="StarsTargetInvalid",
        )
        return True

    checking = await context.bot.send_message(uid, premium_text(f"[loading] Checking username @{raw}..."), parse_mode="HTML")
    resolved = await _stars_resolve_username(raw)
    try:
        await checking.delete()
    except Exception:
        pass

    if resolved is None:
        await notif.send_rich_message_to_chat(
            context.bot, uid,
            f'{emoji("warning")} Username <b>@{raw}</b> tidak ditemukan, atau MTProto belum di-setup owner. Coba username lain, atau lanjut dengan risiko sendiri dengan mengirim ulang username yang sama.',
            premium_text(f"[warning] Username <b>@{raw}</b> tidak ditemukan atau belum bisa diverifikasi. Kirim ulang username yang benar."),
            log_label="StarsTargetNotFound",
        )
        return True

    display_name, real_username, real_id, photo_bytes = resolved
    context.user_data["stars_pending"] = {"target": real_username, "display_name": display_name}
    context.user_data["current_menu_state"] = "stars_ask_qty"
    set_page_reply_map(context, "stars_ask_qty", {"Batal": "stars_order_cancel"})

    try:
        card_img = _build_stars_found_card_image(real_username, real_id, photo_bytes)
        await context.bot.send_photo(chat_id=uid, photo=card_img)
    except Exception as e:
        print(f"[StarsFoundCard] Gagal render kartu: {e}")

    await notif.send_rich_message_to_chat(
        context.bot, uid,
        f'{emoji("verified")} <b>Username ditemukan:</b>\n\n'
        f'<table bordered striped>\n'
        f'<tr><th>Username</th><th>Id Akun</th></tr>\n'
        f'<tr><td>@{real_username}</td><td>{real_id if real_id else "—"}</td></tr>\n'
        f'</table>\n'
        f'{emoji("stars_ico")} Masukkan <b>jumlah Stars</b> yang mau dibeli (contoh: 75) - (minimal: 50 Stars)',
        premium_text(f"[verified] Username ditemukan: {display_name} (@{real_username})\n\n[stars_ico] Masukkan jumlah Stars yang mau dibeli (contoh: 75):"),
        log_label="StarsAskQty",
    )
    return True


async def handle_stars_qty_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if context.user_data.get("current_menu_state") != "stars_ask_qty":
        return False
    uid = update.effective_user.id
    try:
        qty = int((update.message.text or "").strip())
        assert qty > 0
    except (ValueError, AssertionError):
        await notif.send_rich_message_to_chat(
            context.bot, uid,
            f'{emoji("warning")} Jumlah tidak valid. Masukkan angka, contoh: 75',
            premium_text("[warning] Jumlah tidak valid. Masukkan angka, contoh: 75"),
            log_label="StarsQtyInvalid",
        )
        return True

    stars_data = context.user_data.get("stars_pending", {})
    harga = await stars_topup.get_harga_jual_per_star()
    total = qty * harga
    stars_data["qty"] = qty
    stars_data["price"] = total
    context.user_data["stars_pending"] = stars_data
    context.user_data["current_menu_state"] = "idle"

    user_data = get_user(uid)
    saldo = user_data[3] if user_data else 0  # FIX: belance_balance, bukan deposit_balance

    rich_html = f"""\
{emoji('card')} <b>RINGKASAN ORDER STARS</b>

<table bordered striped>
<tr><th>Info</th><th>Detail</th></tr>
<tr><td>Tujuan</td><td>{stars_data.get('display_name','')} (@{stars_data.get('target','')})</td></tr>
<tr><td>Jumlah</td><td>{qty} Stars</td></tr>
<tr><td>Total</td><td><b>{format_currency(total)}</b></td></tr>
<tr><td>Saldo Kamu</td><td>{format_currency(saldo)}</td></tr>
</table>

{emoji('catatan')} Pilih metode pembayaran di bawah ini."""
    text = premium_text(f"""
[card] <b>RINGKASAN ORDER STARS</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[product] <b>Tujuan:</b> {stars_data.get('display_name','')} (@{stars_data.get('target','')})
[stars_ico] <b>Jumlah:</b> {qty} Stars
[dolar] <b>Total:</b> <b>{format_currency(total)}</b>
[duitkarung] <b>Saldo Kamu:</b> {format_currency(saldo)}

[catatan] Pilih metode pembayaran di bawah ini.</blockquote>
""")
    kb = styled_inline_keyboard([
        [styled_button("Bayar Pakai Saldo", callback_data="stars_order_confirm_saldo", style="success", emoji_name="duitkarung")],
        [styled_button("💳 Bayar via QRIS", callback_data="stars_order_confirm_qris", style="primary", emoji_name="dolar")],
        [styled_button("Batal", callback_data="stars_order_cancel", style="danger", emoji_name="back")],
    ])
    await notif.send_rich_message_to_chat(
        context.bot, uid, rich_html, text,
        reply_markup=kb,
        log_label="StarsOrderSummary",
    )
    return True


# ==================== BULK STARS (banyak username sekaligus) ====================

async def bulk_stars_beli_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mulai alur beli Stars BULK — langsung minta 1 tabel: username + jumlah Stars
    masing-masing (boleh beda-beda per username), dalam SATU pesan."""
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    push_nav(context, "bulk_stars_ask_table")
    context.user_data["stars_pending"] = {}
    context.user_data["current_menu_state"] = "bulk_stars_ask_table"

    from src.custom_emoji import styled_keyboard_button
    kb = ReplyKeyboardMarkup([[styled_keyboard_button("Batal", style="danger", emoji_name="back")]], resize_keyboard=True, is_persistent=True)
    set_page_reply_map(context, "bulk_stars_ask_table", {"Batal": "stars_order_cancel"})

    await notif.send_rich_message_to_chat(
        context.bot, uid,
        f'{emoji("bulkstars_ico")} <b>Topup Stars Bulk — banyak username sekaligus</b>\n\n'
        f'Kirim daftar tujuan, <b>1 baris = 1 username + jumlah Stars</b> (boleh beda-beda tiap username). '
        f'Maksimal {BULK_STARS_MAX_TARGETS} baris, minimal 50 Stars per username.\n\n'
        f'<table bordered striped>\n'
        f'<tr><th>Username</th><th>Jumlah Stars</th></tr>\n'
        f'<tr><td>@durov</td><td>50</td></tr>\n'
        f'<tr><td>@pmgue</td><td>150</td></tr>\n'
        f'<tr><td>@campah</td><td>75</td></tr>\n'
        f'</table>\n'
        f'{emoji("catatan")} Kirim semua baris dalam <b>satu pesan</b>.',
        premium_text(
            f"[bulkstars_ico] Topup Stars Bulk. Kirim daftar username + jumlah Stars, 1 baris per username "
            f"(maks {BULK_STARS_MAX_TARGETS}, min 50 Stars/username). Contoh:\n@durov 50\n@pmgue 150\n@campah 75"
        ),
        reply_markup=kb,
        log_label="BulkStarsAskTable",
    )


BULK_STARS_MAX_TARGETS = 25   # batas wajar per order biar gak nge-spam Fragment/TON
BULK_STARS_MIN_QTY     = 50   # minimal Stars per username


async def handle_bulk_stars_table_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Parse 1 pesan berisi banyak baris 'username  jumlah' (satu baris per
    username), qty BOLEH beda tiap baris. Pemisah username↔angka fleksibel:
    spasi, tab, atau koma."""
    if context.user_data.get("current_menu_state") != "bulk_stars_ask_table":
        return False
    uid = update.effective_user.id
    raw = (update.message.text or "").strip()

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if not lines:
        await notif.send_rich_message_to_chat(
            context.bot, uid,
            f'{emoji("warning")} Kirim minimal 1 baris, format: @username jumlah',
            premium_text("[warning] Kirim minimal 1 baris, format: @username jumlah"),
            log_label="BulkStarsTableEmpty",
        )
        return True

    if len(lines) > BULK_STARS_MAX_TARGETS:
        await notif.send_rich_message_to_chat(
            context.bot, uid,
            f'{emoji("warning")} Maksimal {BULK_STARS_MAX_TARGETS} baris/username per order. Kamu kirim {len(lines)}. Kurangi dulu ya.',
            premium_text(f"[warning] Maksimal {BULK_STARS_MAX_TARGETS} baris per order. Kamu kirim {len(lines)}."),
            log_label="BulkStarsTableTooMany",
        )
        return True

    line_pat = re.compile(r"^@?([A-Za-z][A-Za-z0-9_]{4,31})[\s,]+(\d+)$")
    parsed, invalid = [], []
    for ln in lines:
        m = line_pat.match(ln)
        if not m:
            invalid.append(ln)
            continue
        uname, qty_str = m.group(1), m.group(2)
        qty = int(qty_str)
        if qty < BULK_STARS_MIN_QTY:
            invalid.append(f"{ln} (min {BULK_STARS_MIN_QTY} Stars)")
            continue
        parsed.append((uname, qty))

    if invalid:
        await notif.send_rich_message_to_chat(
            context.bot, uid,
            f'{emoji("warning")} Baris berikut tidak valid:\n<blockquote>{html.escape(chr(10).join(invalid))}</blockquote>\n\n'
            f'Format tiap baris: <code>@username jumlah</code> (contoh: <code>@durov 50</code>), minimal {BULK_STARS_MIN_QTY} Stars.',
            premium_text(f"[warning] Baris tidak valid: {', '.join(invalid)}. Format: @username jumlah (min {BULK_STARS_MIN_QTY})."),
            log_label="BulkStarsTableInvalid",
        )
        return True

    # Dedupe by username, jaga urutan, qty terakhir yang menang kalau ada duplikat.
    dedup = {}
    for uname, qty in parsed:
        dedup[uname] = qty
    targets_qty = list(dedup.items())

    harga = await stars_topup.get_harga_jual_per_star()
    total_qty = sum(q for _, q in targets_qty)
    total = total_qty * harga

    stars_data = {
        "mode": "bulk",
        "targets": [u for u, _ in targets_qty],
        "targets_qty": targets_qty,
        "qty": total_qty,
        "price": total,
        "target": f"{len(targets_qty)} username ({', '.join('@'+u for u,_ in targets_qty[:5])}{'…' if len(targets_qty) > 5 else ''})",
    }
    context.user_data["stars_pending"] = stars_data
    context.user_data["current_menu_state"] = "idle"

    user_data = get_user(uid)
    saldo = user_data[3] if user_data else 0  # FIX: belance_balance, bukan deposit_balance

    table_rows = "".join(f"<tr><td>@{u}</td><td>{q}</td></tr>" for u, q in targets_qty)
    rich_html = f"""\
{emoji('card')} <b>RINGKASAN ORDER STARS BULK</b>

<table bordered striped>
<tr><th>Username</th><th>Jumlah Stars</th></tr>
{table_rows}
</table>

<table bordered striped>
<tr><th>Info</th><th>Detail</th></tr>
<tr><td>Jumlah Username</td><td>{len(targets_qty)}</td></tr>
<tr><td>Total Stars</td><td>{total_qty}</td></tr>
<tr><td>Total Bayar</td><td><b>{format_currency(total)}</b></td></tr>
<tr><td>Saldo Kamu</td><td>{format_currency(saldo)}</td></tr>
</table>

{emoji('catatan')} Pilih metode pembayaran di bawah ini."""
    text = premium_text(f"""
[card] <b>RINGKASAN ORDER STARS BULK</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[product] <b>Jumlah Username:</b> {len(targets_qty)}
[bulkstars_ico] <b>Total Stars:</b> {total_qty}
[dolar] <b>Total:</b> <b>{format_currency(total)}</b>
[duitkarung] <b>Saldo Kamu:</b> {format_currency(saldo)}

[catatan] Pilih metode pembayaran di bawah ini.</blockquote>
""")
    kb = styled_inline_keyboard([
        [styled_button("Bayar Pakai Saldo", callback_data="stars_order_confirm_saldo", style="success", emoji_name="duitkarung")],
        [styled_button("💳 Bayar via QRIS", callback_data="stars_order_confirm_qris", style="primary", emoji_name="dolar")],
        [styled_button("Batal", callback_data="stars_order_cancel", style="danger", emoji_name="back")],
    ])
    await notif.send_rich_message_to_chat(
        context.bot, uid, rich_html, text,
        reply_markup=kb,
        log_label="BulkStarsOrderSummary",
    )
    return True


# ==================== TOPUP PREMIUM ====================

async def premium_beli_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mulai alur beli Telegram Premium — tanya username tujuan."""
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    push_nav(context, "premium_ask_target")
    context.user_data["stars_pending"] = {}
    context.user_data["current_menu_state"] = "premium_ask_target"

    from src.custom_emoji import styled_keyboard_button
    kb = ReplyKeyboardMarkup([[styled_keyboard_button("Batal", style="danger", emoji_name="back")]], resize_keyboard=True, is_persistent=True)
    set_page_reply_map(context, "premium_ask_target", {"Batal": "stars_order_cancel"})

    await notif.send_rich_message_to_chat(
        context.bot, uid,
        f'{emoji("premium_acc")} <b>Masukkan username Telegram tujuan</b> untuk Telegram Premium\n\n'
        f'<blockquote>Contoh: @Durov</blockquote>',
        premium_text("[premium_acc] Masukkan username Telegram tujuan untuk Telegram Premium (contoh: @username):"),
        reply_markup=kb,
        log_label="PremiumAskTarget",
    )


async def handle_premium_target_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if context.user_data.get("current_menu_state") != "premium_ask_target":
        return False
    uid = update.effective_user.id
    raw = (update.message.text or "").strip().lstrip("@")

    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{4,31}", raw or ""):
        await notif.send_rich_message_to_chat(
            context.bot, uid,
            f'{emoji("warning")} Format username salah. Contoh: @username',
            premium_text("[warning] Format username salah. Contoh: @username"),
            log_label="PremiumTargetInvalid",
        )
        return True

    checking = await context.bot.send_message(uid, premium_text(f"[loading] Checking username @{raw}..."), parse_mode="HTML")
    resolved = await _stars_resolve_username(raw)
    try:
        await checking.delete()
    except Exception:
        pass

    if resolved is None:
        await notif.send_rich_message_to_chat(
            context.bot, uid,
            f'{emoji("warning")} Username <b>@{raw}</b> tidak ditemukan. Coba username lain.',
            premium_text(f"[warning] Username @{raw} tidak ditemukan. Kirim ulang username yang benar."),
            log_label="PremiumTargetNotFound",
        )
        return True

    display_name, real_username, real_id, photo_bytes = resolved
    context.user_data["stars_pending"] = {"mode": "premium", "target": real_username, "display_name": display_name}
    context.user_data["current_menu_state"] = "idle"

    harga_map = await premium_topup.get_all_harga()
    kb = styled_inline_keyboard([
        [styled_button(f"3 Bulan — {format_currency(harga_map[3])}", callback_data="premium_dur_3", style="primary", emoji_name="premium_acc")],
        [styled_button(f"6 Bulan — {format_currency(harga_map[6])}", callback_data="premium_dur_6", style="primary", emoji_name="premium_acc")],
        [styled_button(f"12 Bulan — {format_currency(harga_map[12])}", callback_data="premium_dur_12", style="primary", emoji_name="premium_acc")],
        [styled_button("Batal", callback_data="stars_order_cancel", style="danger", emoji_name="back")],
    ])
    await notif.send_rich_message_to_chat(
        context.bot, uid,
        f'{emoji("premium_acc")} <b>Username ditemukan:</b> {display_name} (@{real_username})\n\n'
        f'{emoji("card")} Pilih durasi Telegram Premium:',
        premium_text(f"[premium_acc] Username ditemukan: {display_name} (@{real_username})\n\n[card] Pilih durasi Telegram Premium:"),
        reply_markup=kb,
        log_label="PremiumAskDuration",
    )
    return True


async def premium_duration_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    duration = int(q.data.split("_")[-1])
    if duration not in premium_topup.DURATIONS:
        return

    stars_data = context.user_data.get("stars_pending", {})
    if not stars_data or stars_data.get("mode") != "premium":
        await safe_answer(q, "Sesi kadaluarsa. Ulangi dari menu Topup Premium.", show_alert=True)
        return

    price = await premium_topup.get_harga(duration)
    stars_data["duration"] = duration
    stars_data["price"] = price
    context.user_data["stars_pending"] = stars_data

    user_data = get_user(uid)
    saldo = user_data[3] if user_data else 0  # FIX: belance_balance, bukan deposit_balance

    rich_html = f"""\
{emoji('card')} <b>RINGKASAN ORDER PREMIUM</b>

<table bordered striped>
<tr><th>Info</th><th>Detail</th></tr>
<tr><td>Tujuan</td><td>{stars_data.get('display_name','')} (@{stars_data.get('target','')})</td></tr>
<tr><td>Durasi</td><td>{duration} Bulan</td></tr>
<tr><td>Total</td><td><b>{format_currency(price)}</b></td></tr>
<tr><td>Saldo Kamu</td><td>{format_currency(saldo)}</td></tr>
</table>

{emoji('catatan')} Pilih metode pembayaran di bawah ini."""
    text = premium_text(f"""
[card] <b>RINGKASAN ORDER PREMIUM</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[product] <b>Tujuan:</b> {stars_data.get('display_name','')} (@{stars_data.get('target','')})
[premium_acc] <b>Durasi:</b> {duration} Bulan
[dolar] <b>Total:</b> <b>{format_currency(price)}</b>
[duitkarung] <b>Saldo Kamu:</b> {format_currency(saldo)}

[catatan] Pilih metode pembayaran di bawah ini.</blockquote>
""")
    kb = styled_inline_keyboard([
        [styled_button("Bayar Pakai Saldo", callback_data="stars_order_confirm_saldo", style="success", emoji_name="duitkarung")],
        [styled_button("💳 Bayar via QRIS", callback_data="stars_order_confirm_qris", style="primary", emoji_name="dolar")],
        [styled_button("Batal", callback_data="stars_order_cancel", style="danger", emoji_name="back")],
    ])
    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=rich_html, log_label="PremiumOrderSummary")


async def stars_order_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        await safe_answer(q)
        uid = q.from_user.id
    else:
        uid = update.effective_user.id
    context.user_data.pop("stars_pending", None)
    context.user_data["current_menu_state"] = "idle"
    if q and q.message:
        await safe_delete_message(context.bot, q.message.chat_id, q.message.message_id)
    await send_root_menu_new(context, uid)


async def stars_order_confirm_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bayar pakai saldo (belance_balance) — potong saldo dulu, lalu proses kirim Stars."""
    q = update.callback_query
    uid = q.from_user.id
    stars_data = context.user_data.get("stars_pending", {})
    if not stars_data or not stars_data.get("qty"):
        await safe_answer(q, "Sesi kadaluarsa. Ulangi dari menu Topup Stars.", show_alert=True)
        return

    price = stars_data.get("price", 0)
    user_data = get_user(uid)
    # FIX: Cek belance_balance (user_data[3]) bukan deposit_balance (user_data[2]) —
    # deposit_balance itu total histori deposit, bukan saldo yang bisa dipakai belanja.
    saldo = user_data[3] if user_data else 0
    if not user_data or saldo < price:
        await safe_answer(q, f"❌ Saldo tidak cukup! Saldo: {format_currency(saldo)}, dibutuhkan: {format_currency(price)}", show_alert=True)
        return

    await safe_answer(q, "⏳ Memotong saldo & memproses Stars...")
    update_balance(uid, belance_delta=-price)
    try:
        await q.message.delete()
    except Exception:
        pass

    context.user_data.pop("stars_pending", None)
    context.user_data["current_menu_state"] = "idle"
    await _process_stars_delivery(context, uid, stars_data, paid_via="saldo")


async def stars_order_confirm_qris(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dispatcher: pilih flow QRIS otomatis (Pakasir) atau manual (approve owner) sesuai mode payment aktif di owner panel."""
    if get_payment_method() == "manual":
        await stars_order_confirm_manual(update, context)
    else:
        await _stars_order_confirm_otomatis(update, context)


async def _stars_order_confirm_otomatis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buat QRIS otomatis via Pakasir dan kirim foto QR ke user (mode Pakasir aktif)."""
    q = update.callback_query
    await safe_answer(q, "⏳ Membuat QRIS...")
    uid = q.from_user.id
    stars_data = context.user_data.get("stars_pending", {})
    mode = stars_data.get("mode", "single")
    # Validitas sesi beda per mode: mode Stars biasa pakai 'qty', mode Premium
    # pakai 'duration' (qty memang gak pernah di-set buat order Premium, jadi
    # SEBELUM INI kalau langsung ngecek qty, order Premium selalu dianggap
    # sesi kadaluarsa dan QRIS gak pernah ke-generate).
    session_ok = bool(stars_data) and (
        stars_data.get("qty") if mode != "premium" else stars_data.get("duration")
    )
    if not session_ok:
        label = "Topup Premium" if mode == "premium" else "Topup Stars"
        await safe_answer(q, f"Sesi kadaluarsa. Ulangi dari menu {label}.", show_alert=True); return

    price  = stars_data.get("price", 0)
    qty    = stars_data.get("qty")
    target = stars_data.get("target", "")

    qris_data = await create_qris(price)
    if not qris_data:
        await safe_answer(q, "❌ Gagal membuat QRIS. Coba lagi.", show_alert=True); return

    order_id = qris_data["id"]
    qr_path  = qris_data.get("qr_path", "")
    stars_data["order_id"]    = order_id
    stars_data["qris_amount"] = price
    stars_data["qr_path"]     = qr_path
    context.user_data["stars_pending"] = stars_data

    expires_at = int(time.time()) + 300
    try:
        add_pending_payment(uid, order_id, price, qr_path, 0, expires_at)
    except Exception as e:
        print(f"[Stars QRIS add_pending_payment] {e}")

    if mode == "premium":
        duration = stars_data.get("duration", "")
        text = premium_text(f"""
[duitkarung] <b>PEMBAYARAN QRIS — TOPUP PREMIUM</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] <b>ID Order:</b> <code>{order_id}</code>
[premium_acc] <b>Durasi:</b> {duration} Bulan
[product] <b>Tujuan:</b> @{target}
[dolar] <b>Total:</b> <code>{format_currency(price)}</code>

[spikerbiru] Scan QR di atas dan tekan <b>Cek Pembayaran</b> setelah transfer.
Bot akan memproses Telegram Premium otomatis setelah pembayaran terkonfirmasi.</blockquote>
""")
    else:
        text = premium_text(f"""
[duitkarung] <b>PEMBAYARAN QRIS — TOPUP STARS</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] <b>ID Order:</b> <code>{order_id}</code>
[stars_ico] <b>Jumlah:</b> {qty} Stars
[product] <b>Tujuan:</b> @{target}
[dolar] <b>Total:</b> <code>{format_currency(price)}</code>

[spikerbiru] Scan QR di atas dan tekan <b>Cek Pembayaran</b> setelah transfer.
Bot akan memproses pengiriman Stars otomatis setelah pembayaran terkonfirmasi.</blockquote>
""")
    kb = styled_inline_keyboard([
        [styled_button("✅ Cek Pembayaran", callback_data=f"stars_cek_{order_id}", style="success", emoji_name="verified")],
        [styled_button("Batalkan",          callback_data="stars_order_cancel",       style="danger",  emoji_name="back")]
    ])

    try:
        if qr_path and os.path.exists(qr_path):
            with open(qr_path, "rb") as f:
                photo_bytes = f.read()
            await safe_send_photo(context, uid, photo=photo_bytes, caption=text, reply_markup=kb)
            try:
                await q.message.delete()
            except Exception:
                pass
            return
    except Exception as e:
        print(f"[Stars QRIS Otomatis] Gagal kirim foto: {e}")

    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=text, log_label="AutoRich")


async def handle_stars_cek_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cek status bayar QRIS otomatis (Pakasir) → jika lunas, proses kirim Stars via Fragment."""
    q = update.callback_query
    uid = q.from_user.id

    stars_data = context.user_data.get("stars_pending", {})
    order_id   = q.data.replace("stars_cek_", "")
    amount     = stars_data.get("qris_amount", stars_data.get("price", 0))

    is_paid = await check_payment_status(order_id, amount)
    if not is_paid:
        await safe_answer(q, "❌ Pembayaran belum diterima. Tunggu lalu cek lagi.", show_alert=True)
        return

    # ── ATOMIC GUARD: cegah Stars dikirim 2x jika user spam klik ───────────
    try:
        import sqlite3 as _sq3
        _ac = _sq3.connect(DB_PATH)
        _ac.execute(
            "UPDATE pending_payments SET status='paid' WHERE (id=? OR order_id=?) AND status='pending'",
            (order_id, order_id)
        )
        _changed = _ac.execute("SELECT changes()").fetchone()[0]
        _ac.commit()
        _ac.close()
    except Exception as _ae:
        print(f"[Error Atomic Guard Stars]: {_ae}")
        _changed = 0

    if not _changed:
        await safe_answer(q, "⚠️ Pembayaran ini sudah diproses sebelumnya.", show_alert=True)
        return
    # ─────────────────────────────────────────────────────────────────────

    await safe_answer(q, "⏳ Pembayaran diterima, mengirim Stars...")

    qr_path = stars_data.get("qr_path", "")
    try:
        await q.message.delete()
    except Exception:
        pass
    if qr_path and os.path.exists(qr_path):
        try:
            os.remove(qr_path)
        except Exception:
            pass

    context.user_data.pop("stars_pending", None)
    context.user_data["current_menu_state"] = "idle"
    await _process_stars_delivery(context, uid, stars_data, paid_via="qris")


async def stars_order_confirm_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mode manual: tampilkan QRIS/rekening owner, minta user upload bukti TF."""
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    stars_data = context.user_data.get("stars_pending", {})
    mode = stars_data.get("mode", "single")
    session_ok = bool(stars_data) and (
        stars_data.get("qty") if mode != "premium" else stars_data.get("duration")
    )
    if not session_ok:
        label = "Topup Premium" if mode == "premium" else "Topup Stars"
        await safe_answer(q, f"Sesi kadaluarsa. Ulangi dari menu {label}.", show_alert=True)
        return

    price = stars_data.get("price", 0)
    context.user_data["current_menu_state"] = "stars_wait_bukti"

    pay_lines = []
    for label, key, _ in PAYMENT_METHODS_LIST:
        if key == "qris":
            continue
        info = get_payment_info(key)
        if info:
            pay_lines.append(f"[panahijo] <b>{label}:</b> <code>{info}</code>")
    rekening_text = "\n".join(pay_lines) if pay_lines else ""
    qris_file_id = get_payment_info("qris")

    if mode == "premium":
        item_line = f"[premium_acc] <b>Durasi:</b> {stars_data.get('duration','')} Bulan"
        judul = "PEMBAYARAN MANUAL — TOPUP PREMIUM"
    else:
        item_line = f"[stars_ico] <b>Jumlah:</b> {stars_data.get('qty')} Stars"
        judul = "PEMBAYARAN MANUAL — TOPUP STARS"

    text = premium_text(f"""
[duitkarung] <b>{judul}</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>{item_line}
[product] <b>Tujuan:</b> @{stars_data.get('target','')}
[dolar] <b>Total Transfer:</b> <code>{format_currency(price)}</code>

{"" if not rekening_text else rekening_text + chr(10)}{"[card] Scan QRIS di atas atau transfer ke rekening di atas." if qris_file_id and rekening_text else ("[card] Scan QRIS di atas untuk transfer." if qris_file_id else ("[warning] Info rekening belum diset oleh owner." if not rekening_text else ""))}

[warning] Transfer TEPAT <code>{format_currency(price)}</code> sesuai nominal.
[catatan] Setelah transfer, kirim <b>foto/screenshot bukti transfer</b> ke bot ini.
[shield] {"Telegram Premium" if mode == "premium" else "Stars"} akan diproses & dikirim setelah Owner menyetujui.</blockquote>
""")
    kb = styled_inline_keyboard([[styled_button("Batal", callback_data="stars_order_cancel", style="danger", emoji_name="back")]])

    if qris_file_id:
        try:
            try:
                await q.message.delete()
            except Exception:
                pass
            await context.bot.send_photo(chat_id=uid, photo=qris_file_id, caption=text, parse_mode="HTML", reply_markup=kb)
            return
        except Exception as e:
            print(f"[Stars QRIS Manual foto] {e}")

    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=text, log_label="AutoRich")


async def handle_stars_bukti_tf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Tangkap foto bukti TF Stars (mode manual) → kirim ke owner untuk approve."""
    if context.user_data.get("current_menu_state") != "stars_wait_bukti":
        return False
    if not update.message or not update.message.photo:
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            f'{emoji("warning")} Kirim <b>foto/screenshot</b> bukti transfer ya, bukan teks.',
            premium_text("[warning] Harap kirim foto/screenshot bukti transfer, bukan pesan teks."),
            log_label="BuktiTFStarsWrongType",
        )
        return True

    uid = update.effective_user.id
    uname = update.effective_user.username or str(uid)
    stars_data = context.user_data.get("stars_pending", {})
    if not stars_data or not stars_data.get("qty"):
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            f'{emoji("warning")} Sesi topup Stars kadaluarsa. Ulangi dari menu Topup Stars.',
            premium_text("[warning] Sesi topup Stars kadaluarsa. Ulangi dari menu Topup Stars."),
            log_label="StarsSessionExpired",
        )
        context.user_data["current_menu_state"] = "idle"
        return True

    stars_manual_pending[uid] = stars_data
    context.user_data["current_menu_state"] = "idle"

    qty = stars_data.get("qty")
    target = stars_data.get("target", "")
    price = stars_data.get("price", 0)

    try:
        target_owner = OWNER_ID[0] if isinstance(OWNER_ID, list) else OWNER_ID
        kb = styled_inline_keyboard([[
            styled_button("✅ Approve", callback_data=f"stars_approve_manual_{uid}", style="success", emoji_name="verified"),
            styled_button("❌ Tolak", callback_data=f"stars_tolak_manual_{uid}", style="danger", emoji_name="batal"),
        ]])
        caption = premium_text(f"""[duitkarung] <b>REQUEST TOPUP STARS MANUAL</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[card] <b>User:</b> @{uname} (<code>{uid}</code>)
[stars_ico] <b>Jumlah:</b> {qty} Stars
[product] <b>Tujuan:</b> @{target}
[dolar] <b>Nominal:</b> <b>{format_currency(price)}</b></blockquote>""")
        await send_photo_to_owner(context, target_owner, update.message.photo[-1].file_id, caption, kb)
    except Exception as e:
        print(f"[Stars Bukti TF Owner] {e}")

    await notif.send_rich_message_to_chat(
        context.bot, update.effective_chat.id,
        f'{emoji("done")} <b>BUKTI TRANSFER TERKIRIM</b>\n\nBukti transfer topup Stars kamu sudah dikirim ke Owner. Stars akan dikirim ke @{target} setelah disetujui.',
        premium_text(f"[done] Bukti transfer topup Stars kamu sudah dikirim ke Owner.\n[waktu] Stars akan dikirim ke @{target} setelah disetujui."),
        reply_markup=create_reply_keyboard(uid),
        log_label="BuktiTerkirimStars",
    )
    return True


async def stars_approve_manual_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q, "⏳ Memproses Stars...")

    target_uid = int(q.data.split("_")[-1])
    stars_data = stars_manual_pending.pop(target_uid, None)
    if not stars_data:
        await safe_answer(q, "Sesi ini sudah diproses/kadaluarsa.", show_alert=True)
        return

    try:
        await q.message.edit_caption(
            caption=premium_text(f"[done] <b>TOPUP STARS MANUAL DISETUJUI</b>\n<blockquote>User: <code>{target_uid}</code>\n[waktu] Sedang mengirim Stars...</blockquote>"),
            parse_mode="HTML",
        )
    except Exception:
        pass

    await _process_stars_delivery(context, target_uid, stars_data, paid_via="manual")


async def stars_tolak_manual_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q)

    target_uid = int(q.data.split("_")[-1])
    stars_data = stars_manual_pending.pop(target_uid, None)
    qty = stars_data.get("qty", "-") if stars_data else "-"
    price = stars_data.get("price", 0) if stars_data else 0

    try:
        await notif.send_rich_message_to_chat(
            context.bot, target_uid,
            f'{emoji("warning")} <b>TOPUP STARS DITOLAK</b>\n\nJumlah: {qty} Stars — {format_currency(price)}\nBukti transfer tidak valid atau tidak sesuai. Hubungi CS jika ada pertanyaan.',
            premium_text(f"[warning] Topup Stars ({qty} Stars, {format_currency(price)}) ditolak.\n[catatan] Bukti transfer tidak valid atau tidak sesuai. Hubungi CS jika ada pertanyaan."),
            log_label="StarsManualDitolak",
        )
    except Exception as e:
        print(f"[Notif Tolak Stars Manual] {e}")

    try:
        await q.message.edit_caption(
            caption=premium_text(f"[batal] <b>TOPUP STARS MANUAL DITOLAK</b>\n<blockquote>User: <code>{target_uid}</code></blockquote>"),
            parse_mode="HTML",
        )
    except Exception:
        pass


async def _process_stars_delivery(context: ContextTypes.DEFAULT_TYPE, uid: int, stars_data: dict, paid_via: str = "saldo"):
    """Fungsi bersama: catat order ke DB, kirim Stars via Fragment, notif, refund kalau gagal (khusus saldo)."""
    mode = stars_data.get("mode", "single")
    if mode == "bulk":
        await _process_bulk_stars_delivery(context, uid, stars_data, paid_via=paid_via)
        return
    if mode == "premium":
        await _process_premium_delivery(context, uid, stars_data, paid_via=paid_via)
        return

    qty = stars_data.get("qty")
    target = stars_data.get("target", "")
    price = stars_data.get("price", 0)

    user = None
    try:
        user = await context.bot.get_chat(uid)
    except Exception:
        pass
    username = (user.username if user else "") or ""

    order_id, order_code = stars_topup.create_order(uid, username, target, qty, price, paid_via)

    loading = await context.bot.send_message(
        uid, premium_text(f"[loading] Order <b>{order_code}</b> sedang diproses, mohon tunggu..."), parse_mode="HTML",
    )

    try:
        result_msg = await stars_topup.deliver_stars(target, int(qty))
        stars_topup.update_status(order_id, "sukses", result_msg)

        try:
            await loading.delete()
        except Exception:
            pass

        user_data = get_user(uid)
        saldo_sisa = user_data[3] if user_data else None  # FIX: belance_balance, bukan deposit_balance

        # ── Kirim FOTO kartu transaksi dulu (terpisah dari text di bawah) ──
        try:
            from src import card_gen
            card_path = card_gen.generate_transaction_card(
                item_label=f"{qty} Telegram Stars",
                item_sub=f"Terkirim ke @{target}",
                rows=[
                    ("Order ID", order_code),
                    ("Total Bayar", format_currency(price)),
                    ("Metode", paid_via.upper()),
                ],
                icon="star",
            )
            with open(card_path, "rb") as _cf:
                await context.bot.send_photo(chat_id=uid, photo=_cf.read())
            try:
                os.remove(card_path)
            except Exception:
                pass
        except Exception as _ce:
            print(f"[StarsCardGen] {_ce}")
        # ─────────────────────────────────────────────────────────────────

        await notif.send_rich_message_to_chat(
            context.bot, uid,
            f'{emoji("done")} <b>TOPUP STARS BERHASIL</b>\n\n'
            f'<table bordered striped><tr><th>Info</th><th>Detail</th></tr>'
            f'<tr><td>Order</td><td><code>{order_code}</code></td></tr>'
            f'<tr><td>Jumlah</td><td>{qty} Stars</td></tr>'
            f'<tr><td>Tujuan</td><td>@{target}</td></tr>'
            f'<tr><td>Total</td><td>{format_currency(price)}</td></tr></table>\n\n'
            f'{emoji("verified")} {result_msg}\n\nTerima kasih! 🙏',
            premium_text(f"[done] <b>TOPUP STARS BERHASIL</b>\n<blockquote>[card] Order: <code>{order_code}</code>\n[stars_ico] Jumlah: {qty} Stars\n[product] Tujuan: @{target}\n[dolar] Total: {format_currency(price)}</blockquote>\n\n[verified] {result_msg}"),
            reply_markup=create_reply_keyboard(uid),
            log_label="StarsSukses",
        )

        try:
            await notif.notif_pembelian_stars_channel(
                context.bot, uid, username, target, qty, order_code, price, saldo_sisa=saldo_sisa,
            )
        except Exception as e:
            print(f"[StarsNotifChannel] {e}")

    except stars_topup.FragmentCookieExpiredError as e:
        stars_topup.update_status(order_id, "gagal", str(e))
        if paid_via == "saldo":
            update_balance(uid, deposit_delta=price)
        try:
            await loading.delete()
        except Exception:
            pass
        await notif.send_rich_message_to_chat(
            context.bot, uid,
            f'{emoji("warning")} Order <b>{order_code}</b> gagal diproses otomatis. Admin akan menghubungi kamu.'
            + (' Saldo kamu sudah dikembalikan.' if paid_via == "saldo" else ''),
            premium_text(f"[warning] Order {order_code} gagal diproses. Admin akan menghubungi kamu." + (" Saldo sudah dikembalikan." if paid_via == "saldo" else "")),
            reply_markup=create_reply_keyboard(uid),
            log_label="StarsCookieExpired",
        )
        try:
            target_owner = OWNER_ID[0] if isinstance(OWNER_ID, list) else OWNER_ID
            kb = styled_inline_keyboard([[styled_button("Coba Kirim Ulang", callback_data=f"stars_retry_{order_id}", style="primary", emoji_name="refresh")]])
            await notif.send_rich_message_to_chat(
                context.bot, target_owner,
                f'{emoji("warning")} <b>Cookie Fragment expired</b> saat proses order {order_code}.\n\nUpdate cookie dulu lewat Owner Panel &gt; Stars Topup Settings, lalu coba kirim ulang order ini.',
                premium_text(f"[warning] Cookie Fragment expired saat proses order {order_code}. Update cookie lewat Owner Panel > Stars Topup Settings."),
                reply_markup=kb,
                log_label="StarsCookieExpiredOwner",
            )
        except Exception as e2:
            print(f"[StarsCookieExpiredOwnerNotif] {e2}")

    except stars_topup.FragmentDeliveryError as e:
        stars_topup.update_status(order_id, "gagal", str(e))
        if paid_via == "saldo":
            update_balance(uid, deposit_delta=price)
        try:
            await loading.delete()
        except Exception:
            pass
        await notif.send_rich_message_to_chat(
            context.bot, uid,
            f'{emoji("warning")} Order <b>{order_code}</b> gagal diproses: <code>{html.escape(str(e))}</code>.'
            + (' Saldo kamu sudah dikembalikan.' if paid_via == "saldo" else ' Admin akan menghubungi kamu.'),
            premium_text(f"[warning] Order {order_code} gagal diproses: {html.escape(str(e))}." + (" Saldo sudah dikembalikan." if paid_via == "saldo" else " Admin akan menghubungi kamu.")),
            reply_markup=create_reply_keyboard(uid),
            log_label="StarsGagal",
        )


async def _process_bulk_stars_delivery(context: ContextTypes.DEFAULT_TYPE, uid: int, stars_data: dict, paid_via: str = "saldo"):
    """Proses order Stars BULK: kirim ke tiap username satu-satu (qty BOLEH beda
    tiap username), refund proporsional (saldo) untuk target yang gagal, dihitung
    dari qty username itu sendiri -- bukan lagi dibagi rata."""
    targets_qty = stars_data.get("targets_qty") or [(u, stars_data.get("qty", 0)) for u in stars_data.get("targets", [])]
    total_qty = sum(q for _, q in targets_qty) or stars_data.get("qty", 0)
    price = stars_data.get("price", 0)
    n_targets = len(targets_qty)

    user = None
    try:
        user = await context.bot.get_chat(uid)
    except Exception:
        pass
    username = (user.username if user else "") or ""

    order_id, order_code = stars_topup.create_bulk_order(uid, username, targets_qty, price, paid_via)

    loading = await context.bot.send_message(
        uid, premium_text(f"[loading] Order Bulk <b>{order_code}</b> ({n_targets} username) sedang diproses, mohon tunggu..."), parse_mode="HTML",
    )

    try:
        results = await stars_topup.deliver_stars_bulk(targets_qty)
    except stars_topup.FragmentCookieExpiredError as e:
        stars_topup.update_bulk_status(order_id, "gagal", str(e))
        if paid_via == "saldo":
            update_balance(uid, deposit_delta=price)
        try:
            await loading.delete()
        except Exception:
            pass
        await notif.send_rich_message_to_chat(
            context.bot, uid,
            f'{emoji("warning")} Order Bulk <b>{order_code}</b> gagal diproses (cookie Fragment expired). Admin akan menghubungi kamu.'
            + (' Saldo kamu sudah dikembalikan.' if paid_via == "saldo" else ''),
            premium_text(f"[warning] Order Bulk {order_code} gagal (cookie expired)." + (" Saldo sudah dikembalikan." if paid_via == "saldo" else "")),
            reply_markup=create_reply_keyboard(uid),
            log_label="BulkStarsCookieExpired",
        )
        return

    success = results["success"]   # list of (username, qty, msg)
    failed = results["failed"]     # list of (username, qty, err)
    n_ok, n_fail = len(success), len(failed)

    # Refund proporsional PER USERNAME yang gagal, sesuai qty aslinya masing-masing
    # (bukan dibagi rata lagi, karena tiap username sekarang bisa beda qty).
    if paid_via == "saldo" and n_fail > 0 and total_qty:
        harga_per_star = price / total_qty
        refund = round(sum(q for _, q, _ in failed) * harga_per_star)
        if refund > 0:
            update_balance(uid, deposit_delta=refund)

    status = "sukses" if n_fail == 0 else ("sebagian_gagal" if n_ok > 0 else "gagal")
    result_summary = f"{n_ok} sukses, {n_fail} gagal dari {n_targets} username"
    stars_topup.update_bulk_status(order_id, status, result_summary)

    try:
        await loading.delete()
    except Exception:
        pass

    ok_lines = "\n".join(f"✅ @{u} — {q} Stars" for u, q, _ in success) or "-"
    fail_lines = "\n".join(f"❌ @{u} — {q} Stars — {html.escape(err)}" for u, q, err in failed) or "-"

    await notif.send_rich_message_to_chat(
        context.bot, uid,
        f'{emoji("done")} <b>ORDER STARS BULK SELESAI</b>\n\n'
        f'<table bordered striped><tr><th>Info</th><th>Detail</th></tr>'
        f'<tr><td>Order</td><td><code>{order_code}</code></td></tr>'
        f'<tr><td>Berhasil</td><td>{n_ok}/{n_targets}</td></tr>'
        f'<tr><td>Total Bayar</td><td>{format_currency(price)}</td></tr></table>\n\n'
        f'<b>Berhasil:</b>\n<blockquote>{ok_lines}</blockquote>'
        + (f'\n<b>Gagal (saldo dikembalikan proporsional):</b>\n<blockquote>{fail_lines}</blockquote>' if failed else ''),
        premium_text(f"[done] Order Bulk {order_code} selesai. {result_summary}."),
        reply_markup=create_reply_keyboard(uid),
        log_label="BulkStarsSelesai",
    )

    if n_ok > 0:
        try:
            _u = get_user(uid)
            _sisa = _u[3] if _u and len(_u) > 3 else None
            await notif.notif_pembelian_bulkstars_channel(
                context.bot, uid, username, order_code, n_ok, n_targets, total_qty, price, saldo_sisa=_sisa,
            )
        except Exception as e:
            print(f"[BulkStarsNotifChannel] {e}")


async def _process_premium_delivery(context: ContextTypes.DEFAULT_TYPE, uid: int, stars_data: dict, paid_via: str = "saldo"):
    """Fungsi bersama buat order Telegram Premium — mirip _process_stars_delivery
    tapi manggil premium_topup.deliver_premium() & tabel premium_orders."""
    target = stars_data.get("target", "")
    duration = stars_data.get("duration")
    price = stars_data.get("price", 0)

    user = None
    try:
        user = await context.bot.get_chat(uid)
    except Exception:
        pass
    username = (user.username if user else "") or ""

    order_id, order_code = premium_topup.create_order(uid, username, target, duration, price, paid_via)

    loading = await context.bot.send_message(
        uid, premium_text(f"[loading] Order <b>{order_code}</b> sedang diproses, mohon tunggu..."), parse_mode="HTML",
    )

    try:
        result_msg = await premium_topup.deliver_premium(target, int(duration))
        premium_topup.update_status(order_id, "sukses", result_msg)

        try:
            await loading.delete()
        except Exception:
            pass

        try:
            from src import card_gen
            card_path = card_gen.generate_transaction_card(
                item_label=f"Telegram Premium {duration} Bulan",
                item_sub=f"Terkirim ke @{target}",
                rows=[
                    ("Order ID", order_code),
                    ("Total Bayar", format_currency(price)),
                    ("Metode", paid_via.upper()),
                ],
                icon="verified",
            )
            with open(card_path, "rb") as _cf:
                await context.bot.send_photo(chat_id=uid, photo=_cf.read())
            try:
                os.remove(card_path)
            except Exception:
                pass
        except Exception as _ce:
            print(f"[PremiumCardGen] {_ce}")

        await notif.send_rich_message_to_chat(
            context.bot, uid,
            f'{emoji("done")} <b>TOPUP PREMIUM BERHASIL</b>\n\n'
            f'<table bordered striped><tr><th>Info</th><th>Detail</th></tr>'
            f'<tr><td>Order</td><td><code>{order_code}</code></td></tr>'
            f'<tr><td>Durasi</td><td>{duration} Bulan</td></tr>'
            f'<tr><td>Tujuan</td><td>@{target}</td></tr>'
            f'<tr><td>Total</td><td>{format_currency(price)}</td></tr></table>\n\n'
            f'{emoji("verified")} {result_msg}\n\nTerima kasih! 🙏',
            premium_text(f"[done] TOPUP PREMIUM BERHASIL. Order: {order_code}, {duration} Bulan ke @{target}, Total: {format_currency(price)}. {result_msg}"),
            reply_markup=create_reply_keyboard(uid),
            log_label="PremiumSukses",
        )

        try:
            _u = get_user(uid)
            _sisa = _u[3] if _u and len(_u) > 3 else None
            await notif.notif_pembelian_premium_channel(
                context.bot, uid, username, target, duration, order_code, price, saldo_sisa=_sisa,
            )
        except Exception as e:
            print(f"[PremiumNotifChannel] {e}")

    except premium_topup.FragmentCookieExpiredError as e:
        premium_topup.update_status(order_id, "gagal", str(e))
        if paid_via == "saldo":
            update_balance(uid, deposit_delta=price)
        try:
            await loading.delete()
        except Exception:
            pass
        await notif.send_rich_message_to_chat(
            context.bot, uid,
            f'{emoji("warning")} Order <b>{order_code}</b> gagal diproses otomatis. Admin akan menghubungi kamu.'
            + (' Saldo kamu sudah dikembalikan.' if paid_via == "saldo" else ''),
            premium_text(f"[warning] Order {order_code} gagal diproses. Admin akan menghubungi kamu." + (" Saldo sudah dikembalikan." if paid_via == "saldo" else "")),
            reply_markup=create_reply_keyboard(uid),
            log_label="PremiumCookieExpired",
        )
    except premium_topup.FragmentDeliveryError as e:
        premium_topup.update_status(order_id, "gagal", str(e))
        if paid_via == "saldo":
            update_balance(uid, deposit_delta=price)
        try:
            await loading.delete()
        except Exception:
            pass
        await notif.send_rich_message_to_chat(
            context.bot, uid,
            f'{emoji("warning")} Order <b>{order_code}</b> gagal diproses: <code>{html.escape(str(e))}</code>.'
            + (' Saldo kamu sudah dikembalikan.' if paid_via == "saldo" else ' Admin akan menghubungi kamu.'),
            premium_text(f"[warning] Order {order_code} gagal diproses: {html.escape(str(e))}." + (" Saldo sudah dikembalikan." if paid_via == "saldo" else " Admin akan menghubungi kamu.")),
            reply_markup=create_reply_keyboard(uid),
            log_label="PremiumGagal",
        )


async def stars_retry_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner coba kirim ulang order Stars yang gagal."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    order_id = int(q.data.split("_")[-1])
    order = stars_topup.get_order(order_id)
    if not order:
        await safe_answer(q, "Order tidak ditemukan.", show_alert=True); return
    if order["status"] == "sukses":
        await safe_answer(q, "Order ini sudah sukses sebelumnya.", show_alert=True); return

    await safe_answer(q, "Mencoba kirim ulang...")
    stars_data = {"qty": order["qty"], "target": order["target_username"], "price": order["price"]}
    await notif.send_rich_message_to_chat(
        context.bot, order["user_id"],
        f'{emoji("loading")} Order {order["order_code"]} sedang diproses ulang oleh admin...',
        premium_text(f"[loading] Order {order['order_code']} sedang diproses ulang oleh admin..."),
        log_label="StarsRetryNotifBuyer",
    )
    await _process_stars_delivery(context, order["user_id"], stars_data, paid_via=order["paid_via"] or "manual")


async def stars_myorders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    orders = stars_topup.list_user_orders(uid)
    if not orders:
        await notif.send_rich_message_to_chat(
            context.bot, uid,
            f'{emoji("card")} Kamu belum punya order Stars.',
            premium_text("[card] Kamu belum punya order Stars."),
            log_label="StarsNoOrders",
        )
        return
    rows_html = "".join(
        f"<tr><td>{o['order_code']}</td><td>{o['qty']} Stars — {format_currency(o['price'])} — {o['status']}</td></tr>"
        for o in orders
    )
    rich_html = f"""\
{emoji('card')} <b>ORDER STARS TERAKHIR KAMU</b>

<table bordered striped><tr><th>Order</th><th>Detail</th></tr>{rows_html}</table>"""
    lines = "\n".join(f"[panahijo] {o['order_code']} — {o['qty']} Stars — {format_currency(o['price'])} — {o['status']}" for o in orders)
    fallback = premium_text(f"[card] <b>ORDER STARS TERAKHIR KAMU</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>{lines}</blockquote>")
    await notif.send_rich_message_to_chat(context.bot, uid, rich_html, fallback, log_label="StarsMyOrders")


# ---------- OWNER: STARS TOPUP SETTINGS ----------

def _stars_owner_menu_keyboard():
    mode = stars_topup.get_pricing_mode()
    toggle_label = "Mode Harga: Auto (ikut TON) ✅" if mode == "auto" else "Mode Harga: Manual ✅"
    return styled_inline_keyboard([
        [styled_button("Update Cookies", callback_data="stars_owner_set_cookies", style="success", emoji_name="card")],
        [styled_button("Update Wallet Seed", callback_data="stars_owner_set_seed", style="danger", emoji_name="ton_coin")],
        [styled_button("Set Harga per Star (manual)", callback_data="stars_owner_set_harga", style="success", emoji_name="dolar")],
        [styled_button(toggle_label, callback_data="stars_owner_toggle_pricing", style="primary", emoji_name="lightning")],
        [
            styled_button("Set Fee Flat (Rp/50 Stars)", callback_data="stars_owner_set_fee_flat", style="success", emoji_name="dolar"),
            styled_button("Kalibrasi Rasio TON/Star", callback_data="stars_owner_set_ratio", style="success", emoji_name="ton_coin"),
        ],
        [styled_button("Set Margin Auto (%) - mode lama", callback_data="stars_owner_set_margin", style="primary", emoji_name="grafik")],
        [styled_button("Cek Harga TON Sekarang", callback_data="stars_owner_check_ton", style="primary", emoji_name="grafik")],
        [
            styled_button("Order Pending", callback_data="stars_owner_pending", style="primary", emoji_name="grafik"),
            styled_button("Status Fragment/TON", callback_data="stars_owner_status", style="primary", emoji_name="shield"),
        ],
        [styled_button("⚙️ Topup TON Settings", callback_data="ton_owner_status", style="success", emoji_name="ton_coin")],
        [styled_button("⚙️ Premium Topup Settings", callback_data="premium_owner_menu", style="success", emoji_name="premium_acc")],
        [styled_button("Kembali ke Owner Menu", callback_data="menu_owner", style="danger", emoji_name="back")],
    ])


async def stars_owner_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q)
    text = premium_text("""
[stars_ico] <b>STARS TOPUP SETTINGS</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[shield] Kredensial Fragment/TON disimpan aman di database, bukan di file kode.
[catatan] Pilih menu di bawah untuk mengatur.</blockquote>
""")
    rich_html = f"""\
{emoji('stars_ico')} <b>STARS TOPUP SETTINGS</b>

{emoji('shield')} Kredensial Fragment/TON disimpan aman di database, bukan di file kode.
{emoji('catatan')} Pilih menu di bawah untuk mengatur."""
    await fast_edit(q, text, reply_markup=_stars_owner_menu_keyboard(), parse_mode="HTML", rich_html=rich_html, log_label="AutoRich")


async def stars_owner_ask_text(update: Update, context: ContextTypes.DEFAULT_TYPE, state_key: str, prompt_html: str):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q)
    context.user_data["current_menu_state"] = state_key
    kb = styled_inline_keyboard([[styled_button("Batal", callback_data="stars_owner_menu", style="danger", emoji_name="back")]])
    await fast_edit(q, premium_text(prompt_html), reply_markup=kb, parse_mode="HTML", rich_html=premium_text(prompt_html), log_label="AutoRich")


async def handle_stars_owner_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    state = context.user_data.get("current_menu_state", "")
    if not state.startswith("stars_owner_wait_"):
        return False
    uid = update.effective_user.id
    if not is_owner(uid):
        return False

    text = (update.message.text or "").strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    if state == "stars_owner_wait_cookies":
        # Terima semua kredensial sekaligus dalam 1 pesan: 4 cookie + hash + api key.
        # Alias biar toleran typo umum ("fragment_has" -> fragment_hash).
        _key_alias = {
            "stel_ssid": "stel_ssid",
            "stel_dt": "stel_dt",
            "stel_token": "stel_token",
            "stel_ton_token": "stel_ton_token",
            "fragment_hash": "fragment_hash",
            "fragment_has": "fragment_hash",
            "api_key": "ton_api_key",
            "ton_api_key": "ton_api_key",
        }
        _required = ("stel_ssid", "stel_dt", "stel_token", "stel_ton_token", "fragment_hash", "ton_api_key")
        parsed = {}
        for line in text.splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key = key.strip().lower()
            value = value.strip()
            setting_key = _key_alias.get(key)
            if setting_key and value:
                parsed[setting_key] = value
        missing = [k for k in _required if k not in parsed]
        if missing:
            await context.bot.send_message(uid, premium_text(f"[warning] Format gak lengkap, yang kurang: {', '.join(missing)}. Coba lagi dari menu Stars Topup Settings."), parse_mode="HTML", reply_markup=_stars_owner_menu_keyboard())
            context.user_data["current_menu_state"] = "idle"
            return True
        for k, v in parsed.items():
            stars_topup.set_setting(k, v)
        msg = "[done] Semua kredensial (cookies, hash, api key) berhasil diupdate & tersimpan di database."

    elif state == "stars_owner_wait_seed":
        stars_topup.set_setting("ton_wallet_seed", text)
        msg = "[done] TON Wallet Seed berhasil diupdate & tersimpan di database."

    elif state == "stars_owner_wait_harga":
        raw = re.sub(r"\D", "", text)
        if not raw or int(raw) <= 0:
            await context.bot.send_message(uid, premium_text("[warning] Masukkan angka yang valid, contoh: 170"), parse_mode="HTML", reply_markup=_stars_owner_menu_keyboard())
            context.user_data["current_menu_state"] = "idle"
            return True
        stars_topup.set_harga_per_star(int(raw))
        msg = f"[done] Harga per Star berhasil diupdate jadi {format_currency(int(raw))}."

    elif state == "stars_owner_wait_margin":
        try:
            margin = float(text.replace(",", ".").replace("%", "").strip())
            assert margin >= 0
        except (ValueError, AssertionError):
            await context.bot.send_message(uid, premium_text("[warning] Masukkan angka yang valid, contoh: 20"), parse_mode="HTML", reply_markup=_stars_owner_menu_keyboard())
            context.user_data["current_menu_state"] = "idle"
            return True
        stars_topup.set_margin_persen(margin)
        msg = f"[done] Margin auto berhasil diupdate jadi {margin:g}%."

    elif state == "stars_owner_wait_fee_flat":
        try:
            fee_flat = float(text.replace(",", ".").replace("Rp", "").replace("rp", "").strip())
            assert fee_flat >= 0
        except (ValueError, AssertionError):
            await context.bot.send_message(uid, premium_text("[warning] Masukkan angka yang valid, contoh: 1000"), parse_mode="HTML", reply_markup=_stars_owner_menu_keyboard())
            context.user_data["current_menu_state"] = "idle"
            return True
        stars_topup.set_fee_flat_per50_idr(fee_flat)
        msg = (
            f"[done] Fee flat berhasil diupdate jadi Rp{fee_flat:,.0f} per 50 Stars."
            + (" Untung sekarang TETAP segitu per 50 Stars, gak ngikutin naik-turun rate." if fee_flat > 0
               else " Fee flat di-set 0, jadi sekarang balik pakai mode margin persen (lama).")
        )

    elif state == "stars_owner_wait_ratio":
        try:
            ratio = float(text.replace(",", ".").strip())
            assert ratio > 0
        except (ValueError, AssertionError):
            await context.bot.send_message(uid, premium_text("[warning] Masukkan angka yang valid, contoh: 3.6"), parse_mode="HTML", reply_markup=_stars_owner_menu_keyboard())
            context.user_data["current_menu_state"] = "idle"
            return True
        stars_topup.set_ton_per_1000star(ratio)
        msg = f"[done] Rasio kalibrasi diupdate: {ratio:g} TON / 1000 Star."

    else:
        return False

    context.user_data["current_menu_state"] = "idle"
    await context.bot.send_message(uid, premium_text(msg), parse_mode="HTML", reply_markup=_stars_owner_menu_keyboard())
    return True


async def stars_owner_toggle_pricing_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    current = stars_topup.get_pricing_mode()
    new_mode = "manual" if current == "auto" else "auto"
    stars_topup.set_pricing_mode(new_mode)
    label = "🔄 Auto (ikut harga TON real-time)" if new_mode == "auto" else "✍️ Manual"
    await safe_answer(q, f"Mode harga diubah ke: {label}", show_alert=True)

    if new_mode == "auto":
        try:
            info = await stars_topup.compute_auto_harga_per_star()
            if info.get("margin_persen") is None:
                untung_line = f"[catatan] Rasio: {info['ton_per_star']*1000:g} TON/1000 Star, Fee: Rp{info['fee_flat_per50_idr']:,.0f}/50 Stars (flat)"
            else:
                untung_line = f"[catatan] Rasio: {info['ton_per_star']*1000:g} TON/1000 Star, Margin: {info['margin_persen']:g}%"
            await context.bot.send_message(
                uid,
                premium_text(
                    "[done] Mode harga sekarang <b>Auto</b>.\n"
                    f"[dolar] Estimasi harga per Star saat ini: <code>{format_currency(info['harga'])}</code>\n"
                    f"[grafik] Harga TON sekarang: <code>{format_currency(int(info['ton_price_idr']))}</code>\n"
                    f"{untung_line}\n\n"
                    "Kalau rasio/fee belum sesuai, atur lewat menu Kalibrasi Rasio TON/Star & Set Fee Flat."
                ),
                parse_mode="HTML",
                reply_markup=_stars_owner_menu_keyboard(),
            )
        except Exception as e:
            await context.bot.send_message(
                uid,
                premium_text(f"[warning] Mode Auto aktif, tapi gagal ambil harga TON saat ini: {e}\nBot akan fallback ke harga manual sampai API bisa diakses lagi."),
                parse_mode="HTML",
                reply_markup=_stars_owner_menu_keyboard(),
            )
    else:
        await context.bot.send_message(uid, premium_text("[done] Mode harga sekarang <b>Manual</b>, pakai Harga per Star yang diset owner."), parse_mode="HTML", reply_markup=_stars_owner_menu_keyboard())


async def stars_owner_check_ton_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q, "Mengambil harga TON real-time...")
    try:
        info = await stars_topup.compute_auto_harga_per_star()
        if info.get("margin_persen") is None:
            untung_line = f"[catatan] Rasio kalibrasi: {info['ton_per_star']*1000:g} TON/1000 Star | Fee: Rp{info['fee_flat_per50_idr']:,.0f}/50 Stars (flat)"
        else:
            untung_line = f"[catatan] Rasio kalibrasi: {info['ton_per_star']*1000:g} TON/1000 Star | Margin: {info['margin_persen']:g}%"
        msg = (
            "[grafik] <b>HARGA TON REAL-TIME</b>\n\n"
            f"[dolar] Harga TON sekarang: <code>{format_currency(int(info['ton_price_idr']))}</code>\n"
            f"[stars_ico] Estimasi harga jual per Star (kalau mode Auto): <code>{format_currency(info['harga'])}</code>\n"
            f"{untung_line}"
        )
    except Exception as e:
        msg = f"[warning] Gagal ambil harga TON: {e}"
    await context.bot.send_message(uid, premium_text(msg), parse_mode="HTML", reply_markup=_stars_owner_menu_keyboard())


async def stars_owner_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q)
    rows = stars_topup.status_rows()
    rows_html = "".join(f"<tr><td>{label}</td><td>{val}</td></tr>" for label, val in rows)
    rich_html = f"""\
{emoji('shield')} <b>STATUS KREDENSIAL FRAGMENT/TON</b>

<table bordered striped><tr><th>Item</th><th>Status</th></tr>{rows_html}</table>"""
    lines = "\n".join(f"[panahijo] {label}: {val}" for label, val in rows)
    fallback = premium_text(f"[shield] <b>STATUS KREDENSIAL FRAGMENT/TON</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>{lines}</blockquote>")
    kb = styled_inline_keyboard([[styled_button("Kembali", callback_data="stars_owner_menu", style="danger", emoji_name="back")]])
    await fast_edit(q, fallback, reply_markup=kb, parse_mode="HTML", rich_html=rich_html, log_label="AutoRich")


async def stars_owner_pending_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q)
    orders = stars_topup.list_pending_orders()
    kb = styled_inline_keyboard([[styled_button("Kembali", callback_data="stars_owner_menu", style="danger", emoji_name="back")]])
    if not orders:
        await fast_edit(q, premium_text("[card] Tidak ada order Stars yang menunggu konfirmasi."), reply_markup=kb, parse_mode="HTML", rich_html=premium_text("[card] Tidak ada order Stars yang menunggu konfirmasi."), log_label="AutoRich")
        return
    rows_html = "".join(
        f"<tr><td>{o['order_code']}</td><td>{o['qty']} Stars — @{o['username']} — {o['status']}</td></tr>" for o in orders
    )
    rich_html = f"""\
{emoji('grafik')} <b>ORDER STARS MENUNGGU KONFIRMASI</b>

<table bordered striped><tr><th>Order</th><th>Detail</th></tr>{rows_html}</table>"""
    lines = "\n".join(f"[panahijo] {o['order_code']} — {o['qty']} Stars — @{o['username']} — {o['status']}" for o in orders)
    fallback = premium_text(f"[grafik] <b>ORDER STARS MENUNGGU KONFIRMASI</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>{lines}</blockquote>")
    await fast_edit(q, fallback, reply_markup=kb, parse_mode="HTML", rich_html=rich_html, log_label="AutoRich")


# ==================== OWNER: PREMIUM TOPUP SETTINGS (harga dasar live ikut TON + FE) ====================

async def _premium_owner_menu_keyboard():
    detail = await premium_topup.get_all_detail()
    rows = []
    for d in premium_topup.DURATIONS:
        rows.append([
            styled_button(f"Set FE {d}bln: {format_currency(detail[d]['fee'])}", callback_data=f"premium_owner_set_fee_{d}", style="success", emoji_name="dolar"),
            styled_button(f"Kalibrasi TON {d}bln: {detail[d]['ton_per_duration']:g}", callback_data=f"premium_owner_set_ratio_{d}", style="success", emoji_name="ton_coin"),
        ])
    rows.append([styled_button("⚙️ Stars Topup Settings", callback_data="stars_owner_menu", style="success", emoji_name="stars_ico")])
    rows.append([styled_button("Kembali ke Owner Menu", callback_data="menu_owner", style="danger", emoji_name="back")])
    return styled_inline_keyboard(rows)


async def premium_owner_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q)

    detail = await premium_topup.get_all_detail()
    ton_price_idr = next(iter(detail.values()))["ton_price_idr"]
    rows_html = "".join(
        f"<tr><td>{d} Bulan</td><td>{v['ton_per_duration']:g} TON</td><td>{format_currency(v['dasar'])}</td><td>{format_currency(v['fee'])}</td><td><b>{format_currency(v['final'])}</b></td></tr>"
        for d, v in detail.items()
    )
    rich_html = f"""\
{emoji('premium_acc')} <b>PREMIUM TOPUP SETTINGS</b>

{emoji('catatan')} Harga Dasar = rasio modal TON per durasi (di bawah) × harga TON real-time (CoinGecko). Harga TON ikut real-time otomatis, TAPI rasio modal TON per durasi TIDAK auto-update sendiri sampai ada transaksi Premium sukses lewat bot -- kalau beda sama fragment.com/premium sekarang, cocokkan manual pakai tombol "Kalibrasi TON" di bawah. Harga TON saat ini: {format_currency(ton_price_idr)}/TON.

<table bordered striped>
<tr><th>Durasi</th><th>Rasio TON</th><th>Harga Dasar (live)</th><th>FE</th><th>Harga Jual</th></tr>
{rows_html}
</table>

{emoji('lightning')} Tekan tombol di bawah buat ubah FE, atau kalibrasi rasio TON per durasi kalau melenceng dari Fragment."""
    fallback = premium_text(
        "[premium_acc] <b>PREMIUM TOPUP SETTINGS</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"[catatan] Harga dasar = rasio TON per durasi × harga TON real-time (Rp{ton_price_idr:,.0f}/TON). Rasio TON gak auto-update sampai ada transaksi sukses -- kalibrasi manual kalau beda dari Fragment.\n<blockquote>"
        + "\n".join(f"[ton_coin] {d} Bulan: {v['ton_per_duration']:g} TON × harga TON = Dasar {format_currency(v['dasar'])} + FE {format_currency(v['fee'])} = {format_currency(v['final'])}" for d, v in detail.items())
        + "</blockquote>"
    )
    await fast_edit(q, fallback, reply_markup=await _premium_owner_menu_keyboard(), parse_mode="HTML", rich_html=rich_html, log_label="AutoRich")


async def premium_owner_ask_text(update: Update, context: ContextTypes.DEFAULT_TYPE, state_key: str, prompt_html: str):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q)
    context.user_data["current_menu_state"] = state_key
    kb = styled_inline_keyboard([[styled_button("Batal", callback_data="premium_owner_menu", style="danger", emoji_name="back")]])
    await fast_edit(q, premium_text(prompt_html), reply_markup=kb, parse_mode="HTML", rich_html=premium_text(prompt_html), log_label="AutoRich")


async def handle_premium_owner_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    state = context.user_data.get("current_menu_state", "")
    if not state.startswith("premium_owner_wait_"):
        return False
    uid = update.effective_user.id
    if not is_owner(uid):
        return False

    text = (update.message.text or "").strip()
    try:
        await update.message.delete()
    except Exception:
        pass

    m_ratio = re.fullmatch(r"premium_owner_wait_ratio_(\d+)", state)
    if m_ratio:
        duration = int(m_ratio.group(1))
        if duration not in premium_topup.DURATIONS:
            return False
        try:
            value = float(text.replace(",", "."))
            assert value > 0
        except (ValueError, AssertionError):
            await context.bot.send_message(uid, premium_text("[warning] Masukkan angka yang valid, contoh: 11.4"), parse_mode="HTML", reply_markup=await _premium_owner_menu_keyboard())
            context.user_data["current_menu_state"] = "idle"
            return True

        premium_topup.set_ton_per_duration(duration, value)
        harga_final = await premium_topup.get_harga(duration)
        msg = f"[done] Rasio TON {duration} Bulan berhasil diupdate jadi {value:g} TON. Harga jual sekarang: {format_currency(harga_final)}."

        context.user_data["current_menu_state"] = "idle"
        await context.bot.send_message(uid, premium_text(msg), parse_mode="HTML", reply_markup=await _premium_owner_menu_keyboard())
        return True

    raw = re.sub(r"\D", "", text)
    if not raw or int(raw) < 0:
        await context.bot.send_message(uid, premium_text("[warning] Masukkan angka yang valid, contoh: 200000"), parse_mode="HTML", reply_markup=await _premium_owner_menu_keyboard())
        context.user_data["current_menu_state"] = "idle"
        return True
    value = int(raw)

    m = re.fullmatch(r"premium_owner_wait_fee_(\d+)", state)
    if not m:
        return False
    duration = int(m.group(1))
    if duration not in premium_topup.DURATIONS:
        return False

    premium_topup.set_fee(duration, value)
    harga_final = await premium_topup.get_harga(duration)
    msg = f"[done] FE {duration} Bulan berhasil diupdate jadi {format_currency(value)}. Harga jual sekarang: {format_currency(harga_final)}."

    context.user_data["current_menu_state"] = "idle"
    await context.bot.send_message(uid, premium_text(msg), parse_mode="HTML", reply_markup=await _premium_owner_menu_keyboard())
    return True


# ═══════════════════════════════════════════════════════════════════════════════
#   PAGE 8 — MENU TOPUP TON (kirim TON on-chain langsung ke alamat wallet tujuan)
# ═══════════════════════════════════════════════════════════════════════════════
from src import ton_topup

ton_manual_pending = {}  # {user_id: ton_data} — antrian topup TON mode manual menunggu approve owner


async def send_page8_menu_new(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Kirim halaman Topup TON sebagai pesan baru — dipakai dari Reply Keyboard."""
    context.user_data["current_menu_state"] = "page8_ton"
    context.user_data["active_menu_page"] = 8
    context.user_data.pop("ton_pending", None)

    user_data = get_user(user_id)
    saldo = user_data[3] if user_data else 0  # FIX: belance_balance, bukan deposit_balance
    try:
        harga_info = await ton_topup.get_harga_jual_per_ton()
        harga = harga_info["harga"]
    except Exception:
        harga = 0

    text = premium_text(f"""
[ton_coin] <b>TOPUP TON</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[card] <b>Saldo Kamu :</b> <code>{format_currency(saldo)}</code>
[dolar] <b>Harga per TON :</b> <code>{format_currency(harga)}</code> (real-time)
[warning] <b>Minimal beli :</b> {ton_topup.MIN_TON_ORDER} TON

[lightning] <b>Cara Pakai:</b>
[panahijo] Tekan "Beli TON"
[panahijo] Masukkan alamat wallet TON tujuan (contoh: UQ... atau EQ..., bisa dari Tonkeeper)
[panahijo] Masukkan jumlah TON yang mau dibeli
[panahijo] Pilih metode bayar (Saldo / Transfer Manual)
[panahijo] TON terkirim otomatis ke alamat tujuan, on-chain

[shield] <b>Catatan Keamanan:</b> Bot HANYA butuh alamat wallet tujuan kamu (yang diawali UQ/EQ). Bot TIDAK PERNAH meminta frasa 24 kata / seed phrase / private key kamu. Jangan pernah kasih seed phrase ke siapapun termasuk "admin" atau "CS" manapun.</blockquote>
""")
    rich_html = f"""\
{emoji('ton_coin')} <b>TOPUP TON</b>

<table bordered striped>
<tr><th>Info</th><th>Detail</th></tr>
<tr><td>Saldo Kamu</td><td><code>{format_currency(saldo)}</code></td></tr>
<tr><td>Harga per TON</td><td><code>{format_currency(harga)}</code> (real-time)</td></tr>
<tr><td>Minimal Beli</td><td>{ton_topup.MIN_TON_ORDER} TON</td></tr>
</table>
<hr/>
<table bordered striped>
<tr><th>{emoji('lightning')} Cara Pakai</th><th>Langkah</th></tr>
<tr><td>1</td><td>Tekan "Beli TON"</td></tr>
<tr><td>2</td><td>Masukkan alamat wallet TON tujuan (UQ.../EQ..., contoh dari Tonkeeper)</td></tr>
<tr><td>3</td><td>Masukkan jumlah TON yang mau dibeli</td></tr>
<tr><td>4</td><td>Pilih metode bayar (Saldo / Transfer Manual)</td></tr>
<tr><td>5</td><td>TON terkirim otomatis ke alamat tujuan, on-chain</td></tr>
</table>

{emoji('shield')} <b>Catatan Keamanan:</b> Bot HANYA butuh alamat wallet tujuan kamu (UQ/EQ). Bot TIDAK PERNAH minta frasa 24 kata / seed phrase / private key kamu."""

    from src.custom_emoji import styled_keyboard_button
    rows = [
        [styled_keyboard_button("Beli TON", style="success", emoji_name="miniapp_ton")],
        [styled_keyboard_button("Cek Order Saya", style="primary", emoji_name="card")],
        [styled_keyboard_button(RKB_BACK_MAIN, style="danger", emoji_name="back")],
    ]
    kb = ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)
    set_page_reply_map(context, "page8_ton", {
        "Beli TON": "ton_beli_start",
        "Cek Order Saya": "ton_myorders",
    })

    await notif.send_rich_message_to_chat(
        context.bot, user_id, rich_html, text,
        reply_markup=kb,
        log_label="Page8TonMenuNew",
    )


async def ton_beli_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mulai alur beli TON — tanya alamat wallet tujuan."""
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    push_nav(context, "ton_ask_address")
    context.user_data["ton_pending"] = {}
    context.user_data["current_menu_state"] = "ton_ask_address"

    from src.custom_emoji import styled_keyboard_button
    kb = ReplyKeyboardMarkup([[styled_keyboard_button("Batal", style="danger", emoji_name="back")]], resize_keyboard=True, is_persistent=True)
    set_page_reply_map(context, "ton_ask_address", {"Batal": "ton_order_cancel"})

    await notif.send_rich_message_to_chat(
        context.bot, uid,
        f'{emoji("ton_coin")} <b>Masukkan alamat wallet TON tujuan</b>\n\n'
        f'<table bordered striped>\n'
        f'<tr><th>Contoh 1</th><th>Contoh 2</th></tr>\n'
        f'<tr><td><code>UQabc...xyz</code></td><td><code>UQshd...kdudhe</code></td></tr>\n'
        f'</table>\n'
        f'<hr/>\n'
        f'{emoji("shield")} Cukup alamat wallet-nya saja (UQ.../EQ...). JANGAN PERNAH kirim frasa 24 kata / seed phrase kamu ke bot ini atau ke siapapun.',
        premium_text('[ton_coin] Masukkan <b>alamat wallet TON tujuan</b> (contoh: UQabc...xyz):\n\n[shield] Cukup alamat wallet-nya saja. JANGAN PERNAH kirim frasa 24 kata / seed phrase kamu ke bot ini atau ke siapapun.'),
        reply_markup=kb,
        log_label="TonAskAddress",
    )


async def handle_ton_address_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if context.user_data.get("current_menu_state") != "ton_ask_address":
        return False
    uid = update.effective_user.id
    raw = (update.message.text or "").strip()

    # Deteksi kalau user malah kirim seed phrase (12/24 kata) — tolak keras & edukasi,
    # JANGAN pernah proses/simpan itu sebagai input apapun.
    word_count = len(raw.split())
    if word_count in (12, 15, 18, 21, 24) and not ton_topup.is_valid_ton_address(raw):
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            f'{emoji("warning")} <b>JANGAN kirim frasa/seed phrase ke sini!</b>\n\n'
            f'Yang dibutuhkan cuma <b>alamat wallet TON</b> tujuan (diawali UQ atau EQ), bukan frasa pemulihan. '
            f'Frasa 24 kata adalah kunci penuh ke wallet kamu — siapapun yang punya itu bisa menguras semua isi wallet kamu. '
            f'Silakan kirim ulang berupa <b>alamat wallet</b> saja.',
            premium_text("[warning] JANGAN kirim frasa/seed phrase ke sini!\n\nYang dibutuhkan cuma alamat wallet TON tujuan (diawali UQ/EQ), bukan frasa pemulihan. Frasa 24 kata adalah kunci penuh ke wallet kamu. Kirim ulang berupa alamat wallet saja."),
            log_label="TonSeedPhraseRejected",
        )
        return True

    if not ton_topup.is_valid_ton_address(raw):
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            f'{emoji("warning")} Alamat wallet tidak valid. Alamat TON harus diawali <code>UQ</code> atau <code>EQ</code>, total 48 karakter. Contoh: <code>UQabc...xyz</code>',
            premium_text("[warning] Alamat wallet tidak valid. Alamat TON harus diawali UQ atau EQ, total 48 karakter."),
            log_label="TonAddressInvalid",
        )
        return True

    ton_data = {"target_address": raw}
    context.user_data["ton_pending"] = ton_data
    context.user_data["current_menu_state"] = "ton_ask_amount"
    set_page_reply_map(context, "ton_ask_amount", {"Batal": "ton_order_cancel"})

    await notif.send_rich_message_to_chat(
        context.bot, uid,
        f'{emoji("verified")} <b>ALAMAT WALLET DITERIMA:</b>\n\n'
        f'<table bordered striped>\n'
        f'<tr><th>No</th><th>Alamat</th></tr>\n'
        f'<tr><td>1</td><td><code>{raw}</code></td></tr>\n'
        f'</table>\n'
        f'<hr/>\n'
        f'{emoji("ton_coin")} <b>Masukkan jumlah TON</b> yang mau dibeli\n\n'
        f'<table bordered striped>\n'
        f'<tr><th>No</th><th>Contoh</th></tr>\n'
        f'<tr><td>1</td><td>1.5</td></tr>\n'
        f'<tr><td>2</td><td>0.5</td></tr>\n'
        f'</table>\n'
        f'{emoji("warning")} Minimal pembelian TON adalah {ton_topup.MIN_TON_ORDER} TON',
        premium_text(f"[verified] Alamat wallet diterima: {raw}\n\n[ton_coin] Masukkan jumlah TON yang mau dibeli (contoh: 1.5), minimal {ton_topup.MIN_TON_ORDER} TON:"),
        log_label="TonAskAmount",
    )
    return True


async def handle_ton_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if context.user_data.get("current_menu_state") != "ton_ask_amount":
        return False
    uid = update.effective_user.id
    try:
        amount = float((update.message.text or "").strip().replace(",", "."))
        assert amount >= ton_topup.MIN_TON_ORDER
    except (ValueError, AssertionError):
        await notif.send_rich_message_to_chat(
            context.bot, uid,
            f'{emoji("warning")} Jumlah tidak valid. Masukkan angka, minimal {ton_topup.MIN_TON_ORDER} TON (contoh: 1.5)',
            premium_text(f"[warning] Jumlah tidak valid. Masukkan angka, minimal {ton_topup.MIN_TON_ORDER} TON (contoh: 1.5)"),
            log_label="TonAmountInvalid",
        )
        return True

    ton_data = context.user_data.get("ton_pending", {})
    try:
        harga_info = await ton_topup.get_harga_jual_per_ton()
        harga_per_ton = harga_info["harga"]
    except Exception as e:
        await notif.send_rich_message_to_chat(
            context.bot, uid,
            f'{emoji("warning")} Gagal ambil harga TON real-time: {html.escape(str(e))}. Coba lagi sebentar lagi.',
            premium_text(f"[warning] Gagal ambil harga TON real-time. Coba lagi sebentar lagi."),
            log_label="TonPriceError",
        )
        return True

    total = math.ceil(amount * harga_per_ton)
    ton_data["amount"] = amount
    ton_data["price"] = total
    context.user_data["ton_pending"] = ton_data
    context.user_data["current_menu_state"] = "idle"

    user_data = get_user(uid)
    saldo = user_data[3] if user_data else 0  # FIX: belance_balance, bukan deposit_balance

    rich_html = f"""\
{emoji('card')} <b>RINGKASAN ORDER TON</b>

<table bordered striped>
<tr><th>Info</th><th>Detail</th></tr>
<tr><td>Alamat Tujuan</td><td><code>{ton_data.get('target_address','')}</code></td></tr>
<tr><td>Jumlah</td><td>{amount:g} TON</td></tr>
<tr><td>Total</td><td><b>{format_currency(total)}</b></td></tr>
<tr><td>Saldo Kamu</td><td>{format_currency(saldo)}</td></tr>
</table>

{emoji('catatan')} Pilih metode pembayaran di bawah ini."""
    text = premium_text(f"""
[card] <b>RINGKASAN ORDER TON</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[ton_coin] <b>Alamat Tujuan:</b> <code>{ton_data.get('target_address','')}</code>
[dolar] <b>Jumlah:</b> {amount:g} TON
[duitkarung] <b>Total:</b> <b>{format_currency(total)}</b>
[card] <b>Saldo Kamu:</b> {format_currency(saldo)}

[catatan] Pilih metode pembayaran di bawah ini.</blockquote>
""")
    kb = styled_inline_keyboard([
        [styled_button("Bayar Pakai Saldo", callback_data="ton_order_confirm_saldo", style="success", emoji_name="duitkarung")],
        [styled_button("💳 Bayar via QRIS", callback_data="ton_order_confirm_qris", style="primary", emoji_name="dolar")],
        [styled_button("Batal", callback_data="ton_order_cancel", style="danger", emoji_name="back")],
    ])
    await notif.send_rich_message_to_chat(
        context.bot, uid, rich_html, text,
        reply_markup=kb,
        log_label="TonOrderSummary",
    )
    return True


async def ton_order_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q:
        await safe_answer(q)
        uid = q.from_user.id
    else:
        uid = update.effective_user.id
    context.user_data.pop("ton_pending", None)
    context.user_data["current_menu_state"] = "idle"
    if q and q.message:
        await safe_delete_message(context.bot, q.message.chat_id, q.message.message_id)
    await send_root_menu_new(context, uid)


async def ton_order_confirm_saldo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bayar pakai saldo (belance_balance) — potong saldo dulu, lalu proses kirim TON."""
    q = update.callback_query
    uid = q.from_user.id
    ton_data = context.user_data.get("ton_pending", {})
    if not ton_data or not ton_data.get("amount"):
        await safe_answer(q, "Sesi kadaluarsa. Ulangi dari menu Topup TON.", show_alert=True)
        return

    price = ton_data.get("price", 0)
    user_data = get_user(uid)
    # FIX: Cek belance_balance (user_data[3]) bukan deposit_balance (user_data[2]) —
    # deposit_balance itu total histori deposit, bukan saldo yang bisa dipakai belanja.
    saldo = user_data[3] if user_data else 0
    if not user_data or saldo < price:
        await safe_answer(q, f"❌ Saldo tidak cukup! Saldo: {format_currency(saldo)}, dibutuhkan: {format_currency(price)}", show_alert=True)
        return

    await safe_answer(q, "⏳ Memotong saldo & memproses TON...")
    update_balance(uid, belance_delta=-price)
    try:
        await q.message.delete()
    except Exception:
        pass

    context.user_data.pop("ton_pending", None)
    context.user_data["current_menu_state"] = "idle"
    await _process_ton_delivery(context, uid, ton_data, paid_via="saldo")


async def ton_order_confirm_qris(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Dispatcher: pilih flow QRIS otomatis (Pakasir) atau manual (approve owner) sesuai mode payment aktif di owner panel."""
    if get_payment_method() == "manual":
        await ton_order_confirm_manual(update, context)
    else:
        await _ton_order_confirm_otomatis(update, context)


async def _ton_order_confirm_otomatis(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Buat QRIS otomatis via Pakasir dan kirim foto QR ke user (mode Pakasir aktif)."""
    q = update.callback_query
    await safe_answer(q, "⏳ Membuat QRIS...")
    uid = q.from_user.id
    ton_data = context.user_data.get("ton_pending", {})
    if not ton_data or not ton_data.get("amount"):
        await safe_answer(q, "Sesi kadaluarsa. Ulangi dari menu Topup TON.", show_alert=True); return

    price   = ton_data.get("price", 0)
    amount  = ton_data.get("amount")
    target  = ton_data.get("target_address", "")

    qris_data = await create_qris(price)
    if not qris_data:
        await safe_answer(q, "❌ Gagal membuat QRIS. Coba lagi.", show_alert=True); return

    order_id = qris_data["id"]
    qr_path  = qris_data.get("qr_path", "")
    ton_data["order_id"]    = order_id
    ton_data["qris_amount"] = price
    ton_data["qr_path"]     = qr_path
    context.user_data["ton_pending"] = ton_data

    expires_at = int(time.time()) + 300
    try:
        add_pending_payment(uid, order_id, price, qr_path, 0, expires_at)
    except Exception as e:
        print(f"[TON QRIS add_pending_payment] {e}")

    text = premium_text(f"""
[duitkarung] <b>PEMBAYARAN QRIS — TOPUP TON</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[catatan] <b>ID Order:</b> <code>{order_id}</code>
[ton_coin] <b>Jumlah:</b> {amount:g} TON
[card] <b>Alamat Tujuan:</b> <code>{target}</code>
[dolar] <b>Total:</b> <code>{format_currency(price)}</code>

[spikerbiru] Scan QR di atas dan tekan <b>Cek Pembayaran</b> setelah transfer.
Bot akan memproses pengiriman TON otomatis setelah pembayaran terkonfirmasi.</blockquote>
""")
    kb = styled_inline_keyboard([
        [styled_button("✅ Cek Pembayaran", callback_data=f"ton_cek_{order_id}", style="success", emoji_name="verified")],
        [styled_button("Batalkan",          callback_data="ton_order_cancel",       style="danger",  emoji_name="back")]
    ])

    try:
        if qr_path and os.path.exists(qr_path):
            with open(qr_path, "rb") as f:
                photo_bytes = f.read()
            await safe_send_photo(context, uid, photo=photo_bytes, caption=text, reply_markup=kb)
            try:
                await q.message.delete()
            except Exception:
                pass
            return
    except Exception as e:
        print(f"[TON QRIS Otomatis] Gagal kirim foto: {e}")

    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=text, log_label="AutoRich")


async def handle_ton_cek_payment(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cek status bayar QRIS otomatis (Pakasir) → jika lunas, proses kirim TON on-chain."""
    q = update.callback_query
    uid = q.from_user.id

    ton_data = context.user_data.get("ton_pending", {})
    order_id = q.data.replace("ton_cek_", "")
    amount   = ton_data.get("qris_amount", ton_data.get("price", 0))

    is_paid = await check_payment_status(order_id, amount)
    if not is_paid:
        await safe_answer(q, "❌ Pembayaran belum diterima. Tunggu lalu cek lagi.", show_alert=True)
        return

    # ── ATOMIC GUARD: cegah TON dikirim 2x jika user spam klik ─────────────
    try:
        import sqlite3 as _sq3
        _ac = _sq3.connect(DB_PATH)
        _ac.execute(
            "UPDATE pending_payments SET status='paid' WHERE (id=? OR order_id=?) AND status='pending'",
            (order_id, order_id)
        )
        _changed = _ac.execute("SELECT changes()").fetchone()[0]
        _ac.commit()
        _ac.close()
    except Exception as _ae:
        print(f"[Error Atomic Guard TON]: {_ae}")
        _changed = 0

    if not _changed:
        await safe_answer(q, "⚠️ Pembayaran ini sudah diproses sebelumnya.", show_alert=True)
        return
    # ─────────────────────────────────────────────────────────────────────

    await safe_answer(q, "⏳ Pembayaran diterima, mengirim TON...")

    qr_path = ton_data.get("qr_path", "")
    try:
        await q.message.delete()
    except Exception:
        pass
    if qr_path and os.path.exists(qr_path):
        try:
            os.remove(qr_path)
        except Exception:
            pass

    context.user_data.pop("ton_pending", None)
    context.user_data["current_menu_state"] = "idle"
    await _process_ton_delivery(context, uid, ton_data, paid_via="qris")


async def ton_order_confirm_manual(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Mode manual: tampilkan QRIS/rekening owner, minta user upload bukti TF."""
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    ton_data = context.user_data.get("ton_pending", {})
    if not ton_data or not ton_data.get("amount"):
        await safe_answer(q, "Sesi kadaluarsa. Ulangi dari menu Topup TON.", show_alert=True)
        return

    price = ton_data.get("price", 0)
    context.user_data["current_menu_state"] = "ton_wait_bukti"

    pay_lines = []
    for label, key, _ in PAYMENT_METHODS_LIST:
        if key == "qris":
            continue
        info = get_payment_info(key)
        if info:
            pay_lines.append(f"[panahijo] <b>{label}:</b> <code>{info}</code>")
    rekening_text = "\n".join(pay_lines) if pay_lines else ""
    qris_file_id = get_payment_info("qris")

    text = premium_text(f"""
[duitkarung] <b>PEMBAYARAN MANUAL — TOPUP TON</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[ton_coin] <b>Jumlah:</b> {ton_data.get('amount'):g} TON
[card] <b>Alamat Tujuan:</b> <code>{ton_data.get('target_address','')}</code>
[dolar] <b>Total Transfer:</b> <code>{format_currency(price)}</code>

{"" if not rekening_text else rekening_text + chr(10)}{"[card] Scan QRIS di atas atau transfer ke rekening di atas." if qris_file_id and rekening_text else ("[card] Scan QRIS di atas untuk transfer." if qris_file_id else ("[warning] Info rekening belum diset oleh owner." if not rekening_text else ""))}

[warning] Transfer TEPAT <code>{format_currency(price)}</code> sesuai nominal.
[catatan] Setelah transfer, kirim <b>foto/screenshot bukti transfer</b> ke bot ini.
[shield] TON akan diproses & dikirim setelah Owner menyetujui.</blockquote>
""")
    kb = styled_inline_keyboard([[styled_button("Batal", callback_data="ton_order_cancel", style="danger", emoji_name="back")]])

    if qris_file_id:
        try:
            try:
                await q.message.delete()
            except Exception:
                pass
            await context.bot.send_photo(chat_id=uid, photo=qris_file_id, caption=text, parse_mode="HTML", reply_markup=kb)
            return
        except Exception as e:
            print(f"[Ton QRIS Manual foto] {e}")

    await fast_edit(q, text, reply_markup=kb, parse_mode="HTML", rich_html=text, log_label="AutoRich")


async def handle_ton_bukti_tf(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Tangkap foto bukti TF TON (mode manual) → kirim ke owner untuk approve."""
    if context.user_data.get("current_menu_state") != "ton_wait_bukti":
        return False
    if not update.message or not update.message.photo:
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            f'{emoji("warning")} Kirim <b>foto/screenshot</b> bukti transfer ya, bukan teks.',
            premium_text("[warning] Harap kirim foto/screenshot bukti transfer, bukan pesan teks."),
            log_label="BuktiTFTonWrongType",
        )
        return True

    uid = update.effective_user.id
    uname = update.effective_user.username or str(uid)
    ton_data = context.user_data.get("ton_pending", {})
    if not ton_data or not ton_data.get("amount"):
        await notif.send_rich_message_to_chat(
            context.bot, update.effective_chat.id,
            f'{emoji("warning")} Sesi topup TON kadaluarsa. Ulangi dari menu Topup TON.',
            premium_text("[warning] Sesi topup TON kadaluarsa. Ulangi dari menu Topup TON."),
            log_label="TonSessionExpired",
        )
        context.user_data["current_menu_state"] = "idle"
        return True

    ton_manual_pending[uid] = ton_data
    context.user_data["current_menu_state"] = "idle"

    amount = ton_data.get("amount")
    target_address = ton_data.get("target_address", "")
    price = ton_data.get("price", 0)

    try:
        target_owner = OWNER_ID[0] if isinstance(OWNER_ID, list) else OWNER_ID
        kb = styled_inline_keyboard([[
            styled_button("✅ Approve", callback_data=f"ton_approve_manual_{uid}", style="success", emoji_name="verified"),
            styled_button("❌ Tolak", callback_data=f"ton_tolak_manual_{uid}", style="danger", emoji_name="batal"),
        ]])
        caption = premium_text(f"""[duitkarung] <b>REQUEST TOPUP TON MANUAL</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[card] <b>User:</b> @{uname} (<code>{uid}</code>)
[ton_coin] <b>Jumlah:</b> {amount:g} TON
[card] <b>Alamat Tujuan:</b> <code>{target_address}</code>
[dolar] <b>Nominal:</b> <b>{format_currency(price)}</b></blockquote>""")
        await send_photo_to_owner(context, target_owner, update.message.photo[-1].file_id, caption, kb)
    except Exception as e:
        print(f"[Ton Bukti TF Owner] {e}")

    await notif.send_rich_message_to_chat(
        context.bot, update.effective_chat.id,
        f'{emoji("done")} <b>BUKTI TRANSFER TERKIRIM</b>\n<hr/>\nBukti transfer topup TON kamu sudah dikirim ke Owner. TON akan dikirim ke alamat tujuan setelah disetujui.',
        premium_text(f"[done] Bukti transfer topup TON kamu sudah dikirim ke Owner.\n[waktu] TON akan dikirim ke alamat tujuan setelah disetujui."),
        reply_markup=create_reply_keyboard(uid),
        log_label="BuktiTerkirimTon",
    )
    return True


async def ton_approve_manual_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q, "⏳ Memproses TON...")

    target_uid = int(q.data.split("_")[-1])
    ton_data = ton_manual_pending.pop(target_uid, None)
    if not ton_data:
        await safe_answer(q, "Sesi ini sudah diproses/kadaluarsa.", show_alert=True)
        return

    try:
        await q.message.edit_caption(
            caption=premium_text(f"[done] <b>TOPUP TON MANUAL DISETUJUI</b>\n<blockquote>User: <code>{target_uid}</code>\n[waktu] Sedang mengirim TON...</blockquote>"),
            parse_mode="HTML",
        )
    except Exception:
        pass

    await _process_ton_delivery(context, target_uid, ton_data, paid_via="manual")


async def ton_tolak_manual_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q)

    target_uid = int(q.data.split("_")[-1])
    ton_data = ton_manual_pending.pop(target_uid, None)
    amount = ton_data.get("amount", "-") if ton_data else "-"
    price = ton_data.get("price", 0) if ton_data else 0

    try:
        await notif.send_rich_message_to_chat(
            context.bot, target_uid,
            f'{emoji("warning")} <b>TOPUP TON DITOLAK</b>\n\nJumlah: {amount} TON — {format_currency(price)}\nBukti transfer tidak valid atau tidak sesuai. Hubungi CS jika ada pertanyaan.',
            premium_text(f"[warning] Topup TON ({amount} TON, {format_currency(price)}) ditolak.\n[catatan] Bukti transfer tidak valid atau tidak sesuai. Hubungi CS jika ada pertanyaan."),
            log_label="TonManualDitolak",
        )
    except Exception as e:
        print(f"[Notif Tolak Ton Manual] {e}")

    try:
        await q.message.edit_caption(
            caption=premium_text(f"[batal] <b>TOPUP TON MANUAL DITOLAK</b>\n<blockquote>User: <code>{target_uid}</code></blockquote>"),
            parse_mode="HTML",
        )
    except Exception:
        pass


async def _process_ton_delivery(context: ContextTypes.DEFAULT_TYPE, uid: int, ton_data: dict, paid_via: str = "saldo"):
    """Fungsi bersama: catat order ke DB, kirim TON on-chain, notif, refund kalau gagal (khusus saldo)."""
    amount = ton_data.get("amount")
    target_address = ton_data.get("target_address", "")
    price = ton_data.get("price", 0)

    user = None
    try:
        user = await context.bot.get_chat(uid)
    except Exception:
        pass
    username = (user.username if user else "") or ""

    order_id, order_code = ton_topup.create_order(uid, username, target_address, amount, price, paid_via)

    loading = await context.bot.send_message(
        uid, premium_text(f"[loading] Order <b>{order_code}</b> sedang diproses, mohon tunggu..."), parse_mode="HTML",
    )

    try:
        result_msg = await ton_topup.deliver_ton(target_address, float(amount))
        ton_topup.update_status(order_id, "sukses", result_msg)

        try:
            await loading.delete()
        except Exception:
            pass

        user_data = get_user(uid)
        saldo_sisa = user_data[3] if user_data else None  # FIX: belance_balance, bukan deposit_balance

        # ── Kirim FOTO kartu transaksi dulu (terpisah dari text di bawah) ──
        try:
            from src import card_gen
            _tx_match = re.search(r"\(tx:\s*([^)]+)\)", result_msg or "")
            tx_hash = _tx_match.group(1).strip() if _tx_match else "-"
            tx_short = f"{tx_hash[:8]}...{tx_hash[-4:]}" if tx_hash and tx_hash != "-" and len(tx_hash) > 14 else tx_hash
            addr_short = f"{target_address[:10]}...{target_address[-6:]}" if len(target_address) > 20 else target_address
            card_path = card_gen.generate_transaction_card(
                item_label=f"{amount:g} TON",
                item_sub=f"Terkirim ke {addr_short}",
                rows=[
                    ("Order ID", order_code),
                    ("Total Bayar", format_currency(price)),
                    ("Tx Hash", tx_short),
                ],
                icon="diamond",
            )
            with open(card_path, "rb") as _cf:
                await context.bot.send_photo(chat_id=uid, photo=_cf.read())
            try:
                os.remove(card_path)
            except Exception:
                pass
        except Exception as _ce:
            print(f"[TonCardGen] {_ce}")
        # ─────────────────────────────────────────────────────────────────

        await notif.send_rich_message_to_chat(
            context.bot, uid,
            f'{emoji("done")} <b>TOPUP TON BERHASIL</b>\n\n'
            f'<table bordered striped><tr><th>Info</th><th>Detail</th></tr>'
            f'<tr><td>Order</td><td><code>{order_code}</code></td></tr>'
            f'<tr><td>Jumlah</td><td>{amount:g} TON</td></tr>'
            f'<tr><td>Alamat Tujuan</td><td><code>{target_address}</code></td></tr>'
            f'<tr><td>Total</td><td>{format_currency(price)}</td></tr></table>\n\n'
            f'{emoji("verified")} {result_msg}\n\nTerima kasih! 🙏',
            premium_text(f"[done] <b>TOPUP TON BERHASIL</b>\n<blockquote>[card] Order: <code>{order_code}</code>\n[ton_coin] Jumlah: {amount:g} TON\n[card] Alamat Tujuan: {target_address}\n[dolar] Total: {format_currency(price)}</blockquote>\n\n[verified] {result_msg}"),
            reply_markup=create_reply_keyboard(uid),
            log_label="TonSukses",
        )

        try:
            if hasattr(notif, "notif_pembelian_ton_channel"):
                await notif.notif_pembelian_ton_channel(
                    context.bot, uid, username, target_address, amount, order_code, price, saldo_sisa=saldo_sisa,
                )
        except Exception as e:
            print(f"[TonNotifChannel] {e}")

    except ton_topup.TonInsufficientBalanceError as e:
        ton_topup.update_status(order_id, "gagal", str(e))
        if paid_via == "saldo":
            update_balance(uid, deposit_delta=price)
        try:
            await loading.delete()
        except Exception:
            pass
        await notif.send_rich_message_to_chat(
            context.bot, uid,
            f'{emoji("warning")} Order <b>{order_code}</b> gagal: saldo TON di wallet bot sedang tidak cukup. Admin sudah diberi tahu.'
            + (' Saldo kamu sudah dikembalikan.' if paid_via == "saldo" else ''),
            premium_text(f"[warning] Order {order_code} gagal: saldo TON di wallet bot sedang tidak cukup." + (" Saldo sudah dikembalikan." if paid_via == "saldo" else "")),
            reply_markup=create_reply_keyboard(uid),
            log_label="TonInsufficientBalance",
        )
        try:
            target_owner = OWNER_ID[0] if isinstance(OWNER_ID, list) else OWNER_ID
            kb = styled_inline_keyboard([[styled_button("Coba Kirim Ulang", callback_data=f"ton_retry_{order_id}", style="primary", emoji_name="refresh")]])
            await notif.send_rich_message_to_chat(
                context.bot, target_owner,
                f'{emoji("warning")} <b>Saldo TON wallet bot tidak cukup</b> saat proses order {order_code}.\n\n{str(e)}\n\nTopup wallet bot dulu (atau perbaiki seed kalau alamatnya beda), lalu coba kirim ulang order ini.',
                premium_text(f"[warning] Saldo TON wallet bot tidak cukup saat proses order {order_code}. {str(e)} Topup wallet bot dulu, lalu coba kirim ulang."),
                reply_markup=kb,
                log_label="TonInsufficientBalanceOwner",
            )
        except Exception as e2:
            print(f"[TonInsufficientBalanceOwnerNotif] {e2}")

    except ton_topup.TonDeliveryError as e:
        ton_topup.update_status(order_id, "gagal", str(e))
        if paid_via == "saldo":
            update_balance(uid, deposit_delta=price)
        try:
            await loading.delete()
        except Exception:
            pass
        await notif.send_rich_message_to_chat(
            context.bot, uid,
            f'{emoji("warning")} Order <b>{order_code}</b> gagal diproses: <code>{html.escape(str(e))}</code>.'
            + (' Saldo kamu sudah dikembalikan.' if paid_via == "saldo" else ' Admin akan menghubungi kamu.'),
            premium_text(f"[warning] Order {order_code} gagal diproses: {html.escape(str(e))}." + (" Saldo sudah dikembalikan." if paid_via == "saldo" else " Admin akan menghubungi kamu.")),
            reply_markup=create_reply_keyboard(uid),
            log_label="TonGagal",
        )


async def ton_retry_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Owner coba kirim ulang order TON yang gagal."""
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    order_id = int(q.data.split("_")[-1])
    order = ton_topup.get_order(order_id)
    if not order:
        await safe_answer(q, "Order tidak ditemukan.", show_alert=True); return
    if order["status"] == "sukses":
        await safe_answer(q, "Order ini sudah sukses sebelumnya.", show_alert=True); return

    await safe_answer(q, "Mencoba kirim ulang...")
    ton_data = {"amount": order["amount_ton"], "target_address": order["target_address"], "price": order["price"]}
    await notif.send_rich_message_to_chat(
        context.bot, order["user_id"],
        f'{emoji("loading")} Order {order["order_code"]} sedang diproses ulang oleh admin...',
        premium_text(f"[loading] Order {order['order_code']} sedang diproses ulang oleh admin..."),
        log_label="TonRetryNotifBuyer",
    )
    await _process_ton_delivery(context, order["user_id"], ton_data, paid_via=order["paid_via"] or "manual")


async def ton_myorders_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await safe_answer(q)
    uid = q.from_user.id
    orders = ton_topup.list_user_orders(uid)
    if not orders:
        await notif.send_rich_message_to_chat(
            context.bot, uid,
            f'{emoji("card")} Kamu belum punya order TON.',
            premium_text("[card] Kamu belum punya order TON."),
            log_label="TonNoOrders",
        )
        return
    rows_html = "".join(
        f"<tr><td>{o['order_code']}</td><td>{o['amount_ton']:g} TON — {format_currency(o['price'])} — {o['status']}</td></tr>"
        for o in orders
    )
    rich_html = f"""\
{emoji('card')} <b>ORDER TON TERAKHIR KAMU</b>

<table bordered striped><tr><th>Order</th><th>Detail</th></tr>{rows_html}</table>"""
    lines = "\n".join(f"[panahijo] {o['order_code']} — {o['amount_ton']:g} TON — {format_currency(o['price'])} — {o['status']}" for o in orders)
    fallback = premium_text(f"[card] <b>ORDER TON TERAKHIR KAMU</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>{lines}</blockquote>")
    await notif.send_rich_message_to_chat(context.bot, uid, rich_html, fallback, log_label="TonMyOrders")


# ---------- OWNER: TON TOPUP SETTINGS ----------

async def ton_owner_status_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q)

    rows = ton_topup.status_rows()
    warnings = ton_topup.validate()
    rows_html = "".join(f"<tr><td>{label}</td><td>{val}</td></tr>" for label, val in rows)
    warn_html = "".join(f"<tr><td colspan='2'>⚠️ {w}</td></tr>" for w in warnings)
    rich_html = f"""\
{emoji('grafik')} <b>STATUS TOPUP TON</b>

<table bordered striped><tr><th>Setting</th><th>Status</th></tr>{rows_html}{warn_html}</table>"""
    lines = "\n".join(f"[panahijo] {label}: {val}" for label, val in rows)
    warn_lines = "\n".join(f"[warning] {w}" for w in warnings)
    fallback = premium_text(f"[grafik] <b>STATUS TOPUP TON</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>{lines}{chr(10)+warn_lines if warnings else ''}</blockquote>")
    kb = styled_inline_keyboard([
        [styled_button("Update TON API Key", callback_data="ton_owner_set_apikey", style="success", emoji_name="card")],
        [styled_button("Set Fee Flat (Rp/TON)", callback_data="ton_owner_set_fee_flat", style="primary", emoji_name="dolar")],
        [styled_button("Set Margin Jual (mode lama)", callback_data="ton_owner_set_margin", style="primary", emoji_name="grafik")],
    ])
    await fast_edit(q, fallback, reply_markup=kb, parse_mode="HTML", rich_html=rich_html, log_label="AutoRich")


async def ton_owner_pending_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_owner(uid):
        await safe_answer(q, "Owner only!", show_alert=True); return
    await safe_answer(q)
    orders = ton_topup.list_pending_orders()
    kb = styled_inline_keyboard([[styled_button("Kembali", callback_data="ton_owner_status", style="danger", emoji_name="back")]])
    if not orders:
        await fast_edit(q, premium_text("[card] Tidak ada order TON yang menunggu konfirmasi."), reply_markup=kb, parse_mode="HTML", rich_html=premium_text("[card] Tidak ada order TON yang menunggu konfirmasi."), log_label="AutoRich")
        return
    rows_html = "".join(
        f"<tr><td>{o['order_code']}</td><td>{o['amount_ton']:g} TON — @{o['username']} — {o['status']}</td></tr>" for o in orders
    )
    rich_html = f"""\
{emoji('grafik')} <b>ORDER TON MENUNGGU KONFIRMASI</b>

<table bordered striped><tr><th>Order</th><th>Detail</th></tr>{rows_html}</table>"""
    lines = "\n".join(f"[panahijo] {o['order_code']} — {o['amount_ton']:g} TON — @{o['username']} — {o['status']}" for o in orders)
    fallback = premium_text(f"[grafik] <b>ORDER TON MENUNGGU KONFIRMASI</b>\n━━━━━━━━━━━━━━━━━━━━━━━━\n<blockquote>{lines}</blockquote>")
    await fast_edit(q, fallback, reply_markup=kb, parse_mode="HTML", rich_html=rich_html, log_label="AutoRich")


async def _estimate_tg_created(uid: int):
    """Estimasi tanggal pembuatan akun dari Telegram User ID — akurat berdasarkan checkpoint historis."""
    import datetime
    # Tabel checkpoint ID → tanggal (berdasarkan data publik Telegram)
    checkpoints = [
        (0,             datetime.date(2013, 1, 1)),
        (10_000_000,    datetime.date(2013, 4, 1)),
        (50_000_000,    datetime.date(2014, 1, 1)),
        (100_000_000,   datetime.date(2015, 1, 1)),
        (200_000_000,   datetime.date(2016, 1, 1)),
        (300_000_000,   datetime.date(2017, 1, 1)),
        (400_000_000,   datetime.date(2017, 12, 1)),
        (500_000_000,   datetime.date(2018, 6, 1)),
        (700_000_000,   datetime.date(2019, 4, 1)),
        (900_000_000,   datetime.date(2020, 1, 1)),
        (1_100_000_000, datetime.date(2020, 10, 1)),
        (1_300_000_000, datetime.date(2021, 5, 1)),
        (1_500_000_000, datetime.date(2021, 12, 1)),
        (1_800_000_000, datetime.date(2022, 8, 1)),
        (2_000_000_000, datetime.date(2023, 2, 1)),
        (2_500_000_000, datetime.date(2023, 9, 1)),
        (3_000_000_000, datetime.date(2024, 4, 1)),
        (9_999_999_999, datetime.date(2025, 1, 1)),
    ]
    for i in range(len(checkpoints) - 1):
        lo_id, lo_date = checkpoints[i]
        hi_id, hi_date = checkpoints[i + 1]
        if lo_id <= uid < hi_id:
            frac = (uid - lo_id) / (hi_id - lo_id)
            delta = (hi_date - lo_date).days
            return lo_date + datetime.timedelta(days=int(frac * delta))
    return datetime.date(2024, 1, 1)


async def _resolve_telegram_entity(target: str):
    """Resolve target (username / user_id / phone) via Telethon. Return (dict, photo_bytes_or_None, status)."""
    from telethon.tl.functions.users import GetFullUserRequest
    from telethon.tl.functions.contacts import ResolvePhoneRequest
    from telethon.tl.types import (
        User, UserStatusOnline, UserStatusOffline,
        UserStatusRecently, UserStatusLastWeek, UserStatusLastMonth,
    )
    from src.gift_sender import get_gift_client, GIFT_SESSION_FILE
    import os, io, datetime

    if not os.path.exists(GIFT_SESSION_FILE):
        return None, None, "NO_SESSION"

    try:
        client = await get_gift_client(API_ID, API_HASH)
        if not client or not client.is_connected():
            return None, None, "NO_SESSION"

        target = target.strip()

        # ── Resolve entity ──────────────────────────────────────────────
        entity = None
        if target.startswith("+") or (target.lstrip("+").isdigit() and len(target) > 8):
            try:
                res = await client(ResolvePhoneRequest(phone=target.lstrip("+")))
                entity = res.users[0] if res.users else None
            except Exception:
                pass
        elif target.lstrip("-").isdigit():
            try:
                entity = await client.get_entity(int(target))
            except Exception:
                pass
        else:
            try:
                entity = await client.get_entity(target.lstrip("@"))
            except Exception:
                pass

        if not entity:
            return None, None, "NOT_FOUND"
        if not isinstance(entity, User):
            return None, None, "NOT_USER"

        # ── DC: ambil dari entity langsung (paling akurat) ───────────────
        dc = getattr(entity, "dc_id", None)
        if not dc:
            photo_obj = getattr(entity, "photo", None)
            dc = getattr(photo_obj, "dc_id", "—") if photo_obj else "—"
        if not dc:
            dc = "—"

        # ── Full user info ───────────────────────────────────────────────
        try:
            full = await client(GetFullUserRequest(entity))
            full_user = full.users[0] if full.users else entity
        except Exception:
            full_user = entity

        uid        = full_user.id
        name       = " ".join(filter(None, [full_user.first_name or "", full_user.last_name or ""])).strip() or "—"
        username   = f"@{full_user.username}" if full_user.username else "—"
        premium    = "Yes" if getattr(full_user, "premium", False) else "No"
        phone      = getattr(full_user, "phone", None) or "—"
        fake       = "Yes" if getattr(full_user, "fake", False) else "No"
        scam       = "Yes" if getattr(full_user, "scam", False) else "No"
        bot_flag   = "Yes" if getattr(full_user, "bot", False) else "No"
        verified_f = "Yes" if getattr(full_user, "verified", False) else "No"
        restricted = "Yes" if getattr(full_user, "restricted", False) else "No"
        has_photo  = "Set" if getattr(full_user, "photo", None) else "None"

        # ── Tanggal buat dari ID (checkpoint interpolation) ──────────────
        try:
            created_date    = await _estimate_tg_created(uid)
            created_str     = created_date.strftime("%Y-%m")
            today           = datetime.date.today()
            age_days        = (today - created_date).days
            age_years       = age_days // 365
            age_months      = (age_days % 365) // 30
            if age_years > 0 and age_months > 0:
                account_age_str = f"{age_years} year{'s' if age_years>1 else ''} {age_months} month{'s' if age_months>1 else ''}"
            elif age_years > 0:
                account_age_str = f"{age_years} year{'s' if age_years>1 else ''}"
            else:
                account_age_str = f"{age_months} month{'s' if age_months>1 else ''}"
        except Exception:
            created_str     = "—"
            account_age_str = "—"

        # ── ID digits & account rating ───────────────────────────────────
        id_digits = len(str(uid))
        if uid < 100_000_000:
            rating_level = 3
        elif uid < 500_000_000:
            rating_level = 2
        else:
            rating_level = 1

        # ── Last seen / status ───────────────────────────────────────────
        status_str = "Recently"
        try:
            st = full_user.status
            if isinstance(st, UserStatusOnline):
                status_str = "Online 🟢"
            elif isinstance(st, UserStatusOffline):
                status_str = f"Offline ({st.was_online.strftime('%Y-%m-%d %H:%M')} UTC)"
            elif isinstance(st, UserStatusRecently):
                status_str = "Recently"
            elif isinstance(st, UserStatusLastWeek):
                status_str = "Last Week"
            elif isinstance(st, UserStatusLastMonth):
                status_str = "Last Month"
            else:
                status_str = "Long Ago"
        except Exception:
            pass

        # ── Download foto profil target ──────────────────────────────────
        photo_bytes = None
        try:
            if getattr(full_user, "photo", None):
                buf = io.BytesIO()
                await client.download_profile_photo(full_user, file=buf, download_big=True)
                buf.seek(0)
                raw = buf.read()
                photo_bytes = raw if raw else None
        except Exception:
            photo_bytes = None

        now_utc = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M")

        result_data = {
            "id":           uid,
            "id_digits":    id_digits,
            "name":         name,
            "dc":           dc,
            "created":      created_str,
            "username":     username,
            "premium":      premium,
            "phone":        phone,
            "status":       status_str,
            "photos":       has_photo,
            "scam":         scam,
            "fake":         fake,
            "bot":          bot_flag,
            "verified":     verified_f,
            "restricted":   restricted,
            "account_age":  account_age_str,
            "rating_level": rating_level,
            "date_checked": now_utc,
        }
        return result_data, photo_bytes, "OK"

    except Exception as e:
        return None, None, f"ERROR: {e}"


_CEKID_BG_PATH = "media/cekid_card_bg.png"
_CEKID_FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
_CEKID_FONT_BOLD = _CEKID_FONT_DIR + "DejaVuSans-Bold.ttf"
_CEKID_FONT_REG  = _CEKID_FONT_DIR + "DejaVuSans.ttf"


def _cekid_font(path, size):
    from PIL import ImageFont
    return ImageFont.truetype(path, size)


def _cekid_sanitize_text(s: str) -> str:
    """Banyak nama akun Telegram pakai karakter Unicode 'fancy' (mathematical
    bold/script/fraktur, dst). Font yang dipakai untuk render kartu gak punya
    glyph buat karakter itu, hasilnya kotak-kotak (tofu boxes). NFKD decompose
    mengembalikan karakter fancy itu ke huruf dasarnya (mis. '𝓡𝓲𝔃𝓴𝔂' -> 'Rizky')
    sehingga tetap terbaca jelas di gambar. Karakter yang masih tidak bisa
    direpresentasikan dibuang (bukan diganti kotak)."""
    import unicodedata
    if not s:
        return s
    normalized = unicodedata.normalize("NFKD", s)
    # Buang combining marks hasil dekomposisi (aksen, dsb tidak relevan di sini)
    # tapi pertahankan karakter dasar. Karakter non-ASCII yang masih tersisa
    # (emoji dekoratif, dst) dibuang supaya tidak jadi kotak di font terbatas.
    cleaned = "".join(
        ch for ch in normalized
        if not unicodedata.combining(ch) and (ord(ch) < 128 or ch.isspace())
    )
    cleaned = " ".join(cleaned.split())  # rapikan spasi ganda akibat karakter yang dibuang
    return cleaned if cleaned.strip() else "(unnamed)"


def _build_profile_card_image(d: dict, photo_bytes: bytes = None) -> "io.BytesIO":
    """Render kartu 'TELEGRAM PROFILE' sebagai GAMBAR (bukan caption teks) — semua
    field diambil langsung dari data akun yang dicek, rating box dihapus karena
    sudah tidak relevan lagi. Background dasar dipakai ulang dari aset referensi
    (sudah di-crop & area datanya di-wipe via blur supaya tekstur/gradient asli
    tetap natural), lalu semua teks digambar ulang di atasnya sesuai data live.
    """
    from PIL import Image, ImageDraw, ImageFont, ImageOps, ImageFilter

    bg = Image.open(_CEKID_BG_PATH).convert("RGB")

    # Wipe ulang area data (jaga-jaga kalau background diganti file lain di masa depan
    # yang belum di-wipe) — aman dipanggil berulang karena cuma blur, bukan ganti warna.
    x1, y1, x2, y2 = 65, 108, 656, 438
    interior = bg.crop((x1, y1, x2, y2)).filter(ImageFilter.GaussianBlur(20))
    bg.paste(interior, (x1, y1))

    draw = ImageDraw.Draw(bg)

    label_col = (122, 138, 156)
    value_col = (235, 238, 242)
    accent    = (110, 180, 255)
    warn_col  = (240, 165, 70)

    f_label = _cekid_font(_CEKID_FONT_BOLD, 18)
    f_value = _cekid_font(_CEKID_FONT_REG, 18)

    def truncate(text, font, max_w):
        text = str(text)
        if font.getlength(text) <= max_w:
            return text
        while text and font.getlength(text + "…") > max_w:
            text = text[:-1]
        return text + "…"

    def row(x, y, label, value, value_color=value_col, value_font=None, label_w=150, max_value_w=None):
        vf = value_font or f_value
        if max_value_w:
            value = truncate(value, vf, max_value_w)
        draw.text((x, y), label, font=f_label, fill=label_col)
        draw.text((x + label_w, y), ":", font=f_label, fill=label_col)
        draw.text((x + label_w + 16, y), str(value), font=vf, fill=value_color)

    left_x, right_x = 78, 430
    y0, dy = 122, 40
    LW_LEFT, LW_RIGHT = 140, 85
    LEFT_MAXW  = right_x - (left_x + LW_LEFT + 16) - 24
    # Panel data berakhir x≈654, value kolom kanan mulai di x=430+85+14=529
    # RIGHT_MAXW = 654 - 529 - 5 = 120px — cukup untuk semua teks dengan font 13
    RIGHT_VAL_X = right_x + LW_RIGHT + 14
    RIGHT_MAXW  = 654 - RIGHT_VAL_X - 5

    # Font lebih kecil untuk kolom kanan supaya semua teks muat di panel
    f_label_r = _cekid_font(_CEKID_FONT_BOLD, 13)
    f_value_r = _cekid_font(_CEKID_FONT_REG, 13)

    yn_color = lambda v: warn_col if str(v).strip().lower() == "yes" else value_col

    # Status offline yang ada jam-nya dipersingkat jadi tanggal saja, biar gak
    # kepotong elipsis di kartu (ruang kolom kanan terbatas).
    status_disp = d["status"]
    if status_disp.startswith("Offline (") and " " in status_disp:
        status_disp = "Offline (" + status_disp.split("(", 1)[1].split(" ")[0] + ")"

    row(left_x,  y0 + dy*0, "ID", f"{d['id']}", label_w=LW_LEFT, max_value_w=LEFT_MAXW)
    row(left_x,  y0 + dy*1, "Name", _cekid_sanitize_text(d['name']), label_w=LW_LEFT, max_value_w=LEFT_MAXW)
    row(left_x,  y0 + dy*2, "Username", d['username'], value_color=accent, label_w=LW_LEFT, max_value_w=LEFT_MAXW)
    row(left_x,  y0 + dy*3, "DC", d['dc'], label_w=LW_LEFT, max_value_w=LEFT_MAXW)
    row(left_x,  y0 + dy*4, "Created", d['created'], label_w=LW_LEFT, max_value_w=LEFT_MAXW)
    row(left_x,  y0 + dy*5, "Phone", d['phone'], label_w=LW_LEFT, max_value_w=LEFT_MAXW)
    row(left_x,  y0 + dy*6, "Premium", d['premium'], label_w=LW_LEFT, max_value_w=LEFT_MAXW)
    row(left_x,  y0 + dy*7, "Account Age", d['account_age'], label_w=LW_LEFT, max_value_w=LEFT_MAXW)

    # Kolom kanan — pakai font kecil (f_label_r / f_value_r) supaya muat di panel
    def row_r(y, label, value, value_color=value_col):
        draw.text((right_x, y), label, font=f_label_r, fill=label_col)
        draw.text((right_x + LW_RIGHT, y), ":", font=f_label_r, fill=label_col)
        val = truncate(str(value), f_value_r, RIGHT_MAXW)
        draw.text((right_x + LW_RIGHT + 8, y), val, font=f_value_r, fill=value_color)

    row_r(y0 + dy*0, "Status",     status_disp)
    row_r(y0 + dy*1, "Photos",     d['photos'])
    row_r(y0 + dy*2, "Scam",       d['scam'],       value_color=yn_color(d['scam']))
    row_r(y0 + dy*3, "Fake Label", d['fake'],       value_color=yn_color(d['fake']))
    row_r(y0 + dy*4, "Bot",        d['bot'],        value_color=yn_color(d['bot']))
    row_r(y0 + dy*5, "Verified",   d['verified'])
    row_r(y0 + dy*6, "Restricted", d['restricted'], value_color=yn_color(d['restricted']))
    row_r(y0 + dy*7, "Checked",    d['date_checked'])

    # Catatan jumlah digit ID, kecil & inline di sebelah kanan nilai ID
    id_val_x = left_x + LW_LEFT + 16
    id_val_w = f_value.getlength(truncate(str(d['id']), f_value, LEFT_MAXW))
    draw.text((id_val_x + id_val_w + 8, y0 + 2), f"({d['id_digits']}d)",
               font=_cekid_font(_CEKID_FONT_REG, 13), fill=label_col)

    # Foto profil target, dibulatkan + diberi ring biru, di pojok kanan-atas panel.
    if photo_bytes:
        try:
            pp = Image.open(io.BytesIO(photo_bytes)).convert("RGBA")
            size = 56
            pp = ImageOps.fit(pp, (size, size))
            mask = Image.new("L", (size, size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
            bg_rgba = bg.convert("RGBA")
            ring = Image.new("RGBA", (size + 6, size + 6), (0, 0, 0, 0))
            ImageDraw.Draw(ring).ellipse((0, 0, size + 6, size + 6), outline=(90, 170, 255, 255), width=3)
            px, py = 565, 30
            bg_rgba.paste(ring, (px - 3, py - 3), ring)
            bg_rgba.paste(pp, (px, py), mask)
            bg = bg_rgba.convert("RGB")
        except Exception:
            pass

    out = io.BytesIO()
    bg.save(out, format="JPEG", quality=92)
    out.seek(0)
    return out


def _build_profile_card_text(d: dict) -> str:
    """Generate caption HTML detail untuk dikirim di bawah foto kartu profil.
    Format mirip tampilan lama: semua field lengkap dengan emoji icon, rapi di blockquote."""
    prem_icon = "💎" if str(d.get("premium", "No")).lower() == "yes" else "❌"
    scam_icon = "⚠️" if str(d.get("scam", "No")).lower() == "yes" else "✅"
    fake_icon = "⚠️" if str(d.get("fake", "No")).lower() == "yes" else "✅"
    bot_icon  = "🤖" if str(d.get("bot",  "No")).lower() == "yes" else "👤"
    ver_icon  = "✅" if str(d.get("verified", "No")).lower() == "yes" else "❌"
    rest_icon = "⛔" if str(d.get("restricted", "No")).lower() == "yes" else "✅"

    text = premium_text(f"""[shield] <b>HASIL CEK ID — {d['id']}</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<blockquote>[card] <b>ID:</b> <code>{d['id']}</code>  <i>({d.get('id_digits', '?')} digit)</i>
[crown] <b>Name:</b> {html.escape(str(d['name']))}
[sparkle] <b>Username:</b> {html.escape(str(d['username']))}
[Telegram] <b>DC:</b> {d['dc']}
[waktu] <b>Created:</b> {d['created']}
[WhatsApp] <b>Phone:</b> {d['phone']}
{prem_icon} <b>Premium:</b> {d['premium']}
[waktu] <b>Account Age:</b> {d['account_age']}
[online] <b>Status:</b> {d['status']}
[card] <b>Photos:</b> {d['photos']}
{scam_icon} <b>Scam:</b> {d['scam']}
{fake_icon} <b>Fake Label:</b> {d['fake']}
{bot_icon} <b>Bot:</b> {d['bot']}
{ver_icon} <b>Verified:</b> {d['verified']}
{rest_icon} <b>Restricted:</b> {d['restricted']}
[waktu] <b>Checked:</b> {d['date_checked']}</blockquote>""")
    return text


async def cmd_info_telegram(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handler command /info @username | user_id | +phone"""
    user = update.effective_user
    if not user:
        return

    args = context.args
    if not args:
        await notif.send_rich_message_to_chat(context.bot, update.message.chat_id, premium_text(f"""[warning] <b>Cara Pakai /info:</b> <code>/info @username</code>      — cek via username <code>/info 974468120</code>      — cek via User ID <code>/info +6281234567890</code> — cek via nomor HP"""), premium_text("""
[warning] <b>Cara Pakai /info:</b>
━━━━━━━━━━━━━━━━━━━━━━━━
<code>/info @username</code>      — cek via username
<code>/info 974468120</code>      — cek via User ID
<code>/info +6281234567890</code> — cek via nomor HP
"""), log_label="AutoRich2")
        return

    target = args[0].strip()

    loading_msg = await notif.send_rich_message_to_chat(
        context.bot, update.message.chat_id,
        premium_text(f"[shield] Sedang mengambil data akun <code>{target}</code>…"),
        premium_text(f"[shield] Sedang mengambil data akun <code>{target}</code>…"),
        log_label="InfoTelegramLoading",
    )

    data, photo_bytes, status = await _resolve_telegram_entity(target)

    if status == "NO_SESSION":
        await notif.edit_rich_message(
            context.bot, update.message.chat_id, loading_msg,
            premium_text("[warning] <b>Session MTProto belum terhubung.</b>\n<blockquote>Minta owner connect MTProto/Kurir dulu lewat Owner Gift Menu.</blockquote>"),
            premium_text("[warning] <b>Session MTProto belum terhubung.</b>\n<blockquote>Minta owner connect MTProto/Kurir dulu lewat Owner Gift Menu.</blockquote>"),
            log_label="InfoTelegramNoSession",
        )
        return
    if status == "NOT_FOUND" or data is None:
        await notif.edit_rich_message(
            context.bot, update.message.chat_id, loading_msg,
            premium_text(f"[warning] <b>Akun tidak ditemukan:</b> <code>{target}</code>\n<blockquote>Pastikan username / ID / nomor benar dan akun masih aktif.</blockquote>"),
            premium_text(f"[warning] <b>Akun tidak ditemukan:</b> <code>{target}</code>\n<blockquote>Pastikan username / ID / nomor benar dan akun masih aktif.</blockquote>"),
            log_label="InfoTelegramNotFound",
        )
        return
    if status == "NOT_USER":
        await notif.edit_rich_message(
            context.bot, update.message.chat_id, loading_msg,
            premium_text("[warning] <b>Target bukan akun user</b> (bot atau channel tidak didukung)."),
            premium_text("[warning] <b>Target bukan akun user</b> (bot atau channel tidak didukung)."),
            log_label="InfoTelegramNotUser",
        )
        return
    if status.startswith("ERROR"):
        await notif.edit_rich_message(
            context.bot, update.message.chat_id, loading_msg,
            premium_text(f"[warning] <b>Gagal ambil data.</b>\n<blockquote><code>{status}</code></blockquote>"),
            premium_text(f"[warning] <b>Gagal ambil data.</b>\n<blockquote><code>{status}</code></blockquote>"),
            log_label="InfoTelegramError",
        )
        return

    keyboard = styled_inline_keyboard([
        [
            styled_button(f"🆔 Copy ID: {data['id']}", callback_data=f"cekid_copy_{data['id']}", style="primary", emoji_name="card"),
            styled_button("🔍 Cek Lain", callback_data="page7_cek_lain", style="success", emoji_name="verified"),
        ],
    ])

    try:
        _lmid = notif.rich_message_id(loading_msg)
        if _lmid:
            await context.bot.delete_message(chat_id=update.message.chat_id, message_id=_lmid)
    except Exception:
        pass

    # Build caption HTML detail (mirip format lama) yang dikirim di bawah foto kartu
    card_caption = _build_profile_card_text(data)

    # Kartu profil digenerate sebagai gambar + caption HTML detail di bawahnya
    try:
        card_img = _build_profile_card_image(data, photo_bytes)
        await update.message.reply_photo(
            photo=card_img,
            caption=card_caption,
            parse_mode="HTML",
            reply_markup=keyboard
        )
    except Exception as e:
        # Fallback: kirim teks detail saja kalau render gambar gagal
        fallback_text = premium_text(
            f"[warning] <b>Gagal merender kartu gambar.</b>\n"
            f"<blockquote><code>{e}</code></blockquote>\n\n"
        ) + card_caption
        await update.message.reply_text(fallback_text, parse_mode="HTML", reply_markup=keyboard)



async def _global_error_handler(update, context: ContextTypes.DEFAULT_TYPE):
    """Error handler global. Error jaringan (NetworkError/TimedOut) yang sifatnya
    sementara cukup dicatat singkat, biar console tidak kebanjiran traceback
    panjang tiap kali koneksi ke Telegram sempat putus sebentar. PTB otomatis
    retry get_updates setelah error macam ini, jadi tidak fatal."""
    err = context.error
    if isinstance(err, (telegram.error.NetworkError, telegram.error.TimedOut)):
        print(f"[warn] Network hiccup (auto-retry): {type(err).__name__}: {err}")
        return
    print(f"[error] Unhandled exception: {err}")
    traceback.print_exception(type(err), err, err.__traceback__)


def register_all_handlers(app):
    """Daftarkan SEMUA handler bot ke Application manapun (dipakai bot utama
    maupun tiap clone bot) supaya tampilan & fitur clone identik dengan pusat."""
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("bct", broadcast_command))
    app.add_handler(CommandHandler("info", cmd_info_telegram))  # PAGE 7 — CEK ID TELEGRAM
    app.add_handler(CommandHandler("restore_sessions", cmd_restore_sessions))
    app.add_handler(CommandHandler("check_sessions", cmd_check_sessions))
    # ===== CLONE BOT SYSTEM — command owner =====
    app.add_handler(CommandHandler("approveclone", clone_cmd_approveclone))
    app.add_handler(CommandHandler("rejectclone", clone_cmd_rejectclone))
    app.add_handler(CommandHandler("setnego", cmd_setnego))
    app.add_handler(CommandHandler("approvewd", clone_cmd_approvewd))
    app.add_handler(CommandHandler("rejectwd", clone_cmd_rejectwd))

    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo_message))
    app.add_handler(MessageHandler(filters.Document.ALL, cv5_handle_document))
    app.add_error_handler(_global_error_handler)


async def _recover_stuck_gift_orders(bot):
    """Jalan sekali tiap bot startup. Cari order gift yang statusnya sudah
    'paid' di pending_payments TAPI belum ada baris sukses/gagal di
    gift_orders — artinya proses kirim gift kepotong di tengah jalan (mis.
    bot crash/restart gara-gara Bad Gateway persis setelah atomic guard
    nge-flip status jadi 'paid'). Sebelum fix ini, order begini STUCK
    selamanya: user pencet "Cek Pembayaran" cuma dapet "sudah diproses
    sebelumnya" tanpa gift pernah beneran terkirim ataupun ada notif apapun.
    """
    try:
        import json as _json
        cursor.execute("""
            SELECT p.id, p.gift_json FROM pending_payments p
            LEFT JOIN gift_orders g ON g.order_id = p.id
            WHERE p.status='paid' AND p.gift_json IS NOT NULL AND g.order_id IS NULL
        """)
        stuck_rows = cursor.fetchall()
    except Exception as e:
        print(f"[GiftRecovery] Gagal query order stuck: {e}")
        return

    if not stuck_rows:
        return

    print(f"[GiftRecovery] Ketemu {len(stuck_rows)} order gift yang stuck (paid tapi belum terkirim). Melanjutkan...")
    for order_id, gift_json in stuck_rows:
        try:
            gift_data = _json.loads(gift_json)
            buyer_id = gift_data.get("buyer_id")
            if not buyer_id:
                print(f"[GiftRecovery] Order {order_id}: gift_json tidak punya buyer_id, dilewati.")
                continue
            print(f"[GiftRecovery] Melanjutkan order {order_id} untuk user {buyer_id}...")
            await _process_gift_delivery(
                context=None, uid=buyer_id, gift_data=gift_data,
                order_id=order_id, paid_via="qris", buyer_bot=bot,
            )
        except Exception as e:
            print(f"[GiftRecovery] Gagal proses ulang order {order_id}: {e}")


async def main():
    # --- Cek versi `tonutils` pas startup, biar kelihatan di Console panel
    # tanpa perlu akses bash/terminal. WAJIB versi 0.x (bukan 2.x) supaya
    # fitur Topup TON (src/ton_topup.py) jalan -- lihat requirements.txt. ---
    try:
        import importlib.metadata as _ilm

        # CATATAN PERBAIKAN: `tonutils` gak expose atribut `__version__` di
        # __init__.py-nya, jadi `getattr(tonutils, "__version__", "?")` yang
        # dipakai sebelumnya SELALU balikin "?" -- dan fallback lamanya
        # nganggep "?" itu "bukan versi jelek" sehingga selalu ngeprint "OK"
        # walau gak beneran ngecek apa-apa (ini kejadian di server: log bilang
        # "tonutils versi ? OK" padahal fitur Topup TON tetep error).
        # Pakai importlib.metadata (baca metadata paket yang ke-install lewat
        # pip, bukan atribut python) biar versi yang kebaca akurat.
        try:
            _tv = _ilm.version("tonutils")
        except _ilm.PackageNotFoundError:
            _tv = None

        if _tv is None:
            print("[STARTUP][WARNING] package 'tonutils' belum ke-install sama sekali -- fitur Topup TON gak akan jalan.")
        else:
            try:
                from packaging.version import Version
                _bad = Version(_tv) < Version("2.0.3")
            except Exception:
                _bad = _tv.startswith("2.0.1b") or _tv.startswith(("0.", "1."))

            if _bad:
                print(f"[STARTUP][WARNING] tonutils versi {_tv} ke-install (butuh >=2.0.3, versi ini "
                      f"kemungkinan beta lama 2.0.1b2 yang strukturnya beda). Klik Reinstall di panel.")
            else:
                # Metadata bilang versi udah OK, tapi itu gak jamin file
                # submodule-nya beneran lengkap/gak korup (misal sisa install
                # lama yang gagal di tengah jalan, folder ketimpa sebagian).
                # Makanya di sini beneran dicoba import modul yang dipakai
                # src/ton_topup.py, biar ketauan dari LOG STARTUP -- bukan
                # baru ketauan pas ada user order TON.
                try:
                    import tonutils  # noqa: F401
                    from tonutils.clients import TonapiClient  # noqa: F401
                    from tonutils.contracts import WalletV4R2  # noqa: F401
                    print(f"[STARTUP] tonutils versi {_tv} OK, tonutils.clients & tonutils.contracts "
                          f"berhasil di-import (fitur Topup TON siap). Lokasi package: {tonutils.__file__}")
                except Exception as _sub_e:
                    import tonutils as _tu
                    import pkgutil
                    _pkg_dir = getattr(_tu, "__path__", None)
                    _submods = [m.name for m in pkgutil.iter_modules(_pkg_dir)] if _pkg_dir else []
                    print(f"[STARTUP][WARNING] tonutils versi {_tv} (metadata OK) TAPI GAGAL import "
                          f"tonutils.clients / tonutils.contracts: {_sub_e}. Lokasi package: {_tu.__file__}. "
                          f"Submodule yang KETEMU di folder package ini sekarang: {_submods}. "
                          f"Cek apakah import path di src/ton_topup.py `_get_bot_client_and_wallet()` "
                          f"masih cocok sama daftar submodule di atas -- kalau tonutils rilis versi "
                          f"baru lagi dengan struktur beda, update import-nya di sana.")
    except Exception as _e:
        print(f"[STARTUP] (skip cek versi tonutils: {_e})")

    app = (
        ApplicationBuilder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        # get_updates (long polling) pakai koneksi TERPISAH dari request biasa,
        # dan wajib punya read_timeout LEBIH BESAR dari parameter timeout yang
        # dikirim ke start_polling (default 10 detik). Kalau tidak di-set,
        # request lib akan pakai default yang lebih pendek dari waktu tunggu
        # long-poll itu sendiri, sehingga koneksi keputus di tengah jalan dan
        # muncul httpx.ReadError / NetworkError berulang di console (tidak
        # fatal, tapi bikin log penuh dan bisa menunda update masuk).
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(40)
        .get_updates_write_timeout(30)
        .get_updates_pool_timeout(30)
        # ✅ FIX: Enable concurrent updates agar multiple users tidak saling block
        # ✅ FIX: Atur concurrent_updates untuk limit beban server (angka = jumlah maksimum update paralel)
        .concurrent_updates(10)
        .build()
    )

    register_all_handlers(app)

    # Simpan bot PUSAT sebagai instance yang dipakai check_sub() untuk cek
    # member channel/grup wajib-join. Ini dipakai baik oleh bot utama maupun
    # SEMUA bot clone (lihat check_sub), supaya clone tidak perlu jadi
    # member/admin channel sama sekali.
    global _MAIN_BOT_INSTANCE
    _MAIN_BOT_INSTANCE = app.bot

    print("=" * 50)
    print("VIONY BOT NOKTEL - ONLINE")
    print(f"Owner ID: {OWNER_ID}")
    print("Status: polling aktif")
    print("=" * 50)
    
    await app.initialize()
    await app.start()
    # drop_pending_updates=True + allowed_updates=['message','callback_query','inline_query']
    # mencegah Conflict jika ada instance lama yang masih berjalan
    await app.updater.start_polling(
        drop_pending_updates=True,
        allowed_updates=["message", "callback_query", "inline_query", "chosen_inline_result"]
    )
    asyncio.create_task(check_dead_sessions_loop(app))
    asyncio.create_task(rekap_saldo_loop(app.bot))
    asyncio.create_task(auto_backup_user_loop(app.bot))
    asyncio.create_task(auto_cleanup_cache_loop())
    # FIX: selesein order gift yang stuck (paid tapi gift belum sempet
    # terkirim) akibat crash/restart sebelumnya -- lihat _recover_stuck_gift_orders().
    asyncio.create_task(_recover_stuck_gift_orders(app.bot))

    # ===== CLONE BOT SYSTEM: nyalakan semua clone berstatus active =====
    try:
        jumlah_clone = clone_system.start_all_active_clones(DB_PATH, register_all_handlers)
        print(f"[CloneSystem] {jumlah_clone} clone bot aktif dijalankan.")
    except Exception as e:
        print(f"[CloneSystem] Gagal start clone bots: {e}")
    
    try:
        while True:
            await asyncio.sleep(1)
    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\nBot stopped!")
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())