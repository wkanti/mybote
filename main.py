import os
import secrets

from aiohttp import web
from aiogram import Bot
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

from bot import (
    ADMIN_IDS,
    BOT_TOKEN,
    CODE_TTL,
    dp,
    fmt_number,
    get_user,
    init_db,
    normalize_number,
    PG,
    start_login,
    valid_full,
    verify_login,
)

BASE_URL = os.getenv("RENDER_EXTERNAL_URL")  # Render задаёт сам
PATH = "/webhook"
SECRET = os.getenv("WEBHOOK_SECRET", "change-me-secret")
API_KEY = os.getenv("API_KEY") or "yHLDg-JRsSsH4z5VkTtpHc0IUwJ3sb9b"  # ключ для API мессенджера (если в Render задан API_KEY, берётся он)


async def on_startup(bot: Bot):
    await bot.set_webhook(
        f"{BASE_URL}{PATH}",
        secret_token=SECRET,
        allowed_updates=dp.resolve_used_update_types(),
    )
    if not PG:
        # База во временном файле: после перезапуска Render номера пропадают.
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(
                    aid,
                    "⚠️ Бот работает на временной базе (SQLite). Номера и заказы пропадают при "
                    "каждом перезапуске. Добавь в Render переменную DATABASE_URL (Postgres).",
                )
            except Exception:
                pass


async def health(request):
    return web.Response(text="ok")


@web.middleware
async def cors_mw(request, handler):
    """Разрешаем запросы из приложения (WebView) к /api/login/*."""
    if request.method == "OPTIONS":
        resp = web.Response(status=204)
    else:
        try:
            resp = await handler(request)
        except web.HTTPException as e:
            resp = e
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key"
    resp.headers["Access-Control-Max-Age"] = "86400"
    return resp


def authorized(request) -> bool:
    key = request.headers.get("X-API-Key", "")
    return bool(API_KEY) and secrets.compare_digest(key, API_KEY)


async def read_json(request):
    try:
        return await request.json()
    except Exception:
        return {}


async def api_login_start(request):
    """POST /api/login/start  {"number": "+39 12345678"} -> бот отправит код владельцу номера в Telegram."""
    if not authorized(request):
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    data = await read_json(request)
    digits = normalize_number(str(data.get("number", "")))
    if not valid_full(digits):
        return web.json_response({"ok": False, "error": "bad_number"}, status=400)
    status, owner, code = start_login(digits)
    if status == "nonumber":
        return web.json_response({"ok": False, "error": "number_not_found"}, status=404)
    if status == "rate":
        return web.json_response({"ok": False, "error": "too_many_requests"}, status=429)
    if status != "ok":
        return web.json_response({"ok": False, "error": status}, status=403)
    bot: Bot = request.app["bot"]
    try:
        await bot.send_message(
            owner,
            f"🔑 Код входа для <code>{fmt_number(digits)}</code>:\n\n<code>{code}</code>\n\n"
            f"⏳ Действует {CODE_TTL // 60} мин. Никому не сообщайте.",
            parse_mode="HTML",
        )
    except Exception:
        return web.json_response({"ok": False, "error": "cannot_send"}, status=502)
    return web.json_response({"ok": True})


async def api_login_verify(request):
    """POST /api/login/verify  {"number": "+39 12345678", "code": "123456"} -> {"ok": true, "tg_id": ...}"""
    if not authorized(request):
        return web.json_response({"ok": False, "error": "forbidden"}, status=403)
    data = await read_json(request)
    digits = normalize_number(str(data.get("number", "")))
    code = str(data.get("code", "")).strip()
    if not valid_full(digits) or not code.isdigit():
        return web.json_response({"ok": False, "error": "bad_request"}, status=400)
    owner = verify_login(digits, code)
    if owner is None:
        return web.json_response({"ok": False, "error": "wrong_code"}, status=401)
    user = get_user(owner)
    return web.json_response(
        {"ok": True, "tg_id": owner, "username": user["username"] if user else None}
    )


def main():
    init_db()
    bot = Bot(BOT_TOKEN)
    dp.startup.register(on_startup)
    app = web.Application(middlewares=[cors_mw])
    app["bot"] = bot
    app.router.add_get("/", health)
    app.router.add_post("/api/login/start", api_login_start)
    app.router.add_post("/api/login/verify", api_login_verify)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=SECRET).register(app, path=PATH)
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=int(os.getenv("PORT", 10000)))


if __name__ == "__main__":
    main()
