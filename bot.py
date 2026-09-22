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
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
AWAITING_EDIT: dict[int, bool] = {}
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


def recent_trucks(user_id: int, limit: int = 20):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM entries WHERE user_id=? AND kind='mashina' "
        "ORDER BY id DESC LIMIT ?",
        (user_id, limit),
    ).fetchall()
    conn.close()
    return rows


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
}


def t(key: str, user_id: int) -> str:
    lang = get_lang(user_id)
    return UI.get(key, {}).get(lang, UI.get(key, {}).get("uz", key))


# --------------------------------------------------------------------------
# Shahar koordinatalari (51/52/59-band: taxminiy masofa/yoqilg'i/vaqt)
# --------------------------------------------------------------------------

CITY_COORDS = {
    "toshkent": (41.2995, 69.2401), "самарканд": (39.6542, 66.9597),
    "samarqand": (39.6542, 66.9597), "buxoro": (39.7747, 64.4286),
    "qarshi": (38.8606, 65.7891), "карши": (38.8606, 65.7891),
    "termiz": (37.2242, 67.2783), "nukus": (42.4600, 59.6100),
    "urganch": (41.5500, 60.6333), "xiva": (41.3783, 60.3639),
    "namangan": (40.9983, 71.6726), "andijon": (40.7833, 72.3333),
    "farg'ona": (40.3833, 71.7833), "fargona": (40.3833, 71.7833),
    "jizzax": (40.1158, 67.8422), "guliston": (40.4897, 68.7842),
    "navoiy": (40.1030, 65.3686),
    "alashankou": (45.1800, 82.5700), "алашанькоу": (45.1800, 82.5700),
    "улугчат": (45.1800, 82.5700), "ulug'chat": (45.1800, 82.5700),
    "moskva": (55.7558, 37.6173), "москва": (55.7558, 37.6173),
    "praga": (50.0755, 14.4378), "прага": (50.0755, 14.4378),
    "varshava": (52.2297, 21.0122), "almaty": (43.2220, 76.8512),
    "bishkek": (42.8746, 74.5698), "yiwu": (29.3060, 120.0762),
}


def normalize_city(name: str) -> str:
    if not name:
        return ""
    return re.sub(r"[^\w']", "", name.lower())


def find_city_coords(name: str):
    key = normalize_city(name)
    for city_key, coords in CITY_COORDS.items():
        if normalize_city(city_key) == key or key in normalize_city(city_key):
            return coords
    return None


def haversine_km(c1, c2) -> float:
    import math
    lat1, lon1 = map(math.radians, c1)
    lat2, lon2 = map(math.radians, c2)
    dlat, dlon = lat2 - lat1, lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(a))


def estimate_trip(yuklash: str, manzil: str):
    """51/52/59-band: taxminiy masofa (km), vaqt (soat), yoqilg'i xarajati (so'm)."""
    c1, c2 = find_city_coords(yuklash or ""), find_city_coords(manzil or "")
    if not c1 or not c2:
        return None
    straight = haversine_km(c1, c2)
    road_km = straight * 1.3  # yo'l egriligi uchun taxminiy koeffitsient
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

{{"yuklash": string yoki null, "manzil": string yoki null, "yuk_turi": string yoki null, \
"mashina_turi": string yoki null, "soni": number yoki null, "dona": number yoki null, \
"tonna": number yoki null, "kub": number yoki null, "tolov": string yoki null}}

MUHIM qoidalar:
- soni — FAQAT kerakli/mavjud MASHINALAR soni. Tovar/buyum soni bo'lsa "dona"ga yoz.
- yuklash/manzil — shahar/joy nomi. Aniq tanib bo'lmasa, asl yozilishini o'zgartirmay qaytar.
- mashina_turi — kuzov turi (тент, изотерма va h.k.), faqat aniq aytilgan bo'lsa.
- tolov — to'lov turi, qisqa ("нақд", "ўтказма"), bo'lmasa null.
MUHIM: yuklash, manzil, yuk_turi, mashina_turi, tolov — bu matn maydonlarining \
barchasini FAQAT KIRILL alifbosida qaytar, hatto foydalanuvchi lotin yozuvida \
yozgan bo'lsa ham. Aniq tanib bo'lmasa, harflarini kirillga almashtirib yoz.
Noaniq maydonni taxmin qilma — null qoldir.

Matn: "{text}"
"""


def fallback_parse(text: str) -> dict:
    def find(pattern):
        m = re.search(pattern, text, re.IGNORECASE)
        return m.group(1) if m else None

    soni = find(r"(\d+)\s*(?:та|ta)\b")
    tonna = find(r"(\d+(?:[.,]\d+)?)\s*тонн")
    kub = find(r"(\d+(?:[.,]\d+)?)\s*куб")
    dona = find(r"(\d+)\s*(?:шт|дона|dona)\b")
    tolov = None
    if re.search(r"накд|нақд|naqd", text, re.IGNORECASE):
        tolov = "нақд"
    elif re.search(r"перечисл|o'tkazma|otkazma|ўтказма", text, re.IGNORECASE):
        tolov = "ўтказма"
    return {
        "yuklash": None, "manzil": None, "yuk_turi": None, "mashina_turi": None,
        "soni": int(soni) if soni else None, "dona": int(dona) if dona else None,
        "tonna": float(tonna.replace(",", ".")) if tonna else None,
        "kub": float(kub.replace(",", ".")) if kub else None, "tolov": tolov,
    }


def parse_with_claude(text: str) -> dict:
    from anthropic import Anthropic

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": PARSE_PROMPT.format(text=text)}],
    )
    raw = "".join(b.text for b in resp.content if b.type == "text")
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        raise ValueError("JSON topilmadi")
    return json.loads(match.group(0))


def parse_text(text: str) -> dict:
    try:
        return parse_with_claude(text) if ANTHROPIC_API_KEY else fallback_parse(text)
    except Exception:
        log.exception("parse xatosi")
        return fallback_parse(text)


def is_ambiguous(p: dict) -> bool:
    keys = ["yuklash", "manzil", "tonna", "kub", "soni", "dona"]
    return not any(p.get(k) for k in keys)


def cap(s):
    if not s:
        return s
    s = str(s).strip()
    return s[0].upper() + s[1:]


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
    route = f"{cap(p.get('yuklash')) or '?'} \u2192 {cap(p.get('manzil')) or '?'}"
    if p.get("round_trip"):
        route += " \U0001F501"  # borib-kelish belgisi
    lines.append(f"\U0001F4CD {route}")
    if p.get("yuk_turi"):
        lines.append(f"\U0001F4E6 \u042E\u043A: {p['yuk_turi']}")
    if p.get("dona"):
        lines.append(f"\U0001F522 \u041C\u0438\u049B\u0434\u043E\u0440: {p['dona']} \u0434\u043E\u043D\u0430")
    bits = []
    if p.get("soni"):
        mt = p.get("mashina_turi")
        bits.append(f"{p['soni']} \u0442\u0430 {mt if mt else '\u043C\u0430\u0448\u0438\u043D\u0430'}")
    elif p.get("mashina_turi"):
        bits.append(cap(p["mashina_turi"]))
    if p.get("tonna"):
        bits.append(f"{p['tonna']} \u0442\u043E\u043D\u043D\u0430")
    if p.get("kub"):
        bits.append(f"{p['kub']} \u043A\u0443\u0431")
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

def build_entry(text: str, kind: str) -> dict:
    p = parse_text(text)
    p["sana"] = extract_date_hint(text)
    p["round_trip"] = is_round_trip(text)
    p["_kind"] = kind
    p["_template"] = format_template(kind, p)
    p["_trip"] = estimate_trip(p.get("yuklash"), p.get("manzil"))
    return p


def looks_multi(text: str) -> list:
    lines = [l.strip() for l in text.split("\n") if l.strip()]
    if len(lines) < 2:
        return []
    substantive = [l for l in lines if re.search(r"\d", l)]
    return lines if len(substantive) >= 2 else []


# --------------------------------------------------------------------------
# Handlerlar
# --------------------------------------------------------------------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Salom! Menga yuk yoki mashina haqida yozing.\n\n"
        "Masalan:\n"
        "\u2022 Toshkentdan Qarshiga 3 ta tent 22 tonna kerak naqd (ZAPROS)\n"
        "\u2022 Samarqandda 2 ta mashina bor, 20 tonnagacha (MASHINA BOR)\n\n"
        "Buyruqlar: /moshinalar /stat /excel /til"
    )


async def cmd_til(update: Update, context: ContextTypes.DEFAULT_TYPE):
    args = context.args
    if args and args[0] in ("uz", "ru"):
        set_lang(update.effective_user.id, args[0])
        await update.message.reply_text("OK \u2705" if args[0] == "ru" else "Bo'ldi \u2705")
    else:
        await update.message.reply_text("Til tanlang: /til uz  yoki  /til ru")


async def cmd_moshinalar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = recent_trucks(update.effective_user.id)
    if not rows:
        await update.message.reply_text("Ro'yxat bo'sh.")
        return
    chunks = [r["template"] for r in rows]
    await update.message.reply_text("\n\n\u2015\u2015\u2015\n\n".join(chunks))


async def cmd_stat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(TZ) if TZ else datetime.now()
    week_ago = now - timedelta(days=7)
    s = stats_between(week_ago, now)
    text = (
        f"\U0001F4CA So'nggi 7 kun:\n"
        f"\U0001F4E6 Zapros: {s.get('zapros', 0)}\n"
        f"\U0001F69B Mashina: {s.get('mashina', 0)}"
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
    await send_trip_info(update, p)


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    if not text:
        return
    chat_id = update.effective_chat.id
    user_id = update.effective_user.id
    AWAITING_EDIT.pop(chat_id, None)

    thinking = await update.message.reply_text(t("thinking", user_id))

    lines = looks_multi(text)
    if lines:  # 1-band: ko'p yozuvli xabar
        await thinking.delete()
        entries = []
        for line in lines:
            kind = classify_kind(line) or "zapros"
            entries.append(build_entry(line, kind))
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

    kind = classify_kind(text)
    if not kind:  # noaniq — foydalanuvchidan so'raladi
        await thinking.delete()
        PENDING_KIND[chat_id] = text
        await update.message.reply_text(t("which_kind", user_id), reply_markup=kind_keyboard())
        return

    p = build_entry(text, kind)
    await thinking.delete()
    await present_entry(update, user_id, kind, p)


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

    action, token = (data.split(":", 1) + [None])[:2]
    pending = PENDING_ENTRY.get(token) if token else None

    if action == "cancel":
        PENDING_ENTRY.pop(token, None)
        await query.edit_message_text(t("cancelled", user_id))
        return

    if action == "edit":
        AWAITING_EDIT[chat_id] = True
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
    if ADMIN_CHAT_ID:
        try:
            await application.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"\u2705 Bot ishga tushdi \u2014 {datetime.now():%d.%m.%Y %H:%M}",
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

    log.info("Bot ishga tushdi")
    app.run_polling()


if __name__ == "__main__":
    main()
