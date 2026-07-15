import asyncio
import csv
import io
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_USERNAME = os.getenv("BOT_USERNAME", "rbsalebot").lstrip("@")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1001322091992"))
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "640314234"))
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
BASE_URL = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
DB_PATH = os.getenv("DB_PATH", "/tmp/declarant.sqlite3")
LINK_TTL_SECONDS = int(os.getenv("LINK_TTL_SECONDS", "600"))
CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "300"))

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
app = FastAPI()
tasks: list[asyncio.Task] = []


@contextmanager
def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now() -> int:
    return int(time.time())


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS campaigns (
                campaign_key TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                ad_label TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                campaign_key TEXT,
                first_seen_at INTEGER,
                approved_at INTEGER,
                left_at INTEGER,
                active INTEGER NOT NULL DEFAULT 0,
                blocked INTEGER NOT NULL DEFAULT 0,
                block_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS starts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                campaign_key TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS invite_links (
                invite_link TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                campaign_key TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                revoked INTEGER NOT NULL DEFAULT 0
            );
            """
        )


async def telegram(method: str, payload: dict | None = None):
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{API}/{method}", json=payload or {})
        data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data}")
    return data["result"]


def admin_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "➕ Создать ссылку", "callback_data": "admin_new"},
                {"text": "📊 Статистика", "callback_data": "admin_stats"},
            ],
            [
                {"text": "🔗 Кампании", "callback_data": "admin_campaigns"},
                {"text": "🚫 Чёрный список", "callback_data": "admin_blocked"},
            ],
        ]
    }


def stats_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "1 час", "callback_data": "stats_1h"},
                {"text": "24 часа", "callback_data": "stats_24h"},
            ],
            [
                {"text": "7 дней", "callback_data": "stats_7d"},
                {"text": "Всё время", "callback_data": "stats_all"},
            ],
            [{"text": "⬅️ Назад", "callback_data": "admin_home"}],
        ]
    }


@app.on_event("startup")
async def startup():
    init_db()
    await telegram(
        "setWebhook",
        {
            "url": f"{BASE_URL}/telegram/{WEBHOOK_SECRET}",
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": [
                "message",
                "callback_query",
                "chat_join_request",
                "chat_member",
            ],
            "drop_pending_updates": True,
        },
    )
    tasks.append(asyncio.create_task(cleanup_loop()))


@app.on_event("shutdown")
async def shutdown():
    for task in tasks:
        task.cancel()


@app.get("/")
async def health():
    return {"status": "ok"}


@app.post("/telegram/{secret}")
async def webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
):
    if secret != WEBHOOK_SECRET or x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(403, "Forbidden")

    update = await request.json()

    if "message" in update:
        await handle_message(update["message"])
    elif "callback_query" in update:
        await handle_callback(update["callback_query"])
    elif "chat_join_request" in update:
        await handle_join_request(update["chat_join_request"])
    elif "chat_member" in update:
        await handle_chat_member(update["chat_member"])

    return JSONResponse({"ok": True})


async def handle_message(message: dict):
    chat = message.get("chat") or {}
    user = message.get("from") or {}
    text = (message.get("text") or "").strip()

    if chat.get("type") != "private":
        return

    user_id = int(user["id"])
    chat_id = int(chat["id"])

    if user_id == ADMIN_USER_ID:
        if text == "/start" or text == "/admin":
            await telegram(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "Панель управления:",
                    "reply_markup": admin_keyboard(),
                },
            )
            return

        if text.startswith("/new "):
            await create_campaign_from_command(chat_id, text)
            return

        if text.startswith("/block "):
            await block_from_command(chat_id, text)
            return

        if text.startswith("/unblock "):
            await unblock_from_command(chat_id, text)
            return

    if not text.startswith("/start"):
        return

    parts = text.split(maxsplit=1)
    campaign_key = parts[1].strip().lower() if len(parts) == 2 else ""

    with db() as conn:
        existing = conn.execute(
            "SELECT approved_at, blocked FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        # Removed, blocked or previously joined: bot stays silent.
        if existing and (existing["blocked"] or existing["approved_at"]):
            return

        campaign = conn.execute(
            """
            SELECT campaign_key FROM campaigns
            WHERE campaign_key = ? AND active = 1
            """,
            (campaign_key,),
        ).fetchone()

        if not campaign:
            await telegram(
                "sendMessage",
                {
                    "chat_id": chat_id,
                    "text": "Доступ возможен только по рекламной ссылке.",
                },
            )
            return

        conn.execute(
            """
            INSERT INTO starts(user_id, campaign_key, created_at)
            VALUES (?, ?, ?)
            """,
            (user_id, campaign_key, now()),
        )

        conn.execute(
            """
            INSERT INTO users(
                user_id, username, first_name, campaign_key,
                first_seen_at, active, blocked
            )
            VALUES (?, ?, ?, ?, ?, 0, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name
            """,
            (
                user_id,
                user.get("username"),
                user.get("first_name"),
                campaign_key,
                now(),
            ),
        )

    expires_at = now() + LINK_TTL_SECONDS
    invite = await telegram(
        "createChatInviteLink",
        {
            "chat_id": CHANNEL_ID,
            "name": f"{campaign_key}_{user_id}_{now()}",
            "expire_date": expires_at,
            "creates_join_request": True,
        },
    )

    with db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO invite_links(
                invite_link, user_id, campaign_key, expires_at, used, revoked
            )
            VALUES (?, ?, ?, ?, 0, 0)
            """,
            (invite["invite_link"], user_id, campaign_key, expires_at),
        )

    await telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": "Нажмите кнопку, чтобы открыть закрытый каталог.",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "🛍 Открыть каталог", "url": invite["invite_link"]}]
                ]
            },
        },
    )


async def handle_callback(callback: dict):
    user = callback.get("from") or {}
    message = callback.get("message") or {}
    data = callback.get("data") or ""

    if int(user.get("id", 0)) != ADMIN_USER_ID:
        return

    await telegram(
        "answerCallbackQuery",
        {"callback_query_id": callback["id"]},
    )

    chat_id = int((message.get("chat") or {})["id"])
    message_id = int(message["message_id"])

    if data == "admin_home":
        await edit(chat_id, message_id, "Панель управления:", admin_keyboard())

    elif data == "admin_new":
        await edit(
            chat_id,
            message_id,
            "Создай кампанию командой:\n\n/new ключ | источник | название объявления\n\nПример:\n/new fb_video_1 | facebook | Девушка у холодильника",
            {"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "admin_home"}]]},
        )

    elif data == "admin_stats":
        await edit(chat_id, message_id, "Выбери период:", stats_keyboard())

    elif data.startswith("stats_"):
        period = data.removeprefix("stats_")
        seconds = {"1h": 3600, "24h": 86400, "7d": 604800, "all": None}[period]
        await edit(chat_id, message_id, build_stats(seconds), stats_keyboard())

    elif data == "admin_campaigns":
        await edit(
            chat_id,
            message_id,
            build_campaigns(),
            {"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "admin_home"}]]},
        )

    elif data == "admin_blocked":
        await edit(
            chat_id,
            message_id,
            build_blocked(),
            {"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "admin_home"}]]},
        )


async def edit(chat_id: int, message_id: int, text: str, reply_markup: dict):
    await telegram(
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "reply_markup": reply_markup,
        },
    )


async def create_campaign_from_command(chat_id: int, text: str):
    raw = text.removeprefix("/new").strip()
    parts = [part.strip() for part in raw.split("|")]

    if len(parts) != 3:
        await telegram(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "Формат:\n/new ключ | источник | название объявления",
            },
        )
        return

    key, source, ad_label = parts

    if not re.fullmatch(r"[a-z0-9_]{2,40}", key):
        await telegram(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "Ключ: только латиница, цифры и _.",
            },
        )
        return

    with db() as conn:
        conn.execute(
            """
            INSERT INTO campaigns(
                campaign_key, source, ad_label, created_at, active
            )
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(campaign_key) DO UPDATE SET
                source = excluded.source,
                ad_label = excluded.ad_label,
                active = 1
            """,
            (key, source, ad_label, now()),
        )

    await telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": (
                f"✅ Кампания создана\n\n"
                f"{key}\n{source}\n{ad_label}\n\n"
                f"Ссылка для рекламы:\n"
                f"https://t.me/{BOT_USERNAME}?start={key}"
            ),
            "reply_markup": admin_keyboard(),
        },
    )


def build_stats(seconds: int | None) -> str:
    since = now() - seconds if seconds else 0

    with db() as conn:
        campaigns = conn.execute(
            "SELECT * FROM campaigns ORDER BY created_at DESC"
        ).fetchall()

        lines = ["📊 Статистика\n"]
        rows = []

        for campaign in campaigns:
            key = campaign["campaign_key"]

            starts = conn.execute(
                """
                SELECT COUNT(*) AS value FROM starts
                WHERE campaign_key = ? AND created_at >= ?
                """,
                (key, since),
            ).fetchone()["value"]

            unique_users = conn.execute(
                """
                SELECT COUNT(DISTINCT user_id) AS value FROM starts
                WHERE campaign_key = ? AND created_at >= ?
                """,
                (key, since),
            ).fetchone()["value"]

            joins = conn.execute(
                """
                SELECT COUNT(*) AS value FROM users
                WHERE campaign_key = ?
                  AND approved_at IS NOT NULL
                  AND approved_at >= ?
                """,
                (key, since),
            ).fetchone()["value"]

            active = conn.execute(
                """
                SELECT COUNT(*) AS value FROM users
                WHERE campaign_key = ? AND active = 1 AND blocked = 0
                """,
                (key,),
            ).fetchone()["value"]

            left = conn.execute(
                """
                SELECT COUNT(*) AS value FROM users
                WHERE campaign_key = ?
                  AND left_at IS NOT NULL
                  AND left_at >= ?
                """,
                (key, since),
            ).fetchone()["value"]

            conversion = round(joins / unique_users * 100, 1) if unique_users else 0

            rows.append((conversion, joins, key, starts, unique_users, active, left))

        rows.sort(reverse=True)

        for conversion, joins, key, starts, unique_users, active, left in rows:
            lines.append(
                f"\n• {key}\n"
                f"Запуски: {starts}\n"
                f"Уникальные: {unique_users}\n"
                f"Вступили: {joins}\n"
                f"Сейчас в канале: {active}\n"
                f"Вышли: {left}\n"
                f"Конверсия: {conversion}%\n"
            )

        if not rows:
            lines.append("\nКампаний пока нет.")

    return "".join(lines)


def build_campaigns() -> str:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM campaigns ORDER BY created_at DESC"
        ).fetchall()

    if not rows:
        return "Кампаний пока нет."

    lines = ["🔗 Кампании\n"]
    for row in rows:
        status = "активна" if row["active"] else "отключена"
        lines.append(
            f"\n• {row['campaign_key']} — {status}\n"
            f"{row['source']} · {row['ad_label']}\n"
            f"https://t.me/{BOT_USERNAME}?start={row['campaign_key']}\n"
        )
    return "".join(lines)


def build_blocked() -> str:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT user_id, username, first_name, block_reason
            FROM users
            WHERE blocked = 1
            ORDER BY blocked_at DESC
            LIMIT 50
            """
        ).fetchall()

    if not rows:
        return "Чёрный список пуст."

    lines = ["🚫 Чёрный список\n"]
    for row in rows:
        username = f"@{row['username']}" if row["username"] else "без username"
        lines.append(
            f"\n• {row['first_name'] or 'Без имени'} ({username})\n"
            f"ID: {row['user_id']}\n"
            f"Причина: {row['block_reason'] or '—'}\n"
        )
    lines.append("\nРазблокировать:\n/unblock Telegram_ID")
    return "".join(lines)


async def block_from_command(chat_id: int, text: str):
    value = text.removeprefix("/block").strip()
    if not value.isdigit():
        await telegram("sendMessage", {"chat_id": chat_id, "text": "Формат: /block Telegram_ID"})
        return

    user_id = int(value)
    with db() as conn:
        conn.execute(
            """
            UPDATE users SET
                blocked = 1,
                block_reason = 'manual',
                active = 0
            WHERE user_id = ?
            """,
            (user_id,),
        )

    try:
        await telegram("banChatMember", {"chat_id": CHANNEL_ID, "user_id": user_id})
    except Exception:
        pass

    await telegram("sendMessage", {"chat_id": chat_id, "text": "Пользователь заблокирован."})


async def unblock_from_command(chat_id: int, text: str):
    value = text.removeprefix("/unblock").strip()
    if not value.isdigit():
        await telegram("sendMessage", {"chat_id": chat_id, "text": "Формат: /unblock Telegram_ID"})
        return

    user_id = int(value)
    with db() as conn:
        conn.execute(
            """
            UPDATE users SET
                blocked = 0,
                block_reason = NULL
            WHERE user_id = ?
            """,
            (user_id,),
        )

    try:
        await telegram(
            "unbanChatMember",
            {
                "chat_id": CHANNEL_ID,
                "user_id": user_id,
                "only_if_banned": True,
            },
        )
    except Exception:
        pass

    await telegram("sendMessage", {"chat_id": chat_id, "text": "Пользователь разблокирован."})


async def handle_join_request(join_request: dict):
    if (join_request.get("chat") or {}).get("id") != CHANNEL_ID:
        return

    user_id = int((join_request.get("from") or {})["id"])
    invite_link = (join_request.get("invite_link") or {}).get("invite_link")

    with db() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        link = conn.execute(
            "SELECT * FROM invite_links WHERE invite_link = ?",
            (invite_link,),
        ).fetchone()

        valid = bool(
            user
            and not user["blocked"]
            and not user["approved_at"]
            and link
            and link["user_id"] == user_id
            and not link["used"]
            and not link["revoked"]
            and link["expires_at"] >= now()
        )

    if not valid:
        await telegram(
            "declineChatJoinRequest",
            {"chat_id": CHANNEL_ID, "user_id": user_id},
        )
        return

    await telegram(
        "approveChatJoinRequest",
        {"chat_id": CHANNEL_ID, "user_id": user_id},
    )

    try:
        await telegram(
            "revokeChatInviteLink",
            {"chat_id": CHANNEL_ID, "invite_link": invite_link},
        )
    except Exception:
        pass

    with db() as conn:
        conn.execute(
            """
            UPDATE invite_links SET used = 1, revoked = 1
            WHERE invite_link = ?
            """,
            (invite_link,),
        )
        conn.execute(
            """
            UPDATE users SET
                approved_at = ?,
                active = 1,
                left_at = NULL
            WHERE user_id = ?
            """,
            (now(), user_id),
        )
        user = conn.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    await telegram(
        "sendMessage",
        {
            "chat_id": ADMIN_USER_ID,
            "text": (
                "✅ Новое вступление\n\n"
                f"Кампания: {user['campaign_key']}\n"
                f"Пользователь: {user['first_name'] or 'Без имени'}\n"
                f"ID: {user_id}"
            ),
        },
    )


async def handle_chat_member(update: dict):
    if (update.get("chat") or {}).get("id") != CHANNEL_ID:
        return

    new_member = update.get("new_chat_member") or {}
    user_id = int((new_member.get("user") or {})["id"])
    status = new_member.get("status")

    with db() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not user:
            return

        if status in {"member", "administrator", "creator"}:
            conn.execute(
                "UPDATE users SET active = 1, left_at = NULL WHERE user_id = ?",
                (user_id,),
            )
            return

        if status == "kicked":
            conn.execute(
                """
                UPDATE users SET
                    active = 0,
                    left_at = ?,
                    blocked = 1,
                    block_reason = 'removed_from_channel'
                WHERE user_id = ?
                """,
                (now(), user_id),
            )
            action = "удалён из канала и заблокирован"
        else:
            conn.execute(
                """
                UPDATE users SET active = 0, left_at = ?
                WHERE user_id = ?
                """,
                (now(), user_id),
            )
            action = "вышел из канала"

    await telegram(
        "sendMessage",
        {
            "chat_id": ADMIN_USER_ID,
            "text": (
                f"🚪 Пользователь {action}\n"
                f"Кампания: {user['campaign_key']}\n"
                f"ID: {user_id}"
            ),
        },
    )


async def cleanup_loop():
    while True:
        try:
            with db() as conn:
                rows = conn.execute(
                    """
                    SELECT invite_link FROM invite_links
                    WHERE used = 0 AND revoked = 0 AND expires_at < ?
                    """,
                    (now(),),
                ).fetchall()

            for row in rows:
                try:
                    await telegram(
                        "revokeChatInviteLink",
                        {
                            "chat_id": CHANNEL_ID,
                            "invite_link": row["invite_link"],
                        },
                    )
                except Exception:
                    pass

                with db() as conn:
                    conn.execute(
                        """
                        UPDATE invite_links SET revoked = 1
                        WHERE invite_link = ?
                        """,
                        (row["invite_link"],),
                    )
        except Exception:
            pass

        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
