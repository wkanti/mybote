import asyncio
import logging
import os
import secrets
import sqlite3
import time

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = {8922424402}  # админ
CODE_TTL = 300  # код живёт 5 минут
DB = "bot.db"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()],
)

dp = Dispatcher()


# ---------- База ----------
def db():
    conn = sqlite3.connect(DB, timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS users(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER UNIQUE,
                username TEXT,
                number TEXT,
                banned INTEGER DEFAULT 0,
                created REAL
            );
            CREATE TABLE IF NOT EXISTS codes(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER,
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


def log(tg_id: int, action: str):
    with db() as c:
        c.execute("INSERT INTO logs(ts, tg_id, action) VALUES(?,?,?)", (time.time(), tg_id, action))
    logging.info("%s | %s", tg_id, action)


def get_user(tg_id: int):
    with db() as c:
        return c.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,)).fetchone()


def register(tg_id: int, username: str | None):
    if get_user(tg_id):
        return
    with db() as c:
        cur = c.execute(
            "INSERT INTO users(tg_id, username, created) VALUES(?,?,?)",
            (tg_id, username, time.time()),
        )
        number = f"{100000 + cur.lastrowid}"  # внутренний номер твоего мессенджера
        c.execute("UPDATE users SET number=? WHERE id=?", (number, cur.lastrowid))
    log(tg_id, "register")


def issue_code(tg_id: int) -> str:
    code = f"{secrets.randbelow(10**6):06d}"
    with db() as c:
        c.execute("INSERT INTO codes(tg_id, code, created) VALUES(?,?,?)", (tg_id, code, time.time()))
    return code


def check_code(number: str, code: str) -> bool:
    """Вызывай из бэкенда мессенджера для проверки входа."""
    with db() as c:
        row = c.execute(
            """SELECT codes.id FROM codes JOIN users ON users.tg_id = codes.tg_id
               WHERE users.number=? AND codes.code=? AND codes.used=0 AND codes.created>?""",
            (number, code, time.time() - CODE_TTL),
        ).fetchone()
        if row:
            c.execute("UPDATE codes SET used=1 WHERE id=?", (row["id"],))
            return True
    return False


# ---------- Клавиатуры ----------
def main_kb(is_admin: bool):
    rows = [
        [InlineKeyboardButton(text="📱 Получить номер и код", callback_data="get")],
        [InlineKeyboardButton(text="🆔 Мой ID", callback_data="myid")],
    ]
    if is_admin:
        rows.append([InlineKeyboardButton(text="🛠 Админ-панель", callback_data="admin")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статистика", callback_data="a_stats")],
            [InlineKeyboardButton(text="👥 Пользователи", callback_data="a_users")],
            [InlineKeyboardButton(text="📜 Логи", callback_data="a_logs")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
        ]
    )


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


# ---------- Пользовательские хэндлеры ----------
@dp.message(CommandStart())
async def start(m: Message):
    u = get_user(m.from_user.id)
    if u and u["banned"]:
        return await m.answer("⛔ Доступ закрыт.")
    register(m.from_user.id, m.from_user.username)
    await m.answer("Привет! Выбери действие:", reply_markup=main_kb(is_admin(m.from_user.id)))


@dp.callback_query(F.data == "get")
async def get_number(c: CallbackQuery):
    u = get_user(c.from_user.id)
    if not u:
        register(c.from_user.id, c.from_user.username)
        u = get_user(c.from_user.id)
    if u["banned"]:
        return await c.answer("⛔ Доступ закрыт", show_alert=True)
    code = issue_code(c.from_user.id)
    log(c.from_user.id, f"issue_code number={u['number']}")
    await c.message.answer(
        f"📱 Ваш номер: <code>{u['number']}</code>\n"
        f"🔑 Код: <code>{code}</code>\n"
        f"⏳ Действует {CODE_TTL // 60} мин.",
        parse_mode="HTML",
    )
    await c.answer()


@dp.callback_query(F.data == "myid")
async def my_id(c: CallbackQuery):
    await c.message.answer(f"🆔 Ваш ID: <code>{c.from_user.id}</code>", parse_mode="HTML")
    await c.answer()


@dp.callback_query(F.data == "back")
async def back(c: CallbackQuery):
    await c.message.edit_text("Выбери действие:", reply_markup=main_kb(is_admin(c.from_user.id)))
    await c.answer()


@dp.message(Command("id"))
async def cmd_id(m: Message):
    await m.answer(f"🆔 Ваш ID: <code>{m.from_user.id}</code>", parse_mode="HTML")


# ---------- Админ-панель ----------
@dp.message(Command("admin"))
async def admin_cmd(m: Message):
    if not is_admin(m.from_user.id):
        return
    await m.answer("🛠 Админ-панель", reply_markup=admin_kb())


@dp.callback_query(F.data == "admin")
async def admin_cb(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("Нет доступа", show_alert=True)
    await c.message.edit_text("🛠 Админ-панель", reply_markup=admin_kb())
    await c.answer()


@dp.callback_query(F.data == "a_stats")
async def a_stats(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("Нет доступа", show_alert=True)
    with db() as con:
        users = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        banned = con.execute("SELECT COUNT(*) FROM users WHERE banned=1").fetchone()[0]
        codes = con.execute("SELECT COUNT(*) FROM codes").fetchone()[0]
    await c.message.answer(f"📊 Пользователей: {users}\n⛔ Забанено: {banned}\n🔑 Выдано кодов: {codes}")
    await c.answer()


@dp.callback_query(F.data == "a_users")
async def a_users(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("Нет доступа", show_alert=True)
    with db() as con:
        rows = con.execute("SELECT * FROM users ORDER BY id DESC LIMIT 20").fetchall()
    text = "\n".join(
        f"{r['number']} | {r['tg_id']} | @{r['username'] or '-'}{' ⛔' if r['banned'] else ''}" for r in rows
    ) or "Пусто"
    await c.message.answer(f"👥 Последние 20:\n{text}")
    await c.answer()


@dp.callback_query(F.data == "a_logs")
async def a_logs(c: CallbackQuery):
    if not is_admin(c.from_user.id):
        return await c.answer("Нет доступа", show_alert=True)
    with db() as con:
        rows = con.execute("SELECT * FROM logs ORDER BY id DESC LIMIT 25").fetchall()
    text = "\n".join(
        f"{time.strftime('%d.%m %H:%M:%S', time.localtime(r['ts']))} | {r['tg_id']} | {r['action']}"
        for r in rows
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
    with db() as c:
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
    with db() as c:
        c.execute("UPDATE users SET banned=0 WHERE tg_id=?", (uid,))
    log(m.from_user.id, f"unban {uid}")
    await m.answer(f"✅ {uid} разбанен")


@dp.message(Command("broadcast"))
async def broadcast(m: Message, bot: Bot):
    if not is_admin(m.from_user.id):
        return
    text = m.text.partition(" ")[2].strip()
    if not text:
        return await m.answer("Использование: /broadcast текст")
    with db() as c:
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


async def main():
    init_db()
    bot = Bot(BOT_TOKEN)
    logging.info("Bot started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
