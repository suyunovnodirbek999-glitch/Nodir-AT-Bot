import os
import json
import sqlite3
import logging
from datetime import datetime
from openpyxl import Workbook
from google import genai
from google.genai import types
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)

# Railway Variables'dan o'zgaruvchilarni olish
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
CHANNEL_ID = os.getenv("CHANNEL_ID", "@yuklar_at")

# Admin ID raqamlari (Vergul bilan ajratib yozish ham mumkin)
raw_admins = os.getenv("ADMIN_ID", os.getenv("ADMIN_IDS", ""))
ALLOWED_USERS = [int(x.strip()) for x in raw_admins.split(",") if x.strip().isdigit()]

ADMIN_URL = "https://t.me/nodirbek_logist"
CHANNEL_URL = "https://t.me/yuklar_at"

# Gemini Flash mijozini ulash
client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

DB_PATH = "yuklar.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS yuklar (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sana TEXT,
            turi TEXT,
            qayerdan TEXT,
            qayerga TEXT,
            vazni TEXT,
            transport TEXT,
            narx TEXT,
            aloqa TEXT,
            asl_matn TEXT
        )
    """)
    conn.commit()
    conn.close()


def check_whitelist(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True
    return user_id in ALLOWED_USERS


async def send_access_denied(update: Update):
    keyboard = [
        [InlineKeyboardButton("👨‍💻 Admin bilan bog‘lanish", url=ADMIN_URL)],
        [InlineKeyboardButton("📢 Kanalimizga obuna bo‘lish", url=CHANNEL_URL)],
    ]
    markup = InlineKeyboardMarkup(keyboard)
    text = (
        "⛔️ <b>Kechirasiz, bu bot faqat ruxsat etilgan foydalanuvchilar uchun ishlaydi!</b>\n\n"
        "Botdan foydalanish yoki hamkorlik bo‘yicha murojaat qilish uchun admin bilan bog‘laning "
        "hamda rasmiy kanalimizni kuzatib boring:"
    )
    await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")


def parse_cargo_with_gemini(text: str) -> dict:
    prompt = f"""
Quyidagi logistika xabarini tahlil qil va ma'lumotlarni aniq JSON formatda qaytar.
Agar shahar yoki davlat nomi xato, ruscha yoki qisqartma bo'lsa ham to'g'ri topib,
yoniga o'sha davlat bayrog'i emojisini qo'sh (masalan: "🇺🇿 Toshkent", "🇷🇺 Moskva", "🇰🇿 Olmaota").

JSON maydonlari:
- "turi": agar yuk yoki mashina kerak bo'lsa "zapros", agar bo'sh mashina bor bo'lsa "mashina_bor"
- "qayerdan": jo'nash manzili (bayroq + shahar/davlat)
- "qayerga": borish manzili (bayroq + shahar/davlat)
- "vazni": yuk og'irligi yoki hajmi (masalan: "22 tonna", noma'lum bo'lsa "Ko'rsatilmagan")
- "transport": transport turi (masalan: "Tent", "Ref", "Fura", noma'lum bo'lsa "Standart")
- "narx": stavka yoki to'lov turi (noma'lum bo'lsa "Kelishiladi")
- "aloqa": telefon raqam yoki username (noma'lum bo'lsa "@nodirbek_logist")
- "izoh": yuk nomi yoki qo'shimcha ma'lumot

Xabar matni:
{text}
"""
    response = client.models.generate_content(
        model="gemini-flash-latest",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )
    return json.loads(response.text)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_whitelist(update.effective_user.id):
        await send_access_denied(update)
        return

    await update.message.reply_text(
        "✅ <b>Assalomu alaykum! Logistika bot ishga tayyor.</b>\n\n"
        "📝 Yuk yoki transport haqidagi xabarni yuboring — Gemini AI shaharlarni avtomatik aniqlab, bazaga saqlaydi va kanalga chiqaradi.\n\n"
        "📊 /excel — Barcha yuklar ro‘yxatini Excel (.xlsx) formatda yuklab olish\n"
        "🆔 /id — O‘z Telegram ID raqamingizni ko‘rish",
        parse_mode="HTML",
    )


async def id_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"Sizning Telegram ID raqamingiz: <code>{update.effective_user.id}</code>",
        parse_mode="HTML",
    )


async def excel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not check_whitelist(update.effective_user.id):
        await send_access_denied(update)
        return

    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT id, sana, turi, qayerdan, qayerga, vazni, transport, narx, aloqa FROM yuklar ORDER BY id DESC")
    rows = c.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("📭 Hozircha bazada saqlangan ma'lumotlar yo‘q.")
        return

    wb = Workbook()
    ws = wb.active
    ws.title = "Yuklar Reestri"
    ws.append(["ID", "Sana", "Turi", "Qayerdan", "Qayerga", "Vazni", "Transport", "Narx", "Aloqa"])

    for r in rows:
        ws.append(list(r))

    file_name = "yuklar_hisoboti.xlsx"
    wb.save(file_name)

    with open(file_name, "rb") as f:
        await update.message.reply_document(document=f, filename=file_name, caption="📊 Barcha yuklar reestri")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    if not check_whitelist(update.effective_user.id):
        await send_access_denied(update)
        return

    wait_msg = await update.message.reply_text("⏳ Tahlil qilinmoqda...")

    try:
        data = parse_cargo_with_gemini(update.message.text)
        sana = datetime.now().strftime("%Y-%m-%d %H:%M")

        turi = data.get("turi", "zapros")
        qayerdan = data.get("qayerdan", "Noma'lum")
        qayerga = data.get("qayerga", "Noma'lum")
        vazni = data.get("vazni", "Ko'rsatilmagan")
        transport = data.get("transport", "Standart")
        narx = data.get("narx", "Kelishiladi")
        aloqa = data.get("aloqa", "@nodirbek_logist")
        izoh = data.get("izoh", "")

        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT INTO yuklar (sana, turi, qayerdan, qayerga, vazni, transport, narx, aloqa, asl_matn) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (sana, turi, qayerdan, qayerga, vazni, transport, narx, aloqa, update.message.text),
        )
        conn.commit()
        conn.close()

        post_text = (
            f"📦 <b>YANGI YUK / TRANSPORT E'LONI</b>\n\n"
            f"📍 <b>Yo‘nalish:</b> {qayerdan} ➡️ {qayerga}\n"
            f"⚖️ <b>Vazni / Hajmi:</b> {vazni}\n"
            f"🚛 <b>Transport turi:</b> {transport}\n"
            f"💰 <b>To‘lov / Narx:</b> {narx}\n"
        )
        if izoh:
            post_text += f"📝 <b>Qo‘shimcha:</b> {izoh}\n"
        post_text += f"\n📞 <b>Murojaat uchun:</b> {aloqa}\n📢 <b>Kanal:</b> @yuklar_at"

        if turi == "zapros" and CHANNEL_ID:
            try:
                await context.bot.send_message(chat_id=CHANNEL_ID, text=post_text, parse_mode="HTML")
                status_note = "✅ <i>Kanalga joylandi va bazaga saqlandi!</i>"
            except Exception as e:
                status_note = f"⚠️ <i>Bazaga saqlandi (Kanalga yuborishda xatolik: {e})</i>"
        else:
            status_note = "💾 <i>Bo‘sh transport sifatida shaxsiy bazaga saqlandi!</i>"

        await wait_msg.edit_text(f"{post_text}\n\n{status_note}", parse_mode="HTML")

    except Exception as e:
        await wait_msg.edit_text(f"❌ Xatolik yuz berdi: {e}")


def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("id", id_cmd))
    app.add_handler(CommandHandler("excel", excel_cmd))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    app.run_polling()


if __name__ == "__main__":
    main()
