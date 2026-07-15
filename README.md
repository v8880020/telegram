# Declarant — простой Telegram-бот

Без Mini App.

## Пользователь

Рекламная ссылка:

`https://t.me/rbsalebot?start=campaign_key`

После Start бот выдаёт персональную кнопку для вступления.

## Администратор

Откройте бота и отправьте `/start`.

Доступны кнопки:

- создать ссылку;
- статистика;
- кампании;
- чёрный список.

Создание кампании:

`/new fb_video_1 | facebook | Девушка у холодильника`

Блокировка:

`/block TELEGRAM_ID`

Разблокировка:

`/unblock TELEGRAM_ID`

## Render

Build Command:

`pip install -r requirements.txt`

Start Command:

`uvicorn main:app --host 0.0.0.0 --port $PORT`

Environment:

- BOT_TOKEN
- BOT_USERNAME=rbsalebot
- CHANNEL_ID=-1001322091992
- ADMIN_USER_ID=640314234
- WEBHOOK_SECRET
- DB_PATH=/tmp/declarant.sqlite3
- LINK_TTL_SECONDS=600
- CLEANUP_INTERVAL_SECONDS=300

Mini App и Menu Button в BotFather можно отключить.
