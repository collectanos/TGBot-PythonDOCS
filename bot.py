#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import asyncio
import sys
import uuid
import traceback
from pathlib import Path
from datetime import datetime
from typing import List, Tuple

from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command, CommandObject
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.exceptions import TelegramConflictError

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

# ==============================
# 🗃 DATABASE INITIALIZATION
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
    
    # 🔥 Автоматическое добавление админов в БД со статусом 'approved'
    async with aiosqlite.connect(DB_PATH) as db:
        for admin_id in ADMIN_IDS:
            await db.execute("""
                INSERT OR REPLACE INTO users 
                (user_id, username, first_name, status, approved_by, approved_at)
                VALUES (?, ?, ?, 'approved', ?, ?)
            """, (admin_id, "admin", "Администратор", admin_id, datetime.now().isoformat()))
        await db.commit()
    print(f"✅ Администраторы {ADMIN_IDS} автоматически одобрены.")
    print("✅ База данных инициализирована.")

# ==============================
# 🛡 SANDBOX (через subprocess + sandbox_runner.py)
# ==============================

async def safe_exec(code: str, user_id: int) -> Tuple[str, List[str]]:
    """
    Запускает код в изолированном subprocess.
    Возвращает: ("success", [paths]) или ("error", [message])
    """
    temp_subdir = TEMP_DIR / f"{user_id}_{uuid.uuid4().hex}"
    temp_subdir.mkdir(parents=True, exist_ok=True)
    
    code_file = temp_subdir / "code.py"
    runner_path = Path(__file__).parent / "sandbox_runner.py"
    
    if not runner_path.exists():
        return "error", [f"❌ Файл sandbox_runner.py не найден: {runner_path}"]
    
    # Сохраняем код во временный файл
    try:
        with open(code_file, "w", encoding="utf-8") as f:
            f.write(code)
    except Exception as e:
        return "error", [f"❌ Ошибка записи кода: {e}"]
    
    # Запускаем изолированный процесс
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, str(runner_path), str(temp_subdir), str(code_file),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(Path(__file__).parent)
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return "error", ["⚠️ Превышено время выполнения (30 секунд)"]
        
        # Декодируем вывод
        stdout_str = stdout.decode('utf-8', errors='replace').strip()
        stderr_str = stderr.decode('utf-8', errors='replace').strip()
        
        if not stdout_str:
            return "error", [f"Пустой stdout. stderr: {stderr_str[:500]}"]
        
        # Парсим JSON-результат
        try:
            result = json.loads(stdout_str)
        except Exception as e:
            return "error", [f"❌ Некорректный JSON от sandbox:\n{stdout_str[:1000]}\n\nОшибка: {e}"]
        
        if result.get("status") == "success":
            files = result.get("files", [])
            return "success", files
        else:
            msg = result.get("message", "Неизвестная ошибка sandbox")
            return "error", [msg]
    
    except Exception as e:
        return "error", [f"❌ Ошибка запуска sandbox: {e}\nstderr: {stderr_str[:300]}"]


# ==============================
# 🗑 AUTO-DELETE FILES
# ==============================

async def delete_files_after_delay(file_paths: List[str], delay: int = 900):
    """Удаляет файлы через `delay` секунд (по умолчанию 15 мин)"""
    await asyncio.sleep(delay)
    for path in file_paths:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception:
            pass


# ==============================
# 🤖 BOT SETUP
# ==============================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())


# ==============================
# 🧑 USER STATUS HELPERS
# ==============================

async def ensure_user_registered(user: types.User):
    status = 'approved' if user.id in ADMIN_IDS else 'pending'
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR IGNORE INTO users (user_id, username, first_name, status)
            VALUES (?, ?, ?, ?)
        """, (user.id, user.username, user.first_name, status))
        await db.commit()


async def get_user_status(user_id: int) -> str:
    if user_id in ADMIN_IDS:
        return "approved"
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT status FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
            return row[0] if row else "pending"


# ==============================
# 📜 COMMANDS
# ==============================

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await ensure_user_registered(message.from_user)
    status = await get_user_status(message.from_user.id)
    if status == "banned":
        await message.answer("❌ Вы заблокированы.")
        return
    elif status == "pending":
        await message.answer(
            "👋 Добро пожаловать!\n"
            "Я — бот для генерации документов по вашему коду.\n\n"
            "✅ Поддерживаемые форматы:\n"
            " • .docx — через python-docx\n"
            " • .pptx — через python-pptx\n"
            " • .pdf  — через reportlab\n\n"
            "🖼 Онлайн-изображения: requests + PIL (загрузка по URL → вставка в документ).\n\n"
            "⏳ Ваш запрос на использование отправлен администратору.\n"
            "Пожалуйста, ожидайте одобрения."
        )
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"🔔 Новый пользователь:\n"
                    f"ID: `{message.from_user.id}`\n"
                    f"Имя: {message.from_user.full_name}\n"
                    f"Username: @{message.from_user.username or '—'}\n"
                    f"Статус: ⏳ ожидает подтверждения",
                    parse_mode="Markdown"
                )
            except Exception:
                pass
    else:
        await message.answer(
            "✅ Добро пожаловать!\n"
            "Отправьте Python-код (текстом или .py файлом), чтобы сгенерировать документ.\n"
            "Пример: создание таблицы с вопросами к историческим личностям.\n\n"
            "ℹ️ Подробнее — команда /info"
        )


@dp.message(Command("info"))
async def cmd_info(message: types.Message):
    text = (
        "📄 *Бот генерирует документы по вашему коду.*\n\n"
        "✅ *Поддерживаемые форматы:*\n"
        " • `.docx` — через `python-docx`\n"
        " • `.pptx` — через `python-pptx`\n"
        " • `.pdf`  — через `reportlab`\n\n"
        "🖼 *Онлайн-изображения:*\n"
        " • `requests` + `PIL`: загружайте картинки по URL → вставляйте в документ.\n\n"
        "🔧 *Разрешены:*\n"
        " • Встроенные: `random`, `datetime`, `re`, `json`, `math`, `textwrap`, `base64`, `io`\n"
        " • `os.path` (только для путей)\n"
        " • Библиотеки: `docx`, `pptx`, `reportlab`, `PIL` (`Image`, `ImageDraw`, `ImageFont`), `requests`\n\n"
        "❌ *Запрещены:*\n"
        " • `os`, `sys`, `subprocess`, `eval`, `exec`, `__import__` и другие опасные модули.\n\n"
        "💡 Пример кода — см. в описании канала или запросите у админа."
    )
    await message.answer(text, parse_mode="Markdown")


@dp.message(Command("profile"))
async def cmd_profile(message: types.Message):
    await ensure_user_registered(message.from_user)
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT status, username, first_name FROM users WHERE user_id = ?
        """, (message.from_user.id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                await message.answer("❌ Пользователь не найден.")
                return

    status, username, first_name = row
    if message.from_user.id in ADMIN_IDS:
        status = "approved"
        
    status_emoji = {"approved": "✅ одобрен", "banned": "❌ заблокирован", "pending": "⏳ ожидает"}

    profile = (
        f"👤 *Имя:* {first_name or '—'}\n"
        f"🆔 *ID:* `{message.from_user.id}`\n"
        f"📇 *Username:* @{username or '—'}\n"
        f"🛡 *Статус:* {status_emoji.get(status, status)}"
    )
    await message.answer(profile, parse_mode="Markdown")


# ==============================
# 👑 ADMIN COMMANDS
# ==============================

USERS_PER_PAGE = 5

async def get_paginated_users(page: int = 1):
    offset = (page - 1) * USERS_PER_PAGE
    placeholders = ','.join('?' * len(ADMIN_IDS))
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(f"SELECT COUNT(*) FROM users WHERE user_id NOT IN ({placeholders})", ADMIN_IDS) as cursor:
            total = (await cursor.fetchone())[0]
        async with db.execute(f"""
            SELECT user_id, username, first_name, status 
            FROM users 
            WHERE user_id NOT IN ({placeholders})
            ORDER BY created_at DESC 
            LIMIT ? OFFSET ?
        """, ADMIN_IDS + [USERS_PER_PAGE, offset]) as cursor:
            users = await cursor.fetchall()
    return users, total


def build_players_keyboard(users: List[tuple], page: int, total: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for user_id, username, first_name, status in users:
        name = f"{first_name or ''} @{username or '—'}".strip()
        if len(name) > 25:
            name = name[:22] + "..."
        status_icon = {"approved": "✅", "banned": "❌", "pending": "⏳"}.get(status, "❓")
        builder.button(text=f"{status_icon} {name}", callback_data=f"user_{user_id}")
    builder.adjust(1)

    total_pages = (total + USERS_PER_PAGE - 1) // USERS_PER_PAGE
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"players_{page-1}"))
    nav_buttons.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"players_{page+1}"))
    if nav_buttons:
        builder.row(*nav_buttons)

    return builder.as_markup()


@dp.message(Command("players"))
async def cmd_players(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    page = int(command.args) if command.args and command.args.isdigit() else 1
    users, total = await get_paginated_users(page)
    if not users:
        await message.answer("📭 Пользователи не найдены.")
        return
    kb = build_players_keyboard(users, page, total)
    await message.answer(f"👥 Список пользователей (стр. {page}):", reply_markup=kb)


@dp.callback_query(lambda c: c.data.startswith("players_"))
async def cb_players_nav(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    page = int(callback.data.split("_")[1])
    users, total = await get_paginated_users(page)
    kb = build_players_keyboard(users, page, total)
    await callback.message.edit_text(f"👥 Список пользователей (стр. {page}):", reply_markup=kb)
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith("user_"))
async def cb_user_menu(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    user_id = int(callback.data.split("_")[1])
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT username, first_name, status FROM users WHERE user_id = ?", (user_id,)) as cursor:
            row = await cursor.fetchone()
    if not row:
        await callback.answer("❌ Пользователь не найден", show_alert=True)
        return

    username, first_name, status = row
    name = f"{first_name or ''} @{username or '—'}".strip() or f"ID {user_id}"

    builder = InlineKeyboardBuilder()
    if status == "pending":
        builder.button(text="✅ Одобрить", callback_data=f"approve_{user_id}")
    elif status == "approved":
        builder.button(text="⏸ Сбросить", callback_data=f"reset_{user_id}")
    if status != "banned":
        builder.button(text="🚫 Заблокировать", callback_data=f"ban_{user_id}")
    else:
        builder.button(text="🔓 Разблокировать", callback_data=f"unban_{user_id}")
    builder.button(text="⬅️ Назад", callback_data="back_players")
    builder.adjust(2, 1)

    await callback.message.edit_text(
        f"👤 *{name}*\n"
        f"🆔 `{user_id}`\n"
        f"Статус: {'✅ одобрен' if status == 'approved' else '❌ заблокирован' if status == 'banned' else '⏳ ожидает'}",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await callback.answer()


@dp.callback_query(lambda c: c.data == "back_players")
async def cb_back_players(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    users, total = await get_paginated_users(1)
    kb = build_players_keyboard(users, 1, total)
    await callback.message.edit_text("👥 Список пользователей (стр. 1):", reply_markup=kb)
    await callback.answer()


@dp.callback_query(lambda c: c.data.startswith(("approve_", "ban_", "unban_", "reset_")))
async def cb_action(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    action, user_id = callback.data.split("_")
    user_id = int(user_id)

    async with aiosqlite.connect(DB_PATH) as db:
        if action == "approve":
            await db.execute("""
                UPDATE users SET status = 'approved', approved_by = ?, approved_at = ?
                WHERE user_id = ? AND status = 'pending'
            """, (callback.from_user.id, datetime.now().isoformat(), user_id))
        elif action == "ban":
            await db.execute("UPDATE users SET status = 'banned' WHERE user_id = ?", (user_id,))
        elif action == "unban":
            await db.execute("UPDATE users SET status = 'pending' WHERE user_id = ?", (user_id,))
        elif action == "reset":
            await db.execute("UPDATE users SET status = 'pending' WHERE user_id = ?", (user_id,))
        await db.commit()

    await callback.answer(f"✅ Действие выполнено", show_alert=True)
    await cb_user_menu(callback)


@dp.message(Command("approve"))
async def cmd_approve(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not command.args:
        await message.answer("UsageId: `/approve <user_id или @username>`", parse_mode="Markdown")
        return

    target = command.args.strip()
    user_id = None

    if target.isdigit():
        user_id = int(target)
    elif target.startswith("@"):
        username = target[1:]
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT user_id FROM users WHERE username = ?", (username,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    user_id = row[0]

    if not user_id:
        await message.answer("❌ Пользователь не найден.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users SET status = 'approved', approved_by = ?, approved_at = ?
            WHERE user_id = ? AND status = 'pending'
        """, (message.from_user.id, datetime.now().isoformat(), user_id))
        await db.commit()

    await message.answer(f"✅ Пользователь `{user_id}` одобрен.", parse_mode="Markdown")


@dp.message(Command("ban"))
async def cmd_ban(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not command.args or not command.args.isdigit():
        await message.answer("UsageId: `/ban <user_id>`", parse_mode="Markdown")
        return
    user_id = int(command.args)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE users SET status = 'banned' WHERE user_id = ?", (user_id,))
        await db.commit()
    await message.answer(f"🚫 Пользователь `{user_id}` заблокирован.", parse_mode="Markdown")


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("❌ Эта команда доступна только администраторам.")
        return
    help_text = (
        "🛠 *Команды администратора*\n\n"
        "🔹 `/players` — просмотреть список пользователей\n"
        "🔹 `/approve <ID или @username>` — одобрить пользователя\n"
        "🔹 `/ban <ID>` — заблокировать пользователя\n"
        "🔹 `/profile` — ваш профиль\n"
        "🔹 `/info` — информация о возможностях бота\n\n"
        "💡 В интерфейсе `/players`:\n"
        " • Нажмите на пользователя → управление\n"
        " • ✅ Одобрить / 🔄 Сбросить статус\n"
        " • 🚫 Заблокировать / 🔓 Разблокировать"
    )
    await message.answer(help_text, parse_mode="Markdown")


# ==============================
# 📥 CODE HANDLING
# ==============================

@dp.message()
async def handle_code(message: types.Message):
    if not message.text and not (message.document and message.document.mime_type == "text/x-python"):
        return

    user_id = message.from_user.id
    status = await get_user_status(user_id)
    if status != "approved":
        text = "⏳ Ваш аккаунт ожидает одобрения." if status == "pending" else "❌ Вы заблокированы."
        await message.answer(text)
        return

    code = None
    if message.document:
        file = await bot.get_file(message.document.file_id)
        file_path = f"/tmp/{uuid.uuid4().hex}.py" if os.name != 'nt' else f"C:\\Temp\\{uuid.uuid4().hex}.py"
        try:
            await bot.download_file(file.file_path, file_path)
            with open(file_path, "r", encoding="utf-8") as f:
                code = f.read()
        except Exception as e:
            return await message.answer(f"❌ Ошибка чтения файла: {e}")
        finally:
            Path(file_path).unlink(missing_ok=True)
    else:
        code = message.text

    if not code.strip():
        return await message.answer("❌ Код пуст.")

    await message.answer("⏳ Запускаю ваш код... (макс. 30 сек)")

    r_type, r_data = await safe_exec(code, user_id)

    if r_type == "success":
        files = r_data
        if not files:
            await message.answer("⚠️ Код выполнен, но файлы не созданы.")
        else:
            for file_path in files:
                try:
                    await message.answer_document(types.FSInputFile(file_path))
                except Exception as e:
                    await message.answer(f"❌ Не удалось отправить файл: {e}")
            asyncio.create_task(delete_files_after_delay(files, 900))
    else:
        msg = r_data[0] if r_data else "Неизвестная ошибка"
        if len(msg) > 3000:
            msg = msg[:2997] + "..."
        await message.answer(f"❌ Ошибка выполнения:\n```\n{msg}\n```", parse_mode="Markdown")


# ==============================
# 🚀 MAIN
# ==============================

async def main():
    await init_db()
    print("✅ Bot started. Press Ctrl+C to stop.")
    try:
        await dp.start_polling(bot)
    except TelegramConflictError:
        print("❌ Ошибка: обнаружен другой запущенный экземпляр бота.")
        print("💡 Решение: остановите все другие процессы и перезапустите.")
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        traceback.print_exc()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Bot stopped.")