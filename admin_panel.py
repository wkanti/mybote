"""
Админка + логи для Telegram-бота (aiogram 3.x, SQLite).

Подключение в main.py:
    import admin_panel
    await admin_panel.init_db()
    dp.include_router(admin_panel.router)
    dp.message.outer_middleware(admin_panel.BanMiddleware())
    dp.callback_query.outer_middleware(admin_panel.BanMiddleware())

Если у тебя уже есть таблица users - подправь названия колонок
в init_db() (user_id, stars, banned).
"""
import time
from html import escape

import aiosqlite
from aiogram import BaseMiddleware, Bot, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import ADMIN_IDS, DB_PATH, LOG_CHAT_ID

PAGE_SIZE = 10

router = Router()
router.message.filter(F.from_user.id.in_(ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(ADMIN_IDS))


# ---------- БД ----------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """CREATE TABLE IF NOT EXISTS users(
                user_id INTEGER PRIMARY KEY,
                stars INTEGER DEFAULT 0,
                banned INTEGER DEFAULT 0,
                ban_reason TEXT)"""
        )
        # на случай если таблица уже была без этих колонок
        for col in ("stars INTEGER DEFAULT 0", "banned INTEGER DEFAULT 0", "ban_reason TEXT"):
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except aiosqlite.OperationalError:
                pass
        await db.execute(
            """CREATE TABLE IF NOT EXISTS admin_logs(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER,
                admin_id INTEGER,
                admin_name TEXT,
                action TEXT,
                target_id INTEGER,
                details TEXT)"""
        )
        await db.commit()


async def log_action(bot: Bot, admin, action: str, target_id: int, details: str = ""):
    name = f"@{admin.username}" if admin.username else admin.full_name
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO admin_logs(ts,admin_id,admin_name,action,target_id,details) VALUES(?,?,?,?,?,?)",
            (int(time.time()), admin.id, name, action, target_id, details),
        )
        await db.commit()
    if LOG_CHAT_ID:
        text = (
            f"📋 <b>{escape(action)}</b>\n"
            f"👮 Админ: {escape(name)} (<code>{admin.id}</code>)\n"
            f"🎯 Юзер: <code>{target_id}</code>\n"
            f"📝 {escape(details)}"
        )
        try:
            await bot.send_message(LOG_CHAT_ID, text, parse_mode="HTML")
        except Exception:
            pass


# ---------- Бан-мидлварь ----------
class BanMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user = getattr(event, "from_user", None)
        if user and user.id not in ADMIN_IDS:
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute("SELECT banned FROM users WHERE user_id=?", (user.id,))
                row = await cur.fetchone()
            if row and row[0]:
                if isinstance(event, CallbackQuery):
                    await event.answer("Вы заблокированы", show_alert=True)
                else:
                    await event.answer("⛔ Вы заблокированы.")
                return
        return await handler(event, data)


# ---------- Меню ----------
class AdminFSM(StatesGroup):
    waiting = State()


PROMPTS = {
    "stars_add": "Введите: <code>ID сумма</code>\nНапример: <code>123456 50</code>",
    "stars_sub": "Введите: <code>ID сумма</code>\nНапример: <code>123456 50</code>",
    "ban": "Введите: <code>ID причина</code>\nНапример: <code>123456 спам</code>",
    "unban": "Введите: <code>ID</code>",
    "user": "Введите: <code>ID</code>",
}


def menu_kb():
    b = InlineKeyboardBuilder()
    b.button(text="⭐ Выдать звёзды", callback_data="adm:stars_add")
    b.button(text="➖ Забрать звёзды", callback_data="adm:stars_sub")
    b.button(text="🚫 Бан", callback_data="adm:ban")
    b.button(text="✅ Разбан", callback_data="adm:unban")
    b.button(text="👤 Инфо о юзере", callback_data="adm:user")
    b.button(text="📊 Статистика", callback_data="adm:stats")
    b.button(text="📋 Логи", callback_data="adm:logs:0")
    b.adjust(2, 2, 1, 2)
    return b.as_markup()


@router.message(Command("admin"))
async def admin_menu(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🛠 <b>Админ-панель</b>", reply_markup=menu_kb(), parse_mode="HTML")


@router.callback_query(F.data == "adm:menu")
async def back_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.edit_text("🛠 <b>Админ-панель</b>", reply_markup=menu_kb(), parse_mode="HTML")


@router.callback_query(F.data.regexp(r"^adm:(stars_add|stars_sub|ban|unban|user)$"))
async def ask_input(c: CallbackQuery, state: FSMContext):
    action = c.data.split(":")[1]
    await state.set_state(AdminFSM.waiting)
    await state.update_data(action=action)
    await c.message.answer(PROMPTS[action], parse_mode="HTML")
    await c.answer()


# ---------- Выполнение действий ----------
@router.message(AdminFSM.waiting)
async def do_action(m: Message, state: FSMContext, bot: Bot):
    action = (await state.get_data()).get("action")
    parts = (m.text or "").split(maxsplit=1)
    if not parts or not parts[0].lstrip("-").isdigit():
        return await m.answer("❌ Первым должен быть числовой ID.")
    uid = int(parts[0])
    rest = parts[1].strip() if len(parts) > 1 else ""

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users(user_id) VALUES(?)", (uid,))

        if action in ("stars_add", "stars_sub"):
            if not rest.isdigit() or int(rest) <= 0:
                return await m.answer("❌ Укажите сумму положительным числом.")
            amount = int(rest)
            if action == "stars_add":
                await db.execute("UPDATE users SET stars=stars+? WHERE user_id=?", (amount, uid))
                title, sign = "Выдача звёзд", "+"
            else:
                await db.execute("UPDATE users SET stars=MAX(stars-?,0) WHERE user_id=?", (amount, uid))
                title, sign = "Списание звёзд", "-"
            cur = await db.execute("SELECT stars FROM users WHERE user_id=?", (uid,))
            balance = (await cur.fetchone())[0]
            await db.commit()
            details = f"{sign}{amount} ⭐, баланс: {balance}"
            await log_action(bot, m.from_user, title, uid, details)
            await m.answer(f"✅ {title}: <code>{uid}</code>\n{details}", parse_mode="HTML")

        elif action == "ban":
            reason = rest or "без причины"
            await db.execute("UPDATE users SET banned=1, ban_reason=? WHERE user_id=?", (reason, uid))
            await db.commit()
            await log_action(bot, m.from_user, "Бан", uid, f"причина: {reason}")
            await m.answer(f"🚫 <code>{uid}</code> забанен. Причина: {escape(reason)}", parse_mode="HTML")

        elif action == "unban":
            await db.execute("UPDATE users SET banned=0, ban_reason=NULL WHERE user_id=?", (uid,))
            await db.commit()
            await log_action(bot, m.from_user, "Разбан", uid)
            await m.answer(f"✅ <code>{uid}</code> разбанен.", parse_mode="HTML")

        elif action == "user":
            cur = await db.execute("SELECT stars,banned,ban_reason FROM users WHERE user_id=?", (uid,))
            stars, banned, reason = await cur.fetchone()
            status = f"🚫 бан ({escape(reason or '-')})" if banned else "✅ активен"
            await m.answer(
                f"👤 <code>{uid}</code>\n⭐ Звёзды: {stars}\nСтатус: {status}", parse_mode="HTML"
            )
    await state.clear()


# ---------- Статистика ----------
@router.callback_query(F.data == "adm:stats")
async def stats(c: CallbackQuery):
    async with aiosqlite.connect(DB_PATH) as db:
        total = (await (await db.execute("SELECT COUNT(*) FROM users")).fetchone())[0]
        banned = (await (await db.execute("SELECT COUNT(*) FROM users WHERE banned=1")).fetchone())[0]
        stars = (await (await db.execute("SELECT COALESCE(SUM(stars),0) FROM users")).fetchone())[0]
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data="adm:menu")
    await c.message.edit_text(
        f"📊 <b>Статистика</b>\n👥 Юзеров: {total}\n🚫 В бане: {banned}\n⭐ Звёзд на руках: {stars}",
        reply_markup=b.as_markup(),
        parse_mode="HTML",
    )


# ---------- Просмотр логов ----------
@router.callback_query(F.data.startswith("adm:logs:"))
async def show_logs(c: CallbackQuery):
    page = int(c.data.split(":")[2])
    async with aiosqlite.connect(DB_PATH) as db:
        total = (await (await db.execute("SELECT COUNT(*) FROM admin_logs")).fetchone())[0]
        cur = await db.execute(
            "SELECT ts,admin_name,action,target_id,details FROM admin_logs "
            "ORDER BY id DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE, page * PAGE_SIZE),
        )
        rows = await cur.fetchall()

    lines = [f"📋 <b>Логи</b> (стр. {page + 1}, всего {total})\n"]
    for ts, admin, action, target, details in rows:
        t = time.strftime("%d.%m %H:%M", time.localtime(ts))
        lines.append(
            f"<code>{t}</code> {escape(admin)}\n"
            f"└ <b>{escape(action)}</b> → <code>{target}</code> {escape(details or '')}"
        )
    if not rows:
        lines.append("Пока пусто.")

    b = InlineKeyboardBuilder()
    if page > 0:
        b.button(text="◀️", callback_data=f"adm:logs:{page - 1}")
    if (page + 1) * PAGE_SIZE < total:
        b.button(text="▶️", callback_data=f"adm:logs:{page + 1}")
    b.button(text="⬅️ Меню", callback_data="adm:menu")
    b.adjust(2, 1)
    await c.message.edit_text("\n".join(lines), reply_markup=b.as_markup(), parse_mode="HTML")
