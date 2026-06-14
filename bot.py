import asyncio
import logging
import os
import re
from datetime import datetime, timedelta

from google import genai
from google.genai import types
from telegram import Update, ChatPermissions
from telegram.ext import (
    Application,
    MessageHandler,
    filters,
    ContextTypes,
    CommandHandler,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ===================== SOZLAMALAR =====================
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "YOUR_GEMINI_KEY_HERE")
MUTE_HOURS = 12
NOTICE_DELETE_SECONDS = 5  # 0 = umuman chiqmasin
# ======================================================

gemini_client = genai.Client(api_key=GEMINI_API_KEY)
GEMINI_MODEL = "gemini-2.0-flash"

SYSTEM_PROMPT = (
    "Siz guruh chatidagi foydali assistentsiz. "
    "O'zbek, rus yoki ingliz tilida — savolning tiliga qarab javob bering. "
    "Qisqa, aniq va do'stona bo'ling."
)

# -------- REKLAMA ANIQLASH --------

AD_PATTERNS = [
    r"https?://[^\s]+",
    r"t\.me/[^\s]+",
    r"wa\.me/[^\s]+",
    r"@[a-zA-Z0-9_]{4,}",
]

AD_KEYWORDS = [
    # O'zbek
    "reklama", "sotamiz", "sotiladi", "arzon", "chegirma", "aksiya",
    "daromad", "ishlang", "hamkor", "sherik", "obuna", "kanaliga",
    "guruhga qo'shiling", "lotereya", "yutuq", "sovg'a", "tekin",
    "pul ishlang", "investitsiya", "kripto", "bitcoin", "usdt", "nft",
    "token", "pump", "signal", "kurs", "buyurtma", "ulgurji", "optom",
    "dostavka", "yetkazib", "smm", "coaching", "biznes taklif",
    # Rus
    "продаю", "продам", "скидка", "акция", "заработок", "реклама",
    "подписывайтесь", "переходите", "вступайте", "лотерея", "выигрыш",
    "бесплатно", "инвестиции", "крипто", "сигналы", "канал", "группа",
    # Ingliz
    "buy now", "click here", "free money", "earn money", "investment",
    "crypto", "bitcoin", "join now", "subscribe", "discount", "sale",
    "promo", "offer", "limited", "exclusive",
]

def is_advertisement(text: str) -> bool:
    text_lower = text.lower()
    for pattern in AD_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    matched = sum(1 for kw in AD_KEYWORDS if kw in text_lower)
    return matched >= 2

# -------- MUTE --------

async def mute_user(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message:
        return

    user = message.from_user
    chat = message.chat

    try:
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
        if bot_member.status not in ("administrator", "creator"):
            logger.warning("Bot admin emas.")
            return
    except Exception as e:
        logger.error(f"Bot status xatosi: {e}")
        return

    try:
        user_member = await context.bot.get_chat_member(chat.id, user.id)
        if user_member.status in ("administrator", "creator"):
            return
    except Exception:
        pass

    try:
        await message.delete()
    except Exception as e:
        logger.warning(f"Xabar o'chirish xatosi: {e}")

    until_date = datetime.now() + timedelta(hours=MUTE_HOURS)
    try:
        await context.bot.restrict_chat_member(
            chat_id=chat.id,
            user_id=user.id,
            permissions=ChatPermissions(
                can_send_messages=False,
                can_send_polls=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
            ),
            until_date=until_date,
        )
        logger.info(f"{user.id} ({user.username}) {MUTE_HOURS} soatga mute qilindi.")
    except Exception as e:
        logger.error(f"Mute xatosi: {e}")
        return

    if NOTICE_DELETE_SECONDS > 0:
        mention = f"@{user.username}" if user.username else user.first_name
        try:
            notice = await context.bot.send_message(
                chat_id=chat.id,
                text=f"🔇 {mention} reklama tarqatgani uchun {MUTE_HOURS} soatga mute qilindi.",
            )
            await asyncio.sleep(NOTICE_DELETE_SECONDS)
            await notice.delete()
        except Exception as e:
            logger.warning(f"Bildirishnoma xatosi: {e}")

# -------- AI CHAT (Gemini, suhbat xotirali) --------

# chat_id -> [ {role, parts} ]
chat_histories: dict[int, list] = {}

async def ai_reply(update: Update, text: str) -> None:
    chat_id = update.message.chat.id

    if chat_id not in chat_histories:
        chat_histories[chat_id] = []

    chat_histories[chat_id].append(
        types.Content(role="user", parts=[types.Part(text=text)])
    )

    # Oxirgi 20 xabarni saqla
    if len(chat_histories[chat_id]) > 20:
        chat_histories[chat_id] = chat_histories[chat_id][-20:]

    try:
        response = await asyncio.to_thread(
            gemini_client.models.generate_content,
            model=GEMINI_MODEL,
            contents=chat_histories[chat_id],
            config=types.GenerateContentConfig(
                system_instruction=SYSTEM_PROMPT,
                max_output_tokens=1000,
            ),
        )
        reply = response.text.strip()

        chat_histories[chat_id].append(
            types.Content(role="model", parts=[types.Part(text=reply)])
        )

        await update.message.reply_text(reply)
    except Exception as e:
        logger.error(f"Gemini xatosi: {e}")
        await update.message.reply_text("Kechirasiz, hozir javob bera olmayapman.")

# -------- HANDLERLAR --------

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return

    text = message.text

    if is_advertisement(text):
        await mute_user(update, context)
        return

    bot_username = context.bot.username
    if f"@{bot_username}" in text:
        clean_text = text.replace(f"@{bot_username}", "").strip()
        if clean_text:
            await ai_reply(update, clean_text)

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return
    await ai_reply(update, message.text.strip())

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.message.chat.type == "private":
        await update.message.reply_text(
            "Salom! Men AI assistentman (Gemini 2.0 Flash) 🤖\n"
            "Istalgan savol yoki mavzu haqida yozing!"
        )
    else:
        await update.message.reply_text(
            f"✅ Bot faol!\n"
            f"• Reklama → avtomatik o'chiriladi + {MUTE_HOURS} soat mute\n"
            f"• @{context.bot.username} orqali AI bilan gaplashing"
        )

# -------- MAIN --------

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            handle_private_message,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS,
            handle_group_message,
        )
    )

    logger.info("Bot ishga tushdi ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
