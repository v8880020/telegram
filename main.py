import os
import sqlite3
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, Header, HTTPException, Request

BOT_TOKEN = os.environ["BOT_TOKEN"]
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1001322091992"))
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
BASE_URL = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
LINK_TTL_SECONDS = int(os.getenv("LINK_TTL_SECONDS", "600"))
ALLOWED_START_KEYS = {
    key.strip()
    for key in os.getenv("ALLOWED_START_KEYS", "fb_by_2026").split(",")
    if key.strip()
}
DB_PATH = os.getenv("DB_PATH", "/tmp/bot.db")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
app = FastAPI()


def db() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.row_factory = sqlite3.Row
    return connection


def init_db() -> None:
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    with db() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS access_links (
                invite_link TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                campaign TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                campaign TEXT NOT NULL,
                first_seen_at INTEGER NOT NULL,
                approved_at INTEGER
            )
            """
        )


async def telegram(method: str, payload: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(f"{API}/{method}", json=payload or {})
        data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error in {method}: {data}")
    return data["result"]


@app.on_event("startup")
async def startup() -> None:
    init_db()
    await telegram(
        "setWebhook",
        {
            "url": f"{BASE_URL}/telegram/{WEBHOOK_SECRET}",
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": ["message", "chat_join_request"],
            "drop_pending_updates": True,
        },
    )


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

    message = update.get("message") or {}
    if message:
        await handle_message(message)

    return {"ok": True}


async def handle_message(message: dict) -> None:
    text = (message.get("text") or "").strip()
    chat = message.get("chat") or {}
    user = message.get("from") or {}

    if chat.get("type") != "private" or not text.startswith("/start"):
        return

    user_id = user.get("id")
    chat_id = chat.get("id")
    parts = text.split(maxsplit=1)
    start_key = parts[1].strip() if len(parts) == 2 else ""

    if start_key not in ALLOWED_START_KEYS:
        await telegram(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": "Доступ к каталогу доступен только по специальной ссылке из рекламы.",
            },
        )
        return

    try:
        member = await telegram(
            "getChatMember",
            {"chat_id": CHANNEL_ID, "user_id": user_id},
        )
        if member.get("status") in {"member", "administrator", "creator"}:
            await telegram(
                "sendMessage",
                {"chat_id": chat_id, "text": "Вы уже состоите в канале."},
            )
            return
    except Exception:
        pass

    with db() as connection:
        existing = connection.execute(
            "SELECT approved_at FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if existing and existing["approved_at"]:
            await telegram(
                "sendMessage",
                {"chat_id": chat_id, "text": "Доступ для этого аккаунта уже был использован."},
            )
            return

        now = int(time.time())
        connection.execute(
            """
            INSERT INTO users(user_id, campaign, first_seen_at)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET campaign = excluded.campaign
            """,
            (user_id, start_key, now),
        )

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

    with db() as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO access_links
            (invite_link, user_id, campaign, created_at, expires_at, used)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (invite["invite_link"], user_id, start_key, now, expires_at),
        )

    await telegram(
        "sendMessage",
        {
            "chat_id": chat_id,
            "text": (
                "Нажмите кнопку ниже, чтобы открыть каталог.\n\n"
                "Ссылка персональная и действует 10 минут. "
                "После нажатия заявка будет подтверждена автоматически."
            ),
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "🛍 Открыть каталог", "url": invite["invite_link"]}]
                ]
            },
        },
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
            SELECT user_id, expires_at, used
            FROM access_links
            WHERE invite_link = ?
            """,
            (invite_link,),
        ).fetchone()

    valid = (
        row
        and row["user_id"] == user_id
        and row["used"] == 0
        and row["expires_at"] >= now
    )

    if valid:
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
            connection.execute(
                "UPDATE access_links SET used = 1 WHERE invite_link = ?",
                (invite_link,),
            )
            connection.execute(
                "UPDATE users SET approved_at = ? WHERE user_id = ?",
                (now, user_id),
            )
    else:
        await telegram(
            "declineChatJoinRequest",
            {"chat_id": CHANNEL_ID, "user_id": user_id},
        )
