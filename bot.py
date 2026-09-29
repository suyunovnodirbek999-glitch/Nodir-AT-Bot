# -*- coding: utf-8 -*-
"""
Yuk reestri bot — V2

Yangi imkoniyatlar (so'ralgan raqamlar bo'yicha):
  1  - bir xabarda bir nechta yozuvni alohida ajratish
  4  - sana/muddatni aniqlash
  5  - ikki tomonlama (borib-kelish) yo'nalishni aniqlash
 10  - noaniq xabarga qo'shimcha ma'lumot so'rash
 12  - yuborishdan oldin "Tahrirlash" tugmasi
 19  - yuborilgan postni keyin o'chirish tugmasi
 20  - kun oxirida kunlik yig'ma hisobot (61 bilan birlashtirilgan)
 22  - haftalik/oylik hisobot (65 bilan birlashtirilgan, /stat bilan ham)
 26  - Excel eksport (/excel)
 35  - postga aloqa (telefon) qo'shish
 51  - masofani taxminiy hisoblash
 52  - yoqilg'i xarajatini taxminiy hisoblash
 59  - yetkazish vaqtini taxminiy hisoblash
 61  - kunlik avtomatik hisobot (admin'ga)
 65  - haftalik avtomatik hisobot (admin'ga)
 68  - bot qayta ishga tushganda admin'ga xabar
 69  - xatolik bo'lsa admin'ga xabar
 81  - /til — bot interfeysi tili (uz/ru)
 90  - bayram kunlarida shablonga qisqa mavsumiy emoji

Routing:
  - "zapros" (yuk/mashina KERAK)  -> kanalga post qilinadi
  - "mashina bor" (bo'sh transport) -> faqat shaxsiy chatingizda ro'yxatga
    saqlanadi (haqiqiy Telegram "Избранное"ga botlar umuman yoza olmaydi,
    shu sabab bu ro'yxat uning o'rnini bosadi: /moshinalar bilan ko'rasiz)

Muhim eslatma: SQLite fayli Railway'da doimiy diskka ulanmagan bo'lsa,
qayta deploy qilinganda tozalanadi. Doimiy saqlash uchun Railway'da
"Volume" qo'shish kerak (Settings -> Volumes).
"""

import asyncio
import json
import logging
import os
import re
import sqlite3
from datetime import datetime, timedelta, time as dtime

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python <3.9 fallback (odatda kerak bo'lmaydi)
    ZoneInfo = None

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("yuk-bot")

# --------------------------------------------------------------------------
# Sozlamalar (muhit o'zgaruvchilari)
# --------------------------------------------------------------------------

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
CHANNEL_ID = os.environ.get("CHANNEL_ID", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ADMIN_CHAT_ID = os.environ.get("ADMIN_CHAT_ID", "")  # sizning shaxsiy Telegram ID'ingiz
CONTACT_PHONE = os.environ.get("CONTACT_PHONE", "+998509999709")
DIESEL_PRICE = float(os.environ.get("DIESEL_PRICE", "12000"))  # so'm/litr, taxminiy
TIMEZONE_NAME = os.environ.get("TIMEZONE", "Asia/Tashkent")
DAILY_SUMMARY_HOUR = int(os.environ.get("DAILY_SUMMARY_HOUR", "20"))  # 20:00

TZ = ZoneInfo(TIMEZONE_NAME) if ZoneInfo else None
DB_PATH = os.environ.get("DB_PATH", "yuk_bot.db")

# {chat_id: True}  — "Tahrirlash" bosilgach, keyingi xabar qayta ishlanadi
AWAITING_EDIT: dict[int, object] = {}  # tur (zapros/mashina) yoki True
# {chat_id: 'zapros' | 'mashina'}  — menyu tugmasi bosilgach, keyingi xabar shu turda
MODE: dict[int, str] = {}
# {chat_id: [entry, entry, ...]}  — ko'p yozuvli xabar tasdiqni kutmoqda
PENDING_MULTI: dict[int, list] = {}
# {chat_id: text}  — turi (zapros/mashina) noaniq, tugma orqali so'ralmoqda
PENDING_KIND: dict[int, str] = {}

# --------------------------------------------------------------------------
# Ma'lumotlar bazasi
# --------------------------------------------------------------------------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            kind TEXT,
            yuklash TEXT, manzil TEXT, yuk_turi TEXT, mashina_turi TEXT,
            soni INTEGER, dona INTEGER, tonna REAL, kub REAL,
            tolov TEXT, sana TEXT, round_trip INTEGER,
            template TEXT,
            channel_message_id INTEGER,
            status TEXT DEFAULT 'active',
            created_at TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            lang TEXT DEFAULT 'uz'
        )"""
    )
    conn.commit()
    conn.close()


def get_lang(user_id: int) -> str:
    conn = db()
    row = conn.execute("SELECT lang FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row["lang"] if row else "uz"


def set_lang(user_id: int, lang: str):
    conn = db()
    conn.execute(
        "INSERT INTO users (user_id, lang) VALUES (?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET lang=excluded.lang",
        (user_id, lang),
    )
    conn.commit()
    conn.close()


def save_entry(user_id: int, kind: str, p: dict, template: str) -> int:
    conn = db()
    cur = conn.execute(
        """INSERT INTO entries
           (user_id, kind, yuklash, manzil, yuk_turi, mashina_turi, soni, dona,
            tonna, kub, tolov, sana, round_trip, template, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            user_id, kind, p.get("yuklash"), p.get("manzil"), p.get("yuk_turi"),
            p.get("mashina_turi"), p.get("soni"), p.get("dona"), p.get("tonna"),
            p.get("kub"), p.get("tolov"), p.get("sana"), int(bool(p.get("round_trip"))),
            template, datetime.now().isoformat(),
        ),
    )
    conn.commit()
    entry_id = cur.lastrowid
    conn.close()
    return entry_id


def set_channel_message(entry_id: int, message_id: int):
    conn = db()
    conn.execute("UPDATE entries SET channel_message_id=? WHERE id=?", (message_id, entry_id))
    conn.commit()
    conn.close()


def set_status(entry_id: int, status: str):
    conn = db()
    conn.execute("UPDATE entries SET status=? WHERE id=?", (status, entry_id))
    conn.commit()
    conn.close()


def recent_trucks(user_id: int, limit: int = 30, order: str = "date"):
    conn = db()
    order_sql = "created_at DESC" if order == "date" else "yuklash COLLATE NOCASE ASC"
    rows = conn.execute(
        f"SELECT * FROM entries WHERE user_id=? AND kind='mashina' AND status != 'deleted' "
        f"ORDER BY {order_sql} LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return rows


def count_active_trucks(user_id: int) -> int:
    conn = db()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM entries WHERE user_id=? AND kind='mashina' AND status='active'",
        (user_id,),
    ).fetchone()
    conn.close()
    return row["c"] if row else 0


def get_entry(entry_id: int):
    conn = db()
    row = conn.execute("SELECT * FROM entries WHERE id=?", (entry_id,)).fetchone()
    conn.close()
    return row


def hard_delete_entry(entry_id: int):
    conn = db()
    conn.execute("DELETE FROM entries WHERE id=?", (entry_id,))
    conn.commit()
    conn.close()


def delete_old_trucks(user_id: int, days: int = 3) -> int:
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    conn = db()
    cur = conn.execute(
        "DELETE FROM entries WHERE user_id=? AND kind='mashina' AND created_at < ?",
        (user_id, cutoff),
    )
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def delete_all_trucks(user_id: int) -> int:
    conn = db()
    cur = conn.execute("DELETE FROM entries WHERE user_id=? AND kind='mashina'", (user_id,))
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def expire_old_entries(days: int = 7) -> int:
    """11-band: 7 kundan eski faol yozuvlarni avtomatik olib tashlaydi."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    conn = db()
    cur = conn.execute(
        "DELETE FROM entries WHERE created_at < ? AND status != 'deleted'", (cutoff,)
    )
    conn.commit()
    n = cur.rowcount
    conn.close()
    return n


def find_matching_trucks(user_id: int, yuklash: str, manzil: str):
    """12-band: yangi zaprosga mos saqlangan mashina bormi, tekshiradi."""
    if not yuklash or not manzil:
        return []
    conn = db()
    rows = conn.execute(
        "SELECT * FROM entries WHERE user_id=? AND kind='mashina' AND status='active' "
        "AND yuklash IS NOT NULL AND manzil IS NOT NULL",
        (user_id,),
    ).fetchall()
    conn.close()
    y_key, m_key = normalize_city(yuklash), normalize_city(manzil)
    return [
        r for r in rows
        if normalize_city(r["yuklash"]) == y_key and normalize_city(r["manzil"]) == m_key
    ]


def time_ago(created_at: str) -> str:
    try:
        then = datetime.fromisoformat(created_at)
    except Exception:
        return ""
    now = datetime.now(TZ) if TZ and then.tzinfo else datetime.now()
    delta = now - then if then.tzinfo == now.tzinfo else datetime.now() - then.replace(tzinfo=None)
    seconds = max(delta.total_seconds(), 0)
    if seconds < 3600:
        return f"{int(seconds // 60)} daqiqa oldin"
    if seconds < 86400:
        return f"{int(seconds // 3600)} soat oldin"
    return f"{int(seconds // 86400)} kun oldin"


def stats_between(start: datetime, end: datetime):
    conn = db()
    rows = conn.execute(
        "SELECT kind, COUNT(*) as cnt FROM entries WHERE created_at BETWEEN ? AND ? "
        "GROUP BY kind",
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    conn.close()
    return {r["kind"]: r["cnt"] for r in rows}


def all_entries():
    conn = db()
    rows = conn.execute("SELECT * FROM entries ORDER BY id DESC").fetchall()
    conn.close()
    return rows


# --------------------------------------------------------------------------
# Interfeys tillari (81-band: faqat bot matnlari uchun, yuk ma'lumoti emas)
# --------------------------------------------------------------------------

UI = {
    "thinking": {"uz": "Ishlanmoqda...", "ru": "\u041e\u0431\u0440\u0430\u0431\u0430\u0442\u044b\u0432\u0430\u044e..."},
    "need_more": {
        "uz": "Ma'lumot yetarli emas \U0001F914 Qayerdan, qayerga, necha tonna/kub ekanini aniqroq yozing.",
        "ru": "\u041d\u0435\u0434\u043e\u0441\u0442\u0430\u0442\u043e\u0447\u043d\u043e \u0434\u0430\u043d\u043d\u044b\u0445 \U0001F914 \u041d\u0430\u043f\u0438\u0448\u0438\u0442\u0435 \u043e\u0442\u043a\u0443\u0434\u0430, \u043a\u0443\u0434\u0430, \u0441\u043a\u043e\u043b\u044c\u043a\u043e \u0442\u043e\u043d\u043d/\u043a\u0443\u0431.",
    },
    "which_kind": {
        "uz": "Bu qaysi turdagi xabar?",
        "ru": "\u041a \u043a\u0430\u043a\u043e\u043c\u0443 \u0442\u0438\u043f\u0443 \u043e\u0442\u043d\u043e\u0441\u0438\u0442\u0441\u044f \u044d\u0442\u043e \u0441\u043e\u043e\u0431\u0449\u0435\u043d\u0438\u0435?",
    },
    "btn_zapros": {"uz": "\U0001F4E6 Zapros (yuk kerak)", "ru": "\U0001F4E6 \u0417\u0430\u043f\u0440\u043e\u0441 (\u043d\u0443\u0436\u0435\u043d \u0433\u0440\u0443\u0437)"},
    "btn_truck": {"uz": "\U0001F69B Mashina bor", "ru": "\U0001F69B \u0415\u0441\u0442\u044c \u043c\u0430\u0448\u0438\u043d\u0430"},
    "template_ready": {"uz": "Shablon tayyor:", "ru": "\u0428\u0430\u0431\u043b\u043e\u043d \u0433\u043e\u0442\u043e\u0432:"},
    "send_to_channel_q": {"uz": "Kanalga yuborilsinmi?", "ru": "\u041e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0432 \u043a\u0430\u043d\u0430\u043b?"},
    "save_to_list_q": {"uz": "Ro'yxatga saqlansinmi?", "ru": "\u0421\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c \u0432 \u0441\u043f\u0438\u0441\u043e\u043a?"},
    "btn_send": {"uz": "\u2705 Kanalga yuborish", "ru": "\u2705 \u041e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0432 \u043a\u0430\u043d\u0430\u043b"},
    "btn_save": {"uz": "\u2705 Ro'yxatga saqlash", "ru": "\u2705 \u0421\u043e\u0445\u0440\u0430\u043d\u0438\u0442\u044c"},
    "btn_edit": {"uz": "\u270F\uFE0F Tahrirlash", "ru": "\u270F\uFE0F \u0418\u0437\u043c\u0435\u043d\u0438\u0442\u044c"},
    "btn_cancel": {"uz": "\u274C Bekor qilish", "ru": "\u274C \u041e\u0442\u043c\u0435\u043d\u0430"},
    "cancelled": {"uz": "Bekor qilindi.", "ru": "\u041e\u0442\u043c\u0435\u043d\u0435\u043d\u043e."},
    "sent": {"uz": "Yuborildi:", "ru": "\u041e\u0442\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043e:"},
    "saved": {"uz": "Ro'yxatga saqlandi \u2705", "ru": "\u0421\u043e\u0445\u0440\u0430\u043d\u0435\u043d\u043e \u0432 \u0441\u043f\u0438\u0441\u043e\u043a \u2705"},
    "edit_prompt": {
        "uz": "\u270F\uFE0F To'g'irlangan matnni to'liq qayta yozib yuboring.",
        "ru": "\u270F\uFE0F \u041d\u0430\u043f\u0438\u0448\u0438\u0442\u0435 \u0438\u0441\u043f\u0440\u0430\u0432\u043b\u0435\u043d\u043d\u044b\u0439 \u0442\u0435\u043a\u0441\u0442 \u0437\u0430\u043d\u043e\u0432\u043e.",
    },
    "delete_btn": {"uz": "\U0001F5D1 Kanaldan o'chirish", "ru": "\U0001F5D1 \u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0438\u0437 \u043a\u0430\u043d\u0430\u043b\u0430"},
    "deleted": {"uz": "O'chirildi.", "ru": "\u0423\u0434\u0430\u043b\u0435\u043d\u043e."},
    "send_all": {"uz": "\u2705 Barchasini yuborish", "ru": "\u2705 \u041e\u0442\u043f\u0440\u0430\u0432\u0438\u0442\u044c \u0432\u0441\u0451"},
    "photo_need_key": {
        "uz": "\U0001F4F7 Rasmni o'qish uchun AI kaliti (ANTHROPIC_API_KEY) kerak. Hozircha xabarni matn qilib yozing.",
        "ru": "\U0001F4F7 \u0414\u043b\u044f \u0447\u0442\u0435\u043d\u0438\u044f \u0444\u043e\u0442\u043e \u043d\u0443\u0436\u0435\u043d \u043a\u043b\u044e\u0447 AI. \u041f\u043e\u043a\u0430 \u043d\u0430\u043f\u0438\u0448\u0438\u0442\u0435 \u0442\u0435\u043a\u0441\u0442\u043e\u043c.",
    },
    "reading_photo": {"uz": "\U0001F4F7 Rasm o'qilmoqda...", "ru": "\U0001F4F7 \u0427\u0438\u0442\u0430\u044e \u0444\u043e\u0442\u043e..."},
    "photo_failed": {"uz": "Rasmni o'qib bo'lmadi, qayta urinib ko'ring yoki matn yozing.", "ru": "\u041d\u0435 \u0443\u0434\u0430\u043b\u043e\u0441\u044c \u043f\u0440\u043e\u0447\u0438\u0442\u0430\u0442\u044c \u0444\u043e\u0442\u043e."},
    "photo_nothing": {"uz": "Rasmda yuk yoki mashina haqida matn topilmadi.", "ru": "\u041d\u0430 \u0444\u043e\u0442\u043e \u043d\u0435\u0442 \u0434\u0430\u043d\u043d\u044b\u0445 \u043e \u0433\u0440\u0443\u0437\u0435 \u0438\u043b\u0438 \u043c\u0430\u0448\u0438\u043d\u0435."},
    "no_trucks": {"uz": "Ro'yxat bo'sh.", "ru": "\u0421\u043f\u0438\u0441\u043e\u043a \u043f\u0443\u0441\u0442."},
    "truck_count": {"uz": "ta faol mashina", "ru": "\u0430\u043a\u0442\u0438\u0432\u043d\u044b\u0445 \u043c\u0430\u0448\u0438\u043d"},
    "sort_by_date": {"uz": "\U0001F4C5 Sana bo'yicha", "ru": "\U0001F4C5 \u041f\u043e \u0434\u0430\u0442\u0435"},
    "sort_by_city": {"uz": "\U0001F4CD Shahar bo'yicha", "ru": "\U0001F4CD \u041f\u043e \u0433\u043e\u0440\u043e\u0434\u0443"},
    "clear_old": {"uz": "\U0001F9F9 Eskilarini tozalash", "ru": "\U0001F9F9 \u041e\u0447\u0438\u0441\u0442\u0438\u0442\u044c \u0441\u0442\u0430\u0440\u044b\u0435"},
    "clear_all": {"uz": "\U0001F5D1 Barchasini o'chirish", "ru": "\U0001F5D1 \u0423\u0434\u0430\u043b\u0438\u0442\u044c \u0432\u0441\u0435"},
    "truck_delete": {"uz": "\U0001F5D1 O'chirish", "ru": "\U0001F5D1 \u0423\u0434\u0430\u043b\u0438\u0442\u044c"},
    "truck_edit": {"uz": "\u270F\uFE0F Tahrirlash", "ru": "\u270F\uFE0F \u0418\u0437\u043c\u0435\u043d\u0438\u0442\u044c"},
    "truck_mark_taken": {"uz": "\U0001F534 Band qildim", "ru": "\U0001F534 \u0417\u0430\u043d\u044f\u0442\u043e"},
    "truck_mark_free": {"uz": "\U0001F7E2 Bo'shadi", "ru": "\U0001F7E2 \u0421\u0432\u043e\u0431\u043e\u0434\u043d\u043e"},
    "truck_copy": {"uz": "\U0001F4CB Nusxalash", "ru": "\U0001F4CB \u041a\u043e\u043f\u0438\u0440\u043e\u0432\u0430\u0442\u044c"},
    "truck_post": {"uz": "\U0001F4E4 Kanalga joylash", "ru": "\U0001F4E4 \u041e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u0442\u044c \u0432 \u043a\u0430\u043d\u0430\u043b"},
    "truck_deleted": {"uz": "O'chirildi \u2705", "ru": "\u0423\u0434\u0430\u043b\u0435\u043d\u043e \u2705"},
    "truck_status_band": {"uz": "\U0001F534 BAND", "ru": "\U0001F534 \u0417\u0410\u041d\u042f\u0422\u041e"},
    "truck_status_free": {"uz": "\U0001F7E2 BO'SH", "ru": "\U0001F7E2 \u0421\u0412\u041e\u0411\u041e\u0414\u041d\u041e"},
    "cleared_old": {"uz": "yozuv o'chirildi (3 kundan eski)", "ru": "\u0437\u0430\u043f\u0438\u0441\u0435\u0439 \u0443\u0434\u0430\u043b\u0435\u043d\u043e (\u0441\u0442\u0430\u0440\u0448\u0435 3 \u0434\u043d\u0435\u0439)"},
    "cleared_all": {"uz": "yozuv butunlay o'chirildi", "ru": "\u0437\u0430\u043f\u0438\u0441\u0435\u0439 \u043f\u043e\u043b\u043d\u043e\u0441\u0442\u044c\u044e \u0443\u0434\u0430\u043b\u0435\u043d\u043e"},
    "match_found": {
        "uz": "\U0001F50E Ro'yxatingizda mos keladigan mashina bor:",
        "ru": "\U0001F50E \u0412 \u0432\u0430\u0448\u0435\u043c \u0441\u043f\u0438\u0441\u043a\u0435 \u0435\u0441\u0442\u044c \u043f\u043e\u0434\u0445\u043e\u0434\u044f\u0449\u0430\u044f \u043c\u0430\u0448\u0438\u043d\u0430:",
    },
    "posted_to_channel": {"uz": "Kanalga joylandi \u2705", "ru": "\u041e\u043f\u0443\u0431\u043b\u0438\u043a\u043e\u0432\u0430\u043d\u043e \u0432 \u043a\u0430\u043d\u0430\u043b \u2705"},
    "copied": {"uz": "Matn quyida, nusxalab oling:", "ru": "\u0422\u0435\u043a\u0441\u0442 \u043d\u0438\u0436\u0435, \u0441\u043a\u043e\u043f\u0438\u0440\u0443\u0439\u0442\u0435:"},
}


def t(key: str, user_id: int) -> str:
    lang = get_lang(user_id)
    return UI.get(key, {}).get(lang, UI.get(key, {}).get("uz", key))


# --------------------------------------------------------------------------
# Joylar: shahar/davlat lug'ati (nom + davlat + koordinata). AI kalitsiz ham
# shaharni topish, shahar yoniga davlatni yozish va masofani hisoblash uchun.
# Yangi shahar qo'shish: PLACES ro'yxatiga bitta qator qo'shing.
# --------------------------------------------------------------------------

# (ko'rsatiladigan nom, davlat, (lat, lon), [yozilish variantlari])
PLACES = [
    # --- O'zbekiston ---
    ("Тошкент", "Ўзбекистон", (41.2995, 69.2401), ["toshkent", "tashkent", "ташкент", "тошкент"]),
    ("Самарқанд", "Ўзбекистон", (39.6542, 66.9597), ["samarqand", "samarkand", "самарканд", "самарқанд"]),
    ("Бухоро", "Ўзбекистон", (39.7747, 64.4286), ["buxoro", "bukhara", "buxara", "бухара", "бухоро"]),
    ("Қарши", "Ўзбекистон", (38.8606, 65.7891), ["qarshi", "karshi", "карши", "қарши"]),
    ("Термиз", "Ўзбекистон", (37.2242, 67.2783), ["termiz", "termez", "термез", "термиз"]),
    ("Нукус", "Ўзбекистон", (42.4600, 59.6100), ["nukus", "нукус"]),
    ("Урганч", "Ўзбекистон", (41.5500, 60.6333), ["urganch", "urgench", "ургенч", "урганч"]),
    ("Хива", "Ўзбекистон", (41.3783, 60.3639), ["xiva", "khiva", "хива"]),
    ("Наманган", "Ўзбекистон", (40.9983, 71.6726), ["namangan", "наманган"]),
    ("Андижон", "Ўзбекистон", (40.7833, 72.3333), ["andijon", "andijan", "андижан", "андижон"]),
    ("Фарғона", "Ўзбекистон", (40.3833, 71.7833), ["farg'ona", "fargona", "fergana", "фергана", "фарғона"]),
    ("Жиззах", "Ўзбекистон", (40.1158, 67.8422), ["jizzax", "jizzakh", "dzhizak", "джизак", "жиззах"]),
    ("Гулистон", "Ўзбекистон", (40.4897, 68.7842), ["guliston", "gulistan", "гулистан", "гулистон"]),
    ("Навоий", "Ўзбекистон", (40.1030, 65.3686), ["navoiy", "navoi", "навои", "навоий"]),
    ("Ангрен", "Ўзбекистон", (41.0167, 70.1436), ["angren", "ангрен"]),
    ("Чирчиқ", "Ўзбекистон", (41.4689, 69.5822), ["chirchiq", "chirchik", "чирчик", "чирчиқ"]),
    ("Олмалиқ", "Ўзбекистон", (40.8447, 69.5983), ["olmaliq", "almalyk", "алмалык", "олмалик", "олмалиқ"]),
    ("Марғилон", "Ўзбекистон", (40.4711, 71.7246), ["margilon", "margilan", "маргилан", "марғилон"]),
    ("Қўқон", "Ўзбекистон", (40.5286, 70.9425), ["qo'qon", "qoqon", "kokand", "коканд", "қўқон"]),
    ("Денов", "Ўзбекистон", (38.2667, 67.9000), ["denov", "денов"]),
    ("Шаҳрисабз", "Ўзбекистон", (39.0578, 66.8342), ["shahrisabz", "шахрисабз", "шаҳрисабз"]),
    # --- Қозоғистон ---
    ("Алматы", "Қозоғистон", (43.2220, 76.8512), ["almaty", "алматы"]),
    ("Астана", "Қозоғистон", (51.1694, 71.4491), ["astana", "астана"]),
    ("Шымкент", "Қозоғистон", (42.3417, 69.5901), ["shymkent", "shimkent", "chimkent", "шымкент", "чимкент"]),
    ("Актау", "Қозоғистон", (43.6500, 51.1500), ["aktau", "актау"]),
    ("Актобе", "Қозоғистон", (50.2839, 57.1670), ["aktobe", "актобе"]),
    ("Қарағанда", "Қозоғистон", (49.8060, 73.0850), ["karaganda", "qaraganda", "караганда"]),
    # --- Қирғизистон ---
    ("Бишкек", "Қирғизистон", (42.8746, 74.5698), ["bishkek", "бишкек"]),
    ("Ош", "Қирғизистон", (40.5283, 72.7985), ["osh", "ош"]),
    ("Иркештам", "Қирғизистон", (39.6833, 73.9333), ["irkeshtam", "иркештам"]),
    # --- Хитой ---
    ("Иу", "Хитой", (29.3060, 120.0762), ["yiwu", "iwu", "иу", "ию"]),
    ("Алашанькоу", "Хитой", (45.1800, 82.5700), ["alashankou", "alashankov", "алашанькоу", "алашанкоу"]),
    ("Улугчат", "Хитой", (39.7200, 75.2500), ["ulug'chat", "ulugchat", "ulugqat", "wuqia", "улугчат", "улуғчат"]),
    ("Урумчи", "Хитой", (43.8256, 87.6168), ["urumqi", "urumchi", "урумчи"]),
    ("Гуанчжоу", "Хитой", (23.1291, 113.2644), ["guangzhou", "guanchjou", "гуанчжоу", "гуанджоу"]),
    ("Кашгар", "Хитой", (39.4704, 75.9898), ["kashgar", "qashqar", "кашгар"]),
    ("Шанхай", "Хитой", (31.2304, 121.4737), ["shanghai", "shanxay", "шанхай"]),
    # --- Россия ---
    ("Москва", "Россия", (55.7558, 37.6173), ["moskva", "moscow", "москва"]),
    ("Санкт-Петербург", "Россия", (59.9343, 30.3351), ["peterburg", "piter", "петербург", "питер"]),
    ("Пятигорск", "Россия", (44.0486, 43.0594), ["pyatigorsk", "pyatigorsk", "пятигорск"]),
    ("Краснодар", "Россия", (45.0355, 38.9753), ["krasnodar", "краснодар"]),
    ("Новосибирск", "Россия", (55.0084, 82.9357), ["novosibirsk", "новосибирск"]),
    ("Екатеринбург", "Россия", (56.8389, 60.6057), ["yekaterinburg", "ekaterinburg", "екатеринбург"]),
    ("Казань", "Россия", (55.7887, 49.1221), ["kazan", "казань"]),
    ("Ростов", "Россия", (47.2357, 39.7015), ["rostov", "ростов"]),
    ("Самара", "Россия", (53.1959, 50.1002), ["samara", "самара"]),
    ("Волгоград", "Россия", (48.7080, 44.5133), ["volgograd", "волгоград"]),
    ("Челябинск", "Россия", (55.1644, 61.4368), ["chelyabinsk", "челябинск"]),
    ("Омск", "Россия", (54.9885, 73.3242), ["omsk", "омск"]),
    ("Уфа", "Россия", (54.7388, 55.9721), ["ufa", "уфа"]),
    ("Ставрополь", "Россия", (45.0428, 41.9734), ["stavropol", "ставрополь"]),
    ("Новороссийск", "Россия", (44.7235, 37.7687), ["novorossiysk", "новороссийск"]),
    ("Астрахань", "Россия", (46.3497, 48.0408), ["astrakhan", "astraxan", "астрахань"]),
    ("Воронеж", "Россия", (51.6720, 39.1843), ["voronezh", "воронеж"]),
    # --- Беларусь ---
    ("Минск", "Беларусь", (53.9006, 27.5590), ["minsk", "минск"]),
    ("Брест", "Беларусь", (52.0976, 23.7341), ["brest", "брест"]),
    # --- Польша ---
    ("Варшава", "Польша", (52.2297, 21.0122), ["varshava", "warsaw", "варшава"]),
    ("Малашевичи", "Польша", (52.2000, 23.5500), ["malashevichi", "малашевичи"]),
    # --- Чехия ---
    ("Прага", "Чехия", (50.0755, 14.4378), ["praga", "prague", "прага"]),
    ("Брно", "Чехия", (49.1951, 16.6068), ["brno", "брно"]),
    # --- Германия ---
    ("Берлин", "Германия", (52.5200, 13.4050), ["berlin", "берлин"]),
    ("Гамбург", "Германия", (53.5511, 9.9937), ["hamburg", "gamburg", "гамбург"]),
    ("Мюнхен", "Германия", (48.1351, 11.5820), ["munich", "myunxen", "мюнхен"]),
    ("Франкфурт", "Германия", (50.1109, 8.6821), ["frankfurt", "франкфурт"]),
    # --- Туркия ---
    ("Стамбул", "Туркия", (41.0082, 28.9784), ["istanbul", "istambul", "stambul", "стамбул"]),
    ("Анкара", "Туркия", (39.9334, 32.8597), ["ankara", "анкара"]),
    ("Мерсин", "Туркия", (36.8121, 34.6415), ["mersin", "мерсин"]),
    ("Измир", "Туркия", (38.4237, 27.1428), ["izmir", "измир"]),
    # --- Кавказ, Ўрта Осиё, Эрон, Афғонистон ---
    ("Баку", "Озарбайжон", (40.4093, 49.8671), ["boku", "baku", "баку"]),
    ("Тбилиси", "Грузия", (41.7151, 44.8271), ["tbilisi", "тбилиси"]),
    ("Поти", "Грузия", (42.1467, 41.6719), ["poti", "поти"]),
    ("Батуми", "Грузия", (41.6168, 41.6367), ["batumi", "батуми"]),
    ("Душанбе", "Тожикистон", (38.5598, 68.7870), ["dushanbe", "душанбе"]),
    ("Хужанд", "Тожикистон", (40.2826, 69.6220), ["xo'jand", "xujand", "hujand", "худжанд", "хужанд"]),
    ("Ашхабад", "Туркманистон", (37.9601, 58.3261), ["ashgabat", "ashxabod", "ашхабад"]),
    ("Кобул", "Афғонистон", (34.5553, 69.2075), ["kobul", "kabul", "кабул", "кобул"]),
    ("Хайратон", "Афғонистон", (37.2333, 67.7833), ["hayraton", "hairatan", "хайратон"]),
    ("Тахрон", "Эрон", (35.6892, 51.3890), ["tehron", "tehran", "тегеран", "тахрон"]),
    ("Ереван", "Арманистон", (40.1792, 44.4991), ["yerevan", "erevan", "ереван", "эревань"]),
    ("Хоргос", "Хитой", (44.2130, 80.4060), ["xorgos", "khorgos", "horgos", "хоргос"]),
    ("Ижевск", "Россия", (56.8526, 53.2115), ["izhevsk", "ижевск"]),
    ("Гомель", "Беларусь", (52.4345, 30.9754), ["gomel", "гомель", "гомел"]),
    ("Витебск", "Беларусь", (55.1904, 30.2049), ["vitebsk", "витебск"]),
    ("Махачкала", "Россия", (42.9849, 47.5047), ["mahachkala", "makhachkala", "махачкала"]),
    ("Вологда", "Россия", (59.2181, 39.8886), ["vologda", "вологда"]),
    ("Умёт", "Россия", (52.2667, 42.7333), ["umyot", "umet", "умёт", "умет"]),
    ("Тамбов", "Россия", (52.7213, 41.4523), ["tambov", "тамбов"]),
    ("Рязань", "Россия", (54.6296, 39.7412), ["ryazan", "рязань"]),
    ("Смоленск", "Россия", (54.7818, 32.0401), ["smolensk", "смоленск"]),
    ("Брянск", "Россия", (53.2521, 34.3717), ["bryansk", "брянск"]),
    ("Коряжма", "Россия", (61.3167, 47.1500), ["koryazhma", "коряжма"]),
    ("Иваново", "Россия", (57.0000, 40.9739), ["ivanovo", "иваново", "иванова"]),
    ("Ярославль", "Россия", (57.6261, 39.8845), ["yaroslavl", "ярославль", "ярославл"]),
    ("Саратов", "Россия", (51.5924, 46.0348), ["saratov", "саратов"]),
    ("Оренбург", "Россия", (51.7727, 55.0988), ["orenburg", "оренбург"]),
    ("Щёлково", "Россия", (55.9166, 38.0007), ["shchyolkovo", "shelkovo", "щёлково", "щелково"]),
    ("Вильнюс", "Литва", (54.6872, 25.2797), ["vilnius", "вильнюс"]),
    ("Каунас", "Литва", (54.8985, 23.9036), ["kaunas", "каунас"]),
    ("Клайпеда", "Литва", (55.7033, 21.1443), ["клайпеда"]),
    ("Шяуляй", "Литва", (55.9349, 23.3143), ["шяуляй"]),
    ("Паневежис", "Литва", (55.7333, 24.3667), ["паневежис"]),
    ("Алитус", "Литва", (54.3967, 24.0492), ["алитус"]),
    ("Мариямполе", "Литва", (54.56, 23.35), ["мариямполе"]),
    ("Мажейкяй", "Литва", (56.3097, 22.335), ["мажейкяй"]),
    ("Йонава", "Литва", (55.0833, 24.2833), ["йонава"]),
    ("Утена", "Литва", (55.4975, 25.6011), ["утена"]),
    ("Рига", "Латвия", (56.9496, 24.1052), ["рига", "riga"]),
    ("Даугавпилс", "Латвия", (55.8747, 26.5363), ["даугавпилс"]),
    ("Лиепая", "Латвия", (56.5047, 21.0108), ["лиепая"]),
    ("Елгава", "Латвия", (56.6511, 23.7214), ["елгава"]),
    ("Юрмала", "Латвия", (56.968, 23.77), ["юрмала"]),
    ("Вентспилс", "Латвия", (57.3894, 21.5606), ["вентспилс"]),
    ("Резекне", "Латвия", (56.51, 27.34), ["резекне"]),
    ("Валмиера", "Латвия", (57.5406, 25.4278), ["валмиера"]),
    ("Екабпилс", "Латвия", (56.5, 25.8667), ["екабпилс"]),
    ("Огре", "Латвия", (56.8181, 24.6083), ["огре"]),
    ("Кёльн", "Германия", (50.9375, 6.9603), ["кёльн", "koln", "cologne", "kyoln"]),
    ("Штутгарт", "Германия", (48.7758, 9.1829), ["штутгарт", "stuttgart"]),
    ("Дюссельдорф", "Германия", (51.2277, 6.7735), ["дюссельдорф", "dusseldorf"]),
    ("Лейпциг", "Германия", (51.3397, 12.3731), ["лейпциг", "leipzig"]),
    ("Дортмунд", "Германия", (51.5136, 7.4653), ["дортмунд", "dortmund"]),
    ("Эссен", "Германия", (51.4556, 7.0116), ["эссен", "essen"]),
    ("Бремен", "Германия", (53.0793, 8.8017), ["бремен", "bremen"]),
    ("Дрезден", "Германия", (51.0504, 13.7373), ["дрезден", "dresden"]),
    ("Ганновер", "Германия", (52.3759, 9.732), ["ганновер", "hannover", "hanover"]),
    ("Нюрнберг", "Германия", (49.4521, 11.0767), ["нюрнберг", "nurnberg", "nuremberg"]),
    ("Дуйсбург", "Германия", (51.4344, 6.7623), ["дуйсбург", "duisburg"]),
    ("Бурса", "Туркия", (40.1826, 29.0665), ["бурса", "bursa"]),
    ("Анталья", "Туркия", (36.8969, 30.7133), ["анталья", "antalya"]),
    ("Адана", "Туркия", (37.0, 35.3213), ["адана", "adana"]),
    ("Газиантеп", "Туркия", (37.0662, 37.3833), ["газиантеп", "gaziantep"]),
    ("Конья", "Туркия", (37.8746, 32.4932), ["конья", "konya"]),
    ("Кайсери", "Туркия", (38.7312, 35.4787), ["кайсери", "kayseri"]),
    ("Эскишехир", "Туркия", (39.7767, 30.5206), ["эскишехир", "eskisehir"]),
    ("Диярбакыр", "Туркия", (37.9144, 40.2306), ["диярбакыр", "diyarbakir"]),
    ("Самсун", "Туркия", (41.2867, 36.33), ["самсун", "samsun"]),
    ("Денизли", "Туркия", (37.7765, 29.0864), ["денизли", "denizli"]),
    ("Шанлыурфа", "Туркия", (37.1591, 38.7969), ["шанлыурфа", "sanliurfa", "urfa"]),
    ("Караганда", "Қозоғистон", (49.806, 73.085), ["караганда", "karaganda", "qaraganda"]),
    ("Тараз", "Қозоғистон", (42.9, 71.3667), ["тараз", "taraz"]),
    ("Павлодар", "Қозоғистон", (52.2873, 76.9674), ["павлодар", "pavlodar"]),
    ("Усть-Каменогорск", "Қозоғистон", (49.9714, 82.6053), ["усть-каменогорск", "ust-kamenogorsk", "oskemen", "усть каменогорск"]),
    ("Семей", "Қозоғистон", (50.4111, 80.2275), ["семей", "semey", "semipalatinsk"]),
    ("Атырау", "Қозоғистон", (47.1164, 51.883), ["атырау", "atyrau"]),
    ("Костанай", "Қозоғистон", (53.2144, 63.6246), ["костанай", "kostanay"]),
    ("Кызылорда", "Қозоғистон", (44.8479, 65.4999), ["кызылорда", "kyzylorda", "qyzylorda"]),
    ("Уральск", "Қозоғистон", (51.2333, 51.3667), ["уральск", "uralsk", "oral"]),
    ("Петропавловск", "Қозоғистон", (54.8667, 69.15), ["петропавловск", "petropavlovsk", "petropavl"]),
    ("Джалал-Абад", "Қирғизистон", (40.9333, 72.9833), ["джалал-абад", "jalal-abad", "jalalabad"]),
    ("Каракол", "Қирғизистон", (42.49, 78.39), ["каракол", "karakol"]),
    ("Токмок", "Қирғизистон", (42.8433, 75.2925), ["токмок", "tokmok"]),
    ("Узген", "Қирғизистон", (40.7667, 73.3), ["узген", "uzgen"]),
    ("Балыкчи", "Қирғизистон", (42.46, 76.19), ["балыкчи", "balykchy"]),
    ("Кара-Балта", "Қирғизистон", (42.8333, 73.85), ["кара-балта", "kara-balta"]),
    ("Нарын", "Қирғизистон", (41.4288, 75.9911), ["нарын", "naryn"]),
    ("Талас", "Қирғизистон", (42.5228, 72.2419), ["талас", "talas"]),
    ("Могилёв", "Беларусь", (53.9, 30.33), ["могилёв", "mogilev", "могилев"]),
    ("Гродно", "Беларусь", (53.6884, 23.8258), ["гродно", "grodno"]),
    ("Бобруйск", "Беларусь", (53.15, 29.2167), ["бобруйск", "bobruisk", "bobruysk"]),
    ("Барановичи", "Беларусь", (53.1333, 26.0167), ["барановичи", "baranovichi"]),
    ("Борисов", "Беларусь", (54.2333, 28.5), ["борисов", "borisov"]),
    ("Пинск", "Беларусь", (52.1167, 26.1), ["пинск", "pinsk"]),
    ("Орша", "Беларусь", (54.5081, 30.4172), ["орша", "orsha"]),
    ("Мозырь", "Беларусь", (52.05, 29.25), ["мозырь", "mozyr"]),
    ("Солигорск", "Беларусь", (52.7833, 27.5333), ["солигорск", "soligorsk"]),
    ("Новополоцк", "Беларусь", (55.5333, 28.6667), ["новополоцк", "novopolotsk"]),
    ("Лида", "Беларусь", (53.8833, 25.3), ["лида", "lida"]),
    ("Молодечно", "Беларусь", (54.3167, 26.85), ["молодечно", "molodechno"]),
    ("Полоцк", "Беларусь", (55.4833, 28.7833), ["полоцк", "polotsk"]),
    ("Жлобин", "Беларусь", (52.8931, 30.0347), ["жлобин", "zhlobin"]),
    ("Светлогорск", "Беларусь", (52.6333, 29.7333), ["светлогорск", "svetlogorsk"]),
    ("Речица", "Беларусь", (52.3667, 30.4), ["речица", "rechitsa"]),
    ("Слуцк", "Беларусь", (53.0167, 27.55), ["слуцк", "slutsk"]),
    ("Жодино", "Беларусь", (54.1, 28.3333), ["жодино", "zhodino"]),
    ("Слоним", "Беларусь", (53.0917, 25.3197), ["слоним", "slonim"]),
    ("Кобрин", "Беларусь", (52.2139, 24.3597), ["кобрин", "kobrin"]),
    ("Волковыск", "Беларусь", (53.1633, 24.4553), ["волковыск", "volkovysk"]),
    ("Калинковичи", "Беларусь", (52.125, 29.3272), ["калинковичи", "kalinkovichi"]),
    ("Сморгонь", "Беларусь", (54.4708, 26.3986), ["сморгонь", "smorgon"]),
    ("Рогачёв", "Беларусь", (53.0794, 30.0492), ["рогачёв", "rogachev"]),
    ("Осиповичи", "Беларусь", (53.3167, 28.65), ["осиповичи", "osipovichi"]),
    ("Дзержинск", "Беларусь", (53.6833, 27.1333), ["дзержинск", "dzerzhinsk"]),
    ("Новогрудок", "Беларусь", (53.5975, 25.8258), ["новогрудок", "novogrudok"]),
    ("Лунинец", "Беларусь", (52.25, 26.8), ["лунинец", "luninets"]),
    ("Ивацевичи", "Беларусь", (52.7167, 25.3333), ["ивацевичи", "ivatsevichi"]),
    ("Кричев", "Беларусь", (53.6833, 31.7167), ["кричев", "krichev"]),
    ("Ганцевичи", "Беларусь", (52.7667, 26.4333), ["ганцевичи", "gantsevichi"]),
    ("Нижний Новгород", "Россия", (56.2965, 43.9361), ["нижний новгород", "nizhny novgorod", "nizhniy novgorod"]),
    ("Красноярск", "Россия", (56.0184, 92.8672), ["красноярск", "krasnoyarsk"]),
    ("Пермь", "Россия", (58.0105, 56.2502), ["пермь", "perm"]),
    ("Тюмень", "Россия", (57.1522, 65.5272), ["тюмень", "tyumen"]),
    ("Тольятти", "Россия", (53.5303, 49.3461), ["тольятти", "tolyatti"]),
    ("Барнаул", "Россия", (53.3606, 83.7636), ["барнаул", "barnaul"]),
    ("Ульяновск", "Россия", (54.3142, 48.4031), ["ульяновск", "ulyanovsk"]),
    ("Иркутск", "Россия", (52.2869, 104.305), ["иркутск", "irkutsk"]),
    ("Хабаровск", "Россия", (48.4827, 135.084), ["хабаровск", "khabarovsk"]),
    ("Владивосток", "Россия", (43.1155, 131.8855), ["владивосток", "vladivostok"]),
    ("Томск", "Россия", (56.4977, 84.9744), ["томск", "tomsk"]),
    ("Кемерово", "Россия", (55.3333, 86.0833), ["кемерово", "kemerovo"]),
    ("Новокузнецк", "Россия", (53.7557, 87.1099), ["новокузнецк", "novokuznetsk"]),
    ("Набережные Челны", "Россия", (55.7436, 52.3958), ["набережные челны", "naberezhnye chelny"]),
    ("Пенза", "Россия", (53.2007, 45.0046), ["пенза", "penza"]),
    ("Липецк", "Россия", (52.6031, 39.5708), ["липецк", "lipetsk"]),
    ("Киров", "Россия", (58.6035, 49.6679), ["киров", "kirov"]),
    ("Чебоксары", "Россия", (56.1439, 47.2489), ["чебоксары", "cheboksary"]),
    ("Тула", "Россия", (54.1931, 37.6177), ["тула", "tula"]),
    ("Калининград", "Россия", (54.7104, 20.4522), ["калининград", "kaliningrad"]),
    ("Курск", "Россия", (51.7304, 36.1926), ["курск", "kursk"]),
    ("Магнитогорск", "Россия", (53.4187, 58.9805), ["магнитогорск", "magnitogorsk"]),
    ("Тверь", "Россия", (56.8587, 35.9176), ["тверь", "tver"]),
    ("Нижний Тагил", "Россия", (57.9099, 59.9818), ["нижний тагил", "nizhny tagil"]),
    ("Белгород", "Россия", (50.5977, 36.5858), ["белгород", "belgorod"]),
    ("Архангельск", "Россия", (64.5401, 40.5433), ["архангельск", "arkhangelsk"]),
    ("Владимир", "Россия", (56.129, 40.407), ["владимир", "vladimir"]),
    ("Сочи", "Россия", (43.6028, 39.7342), ["сочи", "sochi"]),
    ("Калуга", "Россия", (54.5293, 36.2754), ["калуга", "kaluga"]),
    ("Чита", "Россия", (52.034, 113.4994), ["чита", "chita"]),
    ("Орёл", "Россия", (52.9651, 36.0785), ["орёл", "orel", "oryol"]),
    ("Волжский", "Россия", (48.7808, 44.7767), ["волжский", "volzhsky"]),
    ("Череповец", "Россия", (59.1269, 37.9089), ["череповец", "cherepovets"]),
    ("Саранск", "Россия", (54.1838, 45.1749), ["саранск", "saransk"]),
    ("Владикавказ", "Россия", (43.0241, 44.682), ["владикавказ", "vladikavkaz"]),
    ("Мурманск", "Россия", (68.9585, 33.0827), ["мурманск", "murmansk"]),
    ("Сургут", "Россия", (61.25, 73.4167), ["сургут", "surgut"]),
    ("Грозный", "Россия", (43.3178, 45.6947), ["грозный", "grozny"]),
    ("Якутск", "Россия", (62.0281, 129.7322), ["якутск", "yakutsk"]),
    ("Кострома", "Россия", (57.7665, 40.9269), ["кострома", "kostroma"]),
    ("Петрозаводск", "Россия", (61.7849, 34.3469), ["петрозаводск", "petrozavodsk"]),
    ("Нижневартовск", "Россия", (60.9397, 76.5694), ["нижневартовск", "nizhnevartovsk"]),
    ("Йошкар-Ола", "Россия", (56.6389, 47.8994), ["йошкар-ола", "yoshkar-ola"]),
    ("Лондон", "Буюк Британия", (51.5072, -0.1276), ["лондон", "london"]),
    ("Париж", "Франция", (48.8566, 2.3522), ["париж", "paris"]),
    ("Рим", "Италия", (41.9028, 12.4964), ["рим", "rim", "rome"]),
    ("Милан", "Италия", (45.4642, 9.19), ["милан", "milan"]),
    ("Венеция", "Италия", (45.4408, 12.3155), ["венеция", "venice", "venezia"]),
    ("Барселона", "Испания", (41.3851, 2.1734), ["барселона", "barcelona"]),
    ("Мадрид", "Испания", (40.4168, -3.7038), ["мадрид", "madrid"]),
    ("Амстердам", "Нидерландия", (52.3676, 4.9041), ["амстердам", "amsterdam"]),
    ("Брюссель", "Бельгия", (50.8503, 4.3517), ["брюссель", "brussels", "bryussel"]),
    ("Вена", "Австрия", (48.2082, 16.3738), ["вена", "vienna", "vena"]),
    ("Будапешт", "Венгрия", (47.4979, 19.0402), ["будапешт", "budapest"]),
    ("Стокгольм", "Швеция", (59.3293, 18.0686), ["стокгольм", "stockholm"]),
    ("Осло", "Норвегия", (59.9139, 10.7522), ["осло", "oslo"]),
    ("Копенгаген", "Дания", (55.6761, 12.5683), ["копенгаген", "copenhagen"]),
    ("Хельсинки", "Финляндия", (60.1699, 24.9384), ["хельсинки", "helsinki"]),
    ("Афина", "Греция", (37.9838, 23.7275), ["афина", "athens", "afiny"]),
    ("Дубай", "Бирлашган Араб Амирликлари", (25.2048, 55.2708), ["дубай", "dubai", "dubay"]),
    ("Абу-Даби", "Бирлашган Араб Амирликлари", (24.4539, 54.3773), ["абу-даби", "abu dhabi", "abu-dabi"]),
    ("Эр-Риёд", "Саудия Арабистони", (24.7136, 46.6753), ["эр-риёд", "riyadh", "er-riyod"]),
    ("Қоҳира", "Миср", (30.0444, 31.2357), ["қоҳира", "cairo", "qohira", "каир"]),
    ("Касабланка", "Марокко", (33.5731, -7.5898), ["касабланка", "casablanca"]),
    ("Кейптаун", "Жанубий Африка", (-33.9249, 18.4241), ["кейптаун", "cape town", "keyptaun"]),
    ("Йоханнесбург", "Жанубий Африка", (-26.2041, 28.0473), ["йоханнесбург", "johannesburg"]),
    ("Найроби", "Кения", (-1.2921, 36.8219), ["найроби", "nairobi"]),
    ("Лагос", "Нигерия", (6.5244, 3.3792), ["лагос", "lagos"]),
    ("Нью-Йорк", "АҚШ", (40.7128, -74.006), ["нью-йорк", "new york", "nyu-york"]),
    ("Лос-Анжелес", "АҚШ", (34.0522, -118.2437), ["лос-анжелес", "los angeles"]),
    ("Чикаго", "АҚШ", (41.8781, -87.6298), ["чикаго", "chicago"]),
    ("Сан-Франциско", "АҚШ", (37.7749, -122.4194), ["сан-франциско", "san francisco"]),
    ("Майами", "АҚШ", (25.7617, -80.1918), ["майами", "miami"]),
    ("Вашингтон", "АҚШ", (38.9072, -77.0369), ["вашингтон", "washington"]),
    ("Торонто", "Канада", (43.6532, -79.3832), ["торонто", "toronto"]),
    ("Ванкувер", "Канада", (49.2827, -123.1207), ["ванкувер", "vancouver"]),
    ("Монреаль", "Канада", (45.5019, -73.5674), ["монреаль", "montreal"]),
    ("Мехико", "Мексика", (19.4326, -99.1332), ["мехико", "mexico city", "mехiko"]),
    ("Рио-де-Жанейро", "Бразилия", (-22.9068, -43.1729), ["рио-де-жанейро", "rio de janeiro", "rio"]),
    ("Сан-Паулу", "Бразилия", (-23.5505, -46.6333), ["сан-паулу", "sao paulo"]),
    ("Буэнос-Айрес", "Аргентина", (-34.6037, -58.3816), ["буэнос-айрес", "buenos aires"]),
    ("Сантьяго", "Чили", (-33.4489, -70.6693), ["сантьяго", "santiago"]),
    ("Лима", "Перу", (-12.0464, -77.0428), ["лима", "lima"]),
    ("Богота", "Колумбия", (4.711, -74.0721), ["богота", "bogota"]),
    ("Токио", "Япония", (35.6762, 139.6503), ["токио", "tokyo"]),
    ("Осака", "Япония", (34.6937, 135.5023), ["осака", "osaka"]),
    ("Киото", "Япония", (35.0116, 135.7681), ["киото", "kyoto"]),
    ("Сеул", "Жанубий Корея", (37.5665, 126.978), ["сеул", "seoul"]),
    ("Пекин", "Хитой", (39.9042, 116.4074), ["пекин", "beijing", "pekin"]),
    ("Гонконг", "Хитой", (22.3193, 114.1694), ["гонконг", "hong kong", "gonkong"]),
    ("Сингапур", "Сингапур", (1.3521, 103.8198), ["сингапур", "singapore"]),
    ("Бангкок", "Таиланд", (13.7563, 100.5018), ["бангкок", "bangkok"]),
    ("Хошимин", "Вьетнам", (10.8231, 106.6297), ["хошимин", "ho chi minh"]),
    ("Ханой", "Вьетнам", (21.0278, 105.8342), ["ханой", "hanoi"]),
    ("Джакарта", "Индонезия", (-6.2088, 106.8456), ["джакарта", "jakarta"]),
    ("Куала-Лумпур", "Малайзия", (3.139, 101.6869), ["куала-лумпур", "kuala lumpur"]),
    ("Манила", "Филиппин", (14.5995, 120.9842), ["манила", "manila"]),
    ("Дели", "Ҳиндистон", (28.7041, 77.1025), ["дели", "delhi"]),
    ("Мумбай", "Ҳиндистон", (19.076, 72.8777), ["мумбай", "mumbai", "bombay"]),
    ("Бангалор", "Ҳиндистон", (12.9716, 77.5946), ["бангалор", "bangalore", "bengaluru"]),
    ("Калькутта", "Ҳиндистон", (22.5726, 88.3639), ["калькутта", "kolkata", "calcutta"]),
    ("Сидней", "Австралия", (-33.8688, 151.2093), ["сидней", "sydney"]),
    ("Мельбурн", "Австралия", (-37.8136, 144.9631), ["мельбурн", "melbourne"]),
    ("Окленд", "Янги Зеландия", (-36.8485, 174.7633), ["окленд", "auckland"]),
    ("Тел-Авив", "Исроил", (32.0853, 34.7818), ["тел-авив", "tel aviv"]),
    ("Иерусалим", "Исроил", (31.7683, 35.2137), ["иерусалим", "jerusalem"]),
    ("Бейрут", "Ливан", (33.8938, 35.5018), ["бейрут", "beirut"]),
    ("Доҳа", "Қатар", (25.2854, 51.531), ["доҳа", "doha"]),
    ("Кувейт", "Кувейт", (29.3759, 47.9774), ["кувейт", "kuwait"]),
    ("Бағдод", "Ироқ", (33.3152, 44.3661), ["бағдод", "baghdad", "bagdod"]),
    ("Дамаск", "Сурия", (33.5138, 36.2765), ["дамаск", "damascus"]),
    ("Киев", "Украина", (50.4501, 30.5234), ["киев", "kyiv", "kiev"]),
    ("Таллин", "Эстония", (59.437, 24.7536), ["таллин", "tallinn"]),
    ("Загреб", "Хорватия", (45.815, 15.9819), ["загреб", "zagreb"]),
    ("Белград", "Сербия", (44.7866, 20.4489), ["белград", "belgrade"]),
    ("София", "Болгария", (42.6977, 23.3219), ["софия", "sofia"]),
    ("Бухарест", "Руминия", (44.4268, 26.1025), ["бухарест", "bucharest"]),
    ("Лиссабон", "Португалия", (38.7223, -9.1393), ["лиссабон", "lisbon", "lissabon"]),
    ("Бекобод", "Ўзбекистон", (40.2186, 69.2711), ["бекобод", "bekobod", "bekabad"]),
    ("Қувасой", "Ўзбекистон", (40.2967, 71.9636), ["қувасой", "quvasoy", "kuvasay"]),
    ("Каттақўрғон", "Ўзбекистон", (39.9, 66.25), ["каттақўрғон", "kattaqorgon", "kattakurgan"]),
    ("Машҳад", "Эрон", (36.2605, 59.6168), ["машҳад", "mashhad"]),
    ("Табриз", "Эрон", (38.08, 46.2919), ["табриз", "tabriz"]),
    ("Шероз", "Эрон", (29.5918, 52.5837), ["шероз", "shiraz"]),
    ("Аҳвоз", "Эрон", (31.3183, 48.6706), ["аҳвоз", "ahvaz"]),
    ("Қум", "Эрон", (34.6401, 50.8764), ["қум", "qom"]),
    ("Керманшоҳ", "Эрон", (34.3277, 47.0778), ["керманшоҳ", "kermanshah"]),
    ("Урмия", "Эрон", (37.5527, 45.0761), ["урмия", "urmia"]),
    ("Рашт", "Эрон", (37.2808, 49.5832), ["рашт", "rasht"]),
    ("Заҳедон", "Эрон", (29.4963, 60.8629), ["заҳедон", "zahedan"]),
    ("Ҳамадон", "Эрон", (34.7992, 48.5146), ["ҳамадон", "hamadan"]),
    ("Керман", "Эрон", (30.2839, 57.0834), ["керман", "kerman"]),
    ("Язд", "Эрон", (31.8974, 54.3569), ["язд", "yazd"]),
    ("Ардабил", "Эрон", (38.2498, 48.2933), ["ардабил", "ardabil"]),
    ("Бандар Аббос", "Эрон", (27.1865, 56.2808), ["бандар аббос", "bandar abbas"]),
    ("Арак", "Эрон", (34.0917, 49.6892), ["арак", "arak"]),
    ("Қазвин", "Эрон", (36.2688, 50.0041), ["қазвин", "qazvin"]),
    ("Занжон", "Эрон", (36.6764, 48.4963), ["занжон", "zanjan"]),
    ("Санандаж", "Эрон", (35.3219, 46.9862), ["санандаж", "sanandaj"]),
    ("Хой", "Эрон", (38.5503, 44.9517), ["хой", "khoy"]),
    ("Ғорғон", "Эрон", (36.8427, 54.4392), ["ғорғон", "gorgan"]),
    ("Сабзевор", "Эрон", (36.2126, 57.6819), ["сабзевор", "sabzevar"]),
    ("Кашан", "Эрон", (33.985, 51.41), ["кашан", "kashan"]),
    ("Нишопур", "Эрон", (36.2133, 58.7958), ["нишопур", "nishapur", "neyshabur"]),
    ("Сарахс", "Эрон", (36.5449, 61.1601), ["сарахс", "sarakhs"]),
    ("Исфаҳон", "Эрон", (32.6546, 51.668), ["исфаҳон", "isfahan"]),
    ("Гянжа", "Озарбайжон", (40.6828, 46.3606), ["гянжа", "ganja", "gyandzha"]),
    ("Сумгаит", "Озарбайжон", (40.5892, 49.6685), ["сумгаит", "sumgait", "sumqayit"]),
    ("Мингечевир", "Озарбайжон", (40.77, 47.0492), ["мингечевир", "mingachevir"]),
    ("Ленкорань", "Озарбайжон", (38.7539, 48.8511), ["ленкорань", "lankaran", "lenkoran"]),
    ("Ширван", "Озарбайжон", (39.945, 48.9219), ["ширван", "shirvan"]),
    ("Нахичевань", "Озарбайжон", (39.2089, 45.4122), ["нахичевань", "nakhchivan", "naxchivan"]),
    ("Шамкир", "Озарбайжон", (40.8297, 46.0206), ["шамкир", "shamkir"]),
    ("Шеки", "Озарбайжон", (41.1919, 47.1706), ["шеки", "sheki"]),
    ("Евлах", "Озарбайжон", (40.6167, 47.15), ["евлах", "yevlakh"]),
    ("Хачмаз", "Озарбайжон", (41.465, 48.8144), ["хачмаз", "khachmaz"]),
    ("Агдаш", "Озарбайжон", (40.6547, 47.4761), ["агдаш", "agdash"]),
    ("Барда", "Озарбайжон", (40.3781, 47.1219), ["барда", "barda"]),
    ("Салян", "Озарбайжон", (39.5892, 48.9836), ["салян", "salyan"]),
    ("Имишли", "Озарбайжон", (39.8697, 48.0672), ["имишли", "imishli"]),
    ("Кутаиси", "Грузия", (42.2679, 42.7183), ["кутаиси", "kutaisi"]),
    ("Рустави", "Грузия", (41.5492, 45.0233), ["рустави", "rustavi"]),
    ("Гори", "Грузия", (41.9847, 44.1114), ["гори", "gori"]),
    ("Зугдиди", "Грузия", (42.5088, 41.8709), ["зугдиди", "zugdidi"]),
    ("Хашури", "Грузия", (41.9922, 43.5989), ["хашури", "khashuri"]),
    ("Самтредиа", "Грузия", (42.1594, 42.3383), ["самтредиа", "samtredia"]),
    ("Сенаки", "Грузия", (42.2681, 42.0658), ["сенаки", "senaki"]),
    ("Телави", "Грузия", (41.9192, 45.4736), ["телави", "telavi"]),
    ("Озургети", "Грузия", (41.9256, 42.0086), ["озургети", "ozurgeti"]),
    ("Зестафони", "Грузия", (42.1136, 43.0392), ["зестафони", "zestafoni", "zostafoni"]),
    ("Каспи", "Грузия", (41.9256, 44.4239), ["каспи", "kaspi"]),
    ("Мартвили", "Грузия", (42.3919, 42.3822), ["мартвили", "martvili"]),
    ("Куляб", "Тожикистон", (37.9139, 69.7847), ["куляб", "kulob", "kulyab"]),
    ("Бохтар", "Тожикистон", (37.8333, 68.7833), ["бохтар", "bokhtar", "qurghonteppa"]),
    ("Истаравшан", "Тожикистон", (39.9086, 69.0075), ["истаравшан", "istaravshan"]),
    ("Пенджикент", "Тожикистон", (39.4941, 67.6086), ["пенджикент", "panjakent", "penjikent"]),
    ("Хорог", "Тожикистон", (37.4913, 71.5508), ["хорог", "khorog", "khorugh"]),
    ("Турсунзаде", "Тожикистон", (38.5119, 68.0128), ["турсунзаде", "tursunzoda"]),
    ("Исфара", "Тожикистон", (40.1197, 70.6244), ["исфара", "isfara"]),
    ("Вахдат", "Тожикистон", (38.5567, 69.0344), ["вахдат", "vahdat"]),
    ("Канибадам", "Тожикистон", (40.29, 70.4239), ["канибадам", "konibodom", "kanibadam"]),
    ("Кубодиён", "Тожикистон", (37.7967, 68.6083), ["кубодиён", "qubodiyon", "kubodiyon"]),
    ("Ғарм", "Тожикистон", (39.0333, 70.4167), ["ғарм", "gharm", "garm"]),
    ("Нурек", "Тожикистон", (38.3833, 69.3333), ["нурек", "norak", "nurek"]),
    ("Ёвон", "Тожикистон", (38.3167, 69.0), ["ёвон", "yovon"]),
    ("Туркменабад", "Туркманистон", (39.0833, 63.5667), ["туркменабад", "turkmenabat"]),
    ("Дашогуз", "Туркманистон", (41.8363, 59.9666), ["дашогуз", "dashoguz"]),
    ("Мары", "Туркманистон", (37.6, 61.8333), ["мары", "mary"]),
    ("Балканабад", "Туркманистон", (39.5108, 54.3665), ["балканабад", "balkanabat"]),
    ("Байрамали", "Туркманистон", (37.6167, 62.1667), ["байрамали", "bayramaly"]),
    ("Туркменбаши", "Туркманистон", (40.0231, 52.9581), ["туркменбаши", "turkmenbashi"]),
    ("Теджен", "Туркманистон", (37.39, 60.5031), ["теджен", "tejen"]),
    ("Серахс", "Туркманистон", (36.545, 61.13), ["серахс", "sarakhs tkm"]),
    ("Керки", "Туркманистон", (37.8333, 65.2), ["керки", "kerki", "atamyrat"]),
    ("Ёлётен", "Туркманистон", (37.2833, 62.35), ["ёлётен", "yoloten"]),
    ("Газаджак", "Туркманистон", (41.19, 61.4), ["газаджак", "gazojak"]),
    ("Бахарден", "Туркманистон", (38.4333, 57.4333), ["бахарден", "bakharden"]),
    ("Каахка", "Туркманистон", (37.3167, 59.8333), ["каахка", "kaakhka"]),
    ("Абадан", "Туркманистон", (38.0, 58.3667), ["абадан", "abadan tkm"]),
]

# Davlatlarning o'zi ham manzil bo'lishi mumkin ("Чехияга юради").
# (ko'rsatiladigan nom, [yozilish variantlari])
COUNTRIES = [
    ("Ўзбекистон", ["o'zbekiston", "ozbekiston", "uzbekistan", "узбекистан", "ўзбекистон", "узбекистон"]),
    ("Россия", ["rossiya", "russia", "россия", "росия"]),
    ("Қозоғистон", ["qozog'iston", "qozogiston", "kazakhstan", "казахстан", "қозоғистон", "козогистон"]),
    ("Қирғизистон", ["qirg'iziston", "qirgiziston", "kyrgyzstan", "киргизия", "киргизстан", "қирғизистон", "кыргызстан"]),
    ("Тожикистон", ["tojikiston", "tajikistan", "таджикистан", "тожикистон"]),
    ("Туркманистон", ["turkmaniston", "turkmenistan", "туркменистан", "туркманистон"]),
    ("Хитой", ["xitoy", "china", "китай", "хитой"]),
    ("Туркия", ["turkiya", "turkey", "турция", "туркия"]),
    ("Германия", ["germaniya", "germany", "германия"]),
    ("Польша", ["polsha", "poland", "польша", "полша"]),
    ("Чехия", ["chexiya", "chehiya", "czechia", "чехия"]),
    ("Беларусь", ["belarus", "belorussiya", "беларусь", "беларус", "белоруссия"]),
    ("Озарбайжон", ["ozarbayjon", "azerbaijan", "азербайджан", "озарбайжон"]),
    ("Грузия", ["gruziya", "georgia", "грузия"]),
    ("Афғонистон", ["afg'oniston", "afgoniston", "afghanistan", "афганистан", "афғонистон"]),
    ("Эрон", ["eron", "iran", "иран", "эрон"]),
    ("Италия", ["italiya", "italy", "италия"]),
    ("Франция", ["fransiya", "france", "франция"]),
    ("Испания", ["ispaniya", "spain", "испания"]),
    ("Нидерландия", ["niderlandiya", "gollandiya", "netherlands", "нидерланды", "голландия"]),
    ("Литва", ["litva", "lithuania", "литва"]),
    ("Латвия", ["latviya", "latvia", "латвия"]),
    ("Венгрия", ["vengriya", "hungary", "венгрия"]),
    ("Словакия", ["slovakiya", "slovakia", "словакия"]),
    ("Болгария", ["bolgariya", "bulgaria", "болгария"]),
    ("Руминия", ["ruminiya", "romania", "румыния"]),
    ("Австрия", ["avstriya", "austria", "австрия"]),
    ("Сербия", ["serbiya", "serbia", "сербия"]),
    ("Арманистон", ["armaniston", "armenia", "армения"]),
    ("Буюк Британия", ["буюк британия", "buyuk britaniya", "united kingdom", "great britain", "angliya", "англия", "великобритания"]),
    ("Бельгия", ["бельгия", "belgiya", "belgium", "бельгия"]),
    ("Швеция", ["швеция", "shvetsiya", "sweden", "швеция"]),
    ("Норвегия", ["норвегия", "norvegiya", "norway", "норвегия"]),
    ("Дания", ["дания", "daniya", "denmark", "дания"]),
    ("Финляндия", ["финляндия", "finlyandiya", "finland", "финляндия"]),
    ("Греция", ["греция", "gretsiya", "greece", "греция"]),
    ("Бирлашган Араб Амирликлари", ["бирлашган араб амирликлари", "birlashgan arab amirliklari", "uae", "оаэ", "эмираты"]),
    ("Саудия Арабистони", ["саудия арабистони", "saudiya arabistoni", "saudi arabia", "саудовская аравия"]),
    ("Миср", ["миср", "misr", "egypt", "египет"]),
    ("Марокко", ["марокко", "marokko", "morocco", "марокко"]),
    ("Жанубий Африка", ["жанубий африка", "janubiy afrika", "south africa", "южная африка", "юар"]),
    ("Кения", ["кения", "keniya", "kenya", "кения"]),
    ("Нигерия", ["нигерия", "nigeriya", "nigeria", "нигерия"]),
    ("АҚШ", ["ақш", "aqsh", "usa", "america", "сша", "америка"]),
    ("Канада", ["канада", "kanada", "canada", "канада"]),
    ("Мексика", ["мексика", "meksika", "mexico", "мексика"]),
    ("Бразилия", ["бразилия", "braziliya", "brazil", "бразилия"]),
    ("Аргентина", ["аргентина", "argentina", "аргентина"]),
    ("Чили", ["чили", "chili", "chile", "чили"]),
    ("Перу", ["перу", "peru", "перу"]),
    ("Колумбия", ["колумбия", "kolumbiya", "colombia", "колумбия"]),
    ("Япония", ["япония", "yaponiya", "japan", "япония"]),
    ("Жанубий Корея", ["жанубий корея", "janubiy koreya", "south korea", "южная корея"]),
    ("Сингапур", ["сингапур", "singapur", "singapore"]),
    ("Таиланд", ["таиланд", "tailand", "thailand", "таиланд"]),
    ("Вьетнам", ["вьетнам", "vetnam", "vietnam", "вьетнам"]),
    ("Индонезия", ["индонезия", "indoneziya", "indonesia", "индонезия"]),
    ("Малайзия", ["малайзия", "malayziya", "malaysia", "малайзия"]),
    ("Филиппин", ["филиппин", "filippin", "philippines", "филиппины"]),
    ("Ҳиндистон", ["ҳиндистон", "hindiston", "india", "индия"]),
    ("Австралия", ["австралия", "avstraliya", "australia", "австралия"]),
    ("Янги Зеландия", ["янги зеландия", "yangi zelandiya", "new zealand", "новая зеландия"]),
    ("Исроил", ["исроил", "isroil", "israel", "израиль"]),
    ("Ливан", ["ливан", "livan", "lebanon", "ливан"]),
    ("Қатар", ["қатар", "qatar", "катар"]),
    ("Кувейт", ["кувейт", "kuvayt", "kuwait", "кувейт"]),
    ("Ироқ", ["ироқ", "iroq", "iraq", "ирак"]),
    ("Сурия", ["сурия", "suriya", "syria", "сирия"]),
    ("Украина", ["украина", "ukraina", "ukraine", "украина"]),
    ("Эстония", ["эстония", "estoniya", "estonia", "эстония"]),
    ("Хорватия", ["хорватия", "xorvatiya", "croatia", "хорватия"]),
    ("Португалия", ["португалия", "portugaliya", "portugal", "португалия"]),
]


def normalize_city(name: str) -> str:
    """Kichik harf, faqat harf/raqam (apostrof, bo'shliq, chiziqchalar olib tashlanadi)."""
    if not name:
        return ""
    return re.sub(r"[\W_]", "", name.lower())


class Place:
    def __init__(self, display, country, coords):
        self.display = display
        self.country = country
        self.coords = coords


def _build_place_index():
    index = {}
    for display, country, coords, aliases in PLACES:
        place = Place(display, country, coords)
        for a in aliases + [display]:
            index[normalize_city(a)] = place
    for display, aliases in COUNTRIES:
        place = Place(display, display, None)
        for a in aliases + [display]:
            index[normalize_city(a)] = place
    return index


PLACE_INDEX = _build_place_index()

# Qo'shimchalar: -dan/-да (qayerdan), -da/-да (qayerda), -ga/-га (qayerga)
_SUF_FROM = {"dan", "дан", "den", "дэн"}
_SUF_AT = {"da", "да", "dagi", "даги", "дагы"}
_SUF_TO = {"ga", "ka", "qa", "га", "ка", "қа", "gacha", "гача"}
_SUF_NEUTRAL = {"", "ni", "ни", "ning", "нинг", "dir", "дир"}
_SUF_ALL = _SUF_FROM | _SUF_AT | _SUF_TO | _SUF_NEUTRAL
_RU_END = {"у", "ы", "е", "ой", "ю", "и", "ом"}
_PREV_FROM = {"из", "от", "с", "со", "from"}
_PREV_TO = {"в", "во", "до", "на", "к", "ко", "to"}


def match_place(word_norm: str):
    """So'zni lug'atdagi joyga moslaydi. (Place, qo'shimcha) yoki None."""
    if not word_norm or word_norm.isdigit():
        return None
    if word_norm in PLACE_INDEX:
        return PLACE_INDEX[word_norm], ""
    best = None
    for alias, place in PLACE_INDEX.items():
        if len(alias) < 2:
            continue
        if word_norm.startswith(alias):
            rest = word_norm[len(alias):]
            if rest in _SUF_ALL and (best is None or len(alias) > best[0]):
                best = (len(alias), place, rest)
        # Ruscha: Москву, Праге, Москвы ...
        if len(alias) >= 3 and alias[-1] in ("а", "я"):
            stem = alias[:-1]
            if word_norm.startswith(stem):
                rest = word_norm[len(stem):]
                if rest in _RU_END and (best is None or len(stem) > best[0]):
                    best = (len(stem), place, "")
    return (best[1], best[2]) if best else None


def lookup_place(name: str):
    """Nom bo'yicha joyni topadi (qo'shimchali yozuv ham bo'ladi)."""
    if not name:
        return None
    m = match_place(normalize_city(name))
    return m[0] if m else None


def find_places(text: str):
    """Matndagi barcha joylarni tartib bilan topadi: [{place, hint}]."""
    tokens = re.findall(r"[\w'’ʼ`]+", text)
    found, seen, used = [], set(), set()

    def try_add(i, tok, span):
        m = match_place(normalize_city(tok))
        if not m:
            return False
        place, suffix = m
        if place.display in seen:
            used.update(span)
            return True
        seen.add(place.display)
        hint = None
        if suffix in _SUF_FROM or suffix in _SUF_AT:
            hint = "from"
        elif suffix in _SUF_TO:
            hint = "to"
        elif i > 0 and (i - 1) not in span:
            prev = normalize_city(tokens[i - 1])
            if prev in _PREV_FROM:
                hint = "from"
            elif prev in _PREV_TO:
                hint = "to"
        found.append({"place": place, "hint": hint})
        used.update(span)
        return True

    # Uch so'zli joy nomlari (Рио-де-Жанейро kabi)
    for i in range(len(tokens) - 2):
        if i in used or (i + 1) in used or (i + 2) in used:
            continue
        try_add(i, tokens[i] + tokens[i + 1] + tokens[i + 2], {i, i + 1, i + 2})

    # Ikki so'zli joy nomlarini sinaymiz (Нью-Йорк, Лос-Анжелес, Сан-Франциско)
    for i in range(len(tokens) - 1):
        if i in used or (i + 1) in used:
            continue
        try_add(i, tokens[i] + tokens[i + 1], {i, i + 1})

    for i, tok in enumerate(tokens):
        if i in used:
            continue
        try_add(i, tok, {i})

    return found


def assign_route(found):
    """Topilgan joylardan (qayerdan, qayerga) ni ajratadi."""
    origin = next((f for f in found if f["hint"] == "from"), None)
    dest = next((f for f in found if f["hint"] == "to" and f is not origin), None)
    rest = [f for f in found if f is not origin and f is not dest]
    if origin is None and rest:
        origin = rest.pop(0)
    if dest is None and rest:
        dest = rest.pop(0)
    return (origin["place"] if origin else None, dest["place"] if dest else None)


def place_label(name, country):
    """'Тошкент (Ўзбекистон)'; davlatning o'zi bo'lsa faqat nomi."""
    if not name:
        return "?"
    name = cap(name)
    if country and normalize_city(country) != normalize_city(name):
        return f"{name} ({country})"
    return name


def haversine_km(c1, c2) -> float:
    import math
    lat1, lon1 = map(math.radians, c1)
    lat2, lon2 = map(math.radians, c2)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def estimate_trip(yuklash: str, manzil: str):
    """51/52/59-band: taxminiy masofa (km), vaqt (soat), yoqilg'i xarajati (so'm)."""
    p1, p2 = lookup_place(yuklash), lookup_place(manzil)
    if not p1 or not p2 or not p1.coords or not p2.coords:
        return None
    straight = haversine_km(p1.coords, p2.coords)
    road_km = straight * 1.3  # yo'l egriligi uchun taxminiy koeffitsient
    if road_km < 1:
        return None
    hours = road_km / 55  # o'rtacha yuk mashinasi tezligi
    fuel_l = road_km / 100 * 32  # o'rtacha sarf, litr/100km
    fuel_cost = fuel_l * DIESEL_PRICE
    return {"km": round(road_km), "hours": round(hours, 1), "fuel_cost": round(fuel_cost, -3)}


# --------------------------------------------------------------------------
# Sana aniqlash (4-band) va borib-kelish (5-band)
# --------------------------------------------------------------------------

def extract_date_hint(text: str):
    t_low = text.lower()
    today = datetime.now(TZ) if TZ else datetime.now()
    if "бугун" in t_low or "bugun" in t_low:
        return today.strftime("%d.%m.%Y")
    if "эртага" in t_low or "ertaga" in t_low:
        return (today + timedelta(days=1)).strftime("%d.%m.%Y")
    m = re.search(r"(\d+)\s*(кун|kun)дан?\s*кейин|(\d+)\s*(кун|kun)dan\s*keyin", t_low)
    if m:
        days = int(next(g for g in m.groups() if g and g.isdigit()))
        return (today + timedelta(days=days)).strftime("%d.%m.%Y")
    m2 = re.search(r"\b(\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?)\b", text)
    if m2:
        return m2.group(1)
    return None


def is_round_trip(text: str) -> bool:
    t_low = text.lower()
    return bool(re.search(r"орқага|orqaga|туда.?обратно|w{2}|va qaytish|скво?зн", t_low))


# --------------------------------------------------------------------------
# Turi aniqlash: zapros yoki mashina bor (routing uchun)
# --------------------------------------------------------------------------

ZAPROS_HINTS = ["керак", "kerak", "нужен", "нужна", "требуется", "izlanmoqda", "изланмоқда"]
TRUCK_HINTS = ["бор", "bor", "свобод", "bo'sh", "bosh mashina", "холост"]


def classify_kind(text: str):
    t_low = text.lower()
    has_zapros = any(h in t_low for h in ZAPROS_HINTS)
    has_truck = any(h in t_low for h in TRUCK_HINTS)
    if has_zapros and not has_truck:
        return "zapros"
    if has_truck and not has_zapros:
        return "mashina"
    return None  # noaniq — foydalanuvchidan so'raladi


# --------------------------------------------------------------------------
# Matnni AI/oddiy usulda ajratish
# --------------------------------------------------------------------------

PARSE_PROMPT = """Sen O'zbekistondagi yuk tashish brokerisan. Quyidagi xabar \
— yuk yoki mashina haqidagi xabar, so'zlashuv uslubidagi o'zbek/rus aralash matn. \
Undan quyidagi maydonlarni ol va FAQAT JSON obyekt qaytar, boshqa hech qanday matn yozma:

{{"yuklash": string yoki null, "yuklash_davlat": string yoki null, \
"manzil": string yoki null, "manzil_davlat": string yoki null, \
"yuk_turi": string yoki null, "mashina_turi": string yoki null, \
"soni": number yoki null, "dona": number yoki null, \
"tonna": number yoki null, "kub": number yoki null, "tolov": string yoki null}}

MUHIM qoidalar:
- soni — FAQAT kerakli/mavjud MASHINALAR soni. Tovar/buyum soni bo'lsa "dona"ga yoz.
- yuklash/manzil — shahar yoki joy nomi. Aniq tanib bo'lmasa, asl yozilishini o'zgartirmay qaytar.
- yuklash_davlat/manzil_davlat — o'sha shahar joylashgan davlat (masalan Ўзбекистон, Россия, \
Чехия, Хитой, Қозоғистон). Agar yuklash/manzilning o'zi davlat bo'lsa yoki davlatni bilmasang — null.
- mashina_turi — kuzov turi (тент, изотерма va h.k.), faqat aniq aytilgan bo'lsa.
- tonna — raqam; agar oraliq aytilgan bo'lsa ("7-8 тонна") shu oraliqni matn sifatida yoz ("7-8").
- tolov — to'lov turi, qisqa ("нақд", "ўтказма"), bo'lmasa null.
MUHIM: yuklash, manzil, davlat nomlari, yuk_turi, mashina_turi, tolov — bu matn maydonlarining \
barchasini FAQAT KIRILL alifbosida qaytar, hatto foydalanuvchi lotin yozuvida \
yozgan bo'lsa ham. Aniq tanib bo'lmasa, harflarini kirillga almashtirib yoz.
Noaniq maydonni taxmin qilma — null qoldir. HECH QACHON matnda yo'q raqam yoki \
tafsilotni o'zingdan to'qib chiqarma — masalan matnda faqat "yuk bor" deyilgan \
bo'lsa-yu, tonna yoki kub aytilmagan bo'lsa, ularni albatta null qoldir.

Matn: "{text}"
"""

# Kuzov turlari va yuk turlari (AI kalitsiz ishlaganda ham topish uchun).
BODY_TYPES = [
    (r"тент|tent", "тент"),
    (r"рефриж|refrij|\bреф\w*|\bref\w*", "рефрижератор"),
    (r"изотерм|izoterm", "изотерма"),
    (r"бортов|bortov", "бортовой"),
]
CARGO_WORDS = [
    (r"текстил|tekstil|textil", "Текстиль"),
    (r"мебел|mebel", "Мебель"),
    (r"интернет|internet", "Интернет товар"),
    (r"лифт|lift", "Лифт"),
    (r"электрон|elektron", "Электроника"),
    (r"одежд|kiyim", "Кийим-кечак"),
    (r"озиқ|oziq|продукт|mahsulot", "Озиқ-овқат"),
    (r"мева|meva|овощ|sabzavot|фрукт", "Мева-сабзавот"),
    (r"қурилиш|qurilish|стройматериал", "Қурилиш материаллари"),
    (r"запчаст|ehtiyot", "Эҳтиёт қисмлар"),
    (r"оборудован|uskuna|jihoz", "Ускуна"),
]

_NUM = r"(\d+(?:[.,]\d+)?)"


def _to_number(s):
    v = float(s.replace(",", "."))
    return int(v) if v.is_integer() else v


def fallback_parse(text: str) -> dict:
    """AI kalitsiz ishlaydigan oddiy parser: raqamlar + shaharlar lug'ati + kalit so'zlar."""
    low = text.lower()

    def find(pattern):
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1) if m else None

    # tonna: oraliq ("7-8 тонна") yoki bitta son
    tonna = None
    rng = re.search(_NUM + r"\s*[-–—]\s*" + _NUM + r"\s*(?:тонн\w*|tonn\w*|тн\b)", text, re.IGNORECASE)
    if rng:
        tonna = f"{rng.group(1)}-{rng.group(2)}".replace(",", ".")
    else:
        one = find(_NUM + r"\s*(?:тонн\w*|tonn\w*|тн\b)")
        tonna = _to_number(one) if one else None

    kub = find(_NUM + r"\s*(?:куб\w*|kub\w*|м3|м³|m3)")
    dona = find(r"(\d+)\s*(?:шт|дона|dona)\b")
    soni = find(r"(\d+)\s*(?:машин|mashin)")
    if not soni:
        # "3 та тент" -> 3 ta mashina; "2 та лифт" -> 2 dona yuk
        m = re.search(r"(\d+)\s*(?:та\b|ta\b)(?:\s+(\w+))?", text, re.IGNORECASE)
        if m:
            nxt = (m.group(2) or "").lower()
            if nxt and any(re.search(pat, nxt) for pat, _ in CARGO_WORDS):
                dona = dona or m.group(1)
            else:
                soni = m.group(1)

    tolov = None
    if re.search(r"накд|нақд|naqd|наличн", low):
        tolov = "нақд"
    elif re.search(r"перечисл|перевод|o'tkazma|otkazma|ўтказма", low):
        tolov = "ўтказма"

    mashina_turi = next((name for pat, name in BODY_TYPES if re.search(pat, low)), None)
    yuk_turi = next((name for pat, name in CARGO_WORDS if re.search(pat, low)), None)

    origin, dest = assign_route(find_places(text))

    return {
        "yuklash": origin.display if origin else None,
        "yuklash_davlat": origin.country if origin else None,
        "manzil": dest.display if dest else None,
        "manzil_davlat": dest.country if dest else None,
        "yuk_turi": yuk_turi, "mashina_turi": mashina_turi,
        "soni": int(soni) if soni else None,
        "dona": int(dona) if dona else None,
        "tonna": tonna,
        "kub": _to_number(kub) if kub else None,
        "tolov": tolov,
    }


def parse_with_claude(text: str) -> dict:
    from anthropic import Anthropic

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=500,
        messages=[{"role": "user", "content": PARSE_PROMPT.format(text=text)}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text")
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("JSON topilmadi")
    return json.loads(match.group(0))


def parse_text(text: str) -> dict:
    try:
        p = parse_with_claude(text) if ANTHROPIC_API_KEY else fallback_parse(text)
    except Exception:
        log.exception("parse xatosi")
        p = fallback_parse(text)
    return sanitize_numbers(p, text)


def sanitize_numbers(p: dict, text: str) -> dict:
    """AI matnda yo'q raqamni o'ylab topmasligi uchun tekshiruv."""
    for key in ("soni", "dona", "tonna", "kub"):
        val = p.get(key)
        if val is None or val == "":
            p[key] = None
            continue
        if isinstance(val, str):
            forms = {val, val.replace(",", ".")}
        else:
            try:
                f = float(val)
            except (TypeError, ValueError):
                p[key] = None
                continue
            forms = {str(val), str(f), str(f).replace(".", ",")}
            if f.is_integer():
                forms.add(str(int(f)))
        if not any(x in text for x in forms):
            p[key] = None
    return p


def is_ambiguous(p: dict) -> bool:
    keys = ["yuklash", "manzil", "tonna", "kub", "soni", "dona"]
    return not any(p.get(k) for k in keys)


def cap(s):
    if not s:
        return s
    s = str(s).strip()
    return s[0].upper() + s[1:]


def fmt_num(v):
    """8.0 -> 8, 7.5 -> 7.5, '7-8' -> '7-8'."""
    if isinstance(v, str):
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    return str(int(f)) if f.is_integer() else str(f)


SEASONAL_EMOJI = {
    (3, 21): "\U0001F33C",  # Navro'z
    (1, 1): "\U0001F386",   # Yangi yil
}


def seasonal_prefix() -> str:
    today = datetime.now(TZ) if TZ else datetime.now()
    return SEASONAL_EMOJI.get((today.month, today.day), "")


def format_template(kind: str, p: dict) -> str:
    lines = []
    header = "\U0001F4E6 \u0417\u0410\u041f\u0420\u041e\u0421" if kind == "zapros" else "\U0001F69B \u041c\u0410\u0428\u0418\u041d\u0410 \u0411\u041e\u0420"
    season = seasonal_prefix()
    lines.append((season + " " if season else "") + header)
    route = (
        f"{place_label(p.get('yuklash'), p.get('yuklash_davlat'))} \u2192 "
        f"{place_label(p.get('manzil'), p.get('manzil_davlat'))}"
    )
    if p.get("round_trip"):
        route += " \U0001F501"  # borib-kelish belgisi
    lines.append(f"\U0001F4CD {route}")
    if p.get("yuk_turi"):
        lines.append(f"\U0001F4E6 \u042E\u043A: {p['yuk_turi']}")
    if p.get("dona"):
        lines.append(f"\U0001F522 \u041C\u0438\u049B\u0434\u043E\u0440: {fmt_num(p['dona'])} \u0434\u043E\u043D\u0430")
    bits = []
    if p.get("soni"):
        mt = p.get("mashina_turi")
        bits.append(f"{fmt_num(p['soni'])} \u0442\u0430 {mt if mt else '\u043C\u0430\u0448\u0438\u043D\u0430'}")
    elif p.get("mashina_turi"):
        bits.append(cap(p["mashina_turi"]))
    if p.get("tonna"):
        bits.append(f"{fmt_num(p['tonna'])} \u0442\u043E\u043D\u043D\u0430")
    if p.get("kub"):
        bits.append(f"{fmt_num(p['kub'])} \u043A\u0443\u0431")
    if bits:
        lines.append(f"\U0001F69B {', '.join(bits)}")
    if p.get("sana"):
        lines.append(f"\U0001F4C5 \u0421\u0430\u043D\u0430: {p['sana']}")
    if p.get("tolov"):
        lines.append(f"\U0001F4B0 \u0422\u045E\u043B\u043E\u0432: {p['tolov']}")
    lines.append(f"\U0001F4DE {CONTACT_PHONE}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Bitta yozuvni to'liq qayta ishlash (parse + shablon)
# --------------------------------------------------------------------------

def apply_places(p: dict) -> dict:
    """Shahar nomini lug'atdagi bir xil yozuvga keltiradi va davlatini qo'yadi."""
    for key, ckey in (("yuklash", "yuklash_davlat"), ("manzil", "manzil_davlat")):
        ent = lookup_place(p.get(key))
        if ent:
            p[key] = ent.display
            p[ckey] = ent.country
        elif not p.get(key):
            p[ckey] = None
    return p


def build_entry(text: str, kind: str) -> dict:
    p = apply_places(parse_text(text))
    p["sana"] = extract_date_hint(text)
    p["round_trip"] = is_round_trip(text)
    p["_kind"] = kind
    p["_template"] = format_template(kind, p)
    p["_trip"] = estimate_trip(p.get("yuklash"), p.get("manzil"))
    return p


def looks_multi(text: str) -> list:
    """Bir necha alohida yozuv: bo'sh qator bilan ajratilgan bloklar, yoki
    har bir qatori to'liq (joy + raqam bor) bo'lsa. Bitta zaprosning ko'p qatorli
    yozilishi bo'linmaydi."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    if len(blocks) >= 2:
        return blocks
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) >= 2 and all(find_places(l) and re.search(r"\d", l) for l in lines):
        return lines
    return []


# --------------------------------------------------------------------------
# Handlerlar
# --------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Salom! \U0001F44B Pastdagi menyudan tanlang yoki to'g'ridan-to'g'ri xabar/rasm yuboring.\n\n"
        "Masalan:\n"
        "\u2022 Toshkentdan Qarshiga 3 ta tent 22 tonna kerak naqd (ZAPROS)\n"
        "\u2022 Samarqandda 2 ta mashina bor, 20 tonnagacha (MASHINA BOR)",
        reply_markup=main_menu(),
    )


async def cmd_til(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args and args[0] in ("uz", "ru"):
        set_lang(update.effective_user.id, args[0])
        await update.message.reply_text("OK \u2705" if args[0] == "ru" else "Bo'ldi \u2705")
    else:
        await update.message.reply_text("Til tanlang: /til uz  yoki  /til ru")


def truck_keyboard(user_id: int, row) -> InlineKeyboardMarkup:
    band = row["status"] == "band"
    status_btn = (
        InlineKeyboardButton(t("truck_mark_free", user_id), callback_data=f"tband:{row['id']}:0")
        if band else
        InlineKeyboardButton(t("truck_mark_taken", user_id), callback_data=f"tband:{row['id']}:1")
    )
    return InlineKeyboardMarkup([
        [status_btn, InlineKeyboardButton(t("truck_edit", user_id), callback_data=f"tedit:{row['id']}")],
        [InlineKeyboardButton(t("truck_copy", user_id), callback_data=f"tcopy:{row['id']}"),
         InlineKeyboardButton(t("truck_post", user_id), callback_data=f"tpost:{row['id']}")],
        [InlineKeyboardButton(t("truck_delete", user_id), callback_data=f"tdel:{row['id']}")],
    ])


def truck_entry_text(row) -> str:
    tag = "\U0001F534 BAND\n" if row["status"] == "band" else ""
    ago = time_ago(row["created_at"])
    return f"{tag}{row['template']}\n\U0001F553 {ago}"


async def send_truck_list(message_or_query, user_id: int, chat_id: int, context, order: str = "date"):
    rows = recent_trucks(user_id, order=order)
    if not rows:
        await context.bot.send_message(chat_id, t("no_trucks", user_id))
        return
    header = f"\U0001F69B {len(rows)} {t('truck_count', user_id)}"
    await context.bot.send_message(
        chat_id, header,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton(t("sort_by_date", user_id), callback_data="tsort:date"),
             InlineKeyboardButton(t("sort_by_city", user_id), callback_data="tsort:city")],
            [InlineKeyboardButton(t("clear_old", user_id), callback_data="tclearold"),
             InlineKeyboardButton(t("clear_all", user_id), callback_data="tclearall")],
        ]),
    )
    for row in rows:
        await context.bot.send_message(
            chat_id, truck_entry_text(row), reply_markup=truck_keyboard(user_id, row)
        )


async def cmd_moshinalar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_truck_list(update, update.effective_user.id, update.effective_chat.id, context)


async def cmd_stat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    now = datetime.now(TZ) if TZ else datetime.now()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = stats_between(start_of_day, now)
    week = stats_between(now - timedelta(days=7), now)
    active = count_active_trucks(user_id)
    text = (
        f"\U0001F4CA Hisobot\n\n"
        f"Bugun:\n"
        f"\U0001F4E6 Zapros: {today.get('zapros', 0)}\n"
        f"\U0001F69B Mashina: {today.get('mashina', 0)}\n\n"
        f"So'nggi 7 kun:\n"
        f"\U0001F4E6 Zapros: {week.get('zapros', 0)}\n"
        f"\U0001F69B Mashina: {week.get('mashina', 0)}\n\n"
        f"Ro'yxatdagi faol mashinalar: {active}"
    )
    await update.message.reply_text(text)


async def cmd_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        from openpyxl import Workbook
    except ImportError:
        await update.message.reply_text("openpyxl o'rnatilmagan (requirements.txt ga qo'shing).")
        return

    rows = all_entries()
    wb = Workbook()
    ws = wb.active
    ws.title = "Reestr"
    ws.append(["Sana", "Turi", "Yuklash", "Manzil", "Soni", "Dona", "Tonna", "Kub", "Yuk turi", "To'lov"])
    for r in rows:
        ws.append([
            r["created_at"][:10], r["kind"], r["yuklash"], r["manzil"],
            r["soni"], r["dona"], r["tonna"], r["kub"], r["yuk_turi"], r["tolov"],
        ])
    path = "/tmp/yuk_reestri.xlsx"
    wb.save(path)
    await update.message.reply_document(document=open(path, "rb"), filename="yuk_reestri.xlsx")


def kind_keyboard(index: int = None):
    prefix = f"k{index}:" if index is not None else "k:"
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("\U0001F4E6 Zapros", callback_data=f"{prefix}zapros"),
        InlineKeyboardButton("\U0001F69B Mashina bor", callback_data=f"{prefix}mashina"),
    ]])


def confirm_keyboard(user_id: int, kind: str, token: str):
    send_label = t("btn_send", user_id) if kind == "zapros" else t("btn_save", user_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(send_label, callback_data=f"go:{token}")],
        [
            InlineKeyboardButton(t("btn_edit", user_id), callback_data=f"edit:{token}"),
            InlineKeyboardButton(t("btn_cancel", user_id), callback_data=f"cancel:{token}"),
        ],
    ])


# Vaqtinchalik xotira: token -> (kind, parsed dict) tasdiqni kutayotgan yozuvlar
PENDING_ENTRY: dict[str, tuple] = {}
_token_seq = 0


def new_token() -> str:
    global _token_seq
    _token_seq += 1
    return str(_token_seq)


async def send_trip_info(update: Update, p: dict):
    trip = p.get("_trip")
    if not trip:
        return
    text = (
        f"\U0001F4CF Taxminiy masofa: {trip['km']} km\n"
        f"\u23F1 Taxminiy vaqt: {trip['hours']} soat\n"
        f"\u26FD Taxminiy yoqilg'i xarajati: {trip['fuel_cost']:,.0f} so'm".replace(",", " ")
    )
    await update.message.reply_text(text + "\n\n(faqat siz uchun, kanalga chiqmaydi)")


async def present_entry(update: Update, user_id: int, kind: str, p: dict):
    if is_ambiguous(p):
        await update.message.reply_text(t("need_more", user_id))
        return
    token = new_token()
    PENDING_ENTRY[token] = (kind, p)
    label = t("send_to_channel_q", user_id) if kind == "zapros" else t("save_to_list_q", user_id)
    await update.message.reply_text(
        f"{t('template_ready', user_id)}\n\n{p['_template']}\n\n{label}",
        reply_markup=confirm_keyboard(user_id, kind, token),
    )
    if kind == "zapros":
        matches = find_matching_trucks(user_id, p.get("yuklash"), p.get("manzil"))
        if matches:
            preview = "\n\n\u2015\u2015\u2015\n\n".join(m["template"] for m in matches[:3])
            await update.message.reply_text(f"{t('match_found', user_id)}\n\n{preview}")
    await send_trip_info(update, p)


async def process_text(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str, forced_kind=None):
    """Matnni (yozilgan yoki rasmdan o'qilgan) tahlil qilib, tasdiq so'raydi."""
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    thinking = await update.message.reply_text(t("thinking", user_id))

    blocks = looks_multi(text)
    if blocks:  # 1-band: ko'p yozuvli xabar
        await thinking.delete()
        entries = [build_entry(b, forced_kind or classify_kind(b) or "zapros") for b in blocks]
        PENDING_MULTI[chat_id] = entries
        preview = "\n\n\u2015\u2015\u2015\n\n".join(e["_template"] for e in entries)
        await update.message.reply_text(
            f"{len(entries)} ta yozuv topildi:\n\n{preview}",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton(t("send_all", user_id), callback_data="multi:send"),
                InlineKeyboardButton(t("btn_cancel", user_id), callback_data="multi:cancel"),
            ]]),
        )
        return

    kind = forced_kind or classify_kind(text)
    if not kind:  # noaniq — foydalanuvchidan so'raladi
        await thinking.delete()
        if not ANTHROPIC_API_KEY and is_ambiguous(apply_places(parse_text(text))):
            # ma'lumot umuman yo'q ("Груз ведро") — turini so'rab o'tirmaymiz
            await update.message.reply_text(t("need_more", user_id))
            return
        PENDING_KIND[chat_id] = text
        await update.message.reply_text(t("which_kind", user_id), reply_markup=kind_keyboard())
        return

    p = build_entry(text, kind)
    await thinking.delete()
    await present_entry(update, user_id, kind, p)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return
    chat_id = update.effective_chat.id
    if text in MENU_TEXTS:  # pastki menyu tugmasi bosildi
        AWAITING_EDIT.pop(chat_id, None)
        return await handle_menu(update, context, text)
    edited = AWAITING_EDIT.pop(chat_id, None)
    forced = MODE.pop(chat_id, None) or (edited if isinstance(edited, str) else None)
    await process_text(update, context, text, forced)


# --------------------------------------------------------------------------
# Rasm: rasmdagi matnni o'qib, zapros/mashina ekanini ajratadi (AI kaliti kerak)
# --------------------------------------------------------------------------

IMAGE_PROMPT = """Rasmda yuk tashish (logistika) haqidagi xabar yoki e'lon bor. \
Rasmdagi matnni o'qi va FAQAT JSON obyekt qaytar, boshqa hech narsa yozma:

{"kind": "zapros" yoki "mashina" yoki null, "text": "..."}

- text — rasmdagi asosiy xabar matni, so'zma-so'z ko'chirilgan (o'zbek/rus/lotin/kirill \
qanday yozilgan bo'lsa shunday). Bir nechta alohida e'lon bo'lsa, har birini bo'sh qator bilan ajrat.
- kind — yuk yoki mashina KERAK bo'lsa "zapros"; bo'sh mashina/transport BOR bo'lsa "mashina"; \
aralash yoki noaniq bo'lsa null.
- Rasmda yuk yoki mashina haqida hech narsa bo'lmasa, text ni bo'sh qoldir. \
Matnda yo'q narsani o'zingdan qo'shma."""


def read_image_with_claude(image_bytes: bytes) -> dict:
    import base64
    from anthropic import Anthropic

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    b64 = base64.standard_b64encode(image_bytes).decode()
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1200,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text", "text": IMAGE_PROMPT},
        ]}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text")
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("JSON topilmadi")
    return json.loads(match.group(0))


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    if not ANTHROPIC_API_KEY:
        await update.message.reply_text(t("photo_need_key", user_id))
        return
    thinking = await update.message.reply_text(t("reading_photo", user_id))
    try:
        photo = update.message.photo[-1]
        tg_file = await photo.get_file()
        data = bytes(await tg_file.download_as_bytearray())
        result = await asyncio.to_thread(read_image_with_claude, data)
    except Exception:
        log.exception("rasmni o'qishda xato")
        await thinking.edit_text(t("photo_failed", user_id))
        return
    await thinking.delete()

    text = (result.get("text") or "").strip()
    caption = (update.message.caption or "").strip()
    if caption:
        text = f"{caption}\n{text}".strip()
    if not text:
        await update.message.reply_text(t("photo_nothing", user_id))
        return
    kind = result.get("kind") if result.get("kind") in ("zapros", "mashina") else None
    forced = MODE.pop(chat_id, None) or kind
    await process_text(update, context, text, forced)


# --------------------------------------------------------------------------
# Pastki doimiy menyu (bank botidagidek katta tugmalar)
# --------------------------------------------------------------------------

MENU_ROWS = [
    ["\U0001F4E6 Zapros yozish", "\U0001F69B Mashina qo'shish"],
    ["\U0001F69B Mening mashinalarim", "\U0001F4CA Hisobot"],
    ["\U0001F4E5 Excel", "\U0001F9F9 Eskilarini tozalash"],
    ["\U0001F310 Til", "\u2139\uFE0F Yordam"],
]
MENU_TEXTS = {label for row in MENU_ROWS for label in row}

HELP_TEXT = (
    "\u2139\uFE0F Qanday ishlaydi:\n\n"
    "\U0001F4E6 Zapros yozish \u2014 yuk yoki mashina KERAK bo'lsa, matn yozing yoki rasm yuboring. "
    "Bot shablon qilib, tasdiqlaganingizdan keyin kanalga joylaydi.\n\n"
    "\U0001F69B Mashina qo'shish \u2014 bo'sh mashina BOR bo'lsa. Ro'yxatga saqlanadi, kanalga chiqmaydi.\n\n"
    "\U0001F69B Mening mashinalarim \u2014 saqlangan mashinalar (band qilish, tahrirlash, o'chirish).\n"
    "\U0001F4CA Hisobot \u2014 bugungi va haftalik statistika.\n"
    "\U0001F4E5 Excel \u2014 barcha yozuvlar Excel faylda.\n\n"
    "Misol: Самарканд - Пятигорск 7-8 тонна тент керак нақд"
)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        MENU_ROWS,
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Yuk yoki mashina haqida yozing...",
    )


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    label = text
    if label == MENU_ROWS[0][0]:
        MODE[chat_id] = "zapros"
        await update.message.reply_text(
            "\U0001F4E6 Zaprosni yozing (yoki rasm yuboring).\n\nMasalan:\n"
            "Тошкент - Самарқанд 22 тонна тент керак нақд"
        )
    elif label == MENU_ROWS[0][1]:
        MODE[chat_id] = "mashina"
        await update.message.reply_text(
            "\U0001F69B Mashina ma'lumotini yozing (yoki rasm yuboring).\n\nMasalan:\n"
            "Самарқандда 2 та тент бор, 20 тонна, Россияга юради"
        )
    elif label == MENU_ROWS[1][0]:
        await send_truck_list(update, user_id, chat_id, context)
    elif label == MENU_ROWS[1][1]:
        await cmd_stat(update, context)
    elif label == MENU_ROWS[2][0]:
        await cmd_excel(update, context)
    elif label == MENU_ROWS[2][1]:
        n = delete_old_trucks(user_id, days=3)
        await update.message.reply_text(f"{n} {t('cleared_old', user_id)}")
    elif label == MENU_ROWS[3][0]:
        await update.message.reply_text(
            "Tilni tanlang / \u0412\u044b\u0431\u0435\u0440\u0438\u0442\u0435 \u044f\u0437\u044b\u043a:",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("\U0001F1FA\U0001F1FF O'zbekcha", callback_data="lang:uz"),
                InlineKeyboardButton("\U0001F1F7\U0001F1FA \u0420\u0443\u0441\u0441\u043a\u0438\u0439", callback_data="lang:ru"),
            ]]),
        )
    elif label == MENU_ROWS[3][1]:
        await update.message.reply_text(HELP_TEXT)


async def handle_kind_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    kind = query.data.split(":")[1]
    text = PENDING_KIND.pop(chat_id, None)
    if not text:
        await query.edit_message_text(t("cancelled", user_id))
        return
    p = build_entry(text, kind)
    await query.edit_message_text(f"{t('template_ready', user_id)}\n\n{p['_template']}")
    if is_ambiguous(p):
        await context.bot.send_message(chat_id, t("need_more", user_id))
        return
    token = new_token()
    PENDING_ENTRY[token] = (kind, p)
    label = t("send_to_channel_q", user_id) if kind == "zapros" else t("save_to_list_q", user_id)
    await context.bot.send_message(
        chat_id, label, reply_markup=confirm_keyboard(user_id, kind, token)
    )
    if kind == "zapros":
        matches = find_matching_trucks(user_id, p.get("yuklash"), p.get("manzil"))
        if matches:
            preview = "\n\n\u2015\u2015\u2015\n\n".join(m["template"] for m in matches[:3])
            await context.bot.send_message(chat_id, f"{t('match_found', user_id)}\n\n{preview}")


async def deliver_entry(context, user_id: int, kind: str, p: dict):
    """Zaprosni kanalga, mashina ma'lumotini shaxsiy ro'yxatga yozadi."""
    entry_id = save_entry(user_id, kind, p, p["_template"])
    if kind == "zapros" and CHANNEL_ID:
        msg = await context.bot.send_message(chat_id=CHANNEL_ID, text=p["_template"])
        set_channel_message(entry_id, msg.message_id)
        return entry_id, msg.message_id
    return entry_id, None


async def handle_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = update.effective_user.id
    chat_id = update.effective_chat.id

    if data.startswith("k:") or (data.startswith("k") and ":" in data and PENDING_KIND):
        return await handle_kind_choice(update, context)

    if data == "multi:cancel":
        PENDING_MULTI.pop(chat_id, None)
        await query.edit_message_text(t("cancelled", user_id))
        return

    if data == "multi:send":
        entries = PENDING_MULTI.pop(chat_id, [])
        sent = 0
        for e in entries:
            kind = e["_kind"]
            try:
                await deliver_entry(context, user_id, kind, e)
                sent += 1
            except Exception:
                log.exception("multi yuborishda xato")
        await query.edit_message_text(f"{sent}/{len(entries)} ta yozuv qayta ishlandi \u2705")
        return

    if data.startswith("lang:"):
        lang = data.split(":")[1]
        set_lang(user_id, lang)
        await query.edit_message_text("\u0413\u043e\u0442\u043e\u0432\u043e \u2705" if lang == "ru" else "Bo'ldi \u2705")
        return

    if data.startswith("tsort:"):
        order = data.split(":")[1]
        await send_truck_list(update, user_id, chat_id, context, order=order)
        return

    if data == "tclearold":
        n = delete_old_trucks(user_id, days=3)
        await query.edit_message_text(f"{n} {t('cleared_old', user_id)}")
        return

    if data == "tclearall":
        n = delete_all_trucks(user_id)
        await query.edit_message_text(f"{n} {t('cleared_all', user_id)}")
        return

    if data.startswith("tdel:"):
        entry_id = int(data.split(":")[1])
        hard_delete_entry(entry_id)
        await query.edit_message_text(t("truck_deleted", user_id))
        return

    if data.startswith("tband:"):
        _, entry_id, flag = data.split(":")
        row = get_entry(int(entry_id))
        if row:
            set_status(int(entry_id), "band" if flag == "1" else "active")
            row = get_entry(int(entry_id))
            await query.edit_message_text(
                truck_entry_text(row), reply_markup=truck_keyboard(user_id, row)
            )
        return

    if data.startswith("tedit:"):
        entry_id = int(data.split(":")[1])
        row = get_entry(entry_id)
        if row:
            hard_delete_entry(entry_id)
            AWAITING_EDIT[chat_id] = row["kind"] or True
            await query.edit_message_text(t("edit_prompt", user_id))
        return

    if data.startswith("tcopy:"):
        entry_id = int(data.split(":")[1])
        row = get_entry(entry_id)
        if row:
            await query.answer(t("copied", user_id), show_alert=False)
            await context.bot.send_message(chat_id, row["template"])
        return

    if data.startswith("tpost:"):
        entry_id = int(data.split(":")[1])
        row = get_entry(entry_id)
        if row and CHANNEL_ID:
            try:
                await context.bot.send_message(chat_id=CHANNEL_ID, text=row["template"])
                await query.answer(t("posted_to_channel", user_id), show_alert=False)
            except Exception:
                log.exception("kanalga joylashda xato")
        return

    action, token = (data.split(":", 1) + [None])[:2]
    pending = PENDING_ENTRY.get(token) if token else None

    if action == "cancel":
        PENDING_ENTRY.pop(token, None)
        await query.edit_message_text(t("cancelled", user_id))
        return

    if action == "edit":
        AWAITING_EDIT[chat_id] = pending[0] if pending else True
        await query.edit_message_text(t("edit_prompt", user_id))
        return

    if action == "go" and pending:
        kind, p = pending
        PENDING_ENTRY.pop(token, None)
        try:
            entry_id, msg_id = await deliver_entry(context, user_id, kind, p)
        except Exception as exc:
            log.exception("yuborishda xato")
            await query.edit_message_text(f"Xato: {exc}")
            await notify_admin(context, f"\u26A0\uFE0F Yuborishda xato: {exc}")
            return
        if kind == "zapros" and msg_id:
            await query.edit_message_text(
                f"{t('sent', user_id)}\n\n{p['_template']}",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(t("delete_btn", user_id), callback_data=f"del:{entry_id}:{msg_id}")
                ]]),
            )
        else:
            await query.edit_message_text(f"{t('saved', user_id)}\n\n{p['_template']}")
        return

    if action == "del":
        _, entry_id, msg_id = data.split(":")
        try:
            await context.bot.delete_message(chat_id=CHANNEL_ID, message_id=int(msg_id))
            set_status(int(entry_id), "deleted")
        except Exception:
            log.exception("o'chirishda xato")
        await query.edit_message_text(t("deleted", user_id))
        return


# --------------------------------------------------------------------------
# Admin bildirishnomalari (68, 69, 61, 65-band)
# --------------------------------------------------------------------------

async def notify_admin(context, text: str):
    if not ADMIN_CHAT_ID:
        return
    try:
        await context.bot.send_message(chat_id=ADMIN_CHAT_ID, text=text)
    except Exception:
        log.exception("admin'ga xabar yuborib bo'lmadi")


async def on_startup(application: Application):
    try:  # ko'k "Menyu" tugmasidagi buyruqlar ro'yxati
        await application.bot.set_my_commands([
            BotCommand("start", "Boshlash / menyu"),
            BotCommand("moshinalar", "Mening mashinalarim"),
            BotCommand("stat", "Hisobot"),
            BotCommand("excel", "Excel yuklab olish"),
            BotCommand("til", "Til (uz/ru)"),
        ])
    except Exception:
        log.exception("buyruqlar ro'yxatini o'rnatib bo'lmadi")
    if ADMIN_CHAT_ID:
        try:
            await application.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"\u2705 Bot ishga tushdi \u2014 {datetime.now():%d.%m.%Y %H:%M}",
                reply_markup=main_menu(),
            )
        except Exception:
            log.exception("startup xabarini yuborib bo'lmadi")


async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.exception("Handler xatosi", exc_info=context.error)
    await notify_admin(context, f"\u26A0\uFE0F Botda xatolik:\n{context.error}")


async def daily_summary_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TZ) if TZ else datetime.now()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    s = stats_between(start_of_day, now)
    if not s:
        return
    text = (
        f"\U0001F4C5 Kunlik hisobot ({now:%d.%m.%Y}):\n"
        f"\U0001F4E6 Zapros: {s.get('zapros', 0)}\n"
        f"\U0001F69B Mashina: {s.get('mashina', 0)}"
    )
    await notify_admin(context, text)


async def expire_job(context: ContextTypes.DEFAULT_TYPE):
    n = expire_old_entries(days=7)
    if n:
        log.info("Avtomatik tozalash: %s ta eski yozuv o'chirildi", n)


async def weekly_summary_job(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TZ) if TZ else datetime.now()
    week_ago = now - timedelta(days=7)
    s = stats_between(week_ago, now)
    text = (
        f"\U0001F4CA Haftalik hisobot ({week_ago:%d.%m}-{now:%d.%m}):\n"
        f"\U0001F4E6 Zapros: {s.get('zapros', 0)}\n"
        f"\U0001F69B Mashina: {s.get('mashina', 0)}"
    )
    await notify_admin(context, text)


# --------------------------------------------------------------------------
# Ishga tushirish
# --------------------------------------------------------------------------

def main():
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN muhit o'zgaruvchisi kerak")

    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(on_startup)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("til", cmd_til))
    app.add_handler(CommandHandler("moshinalar", cmd_moshinalar))
    app.add_handler(CommandHandler("stat", cmd_stat))
    app.add_handler(CommandHandler("excel", cmd_excel))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(CallbackQueryHandler(handle_button))
    app.add_error_handler(on_error)

    if app.job_queue and TZ:
        app.job_queue.run_daily(
            daily_summary_job, time=dtime(hour=DAILY_SUMMARY_HOUR, tzinfo=TZ)
        )
        app.job_queue.run_daily(
            weekly_summary_job, time=dtime(hour=DAILY_SUMMARY_HOUR, tzinfo=TZ),
            days=(0,),  # dushanba
        )
        app.job_queue.run_daily(
            expire_job, time=dtime(hour=3, tzinfo=TZ)
        )

    log.info("Bot ishga tushdi")
    app.run_polling()


if __name__ == "__main__":
    main()
