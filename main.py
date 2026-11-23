import asyncio
import logging
import os

from aiohttp import web

import admin_panel
from bot import bot, dp, setup


async def health(request):
    return web.Response(text="ok")


async def start_web():
    # Render (Web Service) требует открытый порт, иначе деплой падает
    app = web.Application()
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.getenv("PORT", "10000")))
    await site.start()


async def main():
    logging.basicConfig(level=logging.INFO)
    await admin_panel.init_db()
    setup()
    await start_web()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
