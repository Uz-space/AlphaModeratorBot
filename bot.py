import logging
import asyncio
import re
import google.generativeai as genai
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, MessageHandler, filters,
    ContextTypes, CommandHandler
)
from config import BOT_TOKEN, GEMINI_API_KEY, AD_PATTERNS

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("AlphaModeratorBot")

genai.configure(api_key=GEMINI_API_KEY)
gemini = genai.GenerativeModel("gemini-2.0-flash")


# ─────────────────────────────────────────────
#  AI tekshiruvlari
# ─────────────────────────────────────────────

async def ai_is_profanity(text: str) -> bool:
    try:
        prompt = (
            "Quyidagi xabar haqorat, so'kinish yoki kimnidir kamsitishni o'z ichiga oladimi?\n"
            "Faqat 'ha' yoki 'yoq' deb javob ber, boshqa hech narsa yozma.\n\n"
            f"Xabar: {text}"
        )
        response = await asyncio.to_thread(gemini.generate_content, prompt)
        return response.text.strip().lower().startswith("ha")
    except Exception as e:
        logger.error(f"AI profanity xato: {e}")
        return False


async def ai_is_ad(text: str) -> bool:
    try:
        prompt = (
            "Quyidagi xabar reklama, spam, mahsulot/xizmat taklifi yoki boshqa kanalga taklif o'z ichiga oladimi?\n"
            "Faqat 'ha' yoki 'yoq' deb javob ber, boshqa hech narsa yozma.\n\n"
            f"Xabar: {text}"
        )
        response = await asyncio.to_thread(gemini.generate_content, prompt)
        return response.text.strip().lower().startswith("ha")
    except Exception as e:
        logger.error(f"AI ad xato: {e}")
        return False


async def ask_gemini(question: str) -> str:
    try:
        prompt = (
            "Sen 'Alpha' ismli aqlli guruh assistentisan. "
            "O'zbek tilida qisqa, aniq va foydali javob ber. "
            "Markdown ishlatma, oddiy matn yoz.\n\n"
            f"Savol: {question}"
        )
        response = await asyncio.to_thread(gemini.generate_content, prompt)
        return response.text.strip()
    except Exception as e:
        logger.error(f"Gemini javob xato: {e}")
        return "Hozir javob bera olmayapman, keyinroq urinib ko'ring."


def contains_ad_link(text: str, message) -> bool:
    if message.forward_from_chat:
        return True
    for pattern in AD_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


def is_wake_word(text: str) -> bool:
    return bool(re.search(r'\balpha\b', text, re.IGNORECASE))


async def warn_and_delete(context, chat_id, text):
    """Ogohlantirish yuboradi va 1 soniyadan keyin o'chiradi."""
    try:
        sent = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        await asyncio.sleep(1)
        await context.bot.delete_message(chat_id=chat_id, message_id=sent.message_id)
    except Exception as e:
        logger.warning(f"warn_and_delete xato: {e}")


# ─────────────────────────────────────────────
#  Main handler
# ─────────────────────────────────────────────

async def moderate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message or update.edited_message
    if not message or not message.from_user:
        return

    if message.chat.type not in ("group", "supergroup"):
        return

    is_admin = False
    try:
        member = await context.bot.get_chat_member(message.chat_id, message.from_user.id)
        if member.status in ("administrator", "creator"):
            is_admin = True
    except Exception:
        pass

    text = message.text or message.caption or ""
    chat_id = message.chat_id
    msg_id = message.message_id
    user = message.from_user
    user_mention = f"<a href='tg://user?id={user.id}'>{user.first_name}</a>"

    # ── Wake word "alpha" — hamma ishlatishi mumkin ──
    if is_wake_word(text):
        question = re.sub(r'\balpha\b', '', text, flags=re.IGNORECASE).strip(" ?,!:")
        if not question:
            question = "O'zingni tanit va nima qila olishingni ayt"

        # Typing ko'rinishi — javob kelguncha davom etadi
        async def keep_typing():
            for _ in range(15):
                try:
                    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
                except Exception:
                    break
                await asyncio.sleep(4)

        typing_task = asyncio.create_task(keep_typing())
        answer = await ask_gemini(question)
        typing_task.cancel()

        # Alpha javobi O'CHIRILMAYDI — qolib turadi
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🤖 <b>Alpha:</b>\n{answer}",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Alpha javob yuborishda xato: {e}")
        return

    # Adminlarni moderatsiyadan o'tkazma
    if is_admin:
        return

    if not text:
        return

    # ── 1. Tez link/forward tekshiruvi ──
    if contains_ad_link(text, message):
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
        asyncio.create_task(warn_and_delete(
            context, chat_id,
            f"🚫 {user_mention} — <b>Reklama aniqlandi!</b> Xabar o'chirildi."
        ))
        return

    # ── 2. AI paralel tekshiruvi ──
    is_prof, is_ad = await asyncio.gather(
        ai_is_profanity(text),
        ai_is_ad(text)
    )

    if is_prof:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user.id)
            logger.info(f"🚫 Ban: {user.id}")
        except Exception as e:
            logger.warning(f"Ban/delete xato: {e}")
        # Ogohlantirish 1 sek da o'chadi
        asyncio.create_task(warn_and_delete(
            context, chat_id,
            f"⛔️ {user_mention} — <b>Haqorat aniqlandi!</b> Ban qilindi."
        ))
        return

    if is_ad:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
        except Exception:
            pass
        asyncio.create_task(warn_and_delete(
            context, chat_id,
            f"🚫 {user_mention} — <b>Reklama aniqlandi!</b> Xabar o'chirildi."
        ))
        return


# ─────────────────────────────────────────────
#  /start
# ─────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>AlphaModeratorBot ishga tayyor!</b>\n\n"
        "🛡️ <b>AI Moderatsiya:</b>\n"
        "• Haqorat → AI aniqlaydi → o'chirish + ban (ogohlantirish 1 sek da o'chadi)\n"
        "• Reklama → AI aniqlaydi → o'chirish (ogohlantirish 1 sek da o'chadi)\n\n"
        "🧠 <b>AI Assistant:</b>\n"
        "• <b>alpha</b> + savol yozing → AI javob beradi\n"
        "• Misol: <i>alpha Python nima?</i>",
        parse_mode="HTML"
    )


# ─────────────────────────────────────────────
#  Run
# ─────────────────────────────────────────────

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(
        (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
        moderate
    ))
    app.add_handler(MessageHandler(
        filters.UpdateType.EDITED_MESSAGE & (filters.TEXT | filters.CAPTION),
        moderate
    ))
    logger.info("🚀 AlphaModeratorBot ishga tushdi...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
