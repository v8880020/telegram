import asyncio
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1001322091992"))
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "640314234"))
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
BASE_URL = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
LINK_TTL_SECONDS = int(os.getenv("LINK_TTL_SECONDS", "600"))
ALLOWED_START_KEYS = {
    key.strip()
    for key in os.getenv("ALLOWED_START_KEYS", "fb_campaign_1").split(",")
    if key.strip()
}
DB_PATH = os.getenv("DB_PATH", "/tmp/bot.db")
CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "600"))

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
app = FastAPI()
cleanup_task: asyncio.Task | None = None


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)

    with db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                campaign TEXT NOT NULL,
                first_seen_at INTEGER NOT NULL,
                approved_at INTEGER,
                left_at INTEGER,
                active INTEGER NOT NULL DEFAULT 0,
                start_message_id INTEGER,
                bot_message_id INTEGER
            )
            """
        )

        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS access_links (
                invite_link TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                campaign TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                revoked INTEGER NOT NULL DEFAULT 0
            )
            """
        )


async def telegram(method: str, payload: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.post(f"{API}/{method}", json=payload or {})
        data = response.json()

    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error in {method}: {data}")

    return data["result"]


async def safe_delete_message(chat_id: int, message_id: int | None) -> None:
    if not message_id:
        return

    try:
        await telegram(
            "deleteMessage",
            {"chat_id": chat_id, "message_id": message_id},
        )
    except Exception:
        pass


async def revoke_expired_links() -> None:
    now = int(time.time())

    with db() as connection:
        rows = connection.execute(
            """
            SELECT invite_link
            FROM access_links
            WHERE used = 0
              AND revoked = 0
              AND expires_at < ?
            """,
            (now,),
        ).fetchall()

    for row in rows:
        invite_link = row["invite_link"]

        try:
            await telegram(
                "revokeChatInviteLink",
                {"chat_id": CHANNEL_ID, "invite_link": invite_link},
            )
        except Exception:
            pass

        with db() as connection:
            connection.execute(
                """
                UPDATE access_links
                SET revoked = 1
                WHERE invite_link = ?
                """,
                (invite_link,),
            )


async def cleanup_loop() -> None:
    while True:
        try:
            await revoke_expired_links()
        except Exception:
            pass

        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)


@app.on_event("startup")
async def startup() -> None:
    global cleanup_task

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

    cleanup_task = asyncio.create_task(cleanup_loop())


@app.on_event("shutdown")
async def shutdown() -> None:
    if cleanup_task:
        cleanup_task.cancel()


@app.get("/")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/telegram/{secret}")
async def webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> dict:
    if secret != WEBHOOK_SECRET or x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    update = await request.json()

    if "chat_join_request" in update:
        await handle_join_request(update["chat_join_request"])
        return {"ok": True}

    if "chat_member" in update:
        await handle_chat_member(update["chat_member"])
        return {"ok": True}

    if "callback_query" in update:
        await handle_callback_query(update["callback_query"])
        return {"ok": True}

    message = update.get("message")
    if message:
        await handle_message(message)

    return {"ok": True}


async def handle_message(message: dict) -> None:
    text = (message.get("text") or "").strip()
    chat = message.get("chat") or {}
    user = message.get("from") or {}

    if chat.get("type") != "private":
        return

    user_id = user.get("id")
    chat_id = chat.get("id")

    if not user_id or not chat_id:
        return

    if text == "/stats":
        if user_id == ADMIN_USER_ID:
            await send_stats_menu(chat_id)
        return

    if not text.startswith("/start"):
        return

    parts = text.split(maxsplit=1)
    start_key = parts[1].strip() if len(parts) == 2 else ""

    if start_key not in ALLOWED_START_KEYS:
        reply = await telegram(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "Доступ к каталогу доступен только по специальной ссылке из рекламы.",
            },
        )

        with db() as connection:
            connection.execute(
                """
                INSERT INTO users(
                    user_id, username, first_name, campaign,
                    first_seen_at, start_message_id, bot_message_id
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    start_message_id = excluded.start_message_id,
                    bot_message_id = excluded.bot_message_id
                """,
                (
                    user_id,
                    user.get("username"),
                    user.get("first_name"),
                    "direct",
                    int(time.time()),
                    message.get("message_id"),
                    reply.get("message_id"),
                ),
            )
        return

    try:
        member = await telegram(
            "getChatMember",
            {"chat_id": CHANNEL_ID, "user_id": user_id},
        )

        if member.get("status") in {"member", "administrator", "creator"}:
            await safe_delete_message(chat_id, message.get("message_id"))
            return
    except Exception:
        pass

    with db() as connection:
        existing = connection.execute(
            """
            SELECT approved_at
            FROM users
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

        if existing and existing["approved_at"]:
            await safe_delete_message(chat_id, message.get("message_id"))
            return

    now = int(time.time())
    expires_at = now + LINK_TTL_SECONDS

    invite = await telegram(
        "createChatInviteLink",
        {
            "chat_id": CHANNEL_ID,
            "name": f"{start_key}_{user_id}_{now}",
            "expire_date": expires_at,
            "creates_join_request": True,
        },
    )

    reply = await telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": (
                "Нажмите кнопку ниже, чтобы открыть каталог.\n\n"
                "Ссылка персональная и действует 10 минут."
            ),
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "🛍 Открыть каталог", "url": invite["invite_link"]}]
                ]
            },
        },
    )

    with db() as connection:
        connection.execute(
            """
            INSERT INTO users(
                user_id, username, first_name, campaign,
                first_seen_at, approved_at, left_at, active,
                start_message_id, bot_message_id
            )
            VALUES (?, ?, ?, ?, ?, NULL, NULL, 0, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                start_message_id = excluded.start_message_id,
                bot_message_id = excluded.bot_message_id
            """,
            (
                user_id,
                user.get("username"),
                user.get("first_name"),
                start_key,
                now,
                message.get("message_id"),
                reply.get("message_id"),
            ),
        )

        connection.execute(
            """
            INSERT OR REPLACE INTO access_links(
                invite_link, user_id, campaign,
                created_at, expires_at, used, revoked
            )
            VALUES (?, ?, ?, ?, ?, 0, 0)
            """,
            (
                invite["invite_link"],
                user_id,
                start_key,
                now,
                expires_at,
            ),
        )


async def handle_join_request(join_request: dict) -> None:
    chat = join_request.get("chat") or {}
    user = join_request.get("from") or {}
    invite_link_data = join_request.get("invite_link") or {}

    if chat.get("id") != CHANNEL_ID:
        return

    user_id = user.get("id")
    invite_link = invite_link_data.get("invite_link")
    now = int(time.time())

    with db() as connection:
        row = connection.execute(
            """
            SELECT user_id, expires_at, used, revoked
            FROM access_links
            WHERE invite_link = ?
            """,
            (invite_link,),
        ).fetchone()

    valid = (
        row
        and row["user_id"] == user_id
        and row["used"] == 0
        and row["revoked"] == 0
        and row["expires_at"] >= now
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

    with db() as connection:
        user_row = connection.execute(
            """
            SELECT start_message_id, bot_message_id
            FROM users
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

        connection.execute(
            """
            UPDATE access_links
            SET used = 1, revoked = 1
            WHERE invite_link = ?
            """,
            (invite_link,),
        )

        connection.execute(
            """
            UPDATE users
            SET approved_at = ?, active = 1, left_at = NULL
            WHERE user_id = ?
            """,
            (now, user_id),
        )

    if user_row:
        await safe_delete_message(user_id, user_row["bot_message_id"])
        await safe_delete_message(user_id, user_row["start_message_id"])


async def handle_chat_member(chat_member_update: dict) -> None:
    chat = chat_member_update.get("chat") or {}
    new_member = chat_member_update.get("new_chat_member") or {}
    user = new_member.get("user") or {}

    if chat.get("id") != CHANNEL_ID:
        return

    user_id = user.get("id")
    status = new_member.get("status")
    now = int(time.time())

    if not user_id:
        return

    active_statuses = {"member", "administrator", "creator"}

    with db() as connection:
        exists = connection.execute(
            "SELECT user_id FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if not exists:
            return

        if status in active_statuses:
            connection.execute(
                """
                UPDATE users
                SET active = 1, left_at = NULL
                WHERE user_id = ?
                """,
                (user_id,),
            )
        else:
            connection.execute(
                """
                UPDATE users
                SET active = 0,
                    left_at = CASE
                        WHEN approved_at IS NOT NULL THEN ?
                        ELSE left_at
                    END
                WHERE user_id = ?
                """,
                (now, user_id),
            )


async def handle_callback_query(callback_query: dict) -> None:
    user = callback_query.get("from") or {}
    message = callback_query.get("message") or {}
    data = callback_query.get("data") or ""

    user_id = user.get("id")
    chat_id = (message.get("chat") or {}).get("id")

    if user_id != ADMIN_USER_ID or not chat_id:
        return

    await telegram(
        "answerCallbackQuery",
        {"callback_query_id": callback_query["id"]},
    )

    period_map = {
        "stats_1h": (3600, "за последний час"),
        "stats_24h": (86400, "за последние сутки"),
        "stats_all": (None, "за всё время"),
    }

    if data not in period_map:
        return

    seconds, title = period_map[data]
    text = build_stats_text(seconds, title)

    await telegram(
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message["message_id"],
            "text": text,
            "reply_markup": stats_keyboard(),
        },
    )


def stats_keyboard() -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "1 час", "callback_data": "stats_1h"},
                {"text": "24 часа", "callback_data": "stats_24h"},
                {"text": "Всё время", "callback_data": "stats_all"},
            ]
        ]
    }


async def send_stats_menu(chat_id: int) -> None:
    await telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": build_stats_text(86400, "за последние сутки"),
            "reply_markup": stats_keyboard(),
        },
    )


def build_stats_text(seconds: int | None, title: str) -> str:
    now = int(time.time())
    since = now - seconds if seconds else 0

    with db() as connection:
        campaigns = connection.execute(
            """
            SELECT DISTINCT campaign
            FROM users
            WHERE campaign != 'direct'
            ORDER BY campaign
            """
        ).fetchall()

        lines = [f"📊 Статистика {title}\n"]

        total_starts = 0
        total_joins = 0
        total_active = 0
        total_left = 0

        for campaign_row in campaigns:
            campaign = campaign_row["campaign"]

            starts = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM users
                WHERE campaign = ?
                  AND first_seen_at >= ?
                """,
                (campaign, since),
            ).fetchone()["count"]

            joins = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM users
                WHERE campaign = ?
                  AND approved_at IS NOT NULL
                  AND approved_at >= ?
                """,
                (campaign, since),
            ).fetchone()["count"]

            left = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM users
                WHERE campaign = ?
                  AND left_at IS NOT NULL
                  AND left_at >= ?
                """,
                (campaign, since),
            ).fetchone()["count"]

            active = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM users
                WHERE campaign = ?
                  AND approved_at IS NOT NULL
                  AND active = 1
                """,
                (campaign,),
            ).fetchone()["count"]

            conversion = round((joins / starts) * 100, 1) if starts else 0

            total_starts += starts
            total_joins += joins
            total_active += active
            total_left += left

            lines.append(
                f"• {campaign}\n"
                f"  Открыли бота: {starts}\n"
                f"  Вступили: {joins}\n"
                f"  Осталось в канале: {active}\n"
                f"  Отписалось за период: {left}\n"
                f"  Конверсия: {conversion}%\n"
            )

        if not campaigns:
            lines.append("Статистики пока нет.")
        else:
            overall_conversion = (
                round((total_joins / total_starts) * 100, 1)
                if total_starts
                else 0
            )

            lines.append(
                "Итого:\n"
                f"Открыли бота: {total_starts}\n"
                f"Вступили: {total_joins}\n"
                f"Сейчас в канале: {total_active}\n"
                f"Отписалось за период: {total_left}\n"
                f"Конверсия: {overall_conversion}%"
            )

    return "\n".join(lines)
