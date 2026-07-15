# Declarant Admin Panel v2

Функции:
- только Mini App и админ-панель, без команд;
- создание кампаний с источником и меткой конкретного объявления;
- топ кампаний по конверсии;
- уведомления о вступлениях и выходах;
- чёрный список;
- пользователь, удалённый администратором из канала, автоматически блокируется;
- заблокированному пользователю бот не показывает сообщения и не создаёт новые ссылки;
- один Telegram ID получает доступ только один раз;
- защита от пересылки приглашений;
- автоматический отзыв старых ссылок;
- PostgreSQL и CSV-экспорт.

## Render Environment

BOT_TOKEN
BOT_USERNAME=rbsalebot
CHANNEL_ID=-1001322091992
ADMIN_USER_ID=640314234
WEBHOOK_SECRET=<длинная случайная строка>
DATABASE_URL=<Internal Database URL PostgreSQL>
LINK_TTL_SECONDS=600
CLEANUP_INTERVAL_SECONDS=600

Build:
pip install -r requirements.txt

Start:
uvicorn app:app --host 0.0.0.0 --port $PORT

## BotFather

Настрой Main Mini App:
@BotFather → бот → Bot Settings → Configure Mini App / Main Mini App

URL:
https://ТВОЙ-СЕРВИС.onrender.com

Также настрой Menu Button на тот же URL. Тогда админ-панель открывается кнопкой меню в профиле бота, без команд.

Рекламные ссылки создаются внутри панели:
https://t.me/rbsalebot?startapp=campaign_key
