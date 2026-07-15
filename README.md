# Private Telegram Ad Bot — Full Version

Features:

- Access only from approved advertising deep links.
- One Telegram account = one access.
- Join-request protection against forwarded invite links.
- Automatic approval only for the intended Telegram user.
- Bot deletes its button message and the user's `/start` after successful joining.
- No “Access granted” message.
- Automatic revocation of expired invite links.
- Campaign statistics for 1 hour, 24 hours, and all time.
- Tracks successful joins, currently active tracked members, and departures.

## Advertising links

Set:

`ALLOWED_START_KEYS=fb_1,fb_2,tiktok_1`

Use:

- `https://t.me/rbsalebot?start=fb_1`
- `https://t.me/rbsalebot?start=fb_2`
- `https://t.me/rbsalebot?start=tiktok_1`

## Statistics

Send `/stats` from the Telegram account whose ID is set in `ADMIN_USER_ID`.

Buttons allow selecting:

- 1 hour
- 24 hours
- All time

## Render environment variables

- `BOT_TOKEN`
- `CHANNEL_ID=-1001322091992`
- `ADMIN_USER_ID=640314234`
- `WEBHOOK_SECRET`
- `ALLOWED_START_KEYS=fb_1,fb_2,tiktok_1`
- `LINK_TTL_SECONDS=600`
- `CLEANUP_INTERVAL_SECONDS=600`
- `DB_PATH=/tmp/bot.db`

Build:

`pip install -r requirements.txt`

Start:

`uvicorn main:app --host 0.0.0.0 --port $PORT`

## Important

The bot must remain an administrator of the channel and must receive `chat_member` updates.

Render's `/tmp` storage is temporary. A restart or redeploy can erase statistics. For reliable long-term data, use PostgreSQL or a persistent disk.
