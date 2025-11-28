#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import asyncio
import multiprocessing
import uuid
from pathlib import Path
from datetime import datetime
from typing import List, Tuple

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.storage.memory import MemoryStorage

import aiosqlite
from dotenv import load_dotenv

# ==============================
# 🔑 CONFIGURATION
# ==============================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("❌ BOT_TOKEN не указан в .env")

ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()
if not ADMIN_IDS_RAW:
    raise RuntimeError("❌ ADMIN_IDS не указан в .env")

try:
    ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip()]
except Exception as e:
    raise RuntimeError(f"❌ Неверный формат ADMIN_IDS: {e}. Используйте: '123,456,789'")

# ==============================
# 🗃 PATHS
# ==============================

DB_PATH = "users.db"
TEMP_DIR = Path("temp_files")
TEMP_DIR.mkdir(exist_ok=True)

ALLOWED_MODULES = {
    'random', 'datetime', 're', 'json', 'math', 'textwrap', 'base64', 'io',
    'os.path',
    'docx', 'pptx', 'reportlab', 'PIL', 'requests',
}

# ==============================
# 🗃 DATABASE
# ==============================

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                status TEXT CHECK(status IN ('pending', 'approved', 'banned')) DEFAULT 'pending',
                approved_by INTEGER,
                approved_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.commit()

# ==============================
# 🛡 SANDBOX
# ==============================

def _run_code_in_sandbox(code: str, temp_subdir: str, result_pipe):
    try:
        import builtins
        original_import = builtins.__import__

        def safe_import(name, *args, **kwargs):
            if name == 'os':
                import types, os as real_os
                fake_os = types.SimpleNamespace()
                fake_os.path = real_os.path
                return fake_os
            if name.split('.')[0] not in ALLOWED_MODULES:
                raise ImportError(f"❌ Запрещён импорт: {name}")
            return original_import(name, *args, **kwargs)

        builtins.__import__ = safe_import

        import io as _io
        import os as _os
        safe_temp = Path(temp_subdir)
        safe_temp.mkdir(parents=True, exist_ok=True)

        # ——— Патч save() ———
        try:
            from docx import Document
            orig = Document.save
            def patched(self, fn):
                fn = _os.path.basename(str(fn))
                if not fn.lower().endswith(('.docx', '.pdf', '.pptx', '.png', '.jpg', '.jpeg')):
                    raise ValueError("❌ Только .docx/.pptx/.pdf/.png/.jpg")
                return orig(self, str(safe_temp / fn))
            Document.save = patched
        except: pass

        try:
            from pptx import Presentation
            orig = Presentation.save
            def patched(self, fn):
                fn = _os.path.basename(str(fn))
                if not fn.lower().endswith(('.pptx', '.pdf')):
                    raise ValueError("❌ Только .pptx/.pdf")
                return orig(self, str(safe_temp / fn))
            Presentation.save = patched
        except: pass

        try:
            from reportlab.pdfgen import canvas
            orig_init = canvas.Canvas.__init__
            def patched_init(self, fn, *a, **kw):
                fn = _os.path.basename(str(fn))
                if not fn.lower().endswith('.pdf'):
                    raise ValueError("❌ Только .pdf")
                return orig_init(self, str(safe_temp / fn), *a, **kw)
            canvas.Canvas.__init__ = patched_init
        except: pass

        # ——— Глобальные ———
        g = {
            '__builtins__': __builtins__,
            '__name__': '__main__',
            'BytesIO': _io.BytesIO,
            'StringIO': _io.StringIO,
        }
        for mod in ['random', 'datetime', 're', 'json', 'math', 'textwrap', 'base64']:
            g[mod] = __import__(mod)

        exec(code, g)

        files = [str(f) for f in safe_temp.iterdir() if f.is_file()]
        result_pipe.send(("success", files))

    except Exception as e:
        import traceback
        result_pipe.send(("error", f"{type(e).__name__}: {e}\n\n{traceback.format_exc(limit=2)}"))


async def safe_exec(code: str, user_id: int) -> Tuple[str, List[str]]:
    temp_subdir = TEMP_DIR / f"{user_id}_{uuid.uuid4().hex}"
    parent_conn, child_conn = multiprocessing.Pipe()
    proc = multiprocessing.Process(target=_run_code_in_sandbox, args=(code, str(temp_subdir), child_conn), daemon=True)
    proc.start()

    try:
        if parent_conn.poll(30):
            return parent_conn.recv()
        proc.terminate()
        await asyncio.sleep(0.1)
        if proc.is_alive():
            proc.kill()
        return "error", ["⚠️ Таймаут: 30 сек"]
    finally:
        proc.join(timeout=1)


# ==============================
# 🗑 CLEANUP
# ==============================

async def delete_files_after_delay(paths: List[str], delay: int = 900):
    await asyncio.sleep(delay)
    for p in paths:
        try:
            Path(p).unlink(missing_ok=True)
        except:
            pass


# ==============================
# 🤖 BOT
# ==============================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


async def ensure_user(user: types.User):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)",
            (user.id, user.username, user.first_name)
        )
        await db.commit()


async def get_status(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT status FROM users WHERE user_id = ?", (user_id,)) as c:
            row = await c.fetchone()
            return row[0] if row else "pending"


# ——— COMMANDS ———

@dp.message(Command("start"))
async def start(m: types.Message):
    await ensure_user(m.from_user)
    st = await get_status(m.from_user.id)
    if st == "banned":
        await m.answer("❌ Вы заблокированы.")
    elif st == "pending":
        text = (
            "👋 Привет! Я — бот-генератор документов.\n\n"
            "Я могу создавать `.docx`, `.pptx`, `.pdf` — по вашему коду.\n"
            "Например, таблицу с вопросами к Петру I или презентацию про Екатерину II.\n\n"
            "✅ Чтобы начать:\n"
            "1. Напишите код на Python (с использованием `docx`, `pptx`, `reportlab`)\n"
            "2. Отправьте его текстом **или прикрепите как `.py` файл**\n\n"
            "⏳ Ваш запрос на использование находится на рассмотрении. Пожалуйста, ожидайте одобрения администратора."
        )
        await m.answer(text)
        for aid in ADMIN_IDS:
            try:
                await bot.send_message(
                    aid,
                    f"🔔 Новый пользователь:\nID: `{m.from_user.id}`\nИмя: {m.from_user.full_name}\n@{m.from_user.username or '—'}",
                    parse_mode="Markdown"
                )
            except: pass
    else:
        text = (
            "✅ Добро пожаловать!\n\n"
            "Я — бот-генератор документов по вашему коду.\n"
            "📄 Поддержка: `.docx`, `.pptx`, `.pdf` + изображения из интернета.\n\n"
            "📤 Как отправить код:\n"
            "• Напишите прямо в чат\n"
            "• Или пришлите файл с расширением `.py`\n\n"
            "❓ Подробнее — команда /info"
        )
        await m.answer(text)


@dp.message(Command("info"))
async def info(m: types.Message):
    await m.answer(
        "📄 *Бот создаёт документы по вашему Python-коду*\n\n"
        "✅ *Поддерживаемые форматы:*\n"
        " • `.docx` — через `python-docx`\n"
        " • `.pptx` — через `python-pptx`\n"
        " • `.pdf`  — через `reportlab`\n\n"
        "🖼 *Изображения:*\n"
        " • Загружайте по URL через `requests`\n"
        " • Вставляйте в документ через `PIL.Image`\n\n"
        "🔧 *Разрешённые модули:*\n"
        "`random`, `datetime`, `re`, `json`, `math`, `textwrap`, `base64`, `io`, `os.path`,\n"
        "`docx`, `pptx`, `reportlab`, `PIL`, `requests`\n\n"
        "❌ *Запрещено:* `os`, `sys`, `subprocess`, `eval`, `exec`, `__import__` и др.",
        parse_mode="Markdown"
    )


@dp.message(Command("profile"))
async def profile(m: types.Message):
    await ensure_user(m.from_user)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT status, username, first_name FROM users WHERE user_id = ?",
            (m.from_user.id,)
        ) as c:
            row = await c.fetchone()
    if not row:
        return await m.answer("❌ Не найден.")

    st, un, fn = row
    status_map = {"approved": "✅ одобрен", "banned": "❌ заблокирован", "pending": "⏳ ожидает"}
    await m.answer(
        f"👤 *Имя:* {fn or '—'}\n"
        f"🆔 *ID:* `{m.from_user.id}`\n"
        f"📇 *Username:* @{un or '—'}\n"
        f"🛡 *Статус:* {status_map.get(st, st)}",
        parse_mode="Markdown"
    )


@dp.message(Command("help"))
async def help_cmd(m: types.Message):
    if m.from_user.id not in ADMIN_IDS:
        return  # не показываем обычным пользователям
    help_text = (
        "🛠 *Команды администратора*\n\n"
        "🔹 `/players` — список всех пользователей (с пагинацией)\n"
        "🔹 `/approve <ID или @username>` — одобрить пользователя\n"
        "🔹 `/ban <ID>` — заблокировать пользователя\n"
        "🔹 `/profile` — посмотреть свой профиль\n"
        "🔹 `/info` — информация о возможностях бота\n\n"
        "💡 В интерфейсе `/players`:\n"
        " • ✅ Одобрить — дать доступ\n"
        " • 🔄 Сбросить — вернуть в «ожидание»\n"
        " • 🚫 Забанить / 🔓 Разбанить\n"
    )
    await m.answer(help_text, parse_mode="Markdown")


# ——— ADMIN ———

USERS_PER_PAGE = 5

async def get_users(page: int = 1):
    offset = (page - 1) * USERS_PER_PAGE
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            total = (await c.fetchone())[0]
        async with db.execute("""
            SELECT user_id, username, first_name, status
            FROM users ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """, (USERS_PER_PAGE, offset)) as c:
            users = await c.fetchall()
    return users, total


def players_kb(users, page, total):
    kb = InlineKeyboardBuilder()
    for uid, un, fn, st in users:
        name = (fn or "") + (" @" + un if un else "")
        name = name[:24] + "…" if len(name) > 25 else name or f"ID{uid}"
        icon = {"approved": "✅", "banned": "❌", "pending": "⏳"}.get(st, "❓")
        kb.button(text=f"{icon} {name}", callback_data=f"user_{uid}")
    kb.adjust(1)

    tp = (total + USERS_PER_PAGE - 1) // USERS_PER_PAGE
    nav = []
    if page > 1: nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"players_{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page}/{tp}", callback_data="noop"))
    if page < tp: nav.append(InlineKeyboardButton(text="➡️", callback_data=f"players_{page+1}"))
    if nav: kb.row(*nav)

    return kb.as_markup()


@dp.message(Command("players"))
async def players(m: types.Message, cmd: CommandObject):
    if m.from_user.id not in ADMIN_IDS: return
    page = int(cmd.args) if cmd.args and cmd.args.isdigit() else 1
    users, total = await get_users(page)
    if not users: return await m.answer("📭 Нет пользователей.")
    await m.answer(f"👥 Пользователи (стр. {page})", reply_markup=players_kb(users, page, total))


@dp.callback_query(lambda c: c.data.startswith("players_"))
async def nav(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS: return
    page = int(cb.data.split("_")[1])
    users, total = await get_users(page)
    await cb.message.edit_text(f"👥 Пользователи (стр. {page})", reply_markup=players_kb(users, page, total))
    await cb.answer()


@dp.callback_query(lambda c: c.data.startswith("user_"))
async def user_menu(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS: return
    uid = int(cb.data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT username, first_name, status FROM users WHERE user_id = ?", (uid,)) as c:
            row = await c.fetchone()
    if not row: return await cb.answer("❌ Не найден", show_alert=True)

    un, fn, st = row
    name = (fn or "") + (" @" + un if un else "") or f"ID{uid}"
    kb = InlineKeyboardBuilder()
    if st == "pending":
        kb.button(text="✅ Одобрить", callback_data=f"approve_{uid}")
    elif st == "approved":
        kb.button(text="🔄 Сбросить", callback_data=f"reset_{uid}")
    if st != "banned":
        kb.button(text="🚫 Забанить", callback_data=f"ban_{uid}")
    else:
        kb.button(text="🔓 Разбанить", callback_data=f"unban_{uid}")
    kb.button(text="⬅️ Назад", callback_data="back_players")
    kb.adjust(2, 1)

    status_text = {"approved": "✅ одобрен", "banned": "❌ заблокирован", "pending": "⏳ ожидает"}.get(st, st)
    await cb.message.edit_text(
        f"👤 *{name}*\n"
        f"🆔 `{uid}`\n"
        f"🛡 Статус: {status_text}",
        parse_mode="Markdown",
        reply_markup=kb.as_markup()
    )
    await cb.answer()


@dp.callback_query(lambda c: c.data == "back_players")
async def back(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS: return
    users, total = await get_users(1)
    await cb.message.edit_text("👥 Пользователи (стр. 1)", reply_markup=players_kb(users, 1, total))
    await cb.answer()


@dp.callback_query(lambda c: c.data.split("_")[0] in ["approve", "ban", "unban", "reset"])
async def action(cb: types.CallbackQuery):
    if cb.from_user.id not in ADMIN_IDS: return
    act, uid = cb.data.split("_")
    uid = int(uid)
    async with aiosqlite.connect(DB_PATH) as db:
        if act == "approve":
            await db.execute("""
                UPDATE users SET status='approved', approved_by=?, approved_at=?
                WHERE user_id=? AND status='pending'
            """, (cb.from_user.id, datetime.now().isoformat(), uid))
        elif act == "ban":
            await db.execute("UPDATE users SET status='banned' WHERE user_id=?", (uid,))
        elif act == "unban":
            await db.execute("UPDATE users SET status='pending' WHERE user_id=?", (uid,))
        elif act == "reset":
            await db.execute("UPDATE users SET status='pending' WHERE user_id=?", (uid,))
        await db.commit()
    await cb.answer("✅ Выполнено", show_alert=True)
    await user_menu(cb)


@dp.message(Command("approve"))
async def approve(m: types.Message, cmd: CommandObject):
    if m.from_user.id not in ADMIN_IDS: return
    arg = cmd.args.strip() if cmd.args else ""
    if not arg: return await m.answer("UsageId: `/approve <ID или @username>`", parse_mode="Markdown")

    uid = None
    if arg.isdigit():
        uid = int(arg)
    elif arg.startswith("@"):
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT user_id FROM users WHERE username = ?", (arg[1:],)) as c:
                row = await c.fetchone()
                if row: uid = row[0]

    if not uid:
        return await m.answer("❌ Пользователь не найден.")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users SET status='approved', approved_by=?, approved_at=?
            WHERE user_id=? AND status='pending'
        """, (m.from_user.id, datetime.now().isoformat(), uid))
        await db.commit()
    await m.answer(f"✅ Пользователь `{uid}` одобрен.", parse_mode="Markdown")


@dp.message(Command("ban"))
async def ban(m: types.Message, cmd: CommandObject):
    if m.from_user.id not in ADMIN_IDS: return
    if not (cmd.args and cmd.args.isdigit()):
        return await m.answer("UsageId: `/ban <ID>`", parse_mode="Markdown")
    uid = int(cmd.args)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET status='banned' WHERE user_id=?", (uid,))
        await db.commit()
    await m.answer(f"🚫 Пользователь `{uid}` заблокирован.", parse_mode="Markdown")


# ——— CODE HANDLING ———

@dp.message()
async def handle(m: types.Message):
    if not m.text and not (m.document and m.document.mime_type == "text/x-python"):
        return

    uid = m.from_user.id
    st = await get_status(uid)
    if st != "approved":
        await m.answer("❌ Доступ запрещён." if st == "banned" else "⏳ Ожидайте одобрения.")
        return

    code = None
    if m.document:
        f = await bot.get_file(m.document.file_id)
        fp = f"/tmp/{uuid.uuid4().hex}.py"
        await bot.download_file(f.file_path, fp)
        try:
            with open(fp, encoding="utf-8") as fio:
                code = fio.read()
        except Exception as e:
            return await m.answer(f"❌ Ошибка чтения файла: {e}")
        finally:
            Path(fp).unlink(missing_ok=True)
    else:
        code = m.text

    if not code.strip():
        return await m.answer("❌ Код пуст.")

    await m.answer("⏳ Выполняю ваш код... (макс. 30 секунд)")

    r_type, r_data = await safe_exec(code, uid)

    if r_type == "success":
        files = r_data
        if not files:
            await m.answer("⚠️ Код выполнен, но файлы не созданы.")
        else:
            for fp in files:
                try:
                    await m.answer_document(types.FSInputFile(fp))
                except Exception as e:
                    await m.answer(f"❌ Не удалось отправить `{Path(fp).name}`: {e}")
            asyncio.create_task(delete_files_after_delay(files))
    else:
        msg = r_data[0] if r_data else "Неизвестная ошибка"
        if len(msg) > 3000: msg = msg[:2997] + "..."
        await m.answer(f"❌ Ошибка выполнения:\n```\n{msg}\n```", parse_mode="Markdown")


# ——— MAIN ———

async def main():
    await init_db()
    print("✅ Бот запущен. Нажмите Ctrl+C для остановки.")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Бот остановлен.")