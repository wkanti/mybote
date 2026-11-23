import asyncio
import logging

import admin_panel
from bot import bot, dp, setup


async def main():
    logging.basicConfig(level=logging.INFO)
    await admin_panel.init_db()
    setup()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
