import aiosqlite
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import CommandStart
from aiogram.types import Message

import admin_panel
from config import BOT_TOKEN, DB_PATH

bot = Bot(BOT_TOKEN)
dp = Dispatcher()

# ---- основные хендлеры (сюда добавляй меню, магазин, награды и т.д.) ----
main_router = Router()


@main_router.message(CommandStart())
async def start(m: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (m.from_user.id,))
        await db.commit()
    await m.answer("Привет! Бот запущен ✅")


def setup():
    # бан-мидлварь должна быть подключена на dp, чтобы работать для всех хендлеров
    dp.message.outer_middleware(admin_panel.BanMiddleware())
    dp.callback_query.outer_middleware(admin_panel.BanMiddleware())
    dp.include_router(admin_panel.router)
    dp.include_router(main_router)
