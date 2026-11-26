"""Настройки бота. На хостинге лучше задавать их переменными окружения."""
import os


def _ids(raw):
    out = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


# Токен от @BotFather. Если репозиторий публичный, токен сюда НЕ вписывай,
# задай переменную окружения BOT_TOKEN.
BOT_TOKEN = os.getenv("BOT_TOKEN") or "ВСТАВЬ_ТОКЕН"

# Твой числовой Telegram ID. Не знаешь его: запусти бота и напиши ему /myid.
# Можно вписать сюда или задать переменную ADMIN_IDS (несколько через запятую).
OWNER_ID = 8922424402
ADMIN_IDS = _ids(os.getenv("ADMIN_IDS")) or ({OWNER_ID} if OWNER_ID else set())

# Нужны только для автоматического приёма кодов (my.telegram.org -> API development tools).
API_ID = int(os.getenv("API_ID") or 0)
API_HASH = os.getenv("API_HASH") or ""

# Где лежит база. На хостинге укажи путь на ПОСТОЯННОМ диске (volume),
# иначе при каждом перезапуске всё пропадёт.
DB_PATH = os.getenv("DB_PATH") or "data/kantimsg_bot.db"

SHOP_CONTACT = "@dev_kanti"   # у кого покупать всё из магазина
FREE_LIMIT = int(os.getenv("FREE_LIMIT") or 1)   # сколько бесплатных +39 может взять один человек
DAILY_REWARD = 1              # звёзд за ежедневную награду
TZ_OFFSET = 3                 # часовой пояс для времени кодов (3 = Москва)
BACKUP_HOURS = 24             # как часто присылать тебе копию базы
