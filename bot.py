import asyncio
import html
import logging
import os
import random
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ---------- Настройки ----------
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {8922424402}  # админ
NUMBER_PREFIX = os.getenv("NUMBER_PREFIX", "+888")  # префикс внутренних номеров
DOWNLOAD_URL = os.getenv("DOWNLOAD_URL", "")  # ссылка на скачивание мессенджера
CODE_TTL = 300  # код живёт 5 минут
START_BALANCE = 100  # стартовые монеты
DAILY_REWARD = 25  # ежедневная награда
BADGE_PRICE = 300  # цена значка верификации
DB = "bot.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()],
)

dp = Dispatcher()
WAIT: dict[int, str] = {}  # кто сейчас вводит юзернейм


# ---------- База ----------
@contextmanager
def conn():
    c = sqlite3.connect(DB, timeout=15)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                tg_id INTEGER PRIMARY KEY,
                username TEXT,
                nick TEXT,
                balance INTEGER DEFAULT 0,
                banned INTEGER DEFAULT 0,
                badge INTEGER DEFAULT 0,
                last_daily REAL DEFAULT 0,
                created REAL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_nick ON users(nick);
            CREATE TABLE IF NOT EXISTS numbers(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT UNIQUE,
                owner INTEGER,
                price INTEGER,
                created REAL
            );
            CREATE TABLE IF NOT EXISTS codes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT,
                code TEXT,
                created REAL,
                used INTEGER DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL,
                tg_id INTEGER,
                action TEXT
            );
            """
        )


def log(uid: int, action: str):
    with conn() as c:
        c.execute("INSERT INTO logs(ts, tg_id, action) VALUES(?,?,?)", (time.time(), uid, action))
    logging.info("%s | %s", uid, action)


def get_user(uid: int):
    with conn() as c:
        return c.execute("SELECT * FROM users WHERE tg_id=?", (uid,)).fetchone()


def ensure_user(uid: int, username):
    u = get_user(uid)
    if u:
        return u
    with conn() as c:
        c.execute(
            "INSERT OR IGNORE INTO users(tg_id, username, balance, created) VALUES(?,?,?,?)",
            (uid, username, START_BALANCE, time.time()),
        )
    log(uid, "register")
    return get_user(uid)


# ---------- Номера ----------
def gen_number():
    r = random.random()
    digits = "0123456789"
    if r < 0.2:
        d = random.choice(digits)
        tail = d * 4 + "".join(random.choices(digits, k=3))
        price = 250
    elif r < 0.4:
        tail = "".join(random.choices(digits, k=4)) + random.choice(["000", "777", "888"])
        price = 180
    else:
        tail = "".join(random.choices(digits, k=7))
        price = 75
    return f"{NUMBER_PREFIX} {tail[:3]} {tail[3:]}", price


def offer_numbers(n: int = 6, fresh: bool = False):
    now = time.time()
    with conn() as c:
        if fresh:
            c.execute("DELETE FROM numbers WHERE owner IS NULL")
        else:
            c.execute("DELETE FROM numbers WHERE owner IS NULL AND created<?", (now - 600,))
        have = c.execute("SELECT COUNT(*) FROM numbers WHERE owner IS NULL").fetchone()[0]
        while have < n:
            num, price = gen_number()
            try:
                c.execute("INSERT INTO numbers(number, price, created) VALUES(?,?,?)", (num, price, now))
                have += 1
            except sqlite3.IntegrityError:
                continue
        return c.execute("SELECT * FROM numbers WHERE owner IS NULL ORDER BY price DESC").fetchall()


def buy_number(uid: int, nid: int):
    with conn() as c:
        n = c.execute("SELECT * FROM numbers WHERE id=?", (nid,)).fetchone()
        if not n or n["owner"] is not None:
            return "gone", None
        bal = c.execute("SELECT balance FROM users WHERE tg_id=?", (uid,)).fetchone()["balance"]
        if bal < n["price"]:
            return "money", n
        cur = c.execute("UPDATE numbers SET owner=? WHERE id=? AND owner IS NULL", (uid, nid))
        if cur.rowcount == 0:
            return "gone", None
        c.execute("UPDATE users SET balance=balance-? WHERE tg_id=?", (n["price"], uid))
        return "ok", n


def owned_number(uid: int, nid: int):
    with conn() as c:
        return c.execute("SELECT * FROM numbers WHERE id=? AND owner=?", (nid, uid)).fetchone()


# ---------- Коды входа ----------
def issue_code(number: str) -> str:
    code = f"{secrets.randbelow(10**6):06d}"
    with conn() as c:
        c.execute("INSERT INTO codes(number, code, created) VALUES(?,?,?)", (number, code, time.time()))
    return code


def check_code(number: str, code: str) -> bool:
    """Вызывай из бэкенда мессенджера для проверки входа."""
    with conn() as c:
        row = c.execute(
            "SELECT id FROM codes WHERE number=? AND code=? AND used=0 AND created>?",
            (number, code, time.time() - CODE_TTL),
        ).fetchone()
        if row:
            c.execute("UPDATE codes SET used=1 WHERE id=?", (row["id"],))
            return True
    return False


# ---------- Юзернеймы и значок ----------
NICK_RE = re.compile(r"^[a-z][a-z0-9_]{3,31}$")


def nick_price(nick: str) -> int:
    if len(nick) <= 4:
        return 500
    if len(nick) == 5:
        return 250
    if len(nick) <= 8:
        return 100
    return 50


def set_nick(uid: int, nick: str):
    price = nick_price(nick)
    with conn() as c:
        bal = c.execute("SELECT balance FROM users WHERE tg_id=?", (uid,)).fetchone()["balance"]
        if bal < price:
            return "money", price
        try:
            c.execute("UPDATE users SET nick=? WHERE tg_id=?", (nick, uid))
        except sqlite3.IntegrityError:
            return "taken", price
        c.execute("UPDATE users SET balance=balance-? WHERE tg_id=?", (price, uid))
    return "ok", price


def buy_badge(uid: int):
    with conn() as c:
        u = c.execute("SELECT balance, badge FROM users WHERE tg_id=?", (uid,)).fetchone()
        if u["badge"]:
            return "have"
        if u["balance"] < BADGE_PRICE:
            return "money"
        c.execute("UPDATE users SET balance=balance-?, badge=1 WHERE tg_id=?", (BADGE_PRICE, uid))
    return "ok"


# ---------- Интерфейс ----------
def b(text: str, data: str):
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_kb(uid: int):
    rows = [
        [b("🛒 Магазин", "shop")],
        [b("👤 Профиль", "profile"), b("🔐 Мои номера", "mynums")],
        [b("🎁 Ежедневная награда", "daily")],
    ]
    if DOWNLOAD_URL:
        rows.append([InlineKeyboardButton(text="📱 Скачать мессенджер", url=DOWNLOAD_URL)])
    rows.append([b("ℹ️ О боте", "about")])
    if uid in ADMIN_IDS:
        rows.append([b("🛠 Админ-панель", "admin")])
    return kb(rows)


MENU_TEXT = (
    "✨ <b>Главное меню</b>\n\n"
    "🛒 Магазин — номера, юзернеймы и значок верификации\n"
    "🔐 Мои номера — твои номера и коды входа\n"
    "👤 Профиль — баланс и данные\n"
    "🎁 Ежедневная награда — бесплатные монеты"
)

BACK = [b("⬅️ В меню", "menu")]


async def show(c: CallbackQuery, text: str, markup):
    try:
        await c.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        await c.message.answer(text, reply_markup=markup, parse_mode="HTML")
    await c.answer()


async def guard(c: CallbackQuery):
    u = ensure_user(c.from_user.id, c.from_user.username)
    if u["banned"]:
        await c.answer("⛔ Доступ закрыт", show_alert=True)
        return None
    return u


# ---------- Пользовательские хэндлеры ----------
@dp.message(CommandStart())
async def start(m: Message):
    u = ensure_user(m.from_user.id, m.from_user.username)
    if u["banned"]:
        return await m.answer("⛔ Доступ закрыт.")
    WAIT.pop(m.from_user.id, None)
    await m.answer(MENU_TEXT, reply_markup=main_kb(m.from_user.id), parse_mode="HTML")


@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"🆔 Ваш ID: <code>{m.from_user.id}</code>", parse_mode="HTML")


@dp.callback_query(F.data == "menu")
async def menu(c: CallbackQuery):
    if not await guard(c):
        return
    WAIT.pop(c.from_user.id, None)
    await show(c, MENU_TEXT, main_kb(c.from_user.id))


@dp.callback_query(F.data == "about")
async def about(c: CallbackQuery):
    if not await guard(c):
        return
    text = (
        "ℹ️ <b>О боте</b>\n\n"
        "Это бот авторизации и магазин нашего мессенджера.\n"
        "Номера, юзернеймы и значки — внутренние, они работают только внутри мессенджера. "
        "Монеты внутренние, их можно получать в награду."
    )
    await show(c, text, kb([BACK]))


@dp.callback_query(F.data == "profile")
async def profile(c: CallbackQuery):
    u = await guard(c)
    if not u:
        return
    with conn() as con:
        cnt = con.execute("SELECT COUNT(*) FROM numbers WHERE owner=?", (u["tg_id"],)).fetchone()[0]
    nick = f"@{html.escape(u['nick'])}" if u["nick"] else "—"
    badge = "✅ есть" if u["badge"] else "нет"
    text = (
        "👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{u['tg_id']}</code>\n"
        f"💬 Юзернейм: {nick}\n"
        f"🪙 Баланс: {u['balance']}\n"
        f"✅ Верификация: {badge}\n"
        f"🔐 Номеров: {cnt}"
    )
    await show(c, text, kb([BACK]))


@dp.callback_query(F.data == "daily")
async def daily(c: CallbackQuery):
    u = await guard(c)
    if not u:
        return
    now = time.time()
    left = 86400 - (now - u["last_daily"])
    if left > 0:
        h, m = int(left // 3600), int(left % 3600 // 60)
        return await show(c, f"🎁 Награда уже получена. Приходи через {h} ч {m} мин.", kb([BACK]))
    with conn() as con:
        con.execute(
            "UPDATE users SET balance=balance+?, last_daily=? WHERE tg_id=?",
            (DAILY_REWARD, now, u["tg_id"]),
        )
    log(u["tg_id"], "daily")
    await show(c, f"🎁 Ты получил <b>{DAILY_REWARD}</b> монет!", kb([BACK]))


# ----- Магазин -----
@dp.callback_query(F.data == "shop")
async def shop(c: CallbackQuery):
    u = await guard(c)
    if not u:
        return
    WAIT.pop(c.from_user.id, None)
    text = f"🛒 <b>Магазин</b>\n\n🪙 Баланс: {u['balance']}"
    await show(
        c,
        text,
        kb(
            [
                [b("📞 Номера", "shop_num")],
                [b("💎 Юзернейм", "shop_user")],
                [b(f"✅ Верификация — {BADGE_PRICE} 🪙", "shop_badge")],
                BACK,
            ]
        ),
    )


@dp.callback_query(F.data.in_({"shop_num", "shop_num_new"}))
async def shop_num(c: CallbackQuery):
    u = await guard(c)
    if not u:
        return
    rows = offer_numbers(fresh=(c.data == "shop_num_new"))
    buttons = [[b(f"{r['number']} — {r['price']} 🪙", f"buy:{r['id']}")] for r in rows]
    buttons.append([b("🔄 Другие номера", "shop_num_new")])
    buttons.append([b("⬅️ Магазин", "shop")])
    await show(c, f"📞 <b>Номера</b>\n\n🪙 Баланс: {u['balance']}\nВыбери номер:", kb(buttons))


@dp.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery):
    u = await guard(c)
    if not u:
        return
    status, n = buy_number(u["tg_id"], int(c.data.split(":")[1]))
    if status == "gone":
        return await c.answer("Номер уже недоступен", show_alert=True)
    if status == "money":
        return await c.answer("Не хватает монет", show_alert=True)
    log(u["tg_id"], f"buy_number {n['number']}")
    await show(
        c,
        f"✅ Номер <code>{n['number']}</code> куплен!\nКод входа можно получить в разделе «Мои номера».",
        kb([[b("🔐 Мои номера", "mynums")], [b("⬅️ Магазин", "shop")]]),
    )


@dp.callback_query(F.data == "shop_user")
async def shop_user(c: CallbackQuery):
    u = await guard(c)
    if not u:
        return
    WAIT[c.from_user.id] = "nick"
    text = (
        "💎 <b>Юзернейм</b>\n\n"
        "Отправь желаемый юзернейм одним сообщением (латиница, цифры, _; от 4 символов).\n\n"
        "Цены: 4 символа — 500 🪙, 5 — 250 🪙, 6–8 — 100 🪙, длиннее — 50 🪙."
    )
    await show(c, text, kb([[b("⬅️ Магазин", "shop")]]))


@dp.callback_query(F.data == "shop_badge")
async def shop_badge(c: CallbackQuery):
    u = await guard(c)
    if not u:
        return
    status = buy_badge(u["tg_id"])
    if status == "have":
        return await c.answer("Значок уже есть", show_alert=True)
    if status == "money":
        return await c.answer("Не хватает монет", show_alert=True)
    log(u["tg_id"], "buy_badge")
    await show(c, "✅ Значок верификации куплен!", kb([[b("⬅️ Магазин", "shop")]]))


@dp.message(F.text & ~F.text.startswith("/"))
async def text_input(m: Message):
    uid = m.from_user.id
    if WAIT.get(uid) != "nick":
        return
    u = ensure_user(uid, m.from_user.username)
    if u["banned"]:
        return
    nick = m.text.strip().lstrip("@").lower()
    if not NICK_RE.match(nick):
        return await m.answer("Неверный формат. Латиница, цифры и _, от 4 символов, начинается с буквы.")
    status, price = set_nick(uid, nick)
    if status == "taken":
        return await m.answer("Этот юзернейм занят. Попробуй другой.")
    if status == "money":
        WAIT.pop(uid, None)
        return await m.answer(f"Не хватает монет. Цена: {price} 🪙.", reply_markup=main_kb(uid))
    WAIT.pop(uid, None)
    log(uid, f"buy_nick {nick}")
    await m.answer(f"✅ Юзернейм @{html.escape(nick)} теперь твой!", reply_markup=main_kb(uid), parse_mode="HTML")


# ----- Мои номера и коды -----
@dp.callback_query(F.data == "mynums")
async def mynums(c: CallbackQuery):
    u = await guard(c)
    if not u:
        return
    with conn() as con:
        rows = con.execute("SELECT * FROM numbers WHERE owner=? ORDER BY id", (u["tg_id"],)).fetchall()
    if not rows:
        return await show(
            c,
            "🔐 У тебя пока нет номеров. Купи в магазине.",
            kb([[b("📞 Купить номер", "shop_num")], BACK]),
        )
    buttons = [[b(r["number"], f"num:{r['id']}")] for r in rows]
    buttons.append(BACK)
    await show(c, "🔐 <b>Мои номера</b>\nВыбери номер:", kb(buttons))


@dp.callback_query(F.data.startswith("num:"))
async def num_detail(c: CallbackQuery):
    u = await guard(c)
    if not u:
        return
    n = owned_number(u["tg_id"], int(c.data.split(":")[1]))
    if not n:
        return await c.answer("Номер не найден", show_alert=True)
    await show(
        c,
        f"🔐 Номер <code>{n['number']}</code>",
        kb([[b("🔑 Получить код входа", f"code:{n['id']}")], [b("⬅️ Мои номера", "mynums")]]),
    )


@dp.callback_query(F.data.startswith("code:"))
async def get_code(c: CallbackQuery):
    u = await guard(c)
    if not u:
        return
    n = owned_number(u["tg_id"], int(c.data.split(":")[1]))
    if not n:
        return await c.answer("Номер не найден", show_alert=True)
    code = issue_code(n["number"])
    log(u["tg_id"], f"issue_code {n['number']}")
    await show(
        c,
        f"🔑 Код для <code>{n['number']}</code>:\n\n<code>{code}</code>\n\n⏳ Действует {CODE_TTL // 60} мин.",
        kb([[b("🔄 Новый код", f"code:{n['id']}")], [b("⬅️ Мои номера", "mynums")]]),
    )


# ---------- Админ-панель ----------
def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def admin_kb():
    return kb(
        [
            [b("📊 Статистика", "a_stats")],
            [b("👥 Пользователи", "a_users")],
            [b("📜 Логи", "a_logs")],
            [b("⬅️ В меню", "menu")],
        ]
    )


@dp.message(Command("admin"))
async def admin_cmd(m: Message):
    if is_admin(m.from_user.id):
        await m.answer("🛠 Админ-панель\n\nКоманды: /ban ID, /unban ID, /give ID сумма, /broadcast текст", reply_markup=admin_kb())


@dp.callback_query(F.data == "admin")
async def admin_cb(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("Нет доступа", show_alert=True)
    await show(c, "🛠 Админ-панель\n\nКоманды: /ban ID, /unban ID, /give ID сумма, /broadcast текст", admin_kb())


@dp.callback_query(F.data == "a_stats")
async def a_stats(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("Нет доступа", show_alert=True)
    with conn() as con:
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        banned = con.execute("SELECT COUNT(*) FROM users WHERE banned=1").fetchone()[0]
        nums = con.execute("SELECT COUNT(*) FROM numbers WHERE owner IS NOT NULL").fetchone()[0]
        codes = con.execute("SELECT COUNT(*) FROM codes").fetchone()[0]
    await c.message.answer(
        f"📊 Пользователей: {users}\n⛔ Забанено: {banned}\n📞 Куплено номеров: {nums}\n🔑 Выдано кодов: {codes}"
    )
    await c.answer()


@dp.callback_query(F.data == "a_users")
async def a_users(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("Нет доступа", show_alert=True)
    with conn() as con:
        rows = con.execute("SELECT * FROM users ORDER BY created DESC LIMIT 20").fetchall()
    text = "\n".join(
        f"{r['tg_id']} | @{r['username'] or '-'} | {r['balance']}🪙{' ⛔' if r['banned'] else ''}" for r in rows
    ) or "Пусто"
    await c.message.answer(f"👥 Последние 20:\n{text}")
    await c.answer()


@dp.callback_query(F.data == "a_logs")
async def a_logs(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("Нет доступа", show_alert=True)
    with conn() as con:
        rows = con.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 25").fetchall()
    text = "\n".join(
        f"{time.strftime('%d.%m %H:%M:%S', time.localtime(r['ts']))} | {r['tg_id']} | {r['action']}" for r in rows
    ) or "Пусто"
    await c.message.answer(f"📜 Логи:\n{text}")
    await c.answer()


@dp.message(Command("ban"))
async def ban(m: Message):
    if not is_admin(m.from_user.id):
        return
    try:
        uid = int(m.text.split()[1])
    except (IndexError, ValueError):
        return await m.answer("Использование: /ban ID")
    with conn() as c:
        c.execute("UPDATE users SET banned=1 WHERE tg_id=?", (uid,))
    log(m.from_user.id, f"ban {uid}")
    await m.answer(f"⛔ {uid} забанен")


@dp.message(Command("unban"))
async def unban(m: Message):
    if not is_admin(m.from_user.id):
        return
    try:
        uid = int(m.text.split()[1])
    except (IndexError, ValueError):
        return await m.answer("Использование: /unban ID")
    with conn() as c:
        c.execute("UPDATE users SET banned=0 WHERE tg_id=?", (uid,))
    log(m.from_user.id, f"unban {uid}")
    await m.answer(f"✅ {uid} разбанен")


@dp.message(Command("give"))
async def give(m: Message):
    if not is_admin(m.from_user.id):
        return
    try:
        _, uid, amount = m.text.split()
        uid, amount = int(uid), int(amount)
    except ValueError:
        return await m.answer("Использование: /give ID сумма")
    if not get_user(uid):
        return await m.answer("Пользователь не найден (он должен сначала нажать /start).")
    with conn() as c:
        c.execute("UPDATE users SET balance=balance+? WHERE tg_id=?", (amount, uid))
    log(m.from_user.id, f"give {uid} {amount}")
    await m.answer(f"🪙 {uid}: {amount:+d}")


@dp.message(Command("broadcast"))
async def broadcast(m: Message, bot: Bot):
    if not is_admin(m.from_user.id):
        return
    text = m.text.partition(" ")[2].strip()
    if not text:
        return await m.answer("Использование: /broadcast текст")
    with conn() as c:
        ids = [r["tg_id"] for r in c.execute("SELECT tg_id FROM users WHERE banned=0")]
    ok = 0
    for uid in ids:
        try:
            await bot.send_message(uid, text)
            ok += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    log(m.from_user.id, f"broadcast {ok}/{len(ids)}")
    await m.answer(f"📢 Отправлено: {ok}/{len(ids)}")


async def run_polling():
    init_db()
    bot = Bot(BOT_TOKEN)
    logging.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(run_polling())
