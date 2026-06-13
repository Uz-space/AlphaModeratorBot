import logging
import asyncio
import re
import os
from groq import Groq
from telegram import Update
from telegram.ext import (
    ApplicationBuilder, MessageHandler, filters,
    ContextTypes, CommandHandler
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

AD_PATTERNS = [
    r'https?://[^\s]+',
    r't\.me/[^\s]+',
    r'(\+998|998)\s*[\d\s\-]{9,}',
    r'\b(click\.uz|payme|uzcard|humo)\b',
]

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("AlphaModeratorBot")

groq_client = Groq(api_key=GROQ_API_KEY)
MODEL = "llama-3.3-70b-versatile"


async def groq_ask(prompt: str) -> str | None:
    try:
        response = await asyncio.to_thread(
            groq_client.chat.completions.create,
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=512,
            temperature=0.3,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"Groq xato: {e}")
        return None


async def ai_is_profanity(text: str) -> bool:
    prompt = (
        "Quyidagi xabar haqorat, so'kinish yoki kimnidir kamsitishni o'z ichiga oladimi?\n"
        "Faqat 'ha' yoki 'yoq' deb javob ber, boshqa hech narsa yozma.\n\n"
        f"Xabar: {text}"
    )
    result = await groq_ask(prompt)
    return result is not None and result.lower().startswith("ha")


async def ai_is_ad(text: str) -> bool:
    prompt = (
        "Quyidagi xabar reklama, spam, mahsulot/xizmat taklifi yoki boshqa kanalga taklif o'z ichiga oladimi?\n"
        "Faqat 'ha' yoki 'yoq' deb javob ber, boshqa hech narsa yozma.\n\n"
        f"Xabar: {text}"
    )
    result = await groq_ask(prompt)
    return result is not None and result.lower().startswith("ha")


async def ask_ai(question: str) -> str:
    prompt = (
        "Sen 'Alpha' ismli aqlli guruh assistentisan. "
        "O'zbek tilida qisqa, aniq va foydali javob ber. "
        "Markdown ishlatma, oddiy matn yoz.\n\n"
        f"Savol: {question}"
    )
    result = await groq_ask(prompt)
    return result or "Hozir javob bera olmayapman, keyinroq urinib ko'ring."


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
    try:
        sent = await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML")
        await asyncio.sleep(1)
        await context.bot.delete_message(chat_id=chat_id, message_id=sent.message_id)
    except Exception as e:
        logger.warning(f"warn_and_delete xato: {e}")


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

    # Wake word "alpha"
    if is_wake_word(text):
        question = re.sub(r'\balpha\b', '', text, flags=re.IGNORECASE).strip(" ?,!:")
        if not question:
            question = "O'zingni tanit va nima qila olishingni ayt"

        async def keep_typing():
            for _ in range(15):
                try:
                    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
                except Exception:
                    break
                await asyncio.sleep(4)

        typing_task = asyncio.create_task(keep_typing())
        answer = await ask_ai(question)
        typing_task.cancel()

        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🤖 <b>Alpha:</b>\n{answer}",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.warning(f"Alpha javob yuborishda xato: {e}")
        return

    if is_admin:
        return

    if not text:
        return

    # Tez link tekshiruvi
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

    # AI paralel tekshiruvi
    is_prof, is_ad = await asyncio.gather(
        ai_is_profanity(text),
        ai_is_ad(text)
    )

    if is_prof:
        try:
            await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user.id)
        except Exception as e:
            logger.warning(f"Ban/delete xato: {e}")
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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>AlphaModeratorBot ishga tayyor!</b>\n\n"
        "🛡️ <b>AI Moderatsiya:</b>\n"
        "• Haqorat → o'chirish + ban\n"
        "• Reklama → o'chirish\n\n"
        "🧠 <b>AI Assistant:</b>\n"
        "• <b>alpha</b> + savol → AI javob beradi\n"
        "• Misol: <i>alpha Python nima?</i>",
        parse_mode="HTML"
    )


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
