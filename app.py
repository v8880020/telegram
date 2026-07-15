import asyncio
import csv
import hashlib
import hmac
import io
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from sqlalchemy import BigInteger, Boolean, Column, DateTime, Float, Integer, String, Text, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import declarative_base

BOT_TOKEN = os.environ["BOT_TOKEN"]
BOT_USERNAME = os.getenv("BOT_USERNAME", "rbsalebot").lstrip("@")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "-1001322091992"))
ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", "640314234"))
WEBHOOK_SECRET = os.environ["WEBHOOK_SECRET"]
BASE_URL = os.environ["RENDER_EXTERNAL_URL"].rstrip("/")
DATABASE_URL = os.environ["DATABASE_URL"].replace("postgres://", "postgresql+asyncpg://", 1).replace(
    "postgresql://", "postgresql+asyncpg://", 1
)
LINK_TTL_SECONDS = int(os.getenv("LINK_TTL_SECONDS", "600"))
CLEANUP_INTERVAL_SECONDS = int(os.getenv("CLEANUP_INTERVAL_SECONDS", "600"))

API = f"https://api.telegram.org/bot{BOT_TOKEN}"
Base = declarative_base()
engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
app = FastAPI()
tasks: list[asyncio.Task] = []


class Campaign(Base):
    __tablename__ = "campaigns"
    key = Column(String(64), primary_key=True)
    source = Column(String(32), nullable=False)
    ad_label = Column(String(80), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    created_by = Column(BigInteger, nullable=False)
    active = Column(Boolean, nullable=False, default=True)


class User(Base):
    __tablename__ = "users"
    user_id = Column(BigInteger, primary_key=True)
    username = Column(String(128))
    first_name = Column(String(128))
    language_code = Column(String(16))
    campaign = Column(String(64))
    first_seen_at = Column(DateTime(timezone=True))
    approved_at = Column(DateTime(timezone=True))
    left_at = Column(DateTime(timezone=True))
    active = Column(Boolean, nullable=False, default=False)
    blocked = Column(Boolean, nullable=False, default=False)
    blocked_at = Column(DateTime(timezone=True))
    block_reason = Column(String(64))


class BotStart(Base):
    __tablename__ = "bot_starts"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False)
    campaign = Column(String(64))
    started_at = Column(DateTime(timezone=True), nullable=False)
    username = Column(String(128))
    first_name = Column(String(128))
    language_code = Column(String(16))


class AccessLink(Base):
    __tablename__ = "access_links"
    invite_link = Column(Text, primary_key=True)
    user_id = Column(BigInteger, nullable=False)
    campaign = Column(String(64), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    used = Column(Boolean, nullable=False, default=False)
    revoked = Column(Boolean, nullable=False, default=False)


class Settings(Base):
    __tablename__ = "settings"
    id = Column(Integer, primary_key=True, default=1)
    notify_joins = Column(Boolean, nullable=False, default=True)
    notify_leaves = Column(Boolean, nullable=False, default=True)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def telegram(method: str, payload: dict | None = None) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"{API}/{method}", json=payload or {})
        data = response.json()
    if not data.get("ok"):
        raise RuntimeError(f"Telegram API error in {method}: {data}")
    return data["result"]


def validate_init_data(init_data: str, max_age: int = 3600) -> dict:
    if not init_data:
        raise HTTPException(401, "Telegram initData отсутствует")
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise HTTPException(401, "Telegram hash отсутствует")
    auth_date = int(pairs.get("auth_date", "0"))
    if abs(int(time.time()) - auth_date) > max_age:
        raise HTTPException(401, "Сессия Telegram устарела")
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    calculated = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(calculated, received_hash):
        raise HTTPException(401, "Неверная подпись Telegram")
    return {
        "user": json.loads(pairs.get("user", "{}")),
        "start_param": pairs.get("start_param", ""),
    }


def require_admin(data: dict) -> None:
    if int(data["user"].get("id", 0)) != ADMIN_USER_ID:
        raise HTTPException(403, "Только для администратора")


@app.on_event("startup")
async def startup() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with Session() as session:
        if not await session.get(Settings, 1):
            session.add(Settings(id=1, notify_joins=True, notify_leaves=True))
            await session.commit()

    await telegram("setWebhook", {
        "url": f"{BASE_URL}/telegram/{WEBHOOK_SECRET}",
        "secret_token": WEBHOOK_SECRET,
        "allowed_updates": ["chat_join_request", "chat_member"],
        "drop_pending_updates": True,
    })
    tasks.append(asyncio.create_task(cleanup_loop()))


@app.on_event("shutdown")
async def shutdown() -> None:
    for task in tasks:
        task.cancel()
    await engine.dispose()


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    with open("static/index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@app.post("/api/access")
async def access(request: Request) -> dict:
    body = await request.json()
    data = validate_init_data(body.get("initData", ""))
    tg_user = data["user"]
    user_id = int(tg_user["id"])
    campaign_key = (data["start_param"] or body.get("campaign") or "").strip().lower()

    async with Session() as session:
        db_user = await session.get(User, user_id)

        # Удалённые/заблокированные пользователи не получают никакого доступа.
        if db_user and db_user.blocked:
            return {"blocked": True}

        # Один Telegram-аккаунт — один доступ навсегда.
        if db_user and db_user.approved_at:
            return {"already_used": True}

        campaign = await session.get(Campaign, campaign_key)
        if not campaign or not campaign.active:
            raise HTTPException(403, "Рекламная ссылка недействительна")

        session.add(BotStart(
            user_id=user_id,
            campaign=campaign_key,
            started_at=utcnow(),
            username=tg_user.get("username"),
            first_name=tg_user.get("first_name"),
            language_code=tg_user.get("language_code"),
        ))

        if not db_user:
            db_user = User(
                user_id=user_id,
                campaign=campaign_key,
                first_seen_at=utcnow(),
                active=False,
                blocked=False,
            )
            session.add(db_user)

        db_user.username = tg_user.get("username")
        db_user.first_name = tg_user.get("first_name")
        db_user.language_code = tg_user.get("language_code")
        if not db_user.campaign:
            db_user.campaign = campaign_key

        expires_at = utcnow() + timedelta(seconds=LINK_TTL_SECONDS)
        invite = await telegram("createChatInviteLink", {
            "chat_id": CHANNEL_ID,
            "name": f"{campaign_key}_{user_id}_{int(time.time())}",
            "expire_date": int(expires_at.timestamp()),
            "creates_join_request": True,
        })

        session.add(AccessLink(
            invite_link=invite["invite_link"],
            user_id=user_id,
            campaign=campaign_key,
            created_at=utcnow(),
            expires_at=expires_at,
        ))
        await session.commit()

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
        raise HTTPException(400, "Ключ: только латиница, цифры и _")
    if not re.fullmatch(r"[a-z0-9_]{2,30}", source):
        raise HTTPException(400, "Источник: только латиница, цифры и _")
    if not ad_label or len(ad_label) > 80:
        raise HTTPException(400, "Укажи название объявления")

    async with Session() as session:
        campaign = await session.get(Campaign, key)
        if campaign:
            campaign.active = True
            campaign.source = source
            campaign.ad_label = ad_label
        else:
            session.add(Campaign(
                key=key,
                source=source,
                ad_label=ad_label,
                created_at=utcnow(),
                created_by=ADMIN_USER_ID,
                active=True,
            ))
        await session.commit()

    return {"url": f"https://t.me/{BOT_USERNAME}?startapp={key}", "key": key}


@app.get("/api/admin/campaigns")
async def campaigns(x_telegram_init_data: str = Header(default="")) -> dict:
    data = validate_init_data(x_telegram_init_data)
    require_admin(data)
    async with Session() as session:
        rows = (await session.execute(
            select(Campaign).order_by(Campaign.created_at.desc())
        )).scalars().all()
    return {"campaigns": [{
        "key": c.key,
        "source": c.source,
        "ad_label": c.ad_label,
        "created_at": c.created_at.isoformat(),
        "active": c.active,
        "url": f"https://t.me/{BOT_USERNAME}?startapp={c.key}",
    } for c in rows]}


@app.post("/api/admin/campaigns/{key}/toggle")
async def toggle_campaign(key: str, request: Request) -> dict:
    body = await request.json()
    data = validate_init_data(body.get("initData", ""))
    require_admin(data)
    async with Session() as session:
        c = await session.get(Campaign, key)
        if not c:
            raise HTTPException(404, "Кампания не найдена")
        c.active = not c.active
        await session.commit()
        return {"active": c.active}


async def stats_data(period_seconds: int | None) -> list[dict]:
    since = utcnow() - timedelta(seconds=period_seconds) if period_seconds else datetime(1970,1,1,tzinfo=timezone.utc)
    async with Session() as session:
        campaigns = (await session.execute(
            select(Campaign).order_by(Campaign.created_at.desc())
        )).scalars().all()

        result = []
        for c in campaigns:
            starts = await session.scalar(select(func.count(BotStart.id)).where(
                BotStart.campaign == c.key, BotStart.started_at >= since
            )) or 0
            unique = await session.scalar(select(func.count(func.distinct(BotStart.user_id))).where(
                BotStart.campaign == c.key, BotStart.started_at >= since
            )) or 0
            joins = await session.scalar(select(func.count(User.user_id)).where(
                User.campaign == c.key, User.approved_at.is_not(None), User.approved_at >= since
            )) or 0
            active = await session.scalar(select(func.count(User.user_id)).where(
                User.campaign == c.key, User.active.is_(True), User.blocked.is_(False)
            )) or 0
            left = await session.scalar(select(func.count(User.user_id)).where(
                User.campaign == c.key, User.left_at.is_not(None), User.left_at >= since
            )) or 0
            blocked = await session.scalar(select(func.count(User.user_id)).where(
                User.campaign == c.key, User.blocked.is_(True)
            )) or 0

            result.append({
                "key": c.key,
                "source": c.source,
                "ad_label": c.ad_label,
                "created_at": c.created_at.isoformat(),
                "starts": int(starts),
                "unique_users": int(unique),
                "joins": int(joins),
                "active_members": int(active),
                "left": int(left),
                "blocked": int(blocked),
                "conversion": round((joins / unique * 100), 1) if unique else 0,
            })
        return result


@app.get("/api/admin/stats")
async def stats(period: str = "24h", x_telegram_init_data: str = Header(default="")) -> dict:
    data = validate_init_data(x_telegram_init_data)
    require_admin(data)
    seconds = {"1h":3600, "24h":86400, "7d":604800, "all":None}.get(period, 86400)
    rows = await stats_data(seconds)
    top = sorted(rows, key=lambda r: (r["conversion"], r["joins"]), reverse=True)
    return {"campaigns": rows, "top": top[:5]}


@app.get("/api/admin/users")
async def users(q: str = "", x_telegram_init_data: str = Header(default="")) -> dict:
    data = validate_init_data(x_telegram_init_data)
    require_admin(data)

    async with Session() as session:
        stmt = select(User).order_by(User.first_seen_at.desc()).limit(100)
        if q:
            qclean = q.strip().lstrip("@")
            if qclean.isdigit():
                stmt = select(User).where(User.user_id == int(qclean)).limit(100)
            else:
                stmt = select(User).where(User.username.ilike(f"%{qclean}%")).limit(100)
        rows = (await session.execute(stmt)).scalars().all()

    return {"users": [{
        "user_id": u.user_id,
        "username": u.username,
        "first_name": u.first_name,
        "campaign": u.campaign,
        "first_seen_at": u.first_seen_at.isoformat() if u.first_seen_at else None,
        "approved_at": u.approved_at.isoformat() if u.approved_at else None,
        "active": u.active,
        "blocked": u.blocked,
        "block_reason": u.block_reason,
    } for u in rows]}


@app.post("/api/admin/users/{user_id}/block")
async def block_user(user_id: int, request: Request) -> dict:
    body = await request.json()
    data = validate_init_data(body.get("initData", ""))
    require_admin(data)

    async with Session() as session:
        user = await session.get(User, user_id)
        if not user:
            raise HTTPException(404, "Пользователь не найден")
        user.blocked = True
        user.blocked_at = utcnow()
        user.block_reason = body.get("reason", "manual")
        user.active = False
        await session.commit()

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

    async with Session() as session:
        user = await session.get(User, user_id)
        if not user:
            raise HTTPException(404, "Пользователь не найден")
        user.blocked = False
        user.block_reason = None
        await session.commit()

    try:
        await telegram("unbanChatMember", {"chat_id": CHANNEL_ID, "user_id": user_id, "only_if_banned": True})
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/admin/settings")
async def get_settings(x_telegram_init_data: str = Header(default="")) -> dict:
    data = validate_init_data(x_telegram_init_data)
    require_admin(data)
    async with Session() as session:
        s = await session.get(Settings, 1)
    return {"notify_joins": s.notify_joins, "notify_leaves": s.notify_leaves}


@app.post("/api/admin/settings")
async def save_settings(request: Request) -> dict:
    body = await request.json()
    data = validate_init_data(body.get("initData", ""))
    require_admin(data)
    async with Session() as session:
        s = await session.get(Settings, 1)
        s.notify_joins = bool(body.get("notify_joins"))
        s.notify_leaves = bool(body.get("notify_leaves"))
        await session.commit()
    return {"ok": True}


@app.get("/api/admin/export.csv")
async def export_csv(x_telegram_init_data: str = Header(default="")) -> StreamingResponse:
    data = validate_init_data(x_telegram_init_data)
    require_admin(data)
    async with Session() as session:
        rows = (await session.execute(select(User).order_by(User.first_seen_at.desc()))).scalars().all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["telegram_id","username","first_name","campaign","first_seen","joined","left","active","blocked","block_reason"])
    for u in rows:
        writer.writerow([u.user_id,u.username,u.first_name,u.campaign,u.first_seen_at,u.approved_at,u.left_at,u.active,u.blocked,u.block_reason])

    return StreamingResponse(
        iter([output.getvalue().encode("utf-8-sig")]),
        media_type="text/csv",
        headers={"Content-Disposition":"attachment; filename=telegram_analytics.csv"},
    )


@app.post("/telegram/{secret}")
async def webhook(
    secret: str,
    request: Request,
    x_telegram_bot_api_secret_token: str | None = Header(default=None),
) -> JSONResponse:
    if secret != WEBHOOK_SECRET or x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(403, "Forbidden")
    update = await request.json()
    if "chat_join_request" in update:
        await handle_join_request(update["chat_join_request"])
    elif "chat_member" in update:
        await handle_chat_member(update["chat_member"])
    return JSONResponse({"ok": True})


async def handle_join_request(req: dict) -> None:
    if (req.get("chat") or {}).get("id") != CHANNEL_ID:
        return
    user = req.get("from") or {}
    user_id = int(user["id"])
    invite = (req.get("invite_link") or {}).get("invite_link")
    now = utcnow()

    async with Session() as session:
        db_user = await session.get(User, user_id)
        link = await session.get(AccessLink, invite)

        valid = bool(
            db_user and not db_user.blocked and not db_user.approved_at
            and link and link.user_id == user_id
            and not link.used and not link.revoked and link.expires_at >= now
        )

        if not valid:
            await telegram("declineChatJoinRequest", {"chat_id": CHANNEL_ID, "user_id": user_id})
            return

        await telegram("approveChatJoinRequest", {"chat_id": CHANNEL_ID, "user_id": user_id})
        try:
            await telegram("revokeChatInviteLink", {"chat_id": CHANNEL_ID, "invite_link": invite})
        except Exception:
            pass

        link.used = True
        link.revoked = True
        db_user.approved_at = now
        db_user.active = True
        db_user.left_at = None

        settings = await session.get(Settings, 1)
        await session.commit()

        if settings.notify_joins:
            username = f"@{db_user.username}" if db_user.username else "без username"
            await telegram("sendMessage", {
                "chat_id": ADMIN_USER_ID,
                "text": (
                    f"✅ Новое вступление\n\n"
                    f"Кампания: {db_user.campaign}\n"
                    f"Пользователь: {db_user.first_name or 'Без имени'} ({username})\n"
                    f"ID: {db_user.user_id}"
                )
            })


async def handle_chat_member(update: dict) -> None:
    if (update.get("chat") or {}).get("id") != CHANNEL_ID:
        return

    new_member = update.get("new_chat_member") or {}
    old_member = update.get("old_chat_member") or {}
    tg_user = new_member.get("user") or {}
    if not tg_user.get("id"):
        return

    user_id = int(tg_user["id"])
    new_status = new_member.get("status")
    old_status = old_member.get("status")
    now = utcnow()

    async with Session() as session:
        user = await session.get(User, user_id)
        if not user:
            return

        settings = await session.get(Settings, 1)

        if new_status in {"member","administrator","creator"}:
            user.active = True
            user.left_at = None

        elif new_status == "kicked":
            # Удалён администратором: постоянный чёрный список.
            user.active = False
            user.left_at = now
            user.blocked = True
            user.blocked_at = now
            user.block_reason = "removed_from_channel"

        elif new_status == "left":
            # Сам вышел: повторный доступ всё равно запрещён правилом one-access.
            user.active = False
            user.left_at = now

        await session.commit()

        if settings.notify_leaves and new_status in {"left","kicked"}:
            action = "удалён из канала" if new_status == "kicked" else "вышел из канала"
            await telegram("sendMessage", {
                "chat_id": ADMIN_USER_ID,
                "text": f"🚪 Пользователь {action}\nКампания: {user.campaign}\nID: {user.user_id}"
            })


async def cleanup_loop() -> None:
    while True:
        try:
            async with Session() as session:
                links = (await session.execute(
                    select(AccessLink).where(
                        AccessLink.used.is_(False),
                        AccessLink.revoked.is_(False),
                        AccessLink.expires_at < utcnow(),
                    )
                )).scalars().all()
                for link in links:
                    try:
                        await telegram("revokeChatInviteLink", {
                            "chat_id": CHANNEL_ID,
                            "invite_link": link.invite_link,
                        })
                    except Exception:
                        pass
                    link.revoked = True
                await session.commit()
        except Exception:
            pass
        await asyncio.sleep(CLEANUP_INTERVAL_SECONDS)
