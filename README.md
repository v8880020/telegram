# Declarant Mini App — SQLite Ready

Готовый проект без PostgreSQL.

## Функции

- Telegram Mini App для пользователей;
- админ-панель внутри Mini App;
- создание рекламных кампаний и ссылок;
- метка источника и конкретного объявления;
- статистика за 1 час, 24 часа, 7 дней и всё время;
- топ кампаний;
- уведомления о вступлениях, выходах и удалениях;
- чёрный список;
- удалённый администратором пользователь автоматически блокируется;
- заблокированному или ранее вступавшему пользователю новые ссылки не выдаются;
- защита от пересылки приглашения;
- один Telegram ID — один доступ;
- автоматический отзыв просроченных ссылок;
- CSV-экспорт;
- геолокация отсутствует;
- команды отсутствуют.

## Render Start Command

uvicorn app:app --host 0.0.0.0 --port $PORT

## Environment Variables

BOT_TOKEN
BOT_USERNAME=rbsalebot
CHANNEL_ID=-1001322091992
ADMIN_USER_ID=640314234
WEBHOOK_SECRET=<длинная случайная строка>
LINK_TTL_SECONDS=600
CLEANUP_INTERVAL_SECONDS=300
DB_PATH=/tmp/declarant.sqlite3

## BotFather

Настрой Main Mini App и Menu Button на URL Render:

https://ИМЯ-СЕРВИСА.onrender.com

Рекламная ссылка создаётся в панели:

https://t.me/rbsalebot?startapp=campaign_key

## Ограничение SQLite на бесплатном Render

Если DB_PATH находится в /tmp, база может исчезнуть после нового деплоя, перезапуска или переноса сервиса.

Для сохранения базы без PostgreSQL можно позднее подключить Render Persistent Disk и изменить:

DB_PATH=/var/data/declarant.sqlite3
