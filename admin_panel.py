import asyncio
import logging
import os
import tempfile
import time
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, FSInputFile, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config

log = logging.getLogger("kantimsg.admin")

router = Router()
# Всё в этом файле работает только для твоих ID из config.ADMIN_IDS
router.message.filter(F.from_user.id.in_(config.ADMIN_IDS))
router.callback_query.filter(F.from_user.id.in_(config.ADMIN_IDS))


def fmt_time(ts):
    return time.strftime("%d.%m %H:%M", time.gmtime(ts + config.TZ_OFFSET * 3600))


async def send_backup(bot, db, chat_ids=None):
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, time.strftime("kantimsg_%Y%m%d_%H%M.db"))
        db.backup_to(path)
        for cid in chat_ids or config.ADMIN_IDS:
            try:
                await bot.send_document(
                    cid, FSInputFile(path),
                    caption="💾 Копия базы. Чтобы восстановить: ответь на этот файл командой /restore\n"
                            "⚠️ В файле могут быть сессии номеров, никому его не передавай.",
                )
            except Exception:
                log.exception("Не удалось отправить бэкап")


def panel_kb():
    b = InlineKeyboardBuilder()
    b.button(text="📊 Обзор", callback_data="adm:ov")
    b.button(text="👥 Люди", callback_data="adm:people")
    b.button(text="🎁 Выдача", callback_data="adm:give")
    b.button(text="📱 Номера", callback_data="adm:nums")
    b.button(text="📢 Рассылка", callback_data="adm:bc")
    b.button(text="💾 Бэкап", callback_data="adm:backup")
    b.button(text="🏠 В главное меню", callback_data="menu")
    b.adjust(2, 2, 2, 1)
    return b.as_markup()


def back_kb():
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Панель", callback_data="adm:menu")
    return b.as_markup()


async def show(cb, text, kb):
    try:
        await cb.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise
    await cb.answer()


def overview(db):
    day = int(time.time()) - 86400

    def c(sql, args=()):
        return db.one(sql, args)[0]

    users = c("SELECT COUNT(*) FROM users")
    users_new = c("SELECT COUNT(*) FROM users WHERE joined_at>?", (day,))
    banned = c("SELECT COUNT(*) FROM users WHERE banned=1")
    free_all = c("SELECT COUNT(*) FROM numbers WHERE kind='free'")
    free_av = c("SELECT COUNT(*) FROM numbers WHERE kind='free' AND owner_id IS NULL")
    paid_all = c("SELECT COUNT(*) FROM numbers WHERE kind='paid'")
    paid_av = c("SELECT COUNT(*) FROM numbers WHERE kind='paid' AND owner_id IS NULL")
    with_sess = c("SELECT COUNT(*) FROM numbers WHERE session IS NOT NULL")
    codes = c("SELECT COUNT(*) FROM codes")
    codes_new = c("SELECT COUNT(*) FROM codes WHERE at>?", (day,))
    return (
        "📊 <b>Обзор</b>\n\n"
        f"Людей: {users} (за сутки +{users_new}, в бане {banned})\n"
        f"Номера +39: {free_all} (свободно {free_av})\n"
        f"Номера +888: {paid_all} (свободно {paid_av})\n"
        f"С автоприёмом кодов: {with_sess}\n"
        f"Кодов всего: {codes} (за сутки {codes_new})"
    )


PANEL_TEXT = "🛠 <b>Админ-панель</b>\nВладелец"


@router.message(Command("admin", "panel"))
async def open_panel(m: Message):
    await m.answer(PANEL_TEXT, reply_markup=panel_kb())


@router.callback_query(F.data.startswith("adm:"))
async def panel_cb(cb: CallbackQuery, db):
    tab = cb.data.split(":")[1]
    if tab == "menu":
        await show(cb, PANEL_TEXT, panel_kb())
    elif tab == "ov":
        await show(cb, overview(db), back_kb())
    elif tab == "people":
        rows = db.q(
            "SELECT u.id, u.username, u.name, u.stars, u.banned, "
            "(SELECT COUNT(*) FROM numbers n WHERE n.owner_id=u.id) AS nn "
            "FROM users u ORDER BY u.joined_at DESC LIMIT 15"
        )
        lines = []
        for r in rows:
            tag = f" @{escape(r['username'])}" if r["username"] else ""
            ban = " 🚫" if r["banned"] else ""
            lines.append(f"<code>{r['id']}</code> {escape(r['name'] or '')}{tag}{ban} ⭐{r['stars']} 📱{r['nn']}")
        text = "👥 <b>Люди</b> (последние 15)\n\n" + ("\n".join(lines) or "Пока никого.")
        text += "\n\n/ban ID · /unban ID · /stars ID ±число"
        await show(cb, text, back_kb())
    elif tab == "give":
        text = (
            "🎁 <b>Выдача</b>\n\n"
            "/give ID +888123456789 — выдать конкретный номер\n"
            "/give ID 888 — первый свободный +888\n"
            "/give ID 39 — первый свободный +39\n"
            "/release +39123456789 — забрать номер у человека\n"
            "/stars ID 50 — добавить звёзды (минус забирает)"
        )
        await show(cb, text, back_kb())
    elif tab == "nums":
        rows = db.q("SELECT phone, kind, session, owner_id FROM numbers ORDER BY id LIMIT 40")
        total = db.one("SELECT COUNT(*) FROM numbers")[0]
        lines = []
        for r in rows:
            icon = "🆓" if r["kind"] == "free" else "💎"
            who = f"у <code>{r['owner_id']}</code>" if r["owner_id"] else "свободен"
            sess = " 📡" if r["session"] else ""
            lines.append(f"{icon} {escape(r['phone'])} · {who}{sess}")
        text = f"📱 <b>Номера</b> ({total})\n📡 = коды приходят автоматически\n\n" + ("\n".join(lines) or "Пока пусто.")
        if total > 40:
            text += f"\n… и ещё {total - 40}"
        text += (
            "\n\n/addnum +39… [сессия] — добавить (можно списком, по номеру в строке)\n"
            "/delnum +39… — удалить\n"
            "/code +39… 12345 — добавить код вручную"
        )
        await show(cb, text, back_kb())
    elif tab == "bc":
        await show(cb, "📢 <b>Рассылка</b>\n\n/broadcast текст сообщения", back_kb())
    elif tab == "backup":
        await send_backup(cb.bot, db, [cb.from_user.id])
        await cb.answer("Бэкап отправлен")


@router.message(Command("addnum"))
async def addnum(m: Message, command: CommandObject, db, tele):
    lines = [ln.split() for ln in (command.args or "").splitlines() if ln.strip()]
    if not lines:
        await m.answer("Формат: <code>/addnum +39123456789</code>\nС автоприёмом кодов: "
                       "<code>/addnum +39123456789 СТРОКА_СЕССИИ</code>\nМожно списком, по одному номеру в строке.")
        return
    out = []
    for parts in lines:
        phone = db.norm(parts[0])
        if len(phone) < 8:
            out.append(f"❌ {escape(parts[0])}: странный номер")
            continue
        session = parts[1] if len(parts) > 1 else None
        nid, created = db.add_number(phone, session)
        status = "добавлен" if created else "уже был, обновлён"
        if session:
            ok = await tele.add(nid, phone, session)
            status += ", приём кодов ✅" if ok else ", приём кодов ❌ (проверь сессию, API_ID и API_HASH)"
        out.append(f"✅ {escape(phone)}: {status}")
    await m.answer("\n".join(out))
    if any(len(p) > 1 for p in lines):
        try:
            await m.delete()  # в сообщении была сессия, убираем её из чата
        except Exception:
            pass


@router.message(Command("delnum"))
async def delnum(m: Message, command: CommandObject, db, tele):
    phone = db.norm(command.args or "")
    n = db.one("SELECT id FROM numbers WHERE phone=?", (phone,))
    if not n:
        await m.answer("Такого номера нет.")
        return
    await tele.stop(n["id"])
    db.x("DELETE FROM codes WHERE number_id=?", (n["id"],))
    db.x("DELETE FROM numbers WHERE id=?", (n["id"],))
    await m.answer(f"🗑 {escape(phone)} удалён.")


@router.message(Command("release"))
async def release(m: Message, command: CommandObject, db):
    phone = db.norm(command.args or "")
    n = db.one("SELECT id FROM numbers WHERE phone=?", (phone,))
    if not n:
        await m.answer("Такого номера нет.")
        return
    db.x("UPDATE numbers SET owner_id=NULL, assigned_at=NULL WHERE id=?", (n["id"],))
    await m.answer(f"↩️ {escape(phone)} снова свободен.")


@router.message(Command("give"))
async def give(m: Message, command: CommandObject, db):
    a = (command.args or "").split()
    if len(a) != 2 or not a[0].isdigit():
        await m.answer("Формат: <code>/give ID +888123456789</code> или <code>/give ID 888</code> или <code>/give ID 39</code>")
        return
    uid, target = int(a[0]), a[1]
    if not db.one("SELECT id FROM users WHERE id=?", (uid,)):
        await m.answer("Этот человек ещё не запускал бота.")
        return
    if target in ("39", "888"):
        kind = "paid" if target == "888" else "free"
        n = db.one("SELECT id, phone FROM numbers WHERE kind=? AND owner_id IS NULL ORDER BY id LIMIT 1", (kind,))
        if not n:
            await m.answer(f"Свободных +{target} нет.")
            return
    else:
        n = db.one("SELECT id, phone, owner_id FROM numbers WHERE phone=?", (db.norm(target),))
        if not n:
            await m.answer("Такого номера нет в базе. Сначала /addnum.")
            return
        if n["owner_id"]:
            await m.answer(f"Номер уже у {n['owner_id']}. Сначала /release.")
            return
    db.x("UPDATE numbers SET owner_id=?, assigned_at=? WHERE id=?", (uid, int(time.time()), n["id"]))
    await m.answer(f"🎁 {escape(n['phone'])} выдан {uid}.")
    try:
        await m.bot.send_message(uid, f"🎁 Тебе выдан номер <b>{escape(n['phone'])}</b>. Смотри его в разделе «Мои номера».")
    except Exception:
        await m.answer("Номер выдан, но написать человеку не вышло.")


@router.message(Command("code"))
async def manual_code(m: Message, command: CommandObject, db, tele):
    a = (command.args or "").split()
    if len(a) != 2:
        await m.answer("Формат: <code>/code +39123456789 12345</code>")
        return
    n = db.one("SELECT id FROM numbers WHERE phone=?", (db.norm(a[0]),))
    if not n:
        await m.answer("Такого номера нет.")
        return
    row = await tele.deliver(n["id"], a[1], "добавлен вручную")
    await m.answer("✅ Код сохранён" + (" и отправлен владельцу." if row and row["owner_id"] else ", у номера пока нет владельца."))


@router.message(Command("stars"))
async def stars(m: Message, command: CommandObject, db):
    a = (command.args or "").split()
    try:
        uid, delta = int(a[0]), int(a[1])
    except Exception:
        await m.answer("Формат: <code>/stars ID 50</code>")
        return
    if not db.one("SELECT id FROM users WHERE id=?", (uid,)):
        await m.answer("Такого человека нет.")
        return
    db.x("UPDATE users SET stars=MAX(0, stars+?) WHERE id=?", (delta, uid))
    bal = db.one("SELECT stars FROM users WHERE id=?", (uid,))["stars"]
    await m.answer(f"⭐ Теперь у {uid}: {bal}")


@router.message(Command("ban", "unban"))
async def ban(m: Message, command: CommandObject, db):
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await m.answer("Формат: <code>/ban ID</code> или <code>/unban ID</code>")
        return
    flag = 1 if command.command == "ban" else 0
    db.x("UPDATE users SET banned=? WHERE id=?", (flag, int(arg)))
    await m.answer("🚫 Забанен." if flag else "✅ Разбанен.")


@router.message(Command("broadcast"))
async def broadcast(m: Message, command: CommandObject, db):
    text = (command.args or "").strip()
    if not text:
        await m.answer("Формат: <code>/broadcast текст</code>")
        return
    ids = [r["id"] for r in db.q("SELECT id FROM users WHERE banned=0")]
    sent = 0
    for uid in ids:
        try:
            await m.bot.send_message(uid, text)
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"📢 Отправлено {sent} из {len(ids)}.")


@router.message(Command("backup"))
async def backup(m: Message, db):
    await send_backup(m.bot, db, [m.from_user.id])


@router.message(F.text.startswith("/restore") | F.caption.startswith("/restore"))
async def restore(m: Message, db, tele):
    doc = m.document or (m.reply_to_message.document if m.reply_to_message else None)
    if not doc:
        await m.answer("Ответь командой /restore на файл бэкапа или отправь файл с подписью /restore.")
        return
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "restore.db")
        await m.bot.download(doc, destination=path)
        try:
            await tele.stop_all()
            db.restore_from(path)
        except Exception as e:
            await m.answer(f"❌ Не получилось: {escape(str(e))}")
            await tele.reload()
            return
    await tele.reload()
    n = db.one("SELECT COUNT(*) FROM numbers")[0]
    u = db.one("SELECT COUNT(*) FROM users")[0]
    await m.answer(f"✅ База восстановлена: {u} людей, {n} номеров.")
