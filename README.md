# Declarant Advanced Bot

Обычный Telegram-бот без Mini App и PostgreSQL.

Реализовано:

- кнопочная админ-панель;
- автоматические Кампания 1, 2, 3...;
- пауза, возобновление, архив и дублирование кампаний;
- лимит вступлений;
- пользователи кампании;
- поиск пользователя и история;
- статистика и рейтинг качества;
- чёрный и белый список;
- настраиваемое приветствие, текст кнопки и TTL;
- глобальная пауза и обслуживание;
- массовый отзыв приглашений;
- антиспам;
- мониторинг прав бота;
- ручные и автоматические backup;
- журнал действий;
- автоматический запрет повторного доступа.

Render:

Build:
pip install -r requirements.txt

Start:
uvicorn main:app --host 0.0.0.0 --port $PORT

Environment:

BOT_TOKEN
BOT_USERNAME=rbsalebot
CHANNEL_ID=-1001322091992
ADMIN_USER_ID=640314234
WEBHOOK_SECRET
DB_PATH=/tmp/declarant.sqlite3
LINK_TTL_SECONDS=600
CLEANUP_INTERVAL_SECONDS=300
MONITOR_INTERVAL_SECONDS=300
BACKUP_INTERVAL_SECONDS=86400

SQLite в /tmp может удалиться при перезапуске Render. Автоматический backup отправляется администратору в Telegram.
