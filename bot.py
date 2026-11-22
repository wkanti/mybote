import asyncio
import html
import logging
import math
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
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

# ======================= НАСТРОЙКИ =======================
BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {8922424402}  # админ
SUPPORT = "@dev_kanti"  # для оплаты рублями и связи
APP_NAME = os.getenv("APP_NAME", "мессенджер")  # название твоего мессенджера
NUMBER_PREFIX = os.getenv("NUMBER_PREFIX", "+888")  # префикс платных внутренних номеров
FREE_PREFIX = os.getenv("FREE_PREFIX", "+39")  # префикс бесплатных номеров
DOWNLOAD_URL = os.getenv("DOWNLOAD_URL", "")  # ссылка на скачивание мессенджера
STAR_RATE = float(os.getenv("STAR_RATE", "1.6"))  # сколько рублей стоит 1 ⭐
CODE_TTL = 300  # код входа живёт 5 минут
DATABASE_URL = os.getenv("DATABASE_URL", "")  # Postgres (если пусто — SQLite)
DB = "bot.db"
PG = bool(DATABASE_URL)
PDIGITS = re.sub(r"\D", "", NUMBER_PREFIX)
FDIGITS = re.sub(r"\D", "", FREE_PREFIX)

# Цены в рублях
MARKS = {
    "Major": {"acc": 150, "chan": 125, "bot": 250},
    "Hold": {"acc": 173, "chan": 144, "bot": 288},
}
KIND_NAMES = {"acc": "аккаунт", "chan": "канал", "bot": "бот"}
NICK_PRICES = {1: 801, 2: 555, 3: 432, 4: 188}
NICK_DEFAULT = 113
NICK_BOT_EXTRA = 150
NUM_PRICES = {2: 225, 3: 188, 4: 150}
NUM_DEFAULT = 113
NUM_PRETTY_EXTRA = 38

if PG:
    import psycopg2
    import psycopg2.extras

    IntegrityError = psycopg2.IntegrityError
else:
    IntegrityError = sqlite3.IntegrityError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()],
)

dp = Dispatcher()
WAIT: dict[int, tuple] = {}  # что сейчас вводит пользователь


# ======================= БАЗА ДАННЫХ =======================
class Db:
    def __init__(self, raw):
        self.raw = raw

    def execute(self, sql, params=()):
        if PG:
            cur = self.raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            sql = sql.replace("?", "%s")
        else:
            cur = self.raw.cursor()
        cur.execute(sql, params)
        return cur


@contextmanager
def conn():
    if PG:
        raw = psycopg2.connect(DATABASE_URL)
    else:
        raw = sqlite3.connect(DB, timeout=15)
        raw.row_factory = sqlite3.Row
    try:
        yield Db(raw)
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def init_db():
    pk = "BIGSERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
    statements = [
        "CREATE TABLE IF NOT EXISTS users(tg_id BIGINT PRIMARY KEY, username TEXT, "
        "banned INTEGER DEFAULT 0, created DOUBLE PRECISION)",
        f"CREATE TABLE IF NOT EXISTS numbers(id {pk}, number TEXT UNIQUE, owner BIGINT, created DOUBLE PRECISION)",
        f"CREATE TABLE IF NOT EXISTS handles(id {pk}, name TEXT UNIQUE, owner BIGINT, kind TEXT, "
        "created DOUBLE PRECISION)",
        f"CREATE TABLE IF NOT EXISTS marks(id {pk}, owner BIGINT, mark TEXT, kind TEXT, target TEXT, "
        "created DOUBLE PRECISION)",
        f"CREATE TABLE IF NOT EXISTS orders(id {pk}, tg_id BIGINT, kind TEXT, item TEXT, extra TEXT, "
        "extra2 TEXT, rub INTEGER, stars INTEGER, status TEXT, method TEXT, charge TEXT, "
        "created DOUBLE PRECISION, paid DOUBLE PRECISION)",
        f"CREATE TABLE IF NOT EXISTS codes(id {pk}, number TEXT, code TEXT, created DOUBLE PRECISION, "
        "used INTEGER DEFAULT 0, attempts INTEGER DEFAULT 0)",
        f"CREATE TABLE IF NOT EXISTS logs(id {pk}, ts DOUBLE PRECISION, tg_id BIGINT, action TEXT)",
    ]
    with conn() as c:
        for s in statements:
            c.execute(s)


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
            "INSERT INTO users(tg_id, username, created) VALUES(?,?,?) ON CONFLICT (tg_id) DO NOTHING",
            (uid, username, time.time()),
        )
    log(uid, "register")
    return get_user(uid)


# ======================= ЦЕНЫ И ПРОВЕРКИ =======================
def stars_for(rub: int) -> int:
    return 0 if rub <= 0 else max(1, math.ceil(rub / STAR_RATE))


def tail_from_input(s: str) -> str:
    """Цифры после префикса магазина (пользователь мог ввести и с префиксом)."""
    d = re.sub(r"\D", "", s)
    if PDIGITS and d.startswith(PDIGITS) and len(d) > len(PDIGITS):
        d = d[len(PDIGITS):]
    return d


def normalize_number(s: str) -> str:
    """Полный номер с кодом страны, только цифры (так номер хранится в базе)."""
    return re.sub(r"\D", "", s)


def valid_tail(d: str) -> bool:
    return d.isdigit() and 2 <= len(d) <= 8


def valid_full(d: str) -> bool:
    return d.isdigit() and 5 <= len(d) <= 15


def fmt_number(canon: str) -> str:
    for p in (PDIGITS, FDIGITS):
        if p and canon.startswith(p):
            return f"+{p} {canon[len(p):]}"
    return "+" + canon


def is_pretty(d: str) -> bool:
    if len(set(d)) == 1:
        return True
    asc = "0123456789"
    if d in asc or d in asc[::-1]:
        return True
    zeros = d.count("0")
    if zeros >= 3 and zeros * 2 >= len(d):
        return True
    return len(d) >= 3 and d == d[::-1]


def number_price(d: str) -> int:
    base = NUM_PRICES.get(len(d), NUM_DEFAULT)
    return base + (NUM_PRETTY_EXTRA if is_pretty(d) else 0)


def nick_price(nick: str, kind: str) -> int:
    base = NICK_PRICES.get(len(nick), NICK_DEFAULT)
    return base + (NICK_BOT_EXTRA if kind == "bot" else 0)


NICK_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
TARGET_RE = re.compile(r"^@?([A-Za-z0-9_]{3,32})$")


def available(kind: str, item: str) -> bool:
    with conn() as c:
        if kind == "number":
            return c.execute("SELECT 1 AS x FROM numbers WHERE number=?", (item,)).fetchone() is None
        if kind == "nick":
            return c.execute("SELECT 1 AS x FROM handles WHERE name=?", (item,)).fetchone() is None
    return True


def random_number():
    for _ in range(50):
        tail = "".join(random.choices("0123456789", k=random.randint(5, 8)))
        d = PDIGITS + tail
        if available("number", d):
            return d
    return None


def free_number_of(uid: int):
    with conn() as c:
        return c.execute(
            "SELECT item FROM orders WHERE tg_id=? AND method='free' AND status='paid'", (uid,)
        ).fetchone()


# ======================= ЗАКАЗЫ =======================
# ======================= ЗАКАЗЫ =======================
def create_order(uid, kind, item, extra, extra2, rub) -> int:
    with conn() as c:
        row = c.execute(
            "INSERT INTO orders(tg_id, kind, item, extra, extra2, rub, stars, status, created) "
            "VALUES(?,?,?,?,?,?,?,?,?) RETURNING id",
            (uid, kind, item, extra, extra2, rub, stars_for(rub), "pending", time.time()),
        ).fetchone()
    return row["id"]


def get_order(oid: int):
    with conn() as c:
        return c.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()


def set_order_status(oid: int, status: str):
    with conn() as c:
        c.execute("UPDATE orders SET status=? WHERE id=? AND status='pending'", (status, oid))


def item_title(o) -> str:
    k = o["kind"]
    if k == "number":
        return f"Номер {fmt_number(o['item'])}"
    if k == "nick":
        return f"Юзернейм @{o['item']}" + (" (на бота)" if o["extra"] == "bot" else "")
    tgt = f" @{o['extra2']}" if o["extra2"] else ""
    return f"Метка {o['item']} — {KIND_NAMES[o['extra']]}{tgt}"


def deliver(oid: int, method: str, charge=None) -> str:
    """Выдаёт товар. Возвращает: ok / taken / done / bad."""
    o = get_order(oid)
    if not o:
        return "bad"
    if o["status"] == "paid":
        return "done"
    if o["status"] != "pending":
        return "bad"
    now = time.time()
    try:
        with conn() as c:
            if o["kind"] == "number":
                c.execute(
                    "INSERT INTO numbers(number, owner, created) VALUES(?,?,?)", (o["item"], o["tg_id"], now)
                )
            elif o["kind"] == "nick":
                c.execute(
                    "INSERT INTO handles(name, owner, kind, created) VALUES(?,?,?,?)",
                    (o["item"], o["tg_id"], o["extra"], now),
                )
            else:
                c.execute(
                    "INSERT INTO marks(owner, mark, kind, target, created) VALUES(?,?,?,?,?)",
                    (o["tg_id"], o["item"], o["extra"], o["extra2"] or str(o["tg_id"]), now),
                )
            c.execute(
                "UPDATE orders SET status='paid', method=?, charge=?, paid=? WHERE id=?",
                (method, charge, now, oid),
            )
    except IntegrityError:
        with conn() as c:
            c.execute("UPDATE orders SET status='failed' WHERE id=?", (oid,))
        return "taken"
    return "ok"


def issue_free_number(uid: int):
    for _ in range(50):
        tail = "".join(random.choices("0123456789", k=8))
        if is_pretty(tail):
            continue
        canon = FDIGITS + tail
        if not available("number", canon):
            continue
        oid = create_order(uid, "number", canon, "", "", 0)
        if deliver(oid, "free") == "ok":
            return canon
    return None


# ======================= КОДЫ ВХОДА =======================
def issue_code(number: str) -> str:
    code = f"{secrets.randbelow(10**6):06d}"
    with conn() as c:
        c.execute("INSERT INTO codes(number, code, created) VALUES(?,?,?)", (number, code, time.time()))
    return code


def start_login(digits: str):
    """Для API мессенджера: создать код входа и узнать, кому его отправить."""
    with conn() as c:
        n = c.execute("SELECT owner FROM numbers WHERE number=?", (digits,)).fetchone()
        if not n:
            return "nonumber", None, None
        last = c.execute(
            "SELECT created FROM codes WHERE number=? ORDER BY id DESC LIMIT 1", (digits,)
        ).fetchone()
        if last and time.time() - last["created"] < 20:
            return "rate", None, None
    u = get_user(n["owner"])
    if u and u["banned"]:
        return "banned", None, None
    return "ok", n["owner"], issue_code(digits)


def verify_login(digits: str, code: str):
    """Проверяет код. Возвращает tg_id владельца или None. Код одноразовый."""
    with conn() as c:
        row = c.execute(
            "SELECT id, code, attempts FROM codes WHERE number=? AND used=0 AND created>? "
            "ORDER BY id DESC LIMIT 1",
            (digits, time.time() - CODE_TTL),
        ).fetchone()
        if not row:
            return None
        if not secrets.compare_digest(row["code"], code):
            if row["attempts"] + 1 >= 5:
                c.execute("UPDATE codes SET used=1 WHERE id=?", (row["id"],))
            else:
                c.execute("UPDATE codes SET attempts=attempts+1 WHERE id=?", (row["id"],))
            return None
        c.execute("UPDATE codes SET used=1 WHERE id=?", (row["id"],))
        owner = c.execute("SELECT owner FROM numbers WHERE number=?", (digits,)).fetchone()
    return owner["owner"] if owner else None


# ======================= ИНТЕРФЕЙС =======================
def b(text: str, data: str):
    return InlineKeyboardButton(text=text, callback_data=data)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def main_kb(uid: int):
    rows = [
        [b(f"🎁 Бесплатный номер {FREE_PREFIX}", "free_num")],
        [b("🛒 Магазин", "shop")],
        [b("👤 Профиль", "profile"), b("🔐 Мои номера", "mynums")],
    ]
    if DOWNLOAD_URL:
        rows.append([InlineKeyboardButton(text="📱 Скачать " + APP_NAME, url=DOWNLOAD_URL)])
    rows.append([b("ℹ️ О боте", "about")])
    if uid in ADMIN_IDS:
        rows.append([b("🛠 Админ-панель", "admin")])
    return kb(rows)


def menu_text() -> str:
    return (
        f"✨ <b>{html.escape(APP_NAME)}</b>\n\n"
        f"<blockquote>🎁 Бесплатный номер {FREE_PREFIX} — один на пользователя\n"
        "🛒 Магазин — номера, юзернеймы и верификации\n"
        "🔐 Мои номера — список ваших номеров и коды входа\n"
        "👤 Профиль — ваш профиль</blockquote>"
    )


BACK = [b("⬅️ В меню", "menu")]
BACK_SHOP = [b("⬅️ Назад", "shop")]


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


def order_card(oid: int):
    o = get_order(oid)
    text = (
        f"🧾 <b>Заказ №{oid}</b>\n\n"
        f"{html.escape(item_title(o))}\n\n"
        f"💳 К оплате: <b>{o['rub']} ₽</b> или <b>{o['stars']} ⭐</b>\n\n"
        "Выберите способ оплаты:"
    )
    markup = kb(
        [
            [b(f"⭐ Оплатить {o['stars']} ⭐", f"pay_star:{oid}")],
            [b(f"💳 Оплатить {o['rub']} ₽ (СБП / карта)", f"pay_rub:{oid}")],
            [b("❌ Отменить", f"cancel:{oid}")],
        ]
    )
    return text, markup


# ======================= МЕНЮ =======================
@dp.message(CommandStart())
async def start(m: Message):
    u = ensure_user(m.from_user.id, m.from_user.username)
    if u["banned"]:
        return await m.answer("⛔ Доступ закрыт.")
    WAIT.pop(m.from_user.id, None)
    await m.answer(menu_text(), reply_markup=main_kb(m.from_user.id), parse_mode="HTML")


@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"🆔 Ваш ID: <code>{m.from_user.id}</code>", parse_mode="HTML")


@dp.callback_query(F.data == "menu")
async def menu(c: CallbackQuery):
    if not await guard(c):
        return
    WAIT.pop(c.from_user.id, None)
    await show(c, menu_text(), main_kb(c.from_user.id))


@dp.callback_query(F.data == "about")
async def about(c: CallbackQuery):
    if not await guard(c):
        return
    text = (
        "ℹ️ <b>О боте</b>\n\n"
        f"Бот авторизации и магазин {html.escape(APP_NAME)}.\n"
        f"Номера, юзернеймы и метки — внутренние, они работают только в {html.escape(APP_NAME)}.\n\n"
        f"Оплата: звёзды Telegram ⭐ или рубли (СБП / карта).\n"
        f"Связь и вопросы: {SUPPORT}"
    )
    await show(c, text, kb([[InlineKeyboardButton(text="💬 Написать " + SUPPORT, url="https://t.me/" + SUPPORT[1:])], BACK]))


@dp.callback_query(F.data == "profile")
async def profile(c: CallbackQuery):
    u = await guard(c)
    if not u:
        return
    uid = u["tg_id"]
    with conn() as con:
        handles = con.execute("SELECT * FROM handles WHERE owner=? ORDER BY id", (uid,)).fetchall()
        nums = con.execute("SELECT COUNT(*) AS n FROM numbers WHERE owner=?", (uid,)).fetchone()["n"]
        marks = con.execute("SELECT * FROM marks WHERE owner=? ORDER BY id", (uid,)).fetchall()
    h = ", ".join(f"@{x['name']}" + (" (бот)" if x["kind"] == "bot" else "") for x in handles) or "—"
    m = ", ".join(f"{x['mark']} ({KIND_NAMES[x['kind']]})" for x in marks) or "—"
    text = (
        "👤 <b>Профиль</b>\n\n"
        f"🆔 ID: <code>{uid}</code>\n"
        f"💬 Юзернеймы: {html.escape(h)}\n"
        f"✅ Метки: {html.escape(m)}\n"
        f"🔐 Номеров: {nums}"
    )
    await show(c, text, kb([BACK]))


# ======================= МАГАЗИН =======================
@dp.callback_query(F.data == "shop")
async def shop(c: CallbackQuery):
    if not await guard(c):
        return
    WAIT.pop(c.from_user.id, None)
    text = (
        "🛒 <b>Магазин</b>\n\n"
        "<blockquote>✅ Верификации — метки рядом с именем\n"
        "💎 Юзернеймы — короткие имена\n"
        f"🔒 Номера {NUMBER_PREFIX} — внутренний номер входа</blockquote>"
    )
    await show(
        c,
        text,
        kb(
            [
                [b("✅ Верификации", "sh_mark")],
                [b("💎 Юзернеймы", "sh_nick"), b(f"🔒 Номера {NUMBER_PREFIX}", "sh_num")],
                BACK,
            ]
        ),
    )


# ----- Верификации -----
@dp.callback_query(F.data == "sh_mark")
async def sh_mark(c: CallbackQuery):
    if not await guard(c):
        return
    WAIT.pop(c.from_user.id, None)
    lines = "\n".join(
        f"• {name} — аккаунт {p['acc']} ₽ · канал {p['chan']} ₽ · бот {p['bot']} ₽" for name, p in MARKS.items()
    )
    text = (
        "✅ <b>Верификации</b>\n\n"
        f"Метка — значок верификации рядом с именем в {html.escape(APP_NAME)}.\n\n"
        f"<blockquote>{lines}</blockquote>\n\nКакую метку хотите?"
    )
    await show(c, text, kb([[b(name, f"mark:{name}") for name in MARKS], BACK_SHOP]))


@dp.callback_query(F.data.startswith("mark:"))
async def mark_pick(c: CallbackQuery):
    if not await guard(c):
        return
    name = c.data.split(":")[1]
    if name not in MARKS:
        return await c.answer()
    p = MARKS[name]
    await show(
        c,
        f"✅ <b>{name}</b>\n\nДля чего нужна метка?",
        kb(
            [
                [b(f"👤 Аккаунт — {p['acc']} ₽", f"mk:{name}:acc")],
                [b(f"📢 Канал — {p['chan']} ₽", f"mk:{name}:chan")],
                [b(f"🤖 Бот — {p['bot']} ₽", f"mk:{name}:bot")],
                [b("⬅️ Назад", "sh_mark")],
            ]
        ),
    )


@dp.callback_query(F.data.startswith("mk:"))
async def mark_kind(c: CallbackQuery):
    u = await guard(c)
    if not u:
        return
    _, name, kind = c.data.split(":")
    if name not in MARKS or kind not in KIND_NAMES:
        return await c.answer()
    if kind == "acc":
        oid = create_order(u["tg_id"], "mark", name, "acc", "", MARKS[name]["acc"])
        text, markup = order_card(oid)
        return await show(c, text, markup)
    WAIT[u["tg_id"]] = ("mark_target", name, kind)
    what = "канала" if kind == "chan" else "бота"
    await show(c, f"✏️ Пришлите @юзернейм {what}, для которого нужна метка {name}.", kb([BACK_SHOP]))


# ----- Юзернеймы -----
@dp.callback_query(F.data == "sh_nick")
async def sh_nick(c: CallbackQuery):
    u = await guard(c)
    if not u:
        return
    WAIT[u["tg_id"]] = ("nick",)
    text = (
        "💎 <b>Юзернеймы</b>\n\n"
        f"<blockquote>Короткий юзернейм для вашего аккаунта или бота в {html.escape(APP_NAME)}.\n"
        f"• 1 символ — {NICK_PRICES[1]} ₽\n"
        f"• 2 символа — {NICK_PRICES[2]} ₽\n"
        f"• 3 символа — {NICK_PRICES[3]} ₽\n"
        f"• 4 символа — {NICK_PRICES[4]} ₽\n"
        f"• 5 и больше — {NICK_DEFAULT} ₽\n"
        f"• на бота — +{NICK_BOT_EXTRA} ₽</blockquote>\n\n"
        "✏️ Пришлите желаемый юзернейм: латиница, цифры и «_», начинается с буквы. Например: @static"
    )
    await show(c, text, kb([BACK_SHOP]))


@dp.callback_query(F.data.startswith("nk:"))
async def nick_kind(c: CallbackQuery):
    u = await guard(c)
    if not u:
        return
    kind = c.data.split(":")[1]
    st = WAIT.get(u["tg_id"])
    if not st or st[0] != "nick_pick" or kind not in ("acc", "bot"):
        return await c.answer("Начните заново", show_alert=True)
    nick = st[1]
    if not available("nick", nick):
        WAIT.pop(u["tg_id"], None)
        return await c.answer("Этот юзернейм уже занят", show_alert=True)
    WAIT.pop(u["tg_id"], None)
    oid = create_order(u["tg_id"], "nick", nick, kind, "", nick_price(nick, kind))
    text, markup = order_card(oid)
    await show(c, text, markup)


# ----- Номера -----
@dp.callback_query(F.data == "sh_num")
async def sh_num(c: CallbackQuery):
    u = await guard(c)
    if not u:
        return
    WAIT[u["tg_id"]] = ("num",)
    p = NUM_PRICES
    text = (
        f"🔒 <b>Номера {NUMBER_PREFIX}</b>\n\n"
        f"<blockquote>Внутренний номер входа в {html.escape(APP_NAME)}: его можно надеть на свой аккаунт "
        "(он станет номером входа) или передать. Это не номер Telegram и не SIM-номер.\n"
        f"• 2 цифры после {NUMBER_PREFIX} — {p[2]} ₽\n"
        f"• 3 цифры — {p[3]} ₽\n"
        f"• 4 цифры — {p[4]} ₽\n"
        f"• 5–8 цифр — {NUM_DEFAULT} ₽\n"
        f"• красивый номер — +{NUM_PRETTY_EXTRA} ₽:\n"
        "  – все цифры одинаковые\n  – цифры подряд\n  – много нулей\n  – палиндром</blockquote>\n\n"
        f"✏️ Пришлите желаемые цифры после {NUMBER_PREFIX} (от 2 до 8), например 1234 → "
        f"{NUMBER_PREFIX} 1234, или нажмите «Случайный номер»."
    )
    await show(c, text, kb([[b("🎲 Случайный номер", "sh_rand")], BACK_SHOP]))


@dp.callback_query(F.data == "sh_rand")
async def sh_rand(c: CallbackQuery):
    u = await guard(c)
    if not u:
        return
    d = random_number()
    if not d:
        return await c.answer("Не удалось подобрать номер, попробуйте ещё раз", show_alert=True)
    WAIT.pop(u["tg_id"], None)
    oid = create_order(u["tg_id"], "number", d, "", "", number_price(d[len(PDIGITS):]))
    text, markup = order_card(oid)
    await show(c, text, markup)


# ----- Ввод текста (юзернейм / цифры / цель метки) -----
@dp.message(F.text & ~F.text.startswith("/"))
async def text_input(m: Message):
    uid = m.from_user.id
    st = WAIT.get(uid)
    if not st:
        return
    u = ensure_user(uid, m.from_user.username)
    if u["banned"]:
        return
    raw = m.text.strip()

    if st[0] == "nick":
        nick = raw.lstrip("@").lower()
        if not NICK_RE.match(nick):
            return await m.answer("Неверный формат. Латиница, цифры и «_», начинается с буквы, до 32 символов.")
        if not available("nick", nick):
            return await m.answer("Этот юзернейм занят. Пришлите другой.")
        WAIT[uid] = ("nick_pick", nick)
        return await m.answer(
            f"💎 Юзернейм @{html.escape(nick)} свободен!\nНа что его надеть?",
            reply_markup=kb(
                [
                    [b(f"👤 На аккаунт — {nick_price(nick, 'acc')} ₽", "nk:acc")],
                    [b(f"🤖 На бота — {nick_price(nick, 'bot')} ₽", "nk:bot")],
                    BACK_SHOP,
                ]
            ),
            parse_mode="HTML",
        )

    if st[0] == "num":
        tail = tail_from_input(raw)
        if not valid_tail(tail):
            return await m.answer("Нужно от 2 до 8 цифр, например 1234.")
        canon = PDIGITS + tail
        if not available("number", canon):
            return await m.answer("Этот номер занят. Пришлите другой.")
        WAIT.pop(uid, None)
        oid = create_order(uid, "number", canon, "", "", number_price(tail))
        text, markup = order_card(oid)
        return await m.answer(text, reply_markup=markup, parse_mode="HTML")

    if st[0] == "mark_target":
        mt = TARGET_RE.match(raw)
        if not mt:
            return await m.answer("Пришлите @юзернейм (латиница, цифры, «_», от 3 символов).")
        _, name, kind = st
        WAIT.pop(uid, None)
        oid = create_order(uid, "mark", name, kind, mt.group(1), MARKS[name][kind])
        text, markup = order_card(oid)
        return await m.answer(text, reply_markup=markup, parse_mode="HTML")


@dp.callback_query(F.data == "free_num")
async def free_num(c: CallbackQuery):
    u = await guard(c)
    if not u:
        return
    have = free_number_of(u["tg_id"])
    if have:
        text = (
            f"🎁 Ваш бесплатный номер: <code>{fmt_number(have['item'])}</code>\n\n"
            "Код входа — в разделе «Мои номера»."
        )
    else:
        canon = issue_free_number(u["tg_id"])
        if not canon:
            return await c.answer("Не удалось выдать номер, попробуйте ещё раз", show_alert=True)
        log(u["tg_id"], f"free_number {canon}")
        text = (
            f"🎁 Готово! Ваш бесплатный номер: <code>{fmt_number(canon)}</code>\n\n"
            f"Это внутренний номер входа в {html.escape(APP_NAME)} (не SIM-номер, СМС на него не приходят). "
            "Код входа — в разделе «Мои номера»."
        )
    await show(c, text, kb([[b("🔐 Мои номера", "mynums")], BACK]))


# ======================= ОПЛАТА =======================
async def own_order(c: CallbackQuery, oid: int):
    o = get_order(oid)
    if not o or o["tg_id"] != c.from_user.id or o["status"] != "pending":
        await c.answer("Заказ недоступен", show_alert=True)
        return None
    if o["kind"] in ("number", "nick") and not available(o["kind"], o["item"]):
        set_order_status(oid, "failed")
        await c.answer("Этот товар уже занят", show_alert=True)
        return None
    return o


@dp.callback_query(F.data.startswith("cancel:"))
async def cancel(c: CallbackQuery):
    if not await guard(c):
        return
    oid = int(c.data.split(":")[1])
    o = get_order(oid)
    if o and o["tg_id"] == c.from_user.id:
        set_order_status(oid, "cancelled")
    await show(c, "Заказ отменён.", kb([[b("🛒 Магазин", "shop")], BACK]))


@dp.callback_query(F.data.startswith("pay_star:"))
async def pay_star(c: CallbackQuery, bot: Bot):
    if not await guard(c):
        return
    oid = int(c.data.split(":")[1])
    o = await own_order(c, oid)
    if not o:
        return
    await bot.send_invoice(
        chat_id=c.from_user.id,
        title=item_title(o)[:32],
        description=f"Заказ №{oid}. Товар выдаётся автоматически после оплаты."[:255],
        payload=f"order:{oid}",
        currency="XTR",
        prices=[LabeledPrice(label=item_title(o)[:32], amount=o["stars"])],
    )
    await c.answer()


@dp.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    try:
        oid = int(q.invoice_payload.split(":")[1])
    except (IndexError, ValueError):
        return await q.answer(ok=False, error_message="Неверный заказ")
    o = get_order(oid)
    if not o or o["status"] != "pending" or o["tg_id"] != q.from_user.id:
        return await q.answer(ok=False, error_message="Заказ недоступен")
    if o["kind"] in ("number", "nick") and not available(o["kind"], o["item"]):
        return await q.answer(ok=False, error_message="Этот товар уже занят")
    await q.answer(ok=True)


async def notify_admins(bot: Bot, text: str, markup=None):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text, reply_markup=markup, parse_mode="HTML")
        except Exception:
            pass


@dp.message(F.successful_payment)
async def paid(m: Message, bot: Bot):
    sp = m.successful_payment
    try:
        oid = int(sp.invoice_payload.split(":")[1])
    except (IndexError, ValueError):
        return
    res = deliver(oid, "stars", sp.telegram_payment_charge_id)
    o = get_order(oid)
    if res == "ok":
        log(m.from_user.id, f"paid_stars order={oid} stars={sp.total_amount}")
        await m.answer(
            f"✅ Оплата получена! {html.escape(item_title(o))} теперь ваш.\n"
            "Номера и коды входа — в разделе «Мои номера».",
            reply_markup=main_kb(m.from_user.id),
            parse_mode="HTML",
        )
        await notify_admins(bot, f"⭐ Оплата: заказ №{oid}, {html.escape(item_title(o))}, {sp.total_amount} ⭐, user {m.from_user.id}")
    elif res == "taken":
        try:
            await bot.refund_star_payment(user_id=m.from_user.id, telegram_payment_charge_id=sp.telegram_payment_charge_id)
            await m.answer("Товар успели купить раньше. Звёзды возвращены.")
        except Exception:
            await m.answer(f"Товар уже занят. Напишите {SUPPORT} — вернём оплату.")
        await notify_admins(bot, f"⚠️ Заказ №{oid}: товар занят, оплата звёздами возвращена/требует проверки.")


@dp.callback_query(F.data.startswith("pay_rub:"))
async def pay_rub(c: CallbackQuery, bot: Bot):
    u = await guard(c)
    if not u:
        return
    oid = int(c.data.split(":")[1])
    o = await own_order(c, oid)
    if not o:
        return
    text = (
        f"💳 <b>Оплата рублями — заказ №{oid}</b>\n\n"
        f"{html.escape(item_title(o))}\nСумма: <b>{o['rub']} ₽</b>\n\n"
        f"Напишите {SUPPORT}, укажите номер заказа и получите реквизиты (СБП / карта). "
        "После подтверждения оплаты товар придёт сюда автоматически."
    )
    await show(
        c,
        text,
        kb(
            [
                [InlineKeyboardButton(text="💬 Написать " + SUPPORT, url="https://t.me/" + SUPPORT[1:])],
                [b("⬅️ К заказу", f"order:{oid}")],
            ]
        ),
    )
    log(u["tg_id"], f"pay_rub_request order={oid}")
    uname = f"@{c.from_user.username}" if c.from_user.username else str(c.from_user.id)
    await notify_admins(
        bot,
        f"💳 Заказ №{oid} (рубли)\n{html.escape(item_title(o))}\n{o['rub']} ₽ — {html.escape(uname)} ({c.from_user.id})",
        kb([[b("✅ Подтвердить оплату", f"ok:{oid}"), b("❌ Отклонить", f"no:{oid}")]]),
    )


@dp.callback_query(F.data.startswith("order:"))
async def order_view(c: CallbackQuery):
    if not await guard(c):
        return
    oid = int(c.data.split(":")[1])
    o = get_order(oid)
    if not o or o["tg_id"] != c.from_user.id or o["status"] != "pending":
        return await c.answer("Заказ недоступен", show_alert=True)
    text, markup = order_card(oid)
    await show(c, text, markup)


async def confirm_order(oid: int, bot: Bot):
    res = deliver(oid, "rub")
    o = get_order(oid)
    if res == "ok":
        log(0, f"confirm_rub order={oid}")
        try:
            await bot.send_message(
                o["tg_id"],
                f"✅ Оплата подтверждена! {html.escape(item_title(o))} теперь ваш.\n"
                "Номера и коды входа — в разделе «Мои номера».",
                parse_mode="HTML",
            )
        except Exception:
            pass
    return res


@dp.callback_query(F.data.startswith("ok:"))
async def admin_ok(c: CallbackQuery, bot: Bot):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("Нет доступа", show_alert=True)
    oid = int(c.data.split(":")[1])
    res = await confirm_order(oid, bot)
    msg = {"ok": "✅ Выдано", "done": "Уже выдано", "taken": "⚠️ Товар уже занят", "bad": "Заказ недоступен"}[res]
    await c.message.answer(f"Заказ №{oid}: {msg}")
    await c.answer()


@dp.callback_query(F.data.startswith("no:"))
async def admin_no(c: CallbackQuery, bot: Bot):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("Нет доступа", show_alert=True)
    oid = int(c.data.split(":")[1])
    o = get_order(oid)
    set_order_status(oid, "cancelled")
    if o:
        try:
            await bot.send_message(o["tg_id"], f"Заказ №{oid} отклонён. Вопросы: {SUPPORT}")
        except Exception:
            pass
    await c.message.answer(f"Заказ №{oid} отклонён")
    await c.answer()


@dp.message(Command("confirm"))
async def cmd_confirm(m: Message, bot: Bot):
    if m.from_user.id not in ADMIN_IDS:
        return
    try:
        oid = int(m.text.split()[1])
    except (IndexError, ValueError):
        return await m.answer("Использование: /confirm НОМЕР_ЗАКАЗА")
    res = await confirm_order(oid, bot)
    await m.answer({"ok": "✅ Выдано", "done": "Уже выдано", "taken": "⚠️ Товар уже занят", "bad": "Заказ недоступен"}[res])


# ======================= МОИ НОМЕРА И КОДЫ =======================
@dp.callback_query(F.data == "mynums")
async def mynums(c: CallbackQuery):
    u = await guard(c)
    if not u:
        return
    with conn() as con:
        rows = con.execute("SELECT * FROM numbers WHERE owner=? ORDER BY id", (u["tg_id"],)).fetchall()
    if not rows:
        return await show(
            c, "🔐 У вас пока нет номеров.", kb([[b(f"🔒 Купить номер {NUMBER_PREFIX}", "sh_num")], BACK])
        )
    buttons = [[b(fmt_number(r["number"]), f"num:{r['id']}")] for r in rows]
    buttons.append(BACK)
    await show(c, "🔐 <b>Мои номера</b>\nВыберите номер:", kb(buttons))


def owned_number(uid: int, nid: int):
    with conn() as c:
        return c.execute("SELECT * FROM numbers WHERE id=? AND owner=?", (nid, uid)).fetchone()


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
        f"🔐 Номер <code>{fmt_number(n['number'])}</code>",
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
        f"🔑 Код для <code>{fmt_number(n['number'])}</code>:\n\n<code>{code}</code>\n\n"
        f"⏳ Действует {CODE_TTL // 60} мин. Никому не сообщайте.",
        kb([[b("🔄 Новый код", f"code:{n['id']}")], [b("⬅️ Мои номера", "mynums")]]),
    )


# ======================= АДМИН-ПАНЕЛЬ =======================
def admin_kb():
    return kb(
        [
            [b("📊 Статистика", "a_stats")],
            [b("🧾 Ожидают оплаты (₽)", "a_orders")],
            [b("👥 Пользователи", "a_users")],
            [b("📜 Логи", "a_logs")],
            BACK,
        ]
    )


ADMIN_HELP = "🛠 Админ-панель\n\nКоманды: /confirm №, /ban ID, /unban ID, /broadcast текст"


@dp.message(Command("admin"))
async def admin_cmd(m: Message):
    if m.from_user.id in ADMIN_IDS:
        await m.answer(ADMIN_HELP, reply_markup=admin_kb())


@dp.callback_query(F.data == "admin")
async def admin_cb(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("Нет доступа", show_alert=True)
    await show(c, ADMIN_HELP, admin_kb())


@dp.callback_query(F.data == "a_stats")
async def a_stats(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("Нет доступа", show_alert=True)
    with conn() as con:
        users = con.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        banned = con.execute("SELECT COUNT(*) AS n FROM users WHERE banned=1").fetchone()["n"]
        paid_n = con.execute("SELECT COUNT(*) AS n FROM orders WHERE status='paid' AND method<>'free'").fetchone()["n"]
        stars = con.execute(
            "SELECT COALESCE(SUM(stars),0) AS s FROM orders WHERE status='paid' AND method='stars'"
        ).fetchone()["s"]
        rub = con.execute(
            "SELECT COALESCE(SUM(rub),0) AS s FROM orders WHERE status='paid' AND method='rub'"
        ).fetchone()["s"]
        nums = con.execute("SELECT COUNT(*) AS n FROM numbers").fetchone()["n"]
    await c.message.answer(
        f"📊 Пользователей: {users}\n⛔ Забанено: {banned}\n🧾 Оплаченных заказов: {paid_n}\n"
        f"⭐ Получено звёзд: {stars}\n💳 Получено рублей: {rub}\n🔒 Номеров выдано: {nums}"
    )
    await c.answer()


@dp.callback_query(F.data == "a_orders")
async def a_orders(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("Нет доступа", show_alert=True)
    with conn() as con:
        rows = con.execute("SELECT * FROM orders WHERE status='pending' ORDER BY id DESC LIMIT 15").fetchall()
    if not rows:
        await c.message.answer("Нет заказов в ожидании.")
    for o in rows:
        await c.message.answer(
            f"№{o['id']} | {html.escape(item_title(o))} | {o['rub']} ₽ | user {o['tg_id']}",
            reply_markup=kb([[b("✅ Подтвердить", f"ok:{o['id']}"), b("❌ Отклонить", f"no:{o['id']}")]]),
            parse_mode="HTML",
        )
    await c.answer()


@dp.callback_query(F.data == "a_users")
async def a_users(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
        return await c.answer("Нет доступа", show_alert=True)
    with conn() as con:
        rows = con.execute("SELECT * FROM users ORDER BY created DESC LIMIT 20").fetchall()
    text = "\n".join(f"{r['tg_id']} | @{r['username'] or '-'}{' ⛔' if r['banned'] else ''}" for r in rows) or "Пусто"
    await c.message.answer(f"👥 Последние 20:\n{text}")
    await c.answer()


@dp.callback_query(F.data == "a_logs")
async def a_logs(c: CallbackQuery):
    if c.from_user.id not in ADMIN_IDS:
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
    if m.from_user.id not in ADMIN_IDS:
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
    if m.from_user.id not in ADMIN_IDS:
        return
    try:
        uid = int(m.text.split()[1])
    except (IndexError, ValueError):
        return await m.answer("Использование: /unban ID")
    with conn() as c:
        c.execute("UPDATE users SET banned=0 WHERE tg_id=?", (uid,))
    log(m.from_user.id, f"unban {uid}")
    await m.answer(f"✅ {uid} разбанен")


@dp.message(Command("broadcast"))
async def broadcast(m: Message, bot: Bot):
    if m.from_user.id not in ADMIN_IDS:
        return
    text = m.text.partition(" ")[2].strip()
    if not text:
        return await m.answer("Использование: /broadcast текст")
    with conn() as c:
        ids = [r["tg_id"] for r in c.execute("SELECT tg_id FROM users WHERE banned=0").fetchall()]
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
