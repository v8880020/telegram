import asyncio
import os
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
DEFAULT_LINK_TTL = int(os.getenv("LINK_TTL_SECONDS", "600"))
CLEANUP_INTERVAL = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "300"))
MONITOR_INTERVAL = int(os.getenv("MONITOR_INTERVAL_SECONDS", "300"))
BACKUP_INTERVAL = int(os.getenv("BACKUP_INTERVAL_SECONDS", "86400"))

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
app = FastAPI()
tasks: list[asyncio.Task[Any]] = []


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


def fmt(ts: int | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%d.%m.%Y %H:%M UTC")


def init_db() -> None:
    with db() as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS campaigns (
                campaign_key TEXT PRIMARY KEY,
                display_name TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                user_limit INTEGER,
                channel_id INTEGER NOT NULL
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
                block_reason TEXT,
                whitelisted INTEGER NOT NULL DEFAULT 0,
                request_count INTEGER NOT NULL DEFAULT 0,
                last_request_at INTEGER
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

            CREATE TABLE IF NOT EXISTS settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS admin_state (
                admin_id INTEGER PRIMARY KEY,
                state TEXT,
                payload TEXT
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                action TEXT NOT NULL,
                details TEXT,
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS user_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                campaign_key TEXT,
                details TEXT,
                created_at INTEGER NOT NULL
            );

            INSERT OR IGNORE INTO settings(setting_key, setting_value)
            VALUES
              ('welcome_text', 'Нажмите кнопку, чтобы открыть закрытый каталог.'),
              ('button_text', '🛍 Открыть каталог'),
              ('link_ttl', '600'),
              ('notifications', '1'),
              ('maintenance', '0'),
              ('global_pause', '0'),
              ('rate_limit_count', '5'),
              ('rate_limit_window', '3600');
            """
        )


async def telegram(method: str, payload: dict | None = None):
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{API}/{method}", json=payload or {})
        data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"{method}: {data}")
    return data["result"]


async def send_document(chat_id: int, file_path: str, caption: str = ""):
    async with httpx.AsyncClient(timeout=60) as client:
        with open(file_path, "rb") as file_handle:
            response = await client.post(
                f"{API}/sendDocument",
                data={"chat_id": str(chat_id), "caption": caption},
                files={"document": (Path(file_path).name, file_handle, "application/octet-stream")},
            )
            data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"sendDocument: {data}")
    return data["result"]


def setting(key: str, default: str) -> str:
    with db() as conn:
        row = conn.execute(
            "SELECT setting_value FROM settings WHERE setting_key = ?",
            (key,),
        ).fetchone()
    return row["setting_value"] if row else default


def set_setting(key: str, value: str) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO settings(setting_key, setting_value)
            VALUES (?, ?)
            ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value
            """,
            (key, value),
        )


def set_state(state: str | None, payload: str | None = None) -> None:
    with db() as conn:
        conn.execute(
            """
            INSERT INTO admin_state(admin_id, state, payload)
            VALUES (?, ?, ?)
            ON CONFLICT(admin_id) DO UPDATE SET
                state = excluded.state,
                payload = excluded.payload
            """,
            (ADMIN_USER_ID, state, payload),
        )


def get_state():
    with db() as conn:
        row = conn.execute(
            "SELECT state, payload FROM admin_state WHERE admin_id = ?",
            (ADMIN_USER_ID,),
        ).fetchone()
    return (row["state"], row["payload"]) if row else (None, None)


def audit(action: str, details: str = "") -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO audit_log(action, details, created_at) VALUES (?, ?, ?)",
            (action, details, now()),
        )


def user_event(user_id: int, event_type: str, campaign_key: str | None, details: str = ""):
    with db() as conn:
        conn.execute(
            """
            INSERT INTO user_events(user_id, event_type, campaign_key, details, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, event_type, campaign_key, details, now()),
        )


def admin_keyboard():
    return {
        "inline_keyboard": [
            [
                {"text": "➕ Создать кампанию", "callback_data": "campaign_new"},
                {"text": "📊 Статистика", "callback_data": "stats_menu"},
            ],
            [
                {"text": "📁 Кампании", "callback_data": "campaigns"},
                {"text": "👥 Пользователи", "callback_data": "users_menu"},
            ],
            [
                {"text": "🚫 Чёрный список", "callback_data": "blocked"},
                {"text": "✅ Белый список", "callback_data": "whitelist"},
            ],
            [
                {"text": "⚙️ Настройки", "callback_data": "settings"},
                {"text": "📋 Журнал", "callback_data": "audit"},
            ],
            [
                {"text": "💾 Backup", "callback_data": "backup"},
                {"text": "🩺 Проверить канал", "callback_data": "channel_check"},
            ],
        ]
    }


def back_keyboard():
    return {"inline_keyboard": [[{"text": "⬅️ Назад", "callback_data": "home"}]]}


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
            [{"text": "⬅️ Назад", "callback_data": "home"}],
        ]
    }


def settings_keyboard():
    paused = setting("global_pause", "0") == "1"
    maintenance = setting("maintenance", "0") == "1"
    notifications = setting("notifications", "1") == "1"
    return {
        "inline_keyboard": [
            [{"text": "✏️ Приветствие", "callback_data": "set_welcome"}],
            [{"text": "🔘 Текст кнопки", "callback_data": "set_button"}],
            [{"text": "⏱ Время ссылки", "callback_data": "set_ttl"}],
            [{"text": f"{'▶️' if paused else '⏸'} Глобальная пауза", "callback_data": "toggle_pause"}],
            [{"text": f"{'✅' if maintenance else '🛠'} Обслуживание", "callback_data": "toggle_maintenance"}],
            [{"text": f"🔔 Уведомления: {'ON' if notifications else 'OFF'}", "callback_data": "toggle_notifications"}],
            [{"text": "❌ Отозвать все ссылки", "callback_data": "revoke_all"}],
            [{"text": "⬅️ Назад", "callback_data": "home"}],
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
            "allowed_updates": ["message", "callback_query", "chat_join_request", "chat_member"],
            "drop_pending_updates": True,
        },
    )
    tasks.extend(
        [
            asyncio.create_task(cleanup_loop()),
            asyncio.create_task(channel_monitor_loop()),
            asyncio.create_task(backup_loop()),
        ]
    )


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
        state, payload = get_state()

        if state:
            handled = await handle_admin_state(chat_id, text, state, payload)
            if handled:
                return

        if text in {"/start", "/admin"}:
            await telegram(
                "sendMessage",
                {"chat_id": chat_id, "text": "Панель управления:", "reply_markup": admin_keyboard()},
            )
            return

    if not text.startswith("/start"):
        return

    parts = text.split(maxsplit=1)
    campaign_key = parts[1].strip().lower() if len(parts) == 2 else ""

    if setting("maintenance", "0") == "1":
        await telegram("sendMessage", {"chat_id": chat_id, "text": "Ведутся технические работы."})
        return

    if setting("global_pause", "0") == "1":
        await telegram("sendMessage", {"chat_id": chat_id, "text": "Доступ временно приостановлен."})
        return

    with db() as conn:
        existing = conn.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if existing and existing["blocked"] and not existing["whitelisted"]:
            return

        if existing and existing["approved_at"]:
            return

        campaign = conn.execute(
            "SELECT * FROM campaigns WHERE campaign_key = ? AND status = 'active'",
            (campaign_key,),
        ).fetchone()

        if not campaign:
            await telegram("sendMessage", {"chat_id": chat_id, "text": "Доступ возможен только по активной рекламной ссылке."})
            return

        # Rate limiting.
        window = int(setting("rate_limit_window", "3600"))
        max_count = int(setting("rate_limit_count", "5"))
        current_time = now()
        request_count = 0
        if existing and existing["last_request_at"] and current_time - existing["last_request_at"] <= window:
            request_count = int(existing["request_count"]) + 1
        else:
            request_count = 1

        if request_count > max_count and not (existing and existing["whitelisted"]):
            conn.execute(
                """
                INSERT INTO users(user_id, username, first_name, blocked, block_reason, request_count, last_request_at)
                VALUES (?, ?, ?, 1, 'rate_limit', ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    blocked = 1, block_reason = 'rate_limit',
                    request_count = excluded.request_count,
                    last_request_at = excluded.last_request_at
                """,
                (user_id, user.get("username"), user.get("first_name"), request_count, current_time),
            )
            audit("auto_block", f"user={user_id}; reason=rate_limit")
            return

        # Campaign user limit.
        if campaign["user_limit"]:
            joined = conn.execute(
                "SELECT COUNT(*) AS value FROM users WHERE campaign_key = ? AND approved_at IS NOT NULL",
                (campaign_key,),
            ).fetchone()["value"]
            if joined >= campaign["user_limit"]:
                conn.execute(
                    "UPDATE campaigns SET status = 'paused' WHERE campaign_key = ?",
                    (campaign_key,),
                )
                await telegram("sendMessage", {"chat_id": chat_id, "text": "Лимит доступа по этой кампании достигнут."})
                return

        conn.execute(
            "INSERT INTO starts(user_id, campaign_key, created_at) VALUES (?, ?, ?)",
            (user_id, campaign_key, current_time),
        )
        conn.execute(
            """
            INSERT INTO users(
                user_id, username, first_name, campaign_key,
                first_seen_at, active, blocked, request_count, last_request_at
            )
            VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                request_count = excluded.request_count,
                last_request_at = excluded.last_request_at
            """,
            (
                user_id,
                user.get("username"),
                user.get("first_name"),
                campaign_key,
                current_time,
                request_count,
                current_time,
            ),
        )

    user_event(user_id, "start", campaign_key)

    ttl = int(setting("link_ttl", str(DEFAULT_LINK_TTL)))
    expires_at = now() + ttl

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
            "text": setting("welcome_text", "Нажмите кнопку, чтобы открыть закрытый каталог."),
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": setting("button_text", "🛍 Открыть каталог"), "url": invite["invite_link"]}]
                ]
            },
        },
    )


async def handle_admin_state(chat_id: int, text: str, state: str, payload: str | None) -> bool:
    if state == "set_welcome":
        if not text or len(text) > 1000:
            await telegram("sendMessage", {"chat_id": chat_id, "text": "Отправь текст до 1000 символов."})
            return True
        set_setting("welcome_text", text)
        set_state(None)
        audit("settings", "welcome_text changed")
        await telegram("sendMessage", {"chat_id": chat_id, "text": "✅ Приветствие сохранено.", "reply_markup": admin_keyboard()})
        return True

    if state == "set_button":
        if not text or len(text) > 64:
            await telegram("sendMessage", {"chat_id": chat_id, "text": "Отправь текст кнопки до 64 символов."})
            return True
        set_setting("button_text", text)
        set_state(None)
        audit("settings", "button_text changed")
        await telegram("sendMessage", {"chat_id": chat_id, "text": "✅ Текст кнопки сохранён.", "reply_markup": admin_keyboard()})
        return True

    if state == "set_ttl":
        if not text.isdigit() or not 60 <= int(text) <= 86400:
            await telegram("sendMessage", {"chat_id": chat_id, "text": "Введи количество секунд от 60 до 86400."})
            return True
        set_setting("link_ttl", text)
        set_state(None)
        audit("settings", f"link_ttl={text}")
        await telegram("sendMessage", {"chat_id": chat_id, "text": "✅ Время ссылки изменено.", "reply_markup": admin_keyboard()})
        return True

    if state == "search_user":
        await show_user_search(chat_id, text)
        set_state(None)
        return True

    if state == "campaign_limit":
        if not text.isdigit():
            await telegram("sendMessage", {"chat_id": chat_id, "text": "Введи число, например 100."})
            return True
        with db() as conn:
            conn.execute(
                "UPDATE campaigns SET user_limit = ? WHERE campaign_key = ?",
                (int(text), payload),
            )
        set_state(None)
        audit("campaign_limit", f"{payload}={text}")
        await telegram("sendMessage", {"chat_id": chat_id, "text": "✅ Лимит сохранён.", "reply_markup": admin_keyboard()})
        return True

    return False


async def handle_callback(callback: dict):
    user = callback.get("from") or {}
    message = callback.get("message") or {}
    data = callback.get("data") or ""

    if int(user.get("id", 0)) != ADMIN_USER_ID:
        return

    await telegram("answerCallbackQuery", {"callback_query_id": callback["id"]})

    chat_id = int((message.get("chat") or {})["id"])
    message_id = int(message["message_id"])

    if data == "home":
        set_state(None)
        await edit(chat_id, message_id, "Панель управления:", admin_keyboard())

    elif data == "campaign_new":
        campaign = create_campaign()
        audit("campaign_created", campaign["campaign_key"])
        await telegram(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": (
                    f"✅ Создана {campaign['display_name']}\n\n"
                    f"Ссылка:\nhttps://t.me/{BOT_USERNAME}?start={campaign['campaign_key']}"
                ),
                "reply_markup": campaign_actions(campaign["campaign_key"]),
            },
        )

    elif data == "stats_menu":
        await edit(chat_id, message_id, "Выбери период:", stats_keyboard())

    elif data.startswith("stats_"):
        period = data.removeprefix("stats_")
        seconds = {"1h": 3600, "24h": 86400, "7d": 604800, "all": None}[period]
        await edit(chat_id, message_id, build_stats(seconds), stats_keyboard())

    elif data == "campaigns":
        await edit(chat_id, message_id, build_campaign_list(), campaign_list_keyboard())

    elif data.startswith("campaign_open:"):
        key = data.split(":", 1)[1]
        await edit(chat_id, message_id, campaign_details(key), campaign_actions(key))

    elif data.startswith("campaign_pause:"):
        key = data.split(":", 1)[1]
        set_campaign_status(key, "paused")
        await edit(chat_id, message_id, campaign_details(key), campaign_actions(key))

    elif data.startswith("campaign_resume:"):
        key = data.split(":", 1)[1]
        set_campaign_status(key, "active")
        await edit(chat_id, message_id, campaign_details(key), campaign_actions(key))

    elif data.startswith("campaign_archive:"):
        key = data.split(":", 1)[1]
        set_campaign_status(key, "archived")
        await edit(chat_id, message_id, campaign_details(key), campaign_actions(key))

    elif data.startswith("campaign_duplicate:"):
        source_key = data.split(":", 1)[1]
        duplicate = create_campaign()
        audit("campaign_duplicated", f"{source_key}->{duplicate['campaign_key']}")
        await telegram(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": (
                    f"✅ Создана копия: {duplicate['display_name']}\n"
                    f"https://t.me/{BOT_USERNAME}?start={duplicate['campaign_key']}"
                ),
                "reply_markup": campaign_actions(duplicate["campaign_key"]),
            },
        )

    elif data.startswith("campaign_limit:"):
        key = data.split(":", 1)[1]
        set_state("campaign_limit", key)
        await telegram("sendMessage", {"chat_id": chat_id, "text": "Введи максимальное число вступивших для кампании:"})

    elif data.startswith("campaign_users:"):
        key = data.split(":", 1)[1]
        await edit(chat_id, message_id, campaign_users(key), back_keyboard())

    elif data == "users_menu":
        set_state("search_user")
        await edit(chat_id, message_id, "Отправь Telegram ID или @username для поиска.", back_keyboard())

    elif data == "blocked":
        await edit(chat_id, message_id, blocked_list(), back_keyboard())

    elif data == "whitelist":
        await edit(chat_id, message_id, whitelist_list(), back_keyboard())

    elif data == "settings":
        await edit(chat_id, message_id, settings_text(), settings_keyboard())

    elif data == "set_welcome":
        set_state("set_welcome")
        await telegram("sendMessage", {"chat_id": chat_id, "text": "Отправь новый текст приветствия."})

    elif data == "set_button":
        set_state("set_button")
        await telegram("sendMessage", {"chat_id": chat_id, "text": "Отправь новый текст кнопки."})

    elif data == "set_ttl":
        set_state("set_ttl")
        await telegram("sendMessage", {"chat_id": chat_id, "text": "Отправь время жизни ссылки в секундах."})

    elif data == "toggle_pause":
        set_setting("global_pause", "0" if setting("global_pause", "0") == "1" else "1")
        audit("settings", "global_pause toggled")
        await edit(chat_id, message_id, settings_text(), settings_keyboard())

    elif data == "toggle_maintenance":
        set_setting("maintenance", "0" if setting("maintenance", "0") == "1" else "1")
        audit("settings", "maintenance toggled")
        await edit(chat_id, message_id, settings_text(), settings_keyboard())

    elif data == "toggle_notifications":
        set_setting("notifications", "0" if setting("notifications", "1") == "1" else "1")
        await edit(chat_id, message_id, settings_text(), settings_keyboard())

    elif data == "revoke_all":
        count = await revoke_all_links()
        audit("revoke_all", f"count={count}")
        await telegram("sendMessage", {"chat_id": chat_id, "text": f"✅ Отозвано ссылок: {count}"})

    elif data == "audit":
        await edit(chat_id, message_id, audit_text(), back_keyboard())

    elif data == "backup":
        await create_and_send_backup(chat_id)

    elif data == "channel_check":
        text = await channel_check_text()
        await edit(chat_id, message_id, text, back_keyboard())


async def edit(chat_id: int, message_id: int, text: str, reply_markup: dict):
    await telegram(
        "editMessageText",
        {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:4090],
            "reply_markup": reply_markup,
        },
    )


def create_campaign():
    with db() as conn:
        row = conn.execute(
            """
            SELECT MAX(CAST(REPLACE(campaign_key, 'campaign_', '') AS INTEGER)) AS max_number
            FROM campaigns WHERE campaign_key LIKE 'campaign_%'
            """
        ).fetchone()
        number = int(row["max_number"] or 0) + 1
        key = f"campaign_{number}"
        display = f"Кампания {number}"
        conn.execute(
            """
            INSERT INTO campaigns(campaign_key, display_name, created_at, status, channel_id)
            VALUES (?, ?, ?, 'active', ?)
            """,
            (key, display, now(), CHANNEL_ID),
        )
    return {"campaign_key": key, "display_name": display}


def campaign_actions(key: str):
    with db() as conn:
        row = conn.execute("SELECT status FROM campaigns WHERE campaign_key = ?", (key,)).fetchone()
    status = row["status"] if row else "archived"
    toggle = (
        {"text": "▶️ Возобновить", "callback_data": f"campaign_resume:{key}"}
        if status != "active"
        else {"text": "⏸ Пауза", "callback_data": f"campaign_pause:{key}"}
    )
    return {
        "inline_keyboard": [
            [toggle, {"text": "📦 Архив", "callback_data": f"campaign_archive:{key}"}],
            [{"text": "👥 Пользователи", "callback_data": f"campaign_users:{key}"}],
            [{"text": "🔢 Лимит", "callback_data": f"campaign_limit:{key}"}],
            [{"text": "📄 Дублировать", "callback_data": f"campaign_duplicate:{key}"}],
            [{"text": "⬅️ Назад", "callback_data": "campaigns"}],
        ]
    }


def campaign_list_keyboard():
    with db() as conn:
        rows = conn.execute(
            "SELECT campaign_key, display_name, status FROM campaigns ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    buttons = [
        [{"text": f"{'🟢' if row['status']=='active' else '⏸' if row['status']=='paused' else '📦'} {row['display_name']}",
          "callback_data": f"campaign_open:{row['campaign_key']}"}]
        for row in rows
    ]
    buttons.append([{"text": "⬅️ Назад", "callback_data": "home"}])
    return {"inline_keyboard": buttons}


def set_campaign_status(key: str, status: str):
    with db() as conn:
        conn.execute("UPDATE campaigns SET status = ? WHERE campaign_key = ?", (status, key))
    audit("campaign_status", f"{key}={status}")


def campaign_details(key: str) -> str:
    with db() as conn:
        campaign = conn.execute("SELECT * FROM campaigns WHERE campaign_key = ?", (key,)).fetchone()
        if not campaign:
            return "Кампания не найдена."
        total = conn.execute("SELECT COUNT(*) AS v FROM starts WHERE campaign_key = ?", (key,)).fetchone()["v"]
        unique = conn.execute("SELECT COUNT(DISTINCT user_id) AS v FROM starts WHERE campaign_key = ?", (key,)).fetchone()["v"]
        joins = conn.execute("SELECT COUNT(*) AS v FROM users WHERE campaign_key = ? AND approved_at IS NOT NULL", (key,)).fetchone()["v"]
        active = conn.execute("SELECT COUNT(*) AS v FROM users WHERE campaign_key = ? AND active = 1", (key,)).fetchone()["v"]
    conversion = round(joins / unique * 100, 1) if unique else 0
    return (
        f"{campaign['display_name']}\n\n"
        f"Статус: {campaign['status']}\n"
        f"Создана: {fmt(campaign['created_at'])}\n"
        f"Лимит: {campaign['user_limit'] or 'нет'}\n\n"
        f"Запуски: {total}\n"
        f"Уникальные: {unique}\n"
        f"Вступили: {joins}\n"
        f"В канале: {active}\n"
        f"Конверсия: {conversion}%\n\n"
        f"https://t.me/{BOT_USERNAME}?start={key}"
    )


def build_campaign_list() -> str:
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) AS v FROM campaigns").fetchone()["v"]
    return f"📁 Кампании: {count}\n\nВыбери кампанию:"


def campaign_users(key: str) -> str:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT user_id, username, first_name, approved_at, active, blocked
            FROM users WHERE campaign_key = ?
            ORDER BY first_seen_at DESC LIMIT 50
            """,
            (key,),
        ).fetchall()
    if not rows:
        return "Пользователей пока нет."
    lines = [f"👥 Пользователи {key}\n"]
    for row in rows:
        status = "⛔" if row["blocked"] else "✅" if row["active"] else "🚪" if row["approved_at"] else "👀"
        username = f"@{row['username']}" if row["username"] else "без username"
        lines.append(f"\n{status} {row['first_name'] or 'Без имени'} ({username})\nID: {row['user_id']}")
    return "".join(lines)[:4000]


def build_stats(seconds: int | None) -> str:
    since = now() - seconds if seconds else 0
    with db() as conn:
        campaigns = conn.execute("SELECT * FROM campaigns ORDER BY created_at DESC").fetchall()
        rows = []
        for c in campaigns:
            key = c["campaign_key"]
            starts = conn.execute("SELECT COUNT(*) AS v FROM starts WHERE campaign_key=? AND created_at>=?", (key, since)).fetchone()["v"]
            unique = conn.execute("SELECT COUNT(DISTINCT user_id) AS v FROM starts WHERE campaign_key=? AND created_at>=?", (key, since)).fetchone()["v"]
            joins = conn.execute("SELECT COUNT(*) AS v FROM users WHERE campaign_key=? AND approved_at IS NOT NULL AND approved_at>=?", (key, since)).fetchone()["v"]
            active = conn.execute("SELECT COUNT(*) AS v FROM users WHERE campaign_key=? AND active=1", (key,)).fetchone()["v"]
            left = conn.execute("SELECT COUNT(*) AS v FROM users WHERE campaign_key=? AND left_at IS NOT NULL AND left_at>=?", (key, since)).fetchone()["v"]
            conversion = round(joins / unique * 100, 1) if unique else 0
            retention = round(active / joins * 100, 1) if joins else 0
            quality = "🟢" if conversion >= 50 and retention >= 70 else "🟡" if conversion >= 25 else "🔴"
            rows.append((conversion, joins, quality, c["display_name"], starts, unique, active, left, retention))
    rows.sort(reverse=True)
    lines = ["📊 Статистика\n"]
    for conversion, joins, quality, name, starts, unique, active, left, retention in rows:
        lines.append(
            f"\n{quality} {name}\nЗапуски: {starts}\nУникальные: {unique}\n"
            f"Вступили: {joins}\nВ канале: {active}\nВышли: {left}\n"
            f"Конверсия: {conversion}%\nУдержание: {retention}%\n"
        )
    return "".join(lines)[:4000] if rows else "Кампаний пока нет."


async def show_user_search(chat_id: int, query: str):
    cleaned = query.strip().lstrip("@")
    with db() as conn:
        if cleaned.isdigit():
            row = conn.execute("SELECT * FROM users WHERE user_id = ?", (int(cleaned),)).fetchone()
        else:
            row = conn.execute("SELECT * FROM users WHERE username LIKE ? LIMIT 1", (f"%{cleaned}%",)).fetchone()
        if not row:
            await telegram("sendMessage", {"chat_id": chat_id, "text": "Пользователь не найден.", "reply_markup": admin_keyboard()})
            return
        events = conn.execute(
            "SELECT event_type, details, created_at FROM user_events WHERE user_id = ? ORDER BY created_at DESC LIMIT 20",
            (row["user_id"],),
        ).fetchall()
    lines = [
        f"👤 {row['first_name'] or 'Без имени'}\n",
        f"ID: {row['user_id']}\n",
        f"Username: @{row['username']}" if row["username"] else "Username: —",
        f"\nКампания: {row['campaign_key'] or '—'}\n",
        f"Первый запуск: {fmt(row['first_seen_at'])}\n",
        f"Вступил: {fmt(row['approved_at'])}\n",
        f"Вышел: {fmt(row['left_at'])}\n",
        f"Статус: {'чёрный список' if row['blocked'] else 'в канале' if row['active'] else 'не в канале'}\n",
        "\nИстория:\n",
    ]
    for event in events:
        lines.append(f"• {fmt(event['created_at'])} — {event['event_type']} {event['details'] or ''}\n")
    await telegram("sendMessage", {"chat_id": chat_id, "text": "".join(lines)[:4000], "reply_markup": admin_keyboard()})


def blocked_list() -> str:
    with db() as conn:
        rows = conn.execute("SELECT * FROM users WHERE blocked=1 ORDER BY user_id DESC LIMIT 50").fetchall()
    if not rows:
        return "Чёрный список пуст."
    return "🚫 Чёрный список\n" + "".join(
        f"\n• {r['first_name'] or 'Без имени'} (@{r['username'] or '—'})\nID: {r['user_id']}\nПричина: {r['block_reason'] or '—'}\n"
        for r in rows
    )[:3900]


def whitelist_list() -> str:
    with db() as conn:
        rows = conn.execute("SELECT * FROM users WHERE whitelisted=1 ORDER BY user_id DESC LIMIT 50").fetchall()
    if not rows:
        return "Белый список пуст."
    return "✅ Белый список\n" + "".join(
        f"\n• {r['first_name'] or 'Без имени'} (@{r['username'] or '—'})\nID: {r['user_id']}\n"
        for r in rows
    )[:3900]


def settings_text() -> str:
    return (
        "⚙️ Настройки\n\n"
        f"Время ссылки: {setting('link_ttl', str(DEFAULT_LINK_TTL))} сек.\n"
        f"Глобальная пауза: {'ON' if setting('global_pause','0')=='1' else 'OFF'}\n"
        f"Обслуживание: {'ON' if setting('maintenance','0')=='1' else 'OFF'}\n"
        f"Уведомления: {'ON' if setting('notifications','1')=='1' else 'OFF'}"
    )


def audit_text() -> str:
    with db() as conn:
        rows = conn.execute("SELECT * FROM audit_log ORDER BY id DESC LIMIT 50").fetchall()
    if not rows:
        return "Журнал пуст."
    return "📋 Журнал\n" + "".join(
        f"\n• {fmt(r['created_at'])}\n{r['action']}: {r['details'] or '—'}\n"
        for r in rows
    )[:3900]


async def revoke_all_links() -> int:
    with db() as conn:
        rows = conn.execute("SELECT invite_link FROM invite_links WHERE used=0 AND revoked=0").fetchall()
    count = 0
    for row in rows:
        try:
            await telegram("revokeChatInviteLink", {"chat_id": CHANNEL_ID, "invite_link": row["invite_link"]})
        except Exception:
            pass
        with db() as conn:
            conn.execute("UPDATE invite_links SET revoked=1 WHERE invite_link=?", (row["invite_link"],))
        count += 1
    return count


async def create_and_send_backup(chat_id: int):
    if not Path(DB_PATH).exists():
        await telegram("sendMessage", {"chat_id": chat_id, "text": "База ещё не создана."})
        return
    backup_path = f"/tmp/declarant_backup_{now()}.sqlite3"
    shutil.copy2(DB_PATH, backup_path)
    await send_document(chat_id, backup_path, "Резервная копия базы")
    try:
        os.remove(backup_path)
    except OSError:
        pass
    audit("backup", "manual backup sent")


async def channel_check_text() -> str:
    try:
        me = await telegram("getMe")
        member = await telegram("getChatMember", {"chat_id": CHANNEL_ID, "user_id": me["id"]})
        status = member.get("status")
        can_invite = member.get("can_invite_users", False)
        can_restrict = member.get("can_restrict_members", False)
        ok = status in {"administrator", "creator"} and can_invite
        return (
            f"{'✅' if ok else '⚠️'} Проверка канала\n\n"
            f"Статус: {status}\n"
            f"Приглашать: {'да' if can_invite else 'нет'}\n"
            f"Блокировать: {'да' if can_restrict else 'нет'}"
        )
    except Exception as exc:
        return f"❌ Ошибка проверки канала:\n{exc}"


async def handle_join_request(req: dict):
    if (req.get("chat") or {}).get("id") != CHANNEL_ID:
        return
    user_id = int((req.get("from") or {})["id"])
    invite = (req.get("invite_link") or {}).get("invite_link")
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        link = conn.execute("SELECT * FROM invite_links WHERE invite_link=?", (invite,)).fetchone()
        valid = bool(
            user and (not user["blocked"] or user["whitelisted"]) and not user["approved_at"]
            and link and link["user_id"] == user_id and not link["used"]
            and not link["revoked"] and link["expires_at"] >= now()
        )
    if not valid:
        await telegram("declineChatJoinRequest", {"chat_id": CHANNEL_ID, "user_id": user_id})
        return
    await telegram("approveChatJoinRequest", {"chat_id": CHANNEL_ID, "user_id": user_id})
    try:
        await telegram("revokeChatInviteLink", {"chat_id": CHANNEL_ID, "invite_link": invite})
    except Exception:
        pass
    with db() as conn:
        conn.execute("UPDATE invite_links SET used=1, revoked=1 WHERE invite_link=?", (invite,))
        conn.execute("UPDATE users SET approved_at=?, active=1, left_at=NULL WHERE user_id=?", (now(), user_id))
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    user_event(user_id, "joined", user["campaign_key"])
    if setting("notifications", "1") == "1":
        await telegram("sendMessage", {"chat_id": ADMIN_USER_ID, "text": f"✅ Новое вступление\nКампания: {user['campaign_key']}\nID: {user_id}"})


async def handle_chat_member(update: dict):
    if (update.get("chat") or {}).get("id") != CHANNEL_ID:
        return
    new_member = update.get("new_chat_member") or {}
    user_id = int((new_member.get("user") or {})["id"])
    status = new_member.get("status")
    with db() as conn:
        user = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if not user:
            return
        if status in {"member", "administrator", "creator"}:
            conn.execute("UPDATE users SET active=1, left_at=NULL WHERE user_id=?", (user_id,))
            return
        if status == "kicked" and not user["whitelisted"]:
            conn.execute(
                "UPDATE users SET active=0,left_at=?,blocked=1,block_reason='removed_from_channel' WHERE user_id=?",
                (now(), user_id),
            )
            action = "removed_and_blocked"
        else:
            conn.execute("UPDATE users SET active=0,left_at=? WHERE user_id=?", (now(), user_id))
            action = "left"
    user_event(user_id, action, user["campaign_key"])
    if setting("notifications", "1") == "1":
        await telegram("sendMessage", {"chat_id": ADMIN_USER_ID, "text": f"🚪 Пользователь: {action}\nID: {user_id}"})


async def cleanup_loop():
    while True:
        try:
            with db() as conn:
                rows = conn.execute("SELECT invite_link FROM invite_links WHERE used=0 AND revoked=0 AND expires_at<?", (now(),)).fetchall()
            for row in rows:
                try:
                    await telegram("revokeChatInviteLink", {"chat_id": CHANNEL_ID, "invite_link": row["invite_link"]})
                except Exception:
                    pass
                with db() as conn:
                    conn.execute("UPDATE invite_links SET revoked=1 WHERE invite_link=?", (row["invite_link"],))
        except Exception:
            pass
        await asyncio.sleep(CLEANUP_INTERVAL)


async def channel_monitor_loop():
    last_ok = True
    while True:
        try:
            text = await channel_check_text()
            current_ok = text.startswith("✅")
            if not current_ok and last_ok:
                await telegram("sendMessage", {"chat_id": ADMIN_USER_ID, "text": text})
            last_ok = current_ok
        except Exception:
            pass
        await asyncio.sleep(MONITOR_INTERVAL)


async def backup_loop():
    while True:
        await asyncio.sleep(BACKUP_INTERVAL)
        try:
            if Path(DB_PATH).exists():
                backup_path = f"/tmp/declarant_auto_backup_{now()}.sqlite3"
                shutil.copy2(DB_PATH, backup_path)
                await send_document(ADMIN_USER_ID, backup_path, "Автоматический backup")
                os.remove(backup_path)
        except Exception:
            pass
