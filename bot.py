"""
Guardian Bot - Telegram Group Protection Bot
- Clone detection → permanent ban (cannot rejoin)
- ALL data saved to guardian_data.json — survives server restarts & migrations
- Crypto (BTC/TRX/HK) buy/sell → allowed for everyone
- Other ads / group links / forwards → message deleted, admin decides mute
- Mute/Unmute inline buttons for admins
- Bot notifications auto-deleted for cleanliness
"""

import json
import logging
import asyncio
import os
from datetime import datetime, timedelta
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, ChatPermissions
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ChatMemberHandler,
    CallbackQueryHandler, ContextTypes, filters
)
from telegram.constants import ChatMemberStatus
from telegram.error import TelegramError

# ─── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN = "8205611933:AAGO6_YCiG_BvgP6AeNGzoQh8-s9YhyM8kg"

# File where all data is saved (same folder as this script)
DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "guardian_data.json")

# Seconds before bot notification messages auto-delete
AUTO_DELETE_DELAY = 10

# Crypto keywords — messages with these pass for EVERYONE
CRYPTO_KEYWORDS = [
    "btc", "bitcoin",
    "trx", "tron",
    "hk",
    "usdt", "p2p",
    "sotaman", "sotiladi", "sotib olaman", "sotib olish",
    "sell", "buy", "buying", "selling", "trade", "swap", "exchange",
    "kripto", "crypto",
]

# ─── LOGGING ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("GuardianBot")

# ─── PERSISTENT STORAGE ────────────────────────────────────────────────────────
#
# guardian_data.json structure:
# {
#   "member_registry": {
#     "CHAT_ID": { "firstname:username": USER_ID, ... }
#   },
#   "permanent_bans": {
#     "CHAT_ID": [USER_ID, USER_ID, ...]
#   },
#   "clone_log": [
#     { "chat_id": ..., "clone_id": ..., "original_id": ..., "name": ..., "banned_at": ... }
#   ]
# }

def load_data() -> dict:
    """Load all data from JSON file. Returns default structure if file missing."""
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            logger.info(f"Data loaded from {DATA_FILE}")
            return data
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Failed to load data: {e} — starting fresh")
    return {"member_registry": {}, "permanent_bans": {}, "clone_log": []}


def save_data():
    """Save all in-memory data to JSON file immediately."""
    data = {
        "member_registry": {
            str(cid): registry
            for cid, registry in member_registry.items()
        },
        "permanent_bans": {
            str(cid): list(bans)
            for cid, bans in permanent_bans.items()
        },
        "clone_log": clone_log,
    }
    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        logger.info("Data saved.")
    except IOError as e:
        logger.error(f"Failed to save data: {e}")


def _load_into_memory(data: dict):
    """Parse loaded JSON into working in-memory dicts."""
    # member_registry
    for chat_id_str, registry in data.get("member_registry", {}).items():
        member_registry[int(chat_id_str)] = {k: int(v) for k, v in registry.items()}

    # permanent_bans
    for chat_id_str, bans in data.get("permanent_bans", {}).items():
        permanent_bans[int(chat_id_str)] = set(int(uid) for uid in bans)

    # clone_log
    clone_log.extend(data.get("clone_log", []))

    total_bans = sum(len(b) for b in permanent_bans.values())
    total_members = sum(len(r) for r in member_registry.values())
    logger.info(
        f"Loaded: {total_members} registered members, "
        f"{total_bans} permanent bans, "
        f"{len(clone_log)} clone log entries"
    )


# In-memory working copies
member_registry: dict[int, dict[str, int]] = {}
permanent_bans: dict[int, set] = {}
clone_log: list[dict] = []

# Load on startup
_load_into_memory(load_data())


# ─── HELPERS ───────────────────────────────────────────────────────────────────

async def auto_delete(context, chat_id: int, message_id: int, delay: int = AUTO_DELETE_DELAY):
    await asyncio.sleep(delay)
    try:
        await context.bot.delete_message(chat_id=chat_id, message_id=message_id)
    except TelegramError:
        pass


async def send_temp(context, chat_id, text, reply_markup=None, delay=AUTO_DELETE_DELAY):
    try:
        msg = await context.bot.send_message(
            chat_id=chat_id, text=text,
            reply_markup=reply_markup, parse_mode="HTML"
        )
        asyncio.create_task(auto_delete(context, chat_id, msg.message_id, delay))
        return msg
    except TelegramError as e:
        logger.error(f"send_temp: {e}")


async def is_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        m = await context.bot.get_chat_member(
            update.effective_chat.id, update.effective_user.id
        )
        return m.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except TelegramError:
        return False


async def get_admin_ids(context, chat_id: int) -> set:
    try:
        admins = await context.bot.get_chat_administrators(chat_id)
        return {a.user.id for a in admins}
    except TelegramError:
        return set()


def make_key(first_name: str, username: str) -> str:
    return f"{(first_name or '').strip().lower()}:{(username or '').strip().lower()}"


async def do_permanent_ban(context, chat_id: int, user_id: int):
    try:
        await context.bot.ban_chat_member(
            chat_id=chat_id, user_id=user_id, until_date=None
        )
        permanent_bans.setdefault(chat_id, set()).add(user_id)
        save_data()  # ← persist immediately
        logger.info(f"Permanent ban saved: user={user_id} chat={chat_id}")
    except TelegramError as e:
        logger.error(f"Ban error: {e}")


def log_clone(chat_id: int, clone_id: int, original_id: int, name: str):
    """Record clone detection to persistent log."""
    entry = {
        "chat_id": chat_id,
        "clone_id": clone_id,
        "original_id": original_id,
        "name": name,
        "banned_at": datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC"),
    }
    clone_log.append(entry)
    save_data()
    logger.warning(f"Clone logged: {entry}")


# ─── MESSAGE ANALYSIS ──────────────────────────────────────────────────────────

def is_crypto_message(message) -> bool:
    text = (message.text or message.caption or "").lower()
    return any(kw in text for kw in CRYPTO_KEYWORDS)


def is_unwanted(message) -> bool:
    if is_crypto_message(message):
        return False

    text = (message.text or message.caption or "").lower()

    if message.forward_from_chat:
        return True
    if message.forward_from and message.forward_from.id != message.from_user.id:
        return True
    if "t.me/" in text or "telegram.me/" in text:
        return True

    bad_phrases = [
        "join our", "join now", "click here", "limited offer",
        "earn money", "investment opportunity", "referral",
        "airdrop", "giveaway", "free tokens", "whitelist",
        "presale", "ido", "ico", "nft drop", "mint now",
        "dm me", "inbox me", "follow us", "check out my",
        "subscribe", "promo code", "discount code",
    ]
    return any(p in text for p in bad_phrases)


# ─── CLONE DETECTION ───────────────────────────────────────────────────────────

async def handle_new_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    result = update.chat_member
    if not result:
        return

    chat_id = result.chat.id
    new_status = result.new_chat_member.status
    user = result.new_chat_member.user

    if new_status not in (ChatMemberStatus.MEMBER, ChatMemberStatus.RESTRICTED):
        return

    user_id = user.id
    first_name = user.first_name or ""
    username = user.username or ""

    # ── Block permanently banned users immediately ──
    if user_id in permanent_bans.get(chat_id, set()):
        await do_permanent_ban(context, chat_id, user_id)
        await send_temp(
            context, chat_id,
            f"🚫 <b>Rejected!</b>\n"
            f"User <code>{user_id}</code> tried to rejoin after a permanent ban.\n"
            f"Banned again automatically.",
            delay=12
        )
        return

    key = make_key(first_name, username)
    registry = member_registry.setdefault(chat_id, {})

    if key in registry:
        original_id = registry[key]
        if original_id != user_id:
            # ── CLONE DETECTED ──
            log_clone(chat_id, user_id, original_id, first_name)
            await do_permanent_ban(context, chat_id, user_id)
            await send_temp(
                context, chat_id,
                f"🔍 <b>Clone Detected &amp; Banned!</b>\n\n"
                f"👤 Name: <b>{first_name}</b>\n"
                f"✅ Original ID: <code>{original_id}</code>\n"
                f"❌ Clone ID: <code>{user_id}</code>\n"
                f"📅 Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}\n\n"
                f"🚫 Clone permanently banned — cannot rejoin.",
                delay=20
            )
            return
    else:
        registry[key] = user_id
        save_data()  # ← persist new member registration
        logger.info(f"Registered: {key} → {user_id}")


# ─── MESSAGE MODERATION ────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.from_user:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    user_name = message.from_user.first_name or "User"

    admins = await get_admin_ids(context, chat_id)
    if user_id in admins:
        return

    if is_unwanted(message):
        try:
            await message.delete()
        except TelegramError as e:
            logger.error(f"Delete failed: {e}")

        keyboard = InlineKeyboardMarkup([[
            InlineKeyboardButton(
                "🔇 Mute 1h",
                callback_data=f"mute:{user_id}:{user_name[:20]}"
            ),
            InlineKeyboardButton(
                "✅ Dismiss",
                callback_data="dismiss"
            ),
        ]])

        await send_temp(
            context, chat_id,
            f"🗑 <b>Message Deleted</b>\n\n"
            f"👤 User: <b>{user_name}</b> (<code>{user_id}</code>)\n"
            f"📌 Reason: Ad / other group link / unauthorized forward\n\n"
            f"<i>Admin: press Mute if needed.</i>",
            reply_markup=keyboard,
            delay=30
        )


# ─── CALLBACK BUTTONS ──────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    chat_id = query.message.chat.id
    clicker_id = query.from_user.id

    admins = await get_admin_ids(context, chat_id)
    if clicker_id not in admins:
        await query.answer("❌ Only admins can use this.", show_alert=True)
        return

    data = query.data

    if data.startswith("mute:"):
        parts = data.split(":", 2)
        target_id = int(parts[1])
        target_name = parts[2] if len(parts) > 2 else str(target_id)

        try:
            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=target_id,
                permissions=ChatPermissions(can_send_messages=False),
                until_date=datetime.now() + timedelta(hours=1)
            )
        except TelegramError as e:
            await query.answer(f"Error: {e}", show_alert=True)
            return

        unmute_kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("🔊 Unmute", callback_data=f"unmute:{target_id}")
        ]])
        try:
            await query.edit_message_text(
                f"🔇 <b>Muted by Admin</b>\n\n"
                f"👤 User: <b>{target_name}</b> (<code>{target_id}</code>)\n"
                f"⏱ Duration: <b>1 hour</b>",
                parse_mode="HTML",
                reply_markup=unmute_kb
            )
        except TelegramError:
            pass
        asyncio.create_task(auto_delete(context, chat_id, query.message.message_id, delay=30))
        await query.answer("Muted for 1 hour.")

    elif data.startswith("unmute:"):
        target_id = int(data.split(":")[1])
        try:
            await context.bot.restrict_chat_member(
                chat_id=chat_id,
                user_id=target_id,
                permissions=ChatPermissions(
                    can_send_messages=True,
                    can_send_media_messages=True,
                    can_send_other_messages=True,
                    can_add_web_page_previews=True,
                )
            )
            await query.edit_message_text(
                f"✅ User <code>{target_id}</code> has been <b>unmuted</b>.",
                parse_mode="HTML"
            )
            asyncio.create_task(auto_delete(context, chat_id, query.message.message_id, delay=8))
            await query.answer("Unmuted.")
        except TelegramError as e:
            await query.answer(f"Error: {e}", show_alert=True)

    elif data == "dismiss":
        try:
            await query.delete_message()
        except TelegramError:
            await query.answer("Dismissed.")


# ─── ADMIN COMMANDS ────────────────────────────────────────────────────────────

async def cmd_mute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    msg = update.message
    chat_id = msg.chat.id
    if not msg.reply_to_message:
        await msg.reply_text("❌ Reply to the user's message, then: /mute [minutes]")
        return
    target = msg.reply_to_message.from_user
    duration = 60
    if context.args:
        try:
            duration = int(context.args[0])
        except ValueError:
            pass
    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target.id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=datetime.now() + timedelta(minutes=duration)
        )
    except TelegramError as e:
        await msg.reply_text(f"❌ Could not mute: {e}")
        return
    await msg.delete()
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔊 Unmute", callback_data=f"unmute:{target.id}")
    ]])
    await send_temp(
        context, chat_id,
        f"🔇 <b>Muted!</b>\n\n"
        f"👤 User: <b>{target.first_name}</b> (<code>{target.id}</code>)\n"
        f"⏱ Duration: <b>{duration} minute(s)</b>",
        reply_markup=kb, delay=30
    )


async def cmd_unmute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    msg = update.message
    chat_id = msg.chat.id
    if not msg.reply_to_message:
        await msg.reply_text("❌ Reply to the user's message to unmute.")
        return
    target = msg.reply_to_message.from_user
    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=target.id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            )
        )
    except TelegramError as e:
        await msg.reply_text(f"❌ Could not unmute: {e}")
        return
    await msg.delete()
    await send_temp(context, chat_id, f"✅ <b>{target.first_name}</b> has been unmuted.", delay=8)


async def cmd_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    msg = update.message
    chat_id = msg.chat.id
    if not msg.reply_to_message:
        await msg.reply_text("❌ Reply to the user's message to ban.")
        return
    target = msg.reply_to_message.from_user
    await do_permanent_ban(context, chat_id, target.id)
    await msg.delete()
    await send_temp(
        context, chat_id,
        f"🚫 <b>Permanently Banned!</b>\n\n"
        f"👤 User: <b>{target.first_name}</b> (<code>{target.id}</code>)\n"
        f"This user <b>can never rejoin</b> this group.",
        delay=15
    )


async def cmd_unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update, context):
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    chat_id = update.effective_chat.id
    try:
        target_id = int(context.args[0])
        await context.bot.unban_chat_member(chat_id=chat_id, user_id=target_id)
        permanent_bans.get(chat_id, set()).discard(target_id)
        save_data()
        await update.message.reply_text(
            f"✅ User <code>{target_id}</code> unbanned and removed from permanent ban list.",
            parse_mode="HTML"
        )
    except (ValueError, TelegramError) as e:
        await update.message.reply_text(f"❌ Error: {e}")


async def cmd_clones(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/clones — show all detected clones for this chat"""
    if not await is_admin(update, context):
        return
    chat_id = update.effective_chat.id
    entries = [e for e in clone_log if e["chat_id"] == chat_id]
    if not entries:
        await update.message.reply_text("✅ No clones detected in this group yet.")
        return
    lines = ["🔍 <b>Clone Detection Log</b>\n"]
    for i, e in enumerate(entries[-20:], 1):  # show last 20
        lines.append(
            f"{i}. 👤 <b>{e['name']}</b>\n"
            f"   ✅ Original: <code>{e['original_id']}</code>\n"
            f"   ❌ Clone: <code>{e['clone_id']}</code>\n"
            f"   📅 {e['banned_at']}\n"
        )
    await update.message.reply_text("\n".join(lines), parse_mode="HTML")


async def cmd_bans(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/bans — list all permanently banned user IDs for this chat"""
    if not await is_admin(update, context):
        return
    chat_id = update.effective_chat.id
    bans = permanent_bans.get(chat_id, set())
    if not bans:
        await update.message.reply_text("✅ No permanent bans in this group.")
        return
    ids = "\n".join(f"• <code>{uid}</code>" for uid in sorted(bans))
    await update.message.reply_text(
        f"🚫 <b>Permanently Banned Users ({len(bans)})</b>\n\n{ids}",
        parse_mode="HTML"
    )


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total_bans = sum(len(b) for b in permanent_bans.values())
    total_clones = len(clone_log)
    await update.message.reply_text(
        "🤖 <b>Guardian Bot — Active</b>\n\n"
        "<b>✅ Allowed for everyone:</b>\n"
        "• BTC, TRX, HK — buy / sell messages\n"
        "• Normal group conversation\n\n"
        "<b>🗑 Auto-deleted (no auto-mute):</b>\n"
        "• Ads / promos for other projects\n"
        "• Links to other groups/channels\n"
        "• Forwards from other chats\n\n"
        "<b>🔍 Clone &amp; Ban protection:</b>\n"
        "• Duplicate name/username → permanent ban\n"
        "• Banned users blocked on re-join attempt\n"
        "• All data saved to disk — survives restarts\n\n"
        f"📊 <b>Stats:</b> {total_clones} clones detected | {total_bans} total bans\n\n"
        "<b>Admin commands:</b>\n"
        "/mute [min] — mute user (reply)\n"
        "/unmute — unmute user (reply)\n"
        "/ban — permanent ban (reply)\n"
        "/unban &lt;id&gt; — remove permanent ban\n"
        "/clones — view clone detection log\n"
        "/bans — list all permanently banned users",
        parse_mode="HTML"
    )


# ─── MAIN ──────────────────────────────────────────────────────────────────────

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("unmute", cmd_unmute))
    app.add_handler(CommandHandler("ban", cmd_ban))
    app.add_handler(CommandHandler("unban", cmd_unban))
    app.add_handler(CommandHandler("clones", cmd_clones))
    app.add_handler(CommandHandler("bans", cmd_bans))

    app.add_handler(ChatMemberHandler(handle_new_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info(f"Guardian Bot started. Data file: {DATA_FILE}")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
