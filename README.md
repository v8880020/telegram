# Private Telegram Ad Bot

This bot only works through approved Telegram deep links from ads.

## Example ad link

`https://t.me/rbsalebot?start=fb_by_2026`

## Render settings

Create a **Web Service** from this repository.

- Runtime: Python
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`

Environment variables:

- `BOT_TOKEN` — token copied from BotFather
- `CHANNEL_ID` — `-1001322091992`
- `WEBHOOK_SECRET` — a long random string using letters, digits, `_` or `-`
- `ALLOWED_START_KEYS` — `fb_by_2026`
- `LINK_TTL_SECONDS` — `600`
- `DB_PATH` — `/tmp/bot.db`

For several campaigns:

`ALLOWED_START_KEYS=fb_by_2026,tiktok_by_2026,facebook_test2`

Then use:

- `https://t.me/rbsalebot?start=fb_by_2026`
- `https://t.me/rbsalebot?start=tiktok_by_2026`

The bot must be an administrator of the private channel with permission to invite users and approve join requests.

Important: Render's local filesystem can be reset after redeploys/restarts. For permanent history and reliable one-user-only tracking, connect a persistent disk or external database.
