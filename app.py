import asyncio
import csv
import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_USERNAME = os.getenv("BOT_USERNAME", "rbsalebot").lstrip("@")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1001322091992"))
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "640314234"))
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
BASE_URL = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
LINK_TTL_SECONDS = int(os.getenv("LINK_TTL_SECONDS", "600"))
CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "300"))
DB_PATH = os.getenv("DB_PATH", "/tmp/declarant.sqlite3")

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
app = FastAPI()
background_tasks: list[asyncio.Task] = []


@contextmanager
def db():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def now_ts() -> int:
    return int(time.time())


def init_db() -> None:
    with db() as connection:
        connection.executescript(
            """
            PRAGMA journal_mode=WAL;

            CREATE TABLE IF NOT EXISTS campaigns (
                key TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                ad_label TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                language_code TEXT,
                campaign TEXT,
                first_seen_at INTEGER,
                approved_at INTEGER,
                left_at INTEGER,
                active INTEGER NOT NULL DEFAULT 0,
                blocked INTEGER NOT NULL DEFAULT 0,
                blocked_at INTEGER,
                block_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS bot_starts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                campaign TEXT,
                started_at INTEGER NOT NULL,
                username TEXT,
                first_name TEXT,
                language_code TEXT
            );

            CREATE TABLE IF NOT EXISTS access_links (
                invite_link TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                campaign TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                expires_at INTEGER NOT NULL,
                used INTEGER NOT NULL DEFAULT 0,
                revoked INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                notify_joins INTEGER NOT NULL DEFAULT 1,
                notify_leaves INTEGER NOT NULL DEFAULT 1
            );

            INSERT OR IGNORE INTO settings(id, notify_joins, notify_leaves)
            VALUES (1, 1, 1);
            """
        )


async def telegram(method: str, payload: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{API}/{method}", json=payload or {})
        data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error in {method}: {data}")
    return data["result"]


def validate_init_data(init_data: str, max_age_seconds: int = 3600) -> dict:
    if not init_data:
        raise HTTPException(status_code=401, detail="Telegram initData отсутствует")

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(status_code=401, detail="Telegram hash отсутствует")

    try:
        auth_date = int(pairs.get("auth_date", "0"))
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="Некорректный auth_date") from exc

    if abs(now_ts() - auth_date) > max_age_seconds:
        raise HTTPException(status_code=401, detail="Сессия Telegram устарела")

    data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
    secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated_hash = hmac.new(
        secret_key,
        data_check_string.encode(),
        hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(calculated_hash, received_hash):
        raise HTTPException(status_code=401, detail="Неверная подпись Telegram")

    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=401, detail="Некорректные данные пользователя") from exc

    return {
        "user": user,
        "start_param": pairs.get("start_param", ""),
    }


def require_admin(data: dict) -> None:
    if int(data["user"].get("id", 0)) != ADMIN_USER_ID:
        raise HTTPException(status_code=403, detail="Доступ только для администратора")


@app.on_event("startup")
async def startup() -> None:
    init_db()
    await telegram(
        "setWebhook",
        {
            "url": f"{BASE_URL}/telegram/{WEBHOOK_SECRET}",
            "secret_token": WEBHOOK_SECRET,
            "allowed_updates": ["chat_join_request", "chat_member"],
            "drop_pending_updates": True,
        },
    )
    background_tasks.append(asyncio.create_task(cleanup_loop()))


@app.on_event("shutdown")
async def shutdown() -> None:
    for task in background_tasks:
        task.cancel()


@app.get("/", response_class=HTMLResponse)
async def mini_app() -> HTMLResponse:
    html = Path("static/index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/api/access")
async def create_access(request: Request) -> dict:
    body = await request.json()
    data = validate_init_data(body.get("initData", ""))
    tg_user = data["user"]
    user_id = int(tg_user["id"])
    campaign_key = (data["start_param"] or body.get("campaign") or "").strip().lower()

    with db() as connection:
        user = connection.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        if user and user["blocked"]:
            return {"silent": True}

        if user and user["approved_at"]:
            return {"silent": True}

        campaign = connection.execute(
            "SELECT * FROM campaigns WHERE key = ? AND active = 1",
            (campaign_key,),
        ).fetchone()
        if not campaign:
            raise HTTPException(status_code=403, detail="Рекламная ссылка недействительна")

        connection.execute(
            """
            INSERT INTO bot_starts(
                user_id, campaign, started_at, username, first_name, language_code
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                campaign_key,
                now_ts(),
                tg_user.get("username"),
                tg_user.get("first_name"),
                tg_user.get("language_code"),
            ),
        )

        connection.execute(
            """
            INSERT INTO users(
                user_id, username, first_name, language_code,
                campaign, first_seen_at, active, blocked
            ) VALUES (?, ?, ?, ?, ?, ?, 0, 0)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username,
                first_name = excluded.first_name,
                language_code = excluded.language_code
            """,
            (
                user_id,
                tg_user.get("username"),
                tg_user.get("first_name"),
                tg_user.get("language_code"),
                campaign_key,
                now_ts(),
            ),
        )

    expires_at = now_ts() + LINK_TTL_SECONDS
    invite = await telegram(
        "createChatInviteLink",
        {
            "chat_id": CHANNEL_ID,
            "name": f"{campaign_key}_{user_id}_{now_ts()}",
            "expire_date": expires_at,
            "creates_join_request": True,
        },
    )

    with db() as connection:
        connection.execute(
            """
            INSERT OR REPLACE INTO access_links(
                invite_link, user_id, campaign, created_at,
                expires_at, used, revoked
            ) VALUES (?, ?, ?, ?, ?, 0, 0)
            """,
            (
                invite["invite_link"],
                user_id,
                campaign_key,
                now_ts(),
                expires_at,
            ),
        )

    return {"invite_link": invite["invite_link"], "expires_in": LINK_TTL_SECONDS}


@app.post("/api/admin/campaigns")
async def create_campaign(request: Request) -> dict:
    body = await request.json()
    data = validate_init_data(body.get("initData", ""))
    require_admin(data)

    key = body.get("key", "").strip().lower()
    source = body.get("source", "").strip().lower()
    ad_label = body.get("ad_label", "").strip()

    if not re.fullmatch(r"[a-z0-9_]{2,40}", key):
        raise HTTPException(status_code=400, detail="Ключ: латинские буквы, цифры и _")
    if not re.fullmatch(r"[a-z0-9_]{2,30}", source):
        raise HTTPException(status_code=400, detail="Источник: латинские буквы, цифры и _")
    if not ad_label or len(ad_label) > 80:
        raise HTTPException(status_code=400, detail="Укажи название объявления до 80 символов")

    with db() as connection:
        connection.execute(
            """
            INSERT INTO campaigns(key, source, ad_label, created_at, active)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(key) DO UPDATE SET
                source = excluded.source,
                ad_label = excluded.ad_label,
                active = 1
            """,
            (key, source, ad_label, now_ts()),
        )

    return {
        "key": key,
        "url": f"https://t.me/{BOT_USERNAME}?startapp={key}",
    }


@app.get("/api/admin/campaigns")
async def list_campaigns(x_telegram_init_data: str = Header(default="")) -> dict:
    data = validate_init_data(x_telegram_init_data)
    require_admin(data)

    with db() as connection:
        rows = connection.execute(
            "SELECT * FROM campaigns ORDER BY created_at DESC"
        ).fetchall()

    return {
        "campaigns": [
            {
                "key": row["key"],
                "source": row["source"],
                "ad_label": row["ad_label"],
                "created_at": row["created_at"],
                "active": bool(row["active"]),
                "url": f"https://t.me/{BOT_USERNAME}?startapp={row['key']}",
            }
            for row in rows
        ]
    }


@app.post("/api/admin/campaigns/{key}/toggle")
async def toggle_campaign(key: str, request: Request) -> dict:
    body = await request.json()
    data = validate_init_data(body.get("initData", ""))
    require_admin(data)

    with db() as connection:
        row = connection.execute(
            "SELECT active FROM campaigns WHERE key = ?",
            (key,),
        ).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Кампания не найдена")

        new_value = 0 if row["active"] else 1
        connection.execute(
            "UPDATE campaigns SET active = ? WHERE key = ?",
            (new_value, key),
        )

    return {"active": bool(new_value)}


def build_stats(period_seconds: int | None) -> list[dict]:
    since = now_ts() - period_seconds if period_seconds else 0

    with db() as connection:
        campaigns = connection.execute(
            "SELECT * FROM campaigns ORDER BY created_at DESC"
        ).fetchall()

        result = []
        for campaign in campaigns:
            key = campaign["key"]

            starts = connection.execute(
                """
                SELECT COUNT(*) AS value FROM bot_starts
                WHERE campaign = ? AND started_at >= ?
                """,
                (key, since),
            ).fetchone()["value"]

            unique_users = connection.execute(
                """
                SELECT COUNT(DISTINCT user_id) AS value FROM bot_starts
                WHERE campaign = ? AND started_at >= ?
                """,
                (key, since),
            ).fetchone()["value"]

            joins = connection.execute(
                """
                SELECT COUNT(*) AS value FROM users
                WHERE campaign = ? AND approved_at IS NOT NULL AND approved_at >= ?
                """,
                (key, since),
            ).fetchone()["value"]

            active_members = connection.execute(
                """
                SELECT COUNT(*) AS value FROM users
                WHERE campaign = ? AND active = 1 AND blocked = 0
                """,
                (key,),
            ).fetchone()["value"]

            left = connection.execute(
                """
                SELECT COUNT(*) AS value FROM users
                WHERE campaign = ? AND left_at IS NOT NULL AND left_at >= ?
                """,
                (key, since),
            ).fetchone()["value"]

            blocked = connection.execute(
                """
                SELECT COUNT(*) AS value FROM users
                WHERE campaign = ? AND blocked = 1
                """,
                (key,),
            ).fetchone()["value"]

            conversion = round((joins / unique_users) * 100, 1) if unique_users else 0

            result.append(
                {
                    "key": key,
                    "source": campaign["source"],
                    "ad_label": campaign["ad_label"],
                    "created_at": campaign["created_at"],
                    "starts": starts,
                    "unique_users": unique_users,
                    "joins": joins,
                    "active_members": active_members,
                    "left": left,
                    "blocked": blocked,
                    "conversion": conversion,
                }
            )

    return result


@app.get("/api/admin/stats")
async def stats(period: str = "24h", x_telegram_init_data: str = Header(default="")) -> dict:
    data = validate_init_data(x_telegram_init_data)
    require_admin(data)

    period_seconds = {
        "1h": 3600,
        "24h": 86400,
        "7d": 604800,
        "all": None,
    }.get(period, 86400)

    rows = build_stats(period_seconds)
    top = sorted(rows, key=lambda item: (item["conversion"], item["joins"]), reverse=True)[:5]
    return {"campaigns": rows, "top": top}


@app.get("/api/admin/users")
async def users(q: str = "", x_telegram_init_data: str = Header(default="")) -> dict:
    data = validate_init_data(x_telegram_init_data)
    require_admin(data)

    q = q.strip().lstrip("@")
    with db() as connection:
        if not q:
            rows = connection.execute(
                "SELECT * FROM users ORDER BY first_seen_at DESC LIMIT 100"
            ).fetchall()
        elif q.isdigit():
            rows = connection.execute(
                "SELECT * FROM users WHERE user_id = ?",
                (int(q),),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT * FROM users
                WHERE username LIKE ?
                ORDER BY first_seen_at DESC
                LIMIT 100
                """,
                (f"%{q}%",),
            ).fetchall()

    return {
        "users": [
            {
                "user_id": row["user_id"],
                "username": row["username"],
                "first_name": row["first_name"],
                "campaign": row["campaign"],
                "first_seen_at": row["first_seen_at"],
                "approved_at": row["approved_at"],
                "active": bool(row["active"]),
                "blocked": bool(row["blocked"]),
                "block_reason": row["block_reason"],
            }
            for row in rows
        ]
    }


@app.post("/api/admin/users/{user_id}/block")
async def block_user(user_id: int, request: Request) -> dict:
    body = await request.json()
    data = validate_init_data(body.get("initData", ""))
    require_admin(data)

    with db() as connection:
        exists = connection.execute(
            "SELECT user_id FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not exists:
            raise HTTPException(status_code=404, detail="Пользователь не найден")

        connection.execute(
            """
            UPDATE users SET
                blocked = 1,
                blocked_at = ?,
                block_reason = ?,
                active = 0
            WHERE user_id = ?
            """,
            (now_ts(), body.get("reason", "manual"), user_id),
        )

    try:
        await telegram("banChatMember", {"chat_id": CHANNEL_ID, "user_id": user_id})
    except Exception:
        pass

    return {"ok": True}


@app.post("/api/admin/users/{user_id}/unblock")
async def unblock_user(user_id: int, request: Request) -> dict:
    body = await request.json()
    data = validate_init_data(body.get("initData", ""))
    require_admin(data)

    with db() as connection:
        connection.execute(
            """
            UPDATE users SET
                blocked = 0,
                blocked_at = NULL,
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

    return {"ok": True}


@app.get("/api/admin/settings")
async def get_settings(x_telegram_init_data: str = Header(default="")) -> dict:
    data = validate_init_data(x_telegram_init_data)
    require_admin(data)

    with db() as connection:
        row = connection.execute("SELECT * FROM settings WHERE id = 1").fetchone()

    return {
        "notify_joins": bool(row["notify_joins"]),
        "notify_leaves": bool(row["notify_leaves"]),
    }


@app.post("/api/admin/settings")
async def save_settings(request: Request) -> dict:
    body = await request.json()
    data = validate_init_data(body.get("initData", ""))
    require_admin(data)

    with db() as connection:
        connection.execute(
            """
            UPDATE settings SET notify_joins = ?, notify_leaves = ?
            WHERE id = 1
            """,
            (
                1 if body.get("notify_joins") else 0,
                1 if body.get("notify_leaves") else 0,
            ),
        )

    return {"ok": True}


@app.get("/api/admin/export.csv")
async def export_csv(x_telegram_init_data: str = Header(default="")) -> StreamingResponse:
    data = validate_init_data(x_telegram_init_data)
    require_admin(data)

    with db() as connection:
        rows = connection.execute(
            "SELECT * FROM users ORDER BY first_seen_at DESC"
        ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "telegram_id",
            "username",
            "first_name",
            "language",
            "campaign",
            "first_seen",
            "joined",
            "left",
            "active",
            "blocked",
            "block_reason",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row["user_id"],
                row["username"],
                row["first_name"],
                row["language_code"],
                row["campaign"],
                row["first_seen_at"],
                row["approved_at"],
                row["left_at"],
                row["active"],
                row["blocked"],
                row["block_reason"],
            ]
        )

    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=telegram_analytics.csv"},
    )


@app.post("/telegram/{secret}")
async def webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> JSONResponse:
    if secret != WEBHOOK_SECRET or x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Forbidden")

    update = await request.json()

    if "chat_join_request" in update:
        await handle_join_request(update["chat_join_request"])
    elif "chat_member" in update:
        await handle_chat_member(update["chat_member"])

    return JSONResponse({"ok": True})


async def handle_join_request(join_request: dict) -> None:
    if (join_request.get("chat") or {}).get("id") != CHANNEL_ID:
        return

    tg_user = join_request.get("from") or {}
    user_id = int(tg_user["id"])
    invite_link = (join_request.get("invite_link") or {}).get("invite_link")
    current_time = now_ts()

    with db() as connection:
        user = connection.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

        link = connection.execute(
            "SELECT * FROM access_links WHERE invite_link = ?",
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
            and link["expires_at"] >= current_time
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
        connection.execute(
            """
            UPDATE access_links SET used = 1, revoked = 1
            WHERE invite_link = ?
            """,
            (invite_link,),
        )
        connection.execute(
            """
            UPDATE users SET
                approved_at = ?,
                active = 1,
                left_at = NULL
            WHERE user_id = ?
            """,
            (current_time, user_id),
        )
        settings = connection.execute(
            "SELECT notify_joins FROM settings WHERE id = 1"
        ).fetchone()
        user = connection.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    if settings["notify_joins"]:
        username = f"@{user['username']}" if user["username"] else "без username"
        await telegram(
            "sendMessage",
            {
                "chat_id": ADMIN_USER_ID,
                "text": (
                    f"✅ Новое вступление\n\n"
                    f"Кампания: {user['campaign']}\n"
                    f"Пользователь: {user['first_name'] or 'Без имени'} ({username})\n"
                    f"Telegram ID: {user_id}"
                ),
            },
        )


async def handle_chat_member(member_update: dict) -> None:
    if (member_update.get("chat") or {}).get("id") != CHANNEL_ID:
        return

    new_member = member_update.get("new_chat_member") or {}
    tg_user = new_member.get("user") or {}
    if not tg_user.get("id"):
        return

    user_id = int(tg_user["id"])
    status = new_member.get("status")
    current_time = now_ts()

    with db() as connection:
        user = connection.execute(
            "SELECT * FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        if not user:
            return

        if status in {"member", "administrator", "creator"}:
            connection.execute(
                "UPDATE users SET active = 1, left_at = NULL WHERE user_id = ?",
                (user_id,),
            )
            action = None

        elif status == "kicked":
            connection.execute(
                """
                UPDATE users SET
                    active = 0,
                    left_at = ?,
                    blocked = 1,
                    blocked_at = ?,
                    block_reason = 'removed_from_channel'
                WHERE user_id = ?
                """,
                (current_time, current_time, user_id),
            )
            action = "удалён из канала"

        else:
            connection.execute(
                """
                UPDATE users SET active = 0, left_at = ?
                WHERE user_id = ?
                """,
                (current_time, user_id),
            )
            action = "вышел из канала"

        settings = connection.execute(
            "SELECT notify_leaves FROM settings WHERE id = 1"
        ).fetchone()

    if action and settings["notify_leaves"]:
        await telegram(
            "sendMessage",
            {
                "chat_id": ADMIN_USER_ID,
                "text": (
                    f"🚪 Пользователь {action}\n"
                    f"Кампания: {user['campaign']}\n"
                    f"Telegram ID: {user_id}"
                ),
            },
        )


async def cleanup_loop() -> None:
    while True:
        try:
            current_time = now_ts()
            with db() as connection:
                links = connection.execute(
                    """
                    SELECT invite_link FROM access_links
                    WHERE used = 0 AND revoked = 0 AND expires_at < ?
                    """,
                    (current_time,),
                ).fetchall()

            for row in links:
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

                with db() as connection:
                    connection.execute(
                        "UPDATE access_links SET revoked = 1 WHERE invite_link = ?",
                        (row["invite_link"],),
                    )
        except Exception:
            pass

        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
