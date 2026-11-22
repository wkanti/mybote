import os

from aiohttp import web
from aiogram import Bot
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from bot import BOT_TOKEN, dp, init_db

BASE_URL = os.getenv("RENDER_EXTERNAL_URL")  # Render задаёт сам
PATH = "/webhook"
SECRET = os.getenv("WEBHOOK_SECRET", "change-me-secret")


async def on_startup(bot: Bot):
    await bot.set_webhook(f"{BASE_URL}{PATH}", secret_token=SECRET)


async def health(request):
    return web.Response(text="ok")


def main():
    init_db()
    bot = Bot(BOT_TOKEN)
    dp.startup.register(on_startup)
    app = web.Application()
    app.router.add_get("/", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=SECRET).register(app, path=PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))


if __name__ == "__main__":
    main()
