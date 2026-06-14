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
NOTICE_DELETE_SECONDS = 5
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
    "reklama", "sotamiz", "sotiladi", "arzon", "chegirma", "aksiya",
    "daromad", "ishlang", "hamkor", "sherik", "obuna", "kanaliga",
    "guruhga qo'shiling", "lotereya", "yutuq", "sovg'a", "tekin",
    "pul ishlang", "investitsiya", "kripto", "bitcoin", "usdt", "nft",
    "token", "pump", "signal", "kurs", "buyurtma", "ulgurji", "optom",
    "dostavka", "yetkazib", "smm", "coaching", "biznes taklif",
    "продаю", "продам", "скидка", "акция", "заработок", "реклама",
    "подписывайтесь", "переходите", "вступайте", "лотерея", "выигрыш",
    "бесплатно", "инвестиции", "крипто", "сигналы", "канал", "группа",
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

# -------- UMUMIY MUTE FUNKSIYASI --------

async def do_mute(context, chat_id: int, user_id: int, hours: int):
    """Foydalanuvchini mute qiladi."""
    until_date = datetime.now() + timedelta(hours=hours)
    await context.bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=ChatPermissions(
            can_send_messages=False,
            can_send_polls=False,
            can_send_other_messages=False,
            can_add_web_page_previews=False,
        ),
        until_date=until_date,
    )

async def do_unmute(context, chat_id: int, user_id: int):
    """Foydalanuvchini unmute qiladi."""
    await context.bot.restrict_chat_member(
        chat_id=chat_id,
        user_id=user_id,
        permissions=ChatPermissions(
            can_send_messages=True,
            can_send_polls=True,
            can_send_other_messages=True,
            can_add_web_page_previews=True,
            can_invite_users=True,
        ),
    )

# -------- /mute KOMANDASI (adminlar uchun, reply qilib) --------

async def mute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or message.chat.type == "private":
        return

    # Admin tekshiruvi
    try:
        caller = await context.bot.get_chat_member(message.chat.id, message.from_user.id)
        if caller.status not in ("administrator", "creator"):
            await message.reply_text("❌ Faqat adminlar mute qila oladi.")
            return
    except Exception as e:
        logger.error(f"Admin tekshiruv xatosi: {e}")
        return

    # Reply bo'lishi kerak
    if not message.reply_to_message:
        await message.reply_text("❌ Kimni mute qilish uchun o'sha odamning xabariga reply qiling.")
        return

    target_user = message.reply_to_message.from_user
    chat_id = message.chat.id

    # Targetni admin bo'lsa o'tkazib yubor
    try:
        target_member = await context.bot.get_chat_member(chat_id, target_user.id)
        if target_member.status in ("administrator", "creator"):
            await message.reply_text("❌ Admin foydalanuvchini mute qilib bo'lmaydi.")
            return
    except Exception:
        pass

    # Mute qil
    try:
        await do_mute(context, chat_id, target_user.id, MUTE_HOURS)
        logger.info(f"{target_user.id} admin tomonidan {MUTE_HOURS} soatga mute qilindi.")
    except Exception as e:
        logger.error(f"Mute xatosi: {e}")
        await message.reply_text("❌ Mute qilib bo'lmadi.")
        return

    # Admin /mute xabarini o'chir
    try:
        await message.delete()
    except Exception:
        pass

    # Userning barcha ko'rinadigan xabarlarini o'chir
    # reply qilingan xabarni o'chir
    try:
        await message.reply_to_message.delete()
    except Exception:
        pass

    # Bildirishnoma yubor va tez o'chir
    mention = f"@{target_user.username}" if target_user.username else target_user.first_name
    try:
        notice = await context.bot.send_message(
            chat_id=chat_id,
            text=f"🔇 {mention} {MUTE_HOURS} soatga mute qilindi.",
        )
        await asyncio.sleep(2)
        await notice.delete()
    except Exception as e:
        logger.warning(f"Bildirishnoma xatosi: {e}")

# -------- AVTOMATIK REKLAMA MUTE --------

async def auto_mute_advertiser(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Reklama yozgan odamni avtomatik mute qiladi va xabarini o'chiradi."""
    message = update.message
    if not message:
        return

    user = message.from_user
    chat = message.chat

    try:
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
        if bot_member.status not in ("administrator", "creator"):
            return
    except Exception:
        return

    try:
        user_member = await context.bot.get_chat_member(chat.id, user.id)
        if user_member.status in ("administrator", "creator"):
            return
    except Exception:
        pass

    try:
        await message.delete()
    except Exception:
        pass

    try:
        await do_mute(context, chat.id, user.id, MUTE_HOURS)
        logger.info(f"Reklamachi {user.id} avtomatik mute qilindi.")
    except Exception as e:
        logger.error(f"Auto mute xatosi: {e}")
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
        except Exception:
            pass

# -------- /unmute KOMANDASI --------

async def unmute_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or message.chat.type == "private":
        return

    try:
        caller = await context.bot.get_chat_member(message.chat.id, message.from_user.id)
        if caller.status not in ("administrator", "creator"):
            await message.reply_text("❌ Faqat adminlar unmute qila oladi.")
            return
    except Exception as e:
        logger.error(f"Admin tekshiruv xatosi: {e}")
        return

    if not message.reply_to_message:
        await message.reply_text("❌ Kimni unmute qilish uchun o'sha odamning xabariga reply qiling.")
        return

    target_user = message.reply_to_message.from_user
    try:
        await do_unmute(context, message.chat.id, target_user.id)
        mention = f"@{target_user.username}" if target_user.username else target_user.first_name
        notice = await message.reply_text(f"✅ {mention} unmute qilindi.")
        await asyncio.sleep(3)
        await notice.delete()
        await message.delete()
    except Exception as e:
        logger.error(f"Unmute xatosi: {e}")
        await message.reply_text("❌ Unmute qilib bo'lmadi.")

# -------- AI CHAT --------

chat_histories: dict = {}

async def ai_reply(update: Update, text: str) -> None:
    chat_id = update.message.chat.id

    if chat_id not in chat_histories:
        chat_histories[chat_id] = []

    chat_histories[chat_id].append(
        types.Content(role="user", parts=[types.Part(text=text)])
    )

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

# -------- GURUH XABARLARI --------

async def handle_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return

    text = message.text

    # Reklama tekshiruvi
    if is_advertisement(text):
        await auto_mute_advertiser(update, context)
        return

    # Bot mention qilinsa AI javob bersin
    bot_username = context.bot.username
    if f"@{bot_username}" in text:
        clean_text = text.replace(f"@{bot_username}", "").strip()
        if clean_text:
            await ai_reply(update, clean_text)

# -------- PRIVATE CHAT --------

async def handle_private_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if not message or not message.text:
        return
    await ai_reply(update, message.text.strip())

# -------- START --------

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
            f"• /mute — xabariga reply qilib yozing (faqat adminlar)\n"
            f"• /unmute — xabariga reply qilib yozing (faqat adminlar)\n"
            f"• @{context.bot.username} savol — AI javob beradi"
        )

# -------- MAIN --------

def main() -> None:
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("mute", mute_command))
    app.add_handler(CommandHandler("unmute", unmute_command))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE,
            handle_private_message,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & (filters.ChatType.GROUPS | filters.ChatType.SUPERGROUP),
            handle_group_message,
        )
    )

    logger.info("Bot ishga tushdi ✅")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
