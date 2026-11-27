import asyncio
import hmac
import logging
import os
import re
import secrets
import shutil
import sqlite3
import time
from html import escape

from aiohttp import web
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import admin_panel
import config

try:
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession
except Exception:  # telethon не установлен: приём кодов отключён
    TelegramClient = None

log = logging.getLogger("kantimsg")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, username TEXT, name TEXT,
    stars INTEGER NOT NULL DEFAULT 0, banned INTEGER NOT NULL DEFAULT 0,
    daily_at INTEGER NOT NULL DEFAULT 0, joined_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS numbers(
    id INTEGER PRIMARY KEY AUTOINCREMENT, phone TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL, session TEXT, owner_id INTEGER, assigned_at INTEGER,
    added_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS codes(
    id INTEGER PRIMARY KEY AUTOINCREMENT, number_id INTEGER NOT NULL,
    code TEXT NOT NULL, text TEXT, at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_numbers_owner ON numbers(owner_id);
CREATE INDEX IF NOT EXISTS idx_codes_number ON codes(number_id);
"""


class DB:
    """Вся база в одном файле SQLite. Номера, владельцы и коды хранятся на диске."""

    def __init__(self, path):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._open()

    def _open(self):
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def q(self, sql, args=()):
        return self.conn.execute(sql, args).fetchall()

    def one(self, sql, args=()):
        return self.conn.execute(sql, args).fetchone()

    def x(self, sql, args=()):
        cur = self.conn.execute(sql, args)
        self.conn.commit()
        return cur

    # ---------- общее ----------
    @staticmethod
    def norm(phone):
        digits = re.sub(r"\D", "", phone or "")
        return "+" + digits if digits else ""

    @staticmethod
    def kind_of(phone):
        return "paid" if phone.startswith("+888") else "free"

    def upsert_user(self, u):
        self.x(
            "INSERT INTO users(id, username, name, joined_at) VALUES(?,?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET username=excluded.username, name=excluded.name",
            (u.id, u.username, u.full_name, int(time.time())),
        )

    def is_banned(self, uid):
        r = self.one("SELECT banned FROM users WHERE id=?", (uid,))
        return bool(r and r["banned"])

    # ---------- номера ----------
    def add_number(self, phone, session=None):
        """Возвращает (id, создан_ли_новый)."""
        row = self.one("SELECT id FROM numbers WHERE phone=?", (phone,))
        if row:
            if session:
                self.x("UPDATE numbers SET session=? WHERE id=?", (session, row["id"]))
            return row["id"], False
        cur = self.x(
            "INSERT INTO numbers(phone, kind, session, added_at) VALUES(?,?,?,?)",
            (phone, self.kind_of(phone), session or None, int(time.time())),
        )
        return cur.lastrowid, True

    def claim_free(self, uid):
        """Выдаёт свободный +39. Возвращает (статус, строка): ok / limit / empty."""
        have = self.one(
            "SELECT COUNT(*) c FROM numbers WHERE owner_id=? AND kind='free'", (uid,)
        )["c"]
        if have >= config.FREE_LIMIT:
            return "limit", None
        row = self.one(
            "SELECT id, phone FROM numbers WHERE kind='free' AND owner_id IS NULL "
            "ORDER BY id LIMIT 1"
        )
        if not row:
            return "empty", None
        self.x(
            "UPDATE numbers SET owner_id=?, assigned_at=? WHERE id=?",
            (uid, int(time.time()), row["id"]),
        )
        return "ok", row

    def add_code(self, number_id, code, text):
        self.x(
            "INSERT INTO codes(number_id, code, text, at) VALUES(?,?,?,?)",
            (number_id, code, (text or "")[:500], int(time.time())),
        )

    def daily(self, uid):
        now = int(time.time())
        u = self.one("SELECT daily_at FROM users WHERE id=?", (uid,))
        left = u["daily_at"] + 86400 - now
        if left > 0:
            return False, left
        self.x(
            "UPDATE users SET stars=stars+?, daily_at=? WHERE id=?",
            (config.DAILY_REWARD, now, uid),
        )
        return True, 0

    # ---------- бэкап ----------
    def backup_to(self, dest):
        d = sqlite3.connect(dest)
        try:
            self.conn.backup(d)
        finally:
            d.close()

    def restore_from(self, src):
        t = sqlite3.connect(src)
        try:
            names = {r[0] for r in t.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            t.close()
        if not {"users", "numbers", "codes"} <= names:
            raise ValueError("Это не база бота KantiMSG")
        self.conn.close()
        shutil.copyfile(src, self.path)
        self._open()


CODE_RE = re.compile(r"\b(\d{5,6})\b")


class Tele:
    """Держит сессии номеров и ловит коды входа от Telegram (аккаунт 777000)."""

    def __init__(self, db, bot):
        self.db = db
        self.bot = bot
        self.clients = {}

    @property
    def enabled(self):
        return TelegramClient is not None and bool(config.API_ID) and bool(config.API_HASH)

    async def start_all(self):
        if not self.enabled:
            log.warning("Автоприём кодов выключен (нет telethon или API_ID/API_HASH). "
                        "Коды можно добавлять вручную командой /code.")
            return
        for r in self.db.q("SELECT id, phone, session FROM numbers WHERE session IS NOT NULL"):
            await self.add(r["id"], r["phone"], r["session"])

    async def add(self, nid, phone, session):
        if not self.enabled:
            return False
        await self.stop(nid)
        try:
            client = TelegramClient(StringSession(session), config.API_ID, config.API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                log.warning("Сессия номера %s не авторизована", phone)
                return False

            @client.on(events.NewMessage(incoming=True))
            async def on_msg(event, nid=nid):
                if event.sender_id != 777000:
                    return
                text = event.raw_text or ""
                m = CODE_RE.search(text)
                if m:
                    await self.deliver(nid, m.group(1), text)

            self.clients[nid] = client
            return True
        except Exception:
            log.exception("Не удалось подключить номер %s", phone)
            return False

    async def stop(self, nid):
        client = self.clients.pop(nid, None)
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass

    async def stop_all(self):
        for nid in list(self.clients):
            await self.stop(nid)

    async def reload(self):
        await self.stop_all()
        await self.start_all()

    async def deliver(self, nid, code, text=""):
        """Сохраняет код и отправляет владельцу номера."""
        self.db.add_code(nid, code, text)
        n = self.db.one("SELECT phone, owner_id FROM numbers WHERE id=?", (nid,))
        if n and n["owner_id"]:
            try:
                await self.bot.send_message(
                    n["owner_id"],
                    f"🔔 Код для <b>{escape(n['phone'])}</b>:\n<code>{escape(code)}</code>",
                )
            except Exception:
                log.exception("Не удалось отправить код владельцу")
        return n


class UserMW(BaseMiddleware):
    def __init__(self, db):
        self.db = db

    async def __call__(self, handler, event, data):
        u = data.get("event_from_user")
        if u and not u.is_bot:
            self.db.upsert_user(u)
            if self.db.is_banned(u.id) and u.id not in config.ADMIN_IDS:
                return None
        return await handler(event, data)


router = Router()


def fmt_time(ts):
    return time.strftime("%d.%m %H:%M", time.gmtime(ts + config.TZ_OFFSET * 3600))


def main_kb(uid=None):
    b = InlineKeyboardBuilder()
    b.button(text="🆓 Получить номер +39 бесплатно", callback_data="claim")
    b.button(text="🛒 Магазин", callback_data="shop")
    b.button(text="📱 Мои номера", callback_data="nums")
    b.button(text="👤 Профиль", callback_data="profile")
    b.button(text="🎁 Ежедневная награда", callback_data="daily")
    sizes = [1, 1, 2, 1]
    if uid in config.ADMIN_IDS:
        b.button(text="🛠 Админ-панель", callback_data="adm:menu")
        sizes.append(1)
    b.adjust(*sizes)
    return b.as_markup()


def back_kb(target="menu", text="⬅️ Меню"):
    b = InlineKeyboardBuilder()
    b.button(text=text, callback_data=target)
    return b.as_markup()


async def show(cb, text, kb=None):
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise
    await cb.answer()


WELCOME = "👋 Добро пожаловать в <b>KantiMSG</b>!\n\nЗдесь можно получить номер для регистрации и принимать на него коды."


@router.message(CommandStart())
async def start(m: Message):
    await m.answer(WELCOME, reply_markup=main_kb(m.from_user.id))


@router.message(Command("myid"))
async def myid(m: Message):
    await m.answer(f"Твой Telegram ID: <code>{m.from_user.id}</code>")


@router.callback_query(F.data == "menu")
async def menu(cb: CallbackQuery):
    await show(cb, WELCOME, main_kb(cb.from_user.id))


@router.callback_query(F.data == "shop")
async def shop(cb: CallbackQuery):
    contact = config.SHOP_CONTACT
    text = (
        "🛒 <b>Магазин</b>\n\n"
        "• ⭐ Звёзды\n• ✅ Верификации\n• 🖼 Рамки и значки\n"
        "• 💎 NFT-юзернеймы\n• 📞 Анонимные номера +888\n• 💳 Кошельки\n\n"
        f"Всё покупается у {escape(contact)}. Напиши ему, и он выдаст покупку."
    )
    b = InlineKeyboardBuilder()
    b.button(text=f"💬 Купить у {contact}", url=f"https://t.me/{contact.lstrip('@')}")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    await show(cb, text, b.as_markup())


@router.callback_query(F.data == "profile")
async def profile(cb: CallbackQuery, db):
    uid = cb.from_user.id
    u = db.one("SELECT stars FROM users WHERE id=?", (uid,))
    n = db.one("SELECT COUNT(*) c FROM numbers WHERE owner_id=?", (uid,))["c"]
    text = (
        "👤 <b>Профиль</b>\n\n"
        f"ID: <code>{uid}</code>\n"
        f"Звёзды: ⭐ {u['stars']}\n"
        f"Номеров: {n}"
    )
    await show(cb, text, back_kb())


@router.callback_query(F.data == "daily")
async def daily(cb: CallbackQuery, db):
    ok, left = db.daily(cb.from_user.id)
    if ok:
        text = f"🎁 Награда получена: +{config.DAILY_REWARD} ⭐\nПриходи завтра."
    else:
        h, mnt = left // 3600, (left % 3600) // 60
        text = f"⏳ Награда уже получена. Следующая через {h} ч {mnt} мин."
    await show(cb, text, back_kb())


@router.callback_query(F.data == "nums")
async def nums(cb: CallbackQuery, db):
    rows = db.q("SELECT id, phone FROM numbers WHERE owner_id=? ORDER BY id", (cb.from_user.id,))
    b = InlineKeyboardBuilder()
    for r in rows:
        b.button(text=f"📱 {r['phone']}", callback_data=f"num:{r['id']}")
    b.button(text="🆓 Получить бесплатный номер +39", callback_data="claim")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    text = "📱 <b>Мои номера</b>\n\n"
    text += "Выбери номер, чтобы увидеть коды." if rows else "У тебя пока нет номеров."
    await show(cb, text, b.as_markup())


@router.callback_query(F.data == "claim")
async def claim(cb: CallbackQuery, db):
    status, row = db.claim_free(cb.from_user.id)
    if status == "ok":
        text = (f"✅ Твой номер: <b>{escape(row['phone'])}</b>\n"
                "Он закреплён за тобой. Код придёт сюда сам, когда его отправят на номер.")
    elif status == "limit":
        text = "У тебя уже есть бесплатный номер. Анонимные +888 покупаются у " + escape(config.SHOP_CONTACT) + "."
    else:
        text = "Свободных номеров +39 сейчас нет. Загляни позже."
    b = InlineKeyboardBuilder()
    b.button(text="📱 Мои номера", callback_data="nums")
    b.button(text="⬅️ Меню", callback_data="menu")
    b.adjust(1)
    await show(cb, text, b.as_markup())


@router.callback_query(F.data.startswith("num:"))
async def number_card(cb: CallbackQuery, db):
    nid = int(cb.data.split(":")[1])
    n = db.one("SELECT id, phone FROM numbers WHERE id=? AND owner_id=?", (nid, cb.from_user.id))
    if not n:
        await cb.answer("Номер не найден", show_alert=True)
        return
    codes = db.q("SELECT code, at FROM codes WHERE number_id=? ORDER BY id DESC LIMIT 5", (nid,))
    text = f"📱 <b>{escape(n['phone'])}</b>\n\n"
    if codes:
        text += "Последние коды:\n" + "\n".join(f"<code>{c['code']}</code>  ({fmt_time(c['at'])})" for c in codes)
    else:
        text += "Кодов пока нет. Как только код придёт, я пришлю его сюда."
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Обновить", callback_data=f"num:{nid}")
    b.button(text="⬅️ Назад", callback_data="nums")
    b.adjust(2)
    await show(cb, text, b.as_markup())


async def auto_backup(bot, db):
    while True:
        await asyncio.sleep(config.BACKUP_HOURS * 3600)
        try:
            await admin_panel.send_backup(bot, db)
        except Exception:
            log.exception("Автобэкап не удался")


LOGIN = {}  # цифры номера -> {"code", "exp", "tries", "sent"}; коды входа живут 5 минут


def jr(data, status=200):
    return web.json_response(data, status=status)


async def read_json(request):
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def make_web_app(db, bot):
    """Адреса для приложения KantiMSG: /api/login/start и /api/login/verify."""

    @web.middleware
    async def cors(request, handler):
        if request.method == "OPTIONS":
            resp = web.Response(status=204)
        else:
            try:
                resp = await handler(request)
            except web.HTTPException as e:
                resp = e
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-API-Key"
        resp.headers["Access-Control-Allow-Methods"] = "POST, GET, OPTIONS"
        resp.headers["Access-Control-Max-Age"] = "86400"
        return resp

    def check_key(request):
        return hmac.compare_digest(request.headers.get("X-API-Key", ""), config.API_KEY)

    def find_number(digits):
        return db.one("SELECT id, phone, owner_id FROM numbers WHERE phone=?", ("+" + digits,))

    async def root(request):
        return web.Response(text="KantiMSG bot is running")

    async def login_start(request):
        if not check_key(request):
            return jr({"ok": False, "error": "forbidden"}, 403)
        data = await read_json(request)
        digits = re.sub(r"\D", "", str(data.get("number", "")))
        if not 7 <= len(digits) <= 15:
            return jr({"ok": False, "error": "bad_number"})
        n = find_number(digits)
        if not n or not n["owner_id"]:
            return jr({"ok": False, "error": "number_not_found"})
        if db.is_banned(n["owner_id"]):
            return jr({"ok": False, "error": "banned"})
        now = time.time()
        st = LOGIN.get(digits)
        if st and now - st["sent"] < 20:
            return jr({"ok": False, "error": "too_many_requests"})
        code = f"{secrets.randbelow(1000000):06d}"
        try:
            await bot.send_message(
                n["owner_id"],
                f"🔐 Код для входа в KantiMSG по номеру <b>{escape(n['phone'])}</b>:\n"
                f"<code>{code}</code>\n\n"
                "Действует 5 минут. Никому не сообщайте код.",
            )
        except Exception:
            log.exception("Не удалось отправить код входа")
            return jr({"ok": False, "error": "cannot_send"})
        LOGIN[digits] = {"code": code, "exp": now + 300, "tries": 0, "sent": now}
        return jr({"ok": True})

    async def login_verify(request):
        if not check_key(request):
            return jr({"ok": False, "error": "forbidden"}, 403)
        data = await read_json(request)
        digits = re.sub(r"\D", "", str(data.get("number", "")))
        code = str(data.get("code", "")).strip()
        st = LOGIN.get(digits)
        if not st or time.time() > st["exp"]:
            LOGIN.pop(digits, None)
            return jr({"ok": False, "error": "wrong_code"})
        st["tries"] += 1
        if st["tries"] > 5:
            LOGIN.pop(digits, None)
            return jr({"ok": False, "error": "wrong_code"})
        if not hmac.compare_digest(st["code"], code):
            return jr({"ok": False, "error": "wrong_code"})
        n = find_number(digits)
        if not n or not n["owner_id"]:
            return jr({"ok": False, "error": "number_not_found"})
        LOGIN.pop(digits, None)
        return jr({"ok": True, "tg_id": n["owner_id"]})

    app = web.Application(middlewares=[cors], client_max_size=64 * 1024)
    app.router.add_get("/", root)
    app.router.add_post("/api/login/start", login_start)
    app.router.add_post("/api/login/verify", login_verify)
    return app


async def start_web(db, bot):
    """Открывает порт (Render Web Service) и обслуживает вход из приложения.
    На / можно слать пинг (UptimeRobot), чтобы бесплатный сервер не засыпал."""
    port = os.getenv("PORT")
    if not port:
        return None
    runner = web.AppRunner(make_web_app(db, bot))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()
    return runner


async def main():
    logging.basicConfig(level=logging.INFO)
    if not config.BOT_TOKEN or config.BOT_TOKEN == "ВСТАВЬ_ТОКЕН":
        raise SystemExit("Не задан токен: переменная BOT_TOKEN или BOT_TOKEN в config.py")
    if not config.ADMIN_IDS:
        log.warning("ADMIN_IDS пуст: админ-панель закрыта. Напиши боту /myid и впиши свой ID.")

    db = DB(config.DB_PATH)
    bot = Bot(token=config.BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    web_runner = await start_web(db, bot)
    tele = Tele(db, bot)
    dp = Dispatcher(db=db, tele=tele)
    dp.update.outer_middleware(UserMW(db))
    dp.include_router(admin_panel.router)
    dp.include_router(router)

    await tele.start_all()
    empty = db.one("SELECT (SELECT COUNT(*) FROM users) + (SELECT COUNT(*) FROM numbers) AS c")["c"] == 0
    if empty:
        for admin_id in config.ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    "⚠️ База пустая. Если это не первый запуск, значит диск хостинга не сохраняется. "
                    "Пришли файл бэкапа и ответь на него командой /restore.",
                )
            except Exception:
                pass
    asyncio.create_task(auto_backup(bot, db))
    await bot.delete_webhook(drop_pending_updates=True)  # старый бот мог оставить вебхук
    try:
        await dp.start_polling(bot)
    finally:
        # перед остановкой сервера (деплой, перезапуск) присылаем копию базы
        try:
            await admin_panel.send_backup(bot, db)
        except Exception:
            log.exception("Бэкап при остановке не удался")
        await tele.stop_all()
        if web_runner:
            await web_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
