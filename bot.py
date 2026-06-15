"""
╔══════════════════════════════════════════════════════════╗
║        ENTERPRISE TELEGRAM MODERATION BOT                ║
║        Barcha kod bitta faylda — bot.py                  ║
╚══════════════════════════════════════════════════════════╝

ISHGA TUSHIRISH:
  1. pip install aiogram aiosqlite
  2. Pastdagi BOT_TOKEN va ADMIN_IDS ni o'zgartiiring
  3. python bot.py

Bot o'zi moderation.db faylini yaratadi, hech narsa qo'shimcha kerak emas.
"""

import asyncio
import logging
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Callable, Dict, List, Optional, Awaitable

import aiosqlite
from aiogram import Bot, Dispatcher, Router, BaseMiddleware, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery, ChatPermissions, InlineKeyboardButton,
    InlineKeyboardMarkup, Message, TelegramObject,
)

# ═══════════════════════════════════════════════════════════
#  ⚙️  SOZLAMALAR — faqat shu joyni o'zgartiring
# ═══════════════════════════════════════════════════════════

BOT_TOKEN  = os.getenv("BOT_TOKEN",  "BU_YERGA_BOT_TOKENINGIZNI_YOZING")
ADMIN_IDS  = [
    int(x) for x in os.getenv("ADMIN_IDS", "123456789").split(",") if x.strip()
]
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", "0"))   # 0 = o'chiriq
DB_PATH        = os.getenv("DB_PATH", "moderation.db")

# Spam chegarasi
SPAM_THRESHOLD = 5    # nechta xabar ...
SPAM_WINDOW    = 10   # ... qancha sekund ichida = spam

# Admin alert o'z-o'zidan o'chish (sekund)
ADMIN_LOG_TTL  = 10

# Impersonation kalit so'zlar
IMPERSONATION_KEYWORDS = [
    "admin", "moderator", "support", "official", "mod", "team",
    "помощник", "модератор", "adminbot",
]

# ═══════════════════════════════════════════════════════════
#  🗄️  DATABASE
# ═══════════════════════════════════════════════════════════

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript("""
        PRAGMA journal_mode=WAL;

        CREATE TABLE IF NOT EXISTS users (
            user_id     INTEGER PRIMARY KEY,
            username    TEXT,
            full_name   TEXT,
            status      TEXT DEFAULT 'pending',
            trusted     INTEGER DEFAULT 0,
            mute_until  TEXT,
            joined_at   TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at  TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS events (
            event_id     INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            type         TEXT NOT NULL,
            detail       TEXT,
            timestamp    TEXT DEFAULT CURRENT_TIMESTAMP,
            handled      INTEGER DEFAULT 0,
            action_taken TEXT
        );

        CREATE TABLE IF NOT EXISTS auto_ban_queue (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id      INTEGER NOT NULL,
            event_id     INTEGER,
            scheduled_at TEXT NOT NULL,
            executed     INTEGER DEFAULT 0,
            reverted     INTEGER DEFAULT 0,
            created_at   TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS settings (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            admin_id    INTEGER,
            action      TEXT NOT NULL,
            target_user INTEGER,
            detail      TEXT,
            timestamp   TEXT DEFAULT CURRENT_TIMESTAMP
        );
        """)

        defaults = {
            "auto_ban":                  "0",       # OFF — xavfsiz
            "auto_mute":                 "1",       # ON
            "link_filter":               "1",       # ON
            "forward_filter":            "1",       # ON
            "safe_mode":                 "1",       # ON — auto-ban yo'q
            "ban_delay":                 "30",      # daqiqa
            "impersonation_sensitivity": "medium",
        }
        for k, v in defaults.items():
            await db.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v)
            )
        await db.commit()


# ── Settings ────────────────────────────────────────────────
async def get_setting(key: str) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key=?", (key,)) as c:
            r = await c.fetchone()
            return r[0] if r else None

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key,value,updated_at) VALUES(?,?,?)",
            (key, value, _now())
        )
        await db.commit()

async def get_all_settings() -> Dict[str, str]:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT key,value FROM settings") as c:
            return {r[0]: r[1] for r in await c.fetchall()}

async def is_enabled(key: str) -> bool:
    return await get_setting(key) == "1"

async def get_ban_delay() -> int:
    v = await get_setting("ban_delay")
    return int(v) if v and v.isdigit() else 30


# ── Users ────────────────────────────────────────────────────
async def upsert_user(user_id: int, username: Optional[str], full_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, username, full_name, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username=excluded.username,
                full_name=excluded.full_name,
                updated_at=excluded.updated_at
        """, (user_id, username, full_name, _now()))
        await db.commit()

async def get_user(user_id: int) -> Optional[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)) as c:
            r = await c.fetchone()
            return dict(r) if r else None

async def set_user_status(user_id: int, status: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET status=?, updated_at=? WHERE user_id=?",
            (status, _now(), user_id)
        )
        await db.commit()

async def set_user_trusted(user_id: int, trusted: bool):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET trusted=?, updated_at=? WHERE user_id=?",
            (int(trusted), _now(), user_id)
        )
        await db.commit()

async def set_mute_until(user_id: int, until: Optional[datetime]):
    val = until.isoformat() if until else None
    status = "muted" if until else "allowed"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET mute_until=?, status=?, updated_at=? WHERE user_id=?",
            (val, status, _now(), user_id)
        )
        await db.commit()

async def set_user_allowed(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET status='allowed', mute_until=NULL, updated_at=? WHERE user_id=?",
            (_now(), user_id)
        )
        await db.commit()


# ── Events ──────────────────────────────────────────────────
async def log_event(user_id: int, etype: str, detail: str = "") -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        c = await db.execute(
            "INSERT INTO events (user_id, type, detail) VALUES (?,?,?)",
            (user_id, etype, detail)
        )
        await db.commit()
        return c.lastrowid

async def mark_event_handled(event_id: int, action: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE events SET handled=1, action_taken=? WHERE event_id=?",
            (action, event_id)
        )
        await db.commit()

async def get_unhandled_events(user_id: int) -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM events WHERE user_id=? AND handled=0 ORDER BY timestamp DESC",
            (user_id,)
        ) as c:
            return [dict(r) for r in await c.fetchall()]


# ── Auto-ban queue ────────────────────────────────────────────
async def enqueue_auto_ban(user_id: int, event_id: int, scheduled_at: datetime) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        c = await db.execute(
            "INSERT INTO auto_ban_queue (user_id, event_id, scheduled_at) VALUES (?,?,?)",
            (user_id, event_id, scheduled_at.isoformat())
        )
        await db.commit()
        return c.lastrowid

async def get_pending_auto_bans() -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT * FROM auto_ban_queue
            WHERE executed=0 AND reverted=0 AND scheduled_at <= ?
        """, (_now(),)) as c:
            return [dict(r) for r in await c.fetchall()]

async def mark_auto_ban_executed(ban_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE auto_ban_queue SET executed=1 WHERE id=?", (ban_id,))
        await db.commit()

async def cancel_auto_ban(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE auto_ban_queue SET reverted=1 WHERE user_id=? AND executed=0",
            (user_id,)
        )
        await db.commit()

async def mark_auto_ban_reverted(ban_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE auto_ban_queue SET reverted=1 WHERE id=?", (ban_id,))
        await db.commit()


# ── Audit log ────────────────────────────────────────────────
async def audit(admin_id: Optional[int], action: str,
                target: Optional[int] = None, detail: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO audit_log (admin_id,action,target_user,detail) VALUES(?,?,?,?)",
            (admin_id, action, target, detail)
        )
        await db.commit()

async def get_audit_log(limit: int = 20) -> List[Dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM audit_log ORDER BY timestamp DESC LIMIT ?", (limit,)
        ) as c:
            return [dict(r) for r in await c.fetchall()]


# ── Spam counter (xotirada) ────────────────────────────────
_spam: Dict[int, List[float]] = defaultdict(list)
_spam_lock = asyncio.Lock()

async def record_message(user_id: int) -> int:
    import time
    now = time.time()
    async with _spam_lock:
        _spam[user_id] = [t for t in _spam[user_id] if now - t < SPAM_WINDOW]
        _spam[user_id].append(now)
        return len(_spam[user_id])

async def reset_spam(user_id: int):
    async with _spam_lock:
        _spam.pop(user_id, None)


# ── Helper ───────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ═══════════════════════════════════════════════════════════
#  🔍  DETECTION
# ═══════════════════════════════════════════════════════════

LINK_RE = re.compile(
    r"(https?://|t\.me/|@\w{5,}|www\.|bit\.ly|tinyurl|youtu\.be)",
    re.IGNORECASE,
)

def detect_link(message: Message) -> Optional[str]:
    text = message.text or message.caption or ""
    if LINK_RE.search(text):
        return text[:100]
    if message.entities:
        for e in message.entities:
            if e.type in ("url", "text_link"):
                return text[:100]
    return None

def is_forward(message: Message) -> bool:
    return bool(
        message.forward_date or
        message.forward_from or
        message.forward_from_chat
    )

async def check_impersonation(full_name: str, username: Optional[str]) -> bool:
    sensitivity = await get_setting("impersonation_sensitivity") or "medium"
    kws = IMPERSONATION_KEYWORDS
    nl = (full_name or "").lower()
    ul = (username or "").lower()
    if sensitivity == "high":
        return any(k in nl or k in ul for k in kws)
    elif sensitivity == "medium":
        return any(
            nl.startswith(k) or nl.endswith(k) or
            ul.startswith(k) or ul.endswith(k)
            for k in kws
        )
    else:
        return any(k == nl or k == ul for k in kws)


# ═══════════════════════════════════════════════════════════
#  🎹  KEYBOARDS
# ═══════════════════════════════════════════════════════════

def kb_action(user_id: int, event_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔇 MUTE",    callback_data=f"mod:mute:{user_id}:{event_id}"),
            InlineKeyboardButton(text="🔊 UNMUTE",  callback_data=f"mod:unmute:{user_id}:{event_id}"),
        ],
        [
            InlineKeyboardButton(text="⛔ BAN",     callback_data=f"mod:ban:{user_id}:{event_id}"),
            InlineKeyboardButton(text="♻️ RESTORE", callback_data=f"mod:restore:{user_id}:{event_id}"),
        ],
        [
            InlineKeyboardButton(text="⚙️ TRUST",   callback_data=f"mod:trust:{user_id}:{event_id}"),
            InlineKeyboardButton(text="✅ DISMISS",  callback_data=f"mod:dismiss:{user_id}:{event_id}"),
        ],
    ])

def kb_recovery(user_id: int, ban_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="♻️ RESTORE",     callback_data=f"rec:restore:{user_id}:{ban_id}"),
        InlineKeyboardButton(text="❌ CONFIRM BAN", callback_data=f"rec:confirm:{user_id}:{ban_id}"),
    ]])

def kb_settings(s: dict) -> InlineKeyboardMarkup:
    def tog(k): return "✅" if s.get(k) == "1" else "❌"
    sens  = s.get("impersonation_sensitivity", "medium")
    delay = s.get("ban_delay", "30")
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text=f"{tog('auto_mute')} Auto Mute",   callback_data="cfg:toggle:auto_mute"),
            InlineKeyboardButton(text=f"{tog('auto_ban')} Auto Ban",     callback_data="cfg:toggle:auto_ban"),
        ],
        [
            InlineKeyboardButton(text=f"{tog('link_filter')} Link Filter",       callback_data="cfg:toggle:link_filter"),
            InlineKeyboardButton(text=f"{tog('forward_filter')} Forward Filter", callback_data="cfg:toggle:forward_filter"),
        ],
        [
            InlineKeyboardButton(text=f"{tog('safe_mode')} Safe Mode",   callback_data="cfg:toggle:safe_mode"),
        ],
        [
            InlineKeyboardButton(text=f"⏱ Ban Delay: {delay}m",          callback_data="cfg:set:ban_delay"),
        ],
        [
            InlineKeyboardButton(text=f"🔍 Sensitivity: {sens.upper()}", callback_data="cfg:cycle:impersonation_sensitivity"),
        ],
        [
            InlineKeyboardButton(text="📋 Audit Log", callback_data="cfg:audit"),
            InlineKeyboardButton(text="❌ Yopish",    callback_data="cfg:close"),
        ],
    ])

def kb_delay() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="5m",   callback_data="cfg:delay:5"),
            InlineKeyboardButton(text="15m",  callback_data="cfg:delay:15"),
            InlineKeyboardButton(text="30m",  callback_data="cfg:delay:30"),
        ],
        [
            InlineKeyboardButton(text="60m",  callback_data="cfg:delay:60"),
            InlineKeyboardButton(text="120m", callback_data="cfg:delay:120"),
            InlineKeyboardButton(text="↩ Orqaga", callback_data="cfg:back"),
        ],
    ])


# ═══════════════════════════════════════════════════════════
#  📣  ALERT
# ═══════════════════════════════════════════════════════════

async def send_admins(bot: Bot, text: str,
                      reply_markup: Optional[InlineKeyboardMarkup] = None,
                      auto_delete: bool = True):
    targets = list(ADMIN_IDS)
    if LOG_CHANNEL_ID:
        targets.append(LOG_CHANNEL_ID)
    for aid in targets:
        try:
            msg = await bot.send_message(aid, text, parse_mode="HTML",
                                         reply_markup=reply_markup)
            if auto_delete and reply_markup is None:
                asyncio.create_task(_delete_later(bot, aid, msg.message_id, ADMIN_LOG_TTL))
        except Exception as e:
            log.warning(f"Admin {aid} ga xabar yuborib bo'lmadi: {e}")

async def _delete_later(bot: Bot, chat_id: int, msg_id: int, delay: int):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass

def alert_text(username: Optional[str], user_id: int, event: str,
               detail: str = "", delay: int = 30) -> str:
    uname = f"@{username}" if username else f"id:{user_id}"
    det   = f"\nDetail: {detail}" if detail else ""
    return (
        f"⚠️ <b>USER EVENT DETECTED</b>\n\n"
        f"User: {uname}\n"
        f"ID: <code>{user_id}</code>\n"
        f"Event: <b>{event}</b>{det}\n\n"
        f"⏱ Auto-ban: <b>{delay} min</b> ichida (agar Safe Mode OFF)"
    )


# ═══════════════════════════════════════════════════════════
#  🛠  MODERATION ACTIONS
# ═══════════════════════════════════════════════════════════

async def do_mute(bot: Bot, chat_id: int, user_id: int,
                  minutes: int = 60, admin_id: Optional[int] = None, reason: str = "") -> bool:
    until = datetime.now(timezone.utc) + timedelta(minutes=minutes)
    try:
        await bot.restrict_chat_member(
            chat_id, user_id,
            permissions=ChatPermissions(can_send_messages=False),
            until_date=until,
        )
        await set_mute_until(user_id, until)
        await audit(admin_id, "mute", user_id, f"until={until.isoformat()} {reason}")
        return True
    except Exception as e:
        log.error(f"Mute xatosi {user_id}: {e}")
        return False

async def do_unmute(bot: Bot, chat_id: int, user_id: int,
                    admin_id: Optional[int] = None) -> bool:
    try:
        await bot.restrict_chat_member(
            chat_id, user_id,
            permissions=ChatPermissions(
                can_send_messages=True,
                can_send_media_messages=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
        await set_user_allowed(user_id)
        await cancel_auto_ban(user_id)
        await audit(admin_id, "unmute", user_id)
        return True
    except Exception as e:
        log.error(f"Unmute xatosi {user_id}: {e}")
        return False

async def do_ban(bot: Bot, chat_id: int, user_id: int,
                 admin_id: Optional[int] = None, reason: str = "") -> bool:
    try:
        await bot.ban_chat_member(chat_id, user_id)
        await set_user_status(user_id, "banned")
        await audit(admin_id, "ban", user_id, reason)
        return True
    except Exception as e:
        log.error(f"Ban xatosi {user_id}: {e}")
        return False

async def do_unban(bot: Bot, chat_id: int, user_id: int,
                   admin_id: Optional[int] = None) -> bool:
    try:
        await bot.unban_chat_member(chat_id, user_id, only_if_banned=True)
        await set_user_trusted(user_id, True)
        await set_user_status(user_id, "allowed")
        await audit(admin_id, "restore", user_id, "unban+trusted")
        return True
    except Exception as e:
        log.error(f"Unban xatosi {user_id}: {e}")
        return False

async def do_trust(user_id: int, admin_id: Optional[int] = None):
    await set_user_trusted(user_id, True)
    await set_user_allowed(user_id)
    await cancel_auto_ban(user_id)
    await audit(admin_id, "trust", user_id)

async def do_delete(bot: Bot, chat_id: int, msg_id: int):
    try:
        await bot.delete_message(chat_id, msg_id)
    except Exception:
        pass

async def schedule_auto_ban(bot: Bot, chat_id: int, user_id: int, event_id: int):
    """Ban delay minutdan keyin auto-ban qiladi (agar bekor qilinmasa)."""
    delay = await get_ban_delay()
    scheduled_at = datetime.now(timezone.utc) + timedelta(minutes=delay)
    ban_id = await enqueue_auto_ban(user_id, event_id, scheduled_at)

    async def _run():
        await asyncio.sleep(delay * 60)

        # Tekshir: bekor qilinganmi?
        pending = await get_pending_auto_bans()
        this = next((b for b in pending if b["id"] == ban_id), None)
        if not this:
            return  # bekor qilingan yoki bajarilgan

        # Tekshir: safe_mode yoki auto_ban o'chirilganmi?
        if await is_enabled("safe_mode") or not await is_enabled("auto_ban"):
            return

        # Tekshir: trusted user?
        u = await get_user(user_id)
        if u and u.get("trusted"):
            return

        ok = await do_ban(bot, chat_id, user_id, reason=f"auto-ban: {delay}m ichida javob yo'q")
        if ok:
            await mark_auto_ban_executed(ban_id)
            await mark_event_handled(event_id, "auto_ban")
            txt = (
                f"⛔ <b>AUTO BAN BAJARILDI</b>\n\n"
                f"User: <code>{user_id}</code>\n"
                f"Sabab: {delay} daqiqa ichida admin javob bermadi\n"
                f"Ban ID: <code>{ban_id}</code>\n\n"
                f"⚠️ Bekor qilish mumkin!"
            )
            await send_admins(bot, txt,
                              reply_markup=kb_recovery(user_id, ban_id),
                              auto_delete=False)

    asyncio.create_task(_run())


# ═══════════════════════════════════════════════════════════
#  🔐  GUARDS
# ═══════════════════════════════════════════════════════════

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def admin_only(fn):
    @wraps(fn)
    async def wrap(message: Message, *a, **kw):
        if not is_admin(message.from_user.id):
            return
        return await fn(message, *a, **kw)
    return wrap


# ═══════════════════════════════════════════════════════════
#  🔄  MIDDLEWARE — foydalanuvchi o'zgarishlarini kuzatadi
# ═══════════════════════════════════════════════════════════

class UserTrackerMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        if isinstance(event, Message) and event.from_user and not event.from_user.is_bot:
            u = event.from_user
            existing = await get_user(u.id)
            if existing:
                changes = []
                if existing.get("username") != u.username:
                    changes.append(f"username: {existing.get('username')} → {u.username}")
                if existing.get("full_name") != u.full_name:
                    changes.append(f"name: {existing.get('full_name')} → {u.full_name}")
                if changes:
                    await log_event(u.id, "profile_change", " | ".join(changes))
            await upsert_user(u.id, u.username, u.full_name)
        return await handler(event, data)


# ═══════════════════════════════════════════════════════════
#  📨  HANDLERS
# ═══════════════════════════════════════════════════════════

router = Router()
log = logging.getLogger("modbot")


# ── Yangi a'zo ──────────────────────────────────────────────
@router.message(F.new_chat_members)
async def on_new_member(message: Message):
    for member in message.new_chat_members:
        if member.is_bot:
            continue
        uid  = member.id
        uname = member.username
        fname = member.full_name

        await upsert_user(uid, uname, fname)
        await set_user_status(uid, "pending")

        if await check_impersonation(fname, uname):
            eid   = await log_event(uid, "impersonation", f"name='{fname}' user='{uname}'")
            delay = await get_ban_delay()
            txt   = alert_text(uname, uid, "impersonation attempt", f"Name: {fname}", delay)

            if await is_enabled("auto_mute"):
                await do_mute(message.bot, message.chat.id, uid, reason="impersonation on join")
            if await is_enabled("auto_ban"):
                await schedule_auto_ban(message.bot, message.chat.id, uid, eid)

            await send_admins(message.bot, txt,
                              reply_markup=kb_action(uid, eid), auto_delete=False)
        else:
            await set_user_status(uid, "allowed")


# ── Xabarlar: spam / link / forward ─────────────────────────
@router.message()
async def on_message(message: Message):
    if not message.from_user or message.from_user.is_bot:
        return
    if is_admin(message.from_user.id):
        return

    u      = message.from_user
    uid    = u.id
    chat   = message.chat.id
    bot    = message.bot

    await upsert_user(uid, u.username, u.full_name)
    db_user = await get_user(uid)

    if db_user and db_user.get("status") == "banned":
        await do_delete(bot, chat, message.message_id)
        return

    # Forward
    if await is_enabled("forward_filter") and is_forward(message):
        await do_delete(bot, chat, message.message_id)
        eid = await log_event(uid, "forward", "forwarded xabar o'chirildi")
        await _fire(bot, chat, u, eid, "forward", "forwarded xabar", db_user)
        return

    # Link
    if await is_enabled("link_filter"):
        link = detect_link(message)
        if link:
            await do_delete(bot, chat, message.message_id)
            eid = await log_event(uid, "link", link)
            await _fire(bot, chat, u, eid, "link", link[:80], db_user)
            return

    # Spam
    count = await record_message(uid)
    if count >= SPAM_THRESHOLD:
        await do_delete(bot, chat, message.message_id)
        eid = await log_event(uid, "spam", f"{count} xabar/{SPAM_WINDOW}s")
        await reset_spam(uid)
        await _fire(bot, chat, u, eid, "spam", f"{count} xabar/{SPAM_WINDOW}s", db_user)


async def _fire(bot: Bot, chat_id: int, u, event_id: int,
                etype: str, detail: str, db_user: Optional[Dict]):
    """Event bo'lganda admin ogohlantirish va ixtiyoriy auto-mute/ban."""
    uid     = u.id
    trusted = db_user.get("trusted", 0) if db_user else 0

    if await is_enabled("auto_mute"):
        await do_mute(bot, chat_id, uid, reason=f"auto-mute: {etype}")
    if await is_enabled("auto_ban") and not trusted:
        await schedule_auto_ban(bot, chat_id, uid, event_id)

    delay = await get_ban_delay()
    txt   = alert_text(u.username, uid, etype, detail, delay)
    if trusted:
        txt += "\n\n🔰 <i>User TRUSTED — auto-ban yo'q.</i>"

    await send_admins(bot, txt, reply_markup=kb_action(uid, event_id), auto_delete=False)


# ── Admin buyruqlari ─────────────────────────────────────────
@router.message(Command("start", "help"))
@admin_only
async def cmd_start(message: Message):
    await message.answer(
        "🤖 <b>Moderation Bot</b>\n\n"
        "📌 Buyruqlar:\n"
        "/config — sozlamalar paneli\n"
        "/status {user_id} — foydalanuvchi holati\n"
        "/trust {user_id} — ishonchli deb belgilash\n"
        "/mute {user_id} [daqiqa] — jim qilish\n"
        "/ban {user_id} — ban\n"
        "/unban {user_id} — unban + trust\n"
        "/auditlog — so'nggi amallar tarixi",
        parse_mode="HTML"
    )

@router.message(Command("config"))
@admin_only
async def cmd_config(message: Message):
    s = await get_all_settings()
    safe  = "✅ ON" if s.get("safe_mode") == "1" else "❌ OFF"
    abn   = "✅ ON" if s.get("auto_ban")   == "1" else "❌ OFF"
    delay = s.get("ban_delay", "30")
    sens  = s.get("impersonation_sensitivity", "medium").upper()
    await message.answer(
        f"⚙️ <b>Sozlamalar</b>\n\n"
        f"🛡 Safe Mode: {safe}\n"
        f"⛔ Auto Ban: {abn}\n"
        f"⏱ Ban Delay: {delay} min\n"
        f"🔍 Sensitivity: {sens}\n\n"
        f"Tugmalar bilan boshqaring:",
        parse_mode="HTML",
        reply_markup=kb_settings(s),
    )

@router.message(Command("status"))
@admin_only
async def cmd_status(message: Message):
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Ishlatish: /status {user_id}"); return
    uid = int(parts[1])
    u   = await get_user(uid)
    if not u:
        await message.answer(f"❌ {uid} topilmadi."); return
    ev     = await get_unhandled_events(uid)
    trusted = "✅" if u.get("trusted") else "❌"
    await message.answer(
        f"👤 <b>Foydalanuvchi</b>\n\n"
        f"ID: <code>{u['user_id']}</code>\n"
        f"Username: @{u.get('username') or 'yo\'q'}\n"
        f"Ism: {u.get('full_name') or 'yo\'q'}\n"
        f"Status: <b>{u.get('status')}</b>\n"
        f"Trusted: {trusted}\n"
        f"Mute: {u.get('mute_until') or 'yo\'q'}\n"
        f"Kutayotgan eventlar: {len(ev)}",
        parse_mode="HTML"
    )

@router.message(Command("trust"))
@admin_only
async def cmd_trust(message: Message):
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Ishlatish: /trust {user_id}"); return
    uid = int(parts[1])
    await do_trust(uid, admin_id=message.from_user.id)
    await message.answer(f"⚙️ <code>{uid}</code> endi trusted.", parse_mode="HTML")

@router.message(Command("mute"))
@admin_only
async def cmd_mute(message: Message):
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Ishlatish: /mute {user_id} [daqiqa]"); return
    uid  = int(parts[1])
    mins = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 60
    ok   = await do_mute(message.bot, message.chat.id, uid,
                         minutes=mins, admin_id=message.from_user.id, reason="admin buyrug'i")
    await message.answer(
        f"🔇 <code>{uid}</code> {mins} daqiqa jim qilindi." if ok else "❌ Mute bo'lmadi.",
        parse_mode="HTML"
    )

@router.message(Command("ban"))
@admin_only
async def cmd_ban(message: Message):
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Ishlatish: /ban {user_id}"); return
    uid = int(parts[1])
    ok  = await do_ban(message.bot, message.chat.id, uid,
                       admin_id=message.from_user.id, reason="admin buyrug'i")
    await message.answer(
        f"⛔ <code>{uid}</code> ban qilindi." if ok else "❌ Ban bo'lmadi.",
        parse_mode="HTML"
    )

@router.message(Command("unban"))
@admin_only
async def cmd_unban(message: Message):
    parts = message.text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        await message.answer("Ishlatish: /unban {user_id}"); return
    uid = int(parts[1])
    ok  = await do_unban(message.bot, message.chat.id, uid,
                         admin_id=message.from_user.id)
    await message.answer(
        f"♻️ <code>{uid}</code> tiklandi + trusted." if ok else "❌ Tiklash bo'lmadi.",
        parse_mode="HTML"
    )

@router.message(Command("auditlog"))
@admin_only
async def cmd_auditlog(message: Message):
    logs = await get_audit_log(20)
    if not logs:
        await message.answer("Hali hech narsa yo'q."); return
    lines = ["📋 <b>Audit Log (oxirgi 20)</b>\n"]
    for e in logs:
        ts     = e["timestamp"][:16]
        by     = e["admin_id"] or "AUTO"
        act    = e["action"]
        target = f"→ {e['target_user']}" if e["target_user"] else ""
        det    = f"[{e['detail']}]" if e["detail"] else ""
        lines.append(f"[{ts}] {by} {act} {target} {det}")
    await message.answer("\n".join(lines), parse_mode="HTML")


# ── Callback: mod actions ────────────────────────────────────
@router.callback_query(F.data.startswith("mod:"))
async def cb_mod(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Faqat adminlar.", show_alert=True); return

    _, action, uid_s, eid_s = callback.data.split(":")
    uid     = int(uid_s)
    eid     = int(eid_s)
    aid     = callback.from_user.id
    chat_id = callback.message.chat.id

    await cancel_auto_ban(uid)   # Admin harakat qildi — auto-ban bekor

    labels = {
        "mute":    "🔇 JIM QILINDI",
        "unmute":  "🔊 OVOZ QAYTARILDI",
        "ban":     "⛔ BAN QILINDI",
        "restore": "♻️ TIKLANDI",
        "trust":   "⚙️ TRUSTED",
        "dismiss": "✅ YOPILDI",
    }

    if action == "mute":
        ok = await do_mute(callback.bot, chat_id, uid, admin_id=aid, reason="admin mute")
        await mark_event_handled(eid, "admin_mute")
        await callback.answer("🔇 Jim qilindi." if ok else "❌ Xato.", show_alert=True)

    elif action == "unmute":
        ok = await do_unmute(callback.bot, chat_id, uid, admin_id=aid)
        await mark_event_handled(eid, "admin_unmute")
        await callback.answer("🔊 Ovoz qaytarildi." if ok else "❌ Xato.", show_alert=True)

    elif action == "ban":
        ok = await do_ban(callback.bot, chat_id, uid, admin_id=aid, reason="admin ban")
        await mark_event_handled(eid, "admin_ban")
        await callback.answer("⛔ Ban qilindi." if ok else "❌ Xato.", show_alert=True)

    elif action == "restore":
        ok = await do_unban(callback.bot, chat_id, uid, admin_id=aid)
        await mark_event_handled(eid, "admin_restore")
        await callback.answer("♻️ Tiklandi + trusted." if ok else "❌ Xato.", show_alert=True)

    elif action == "trust":
        await do_trust(uid, admin_id=aid)
        await mark_event_handled(eid, "admin_trust")
        await callback.answer("⚙️ Trusted belgilandi.", show_alert=True)

    elif action == "dismiss":
        await mark_event_handled(eid, "admin_dismiss")
        await callback.answer("✅ Yopildi.")

    try:
        label = labels.get(action, action.upper())
        orig  = callback.message.text or ""
        by    = f"@{callback.from_user.username}" if callback.from_user.username else str(aid)
        await callback.message.edit_text(
            f"{orig}\n\n<b>Amal: {label}</b>\nKim: {by}",
            parse_mode="HTML", reply_markup=None,
        )
    except Exception:
        pass


# ── Callback: recovery ───────────────────────────────────────
@router.callback_query(F.data.startswith("rec:"))
async def cb_recovery(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Faqat adminlar.", show_alert=True); return

    _, action, uid_s, bid_s = callback.data.split(":")
    uid     = int(uid_s)
    bid     = int(bid_s)
    aid     = callback.from_user.id
    chat_id = callback.message.chat.id

    if action == "restore":
        ok = await do_unban(callback.bot, chat_id, uid, admin_id=aid)
        await mark_auto_ban_reverted(bid)
        label = "♻️ TIKLANDI (admin tomonidan)"
        await callback.answer("♻️ Tiklandi + trusted." if ok else "❌ Xato.", show_alert=True)
    else:  # confirm
        await audit(aid, "confirm_auto_ban", uid, f"ban_id={bid}")
        label = "✅ BAN TASDIQLANDI"
        await callback.answer("✅ Ban tasdiqlandi.", show_alert=True)

    try:
        orig = callback.message.text or ""
        by   = f"@{callback.from_user.username}" if callback.from_user.username else str(aid)
        await callback.message.edit_text(
            f"{orig}\n\n<b>{label}</b>\nKim: {by}",
            parse_mode="HTML", reply_markup=None,
        )
    except Exception:
        pass


# ── Callback: config panel ───────────────────────────────────
@router.callback_query(F.data.startswith("cfg:"))
async def cb_config(callback: CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔ Faqat adminlar.", show_alert=True); return

    parts  = callback.data.split(":")
    action = parts[1]

    if action == "toggle":
        key     = parts[2]
        current = await get_setting(key)
        new_val = "0" if current == "1" else "1"
        await set_setting(key, new_val)
        await audit(callback.from_user.id, f"config:{key}", detail=f"{current}→{new_val}")
        await callback.answer(f"{'✅ ON' if new_val == '1' else '❌ OFF'}: {key}")
        await _refresh_cfg(callback)

    elif action == "cycle":
        key  = parts[2]
        opts = ["low", "medium", "high"]
        cur  = await get_setting(key) or "medium"
        nxt  = opts[(opts.index(cur) + 1) % len(opts)] if cur in opts else "medium"
        await set_setting(key, nxt)
        await callback.answer(f"Sensitivity: {nxt.upper()}")
        await _refresh_cfg(callback)

    elif action == "set" and parts[2] == "ban_delay":
        await callback.message.edit_reply_markup(reply_markup=kb_delay())
        await callback.answer()

    elif action == "delay":
        await set_setting("ban_delay", parts[2])
        await audit(callback.from_user.id, "config:ban_delay", detail=f"{parts[2]}m")
        await callback.answer(f"⏱ Ban delay: {parts[2]} min")
        await _refresh_cfg(callback)

    elif action == "back":
        await _refresh_cfg(callback)

    elif action == "audit":
        logs = await get_audit_log(15)
        if not logs:
            await callback.answer("Hali hech narsa yo'q.", show_alert=True); return
        lines = ["📋 <b>Audit Log</b>\n"]
        for e in logs:
            ts  = e["timestamp"][:16]
            by  = e["admin_id"] or "AUTO"
            act = e["action"]
            tgt = f"→{e['target_user']}" if e["target_user"] else ""
            lines.append(f"[{ts}] {by} {act} {tgt}")
        await callback.message.answer("\n".join(lines), parse_mode="HTML")
        await callback.answer()

    elif action == "close":
        await callback.message.delete()
        await callback.answer()


async def _refresh_cfg(callback: CallbackQuery):
    s = await get_all_settings()
    try:
        await callback.message.edit_reply_markup(reply_markup=kb_settings(s))
    except Exception:
        pass


# ═══════════════════════════════════════════════════════════
#  🚀  MAIN
# ═══════════════════════════════════════════════════════════

async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    if BOT_TOKEN == "BU_YERGA_BOT_TOKENINGIZNI_YOZING":
        print("\n❌ XATO: BOT_TOKEN o'rnatilmagan!")
        print("   1. bot.py faylini oching")
        print("   2. BOT_TOKEN = '...' qatorini toping")
        print("   3. @BotFather dan olgan tokeningizni kiriting\n")
        sys.exit(1)

    await init_db()
    log.info("✅ Database tayyor")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.message.middleware(UserTrackerMiddleware())
    dp.include_router(router)

    log.info(f"🤖 Bot ishga tushdi | Adminlar: {ADMIN_IDS}")
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        await bot.session.close()
        log.info("Bot to'xtatildi.")


if __name__ == "__main__":
    asyncio.run(main())
