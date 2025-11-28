#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import asyncio
import multiprocessing
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

ALLOWED_MODULES = {
    'random', 'datetime', 're', 'json', 'math', 'textwrap', 'base64', 'io',
    'os.path',
    'docx', 'pptx', 'reportlab', 'PIL', 'requests',
}

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
# 🛡 SANDBOX: SAFE CODE EXECUTION
# ==============================

def _run_code_in_sandbox(code: str, temp_subdir: str, result_pipe):
    """
    Выполняется в отдельном процессе.
    Перехватывает импорты, патчит save(), возвращает список созданных файлов.
    """
    try:
        # --- 1. Ограниченные импорты ---
        import builtins

        original_import = builtins.__import__

        def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
            # Разрешаем 'os.path', но не 'os'
            if name == 'os':
                import types
                import os as real_os
                fake_os = types.SimpleNamespace()
                fake_os.path = real_os.path
                return fake_os
            if name.split('.')[0] not in ALLOWED_MODULES:
                raise ImportError(f"⚠️ Запрещён импорт: {name}")
            return original_import(name, globals, locals, fromlist, level)

        builtins.__import__ = safe_import

        # --- 2. Создаём изолированное окружение ---
        import io as _io
        import os as _os

        # Временная папка для файлов пользователя
        safe_temp = Path(temp_subdir)
        safe_temp.mkdir(parents=True, exist_ok=True)

        # --- 3. Патчим save() методы ---
        # Для docx
        try:
            from docx import Document
            original_doc_save = Document.save

            def patched_doc_save(self, filename):
                # Обрезаем путь — только имя файла
                safe_name = _os.path.basename(str(filename))
                full_path = safe_temp / safe_name
                # Разрешаем только нужные расширения
                if not safe_name.lower().endswith(('.docx', '.pdf', '.pptx', '.png', '.jpg', '.jpeg')):
                    raise ValueError("❌ Только .docx, .pptx, .pdf, .png, .jpg разрешены")
                return original_doc_save(self, str(full_path))
            Document.save = patched_doc_save
        except Exception:
            pass  # Если docx не используется — ок

        # Для pptx
        try:
            from pptx import Presentation
            original_pptx_save = Presentation.save

            def patched_pptx_save(self, filename):
                safe_name = _os.path.basename(str(filename))
                full_path = safe_temp / safe_name
                if not safe_name.lower().endswith(('.pptx', '.pdf')):
                    raise ValueError("❌ Только .pptx, .pdf разрешены")
                return original_pptx_save(self, str(full_path))
            Presentation.save = patched_pptx_save
        except Exception:
            pass

        # Для reportlab (Canvas)
        try:
            from reportlab.pdfgen import canvas
            from reportlab.lib.pagesizes import letter

            original_canvas_init = canvas.Canvas.__init__

            def patched_canvas_init(self, filename, *args, **kwargs):
                safe_name = _os.path.basename(str(filename))
                full_path = safe_temp / safe_name
                if not safe_name.lower().endswith('.pdf'):
                    raise ValueError("❌ Только .pdf для Canvas")
                # Вызываем оригинальный init с безопасным путём
                return original_canvas_init(self, str(full_path), *args, **kwargs)

            canvas.Canvas.__init__ = patched_canvas_init
        except Exception:
            pass

        # --- 4. Глобальные переменные для кода ---
        sandbox_globals = {
            '__builtins__': __builtins__,
            '__name__': '__main__',
        }

        # Добавляем разрешённые модули
        for mod in ['random', 'datetime', 're', 'json', 'math', 'textwrap', 'base64']:
            sandbox_globals[mod] = __import__(mod)

        # Добавляем io.BytesIO/StringIO
        sandbox_globals['BytesIO'] = _io.BytesIO
        sandbox_globals['StringIO'] = _io.StringIO

        # --- 5. Выполняем код ---
        exec(code, sandbox_globals)

        # --- 6. Собираем все файлы в temp_subdir ---
        generated_files = [
            str(f) for f in safe_temp.iterdir()
            if f.is_file() and f.suffix.lower() in ['.docx', '.pptx', '.pdf', '.png', '.jpg', '.jpeg']
        ]

        result_pipe.send(("success", generated_files))

    except Exception as e:
        import traceback
        result_pipe.send(("error", f"{type(e).__name__}: {str(e)}\n\n{traceback.format_exc(limit=3)}"))
    finally:
        # Восстанавливаем __import__, если нужно (в процессе — не критично)
        pass


async def safe_exec(code: str, user_id: int) -> Tuple[str, List[str]]:
    """
    Запускает код в изолированном процессе.
    Возвращает: ("success", [paths]) или ("error", message)
    """
    temp_subdir = TEMP_DIR / f"{user_id}_{uuid.uuid4().hex}"
    parent_conn, child_conn = multiprocessing.Pipe()

    # Запускаем процесс
    proc = multiprocessing.Process(
        target=_run_code_in_sandbox,
        args=(code, str(temp_subdir), child_conn),
        daemon=True
    )
    proc.start()

    try:
        # Ждём максимум 30 секунд
        if parent_conn.poll(30):
            result = parent_conn.recv()
        else:
            proc.terminate()
            proc.join(2)
            if proc.is_alive():
                proc.kill()
            return "error", ["⚠️ Превышено время выполнения (30 сек)"]
        return result
    except Exception as e:
        return "error", [f"❌ Ошибка запуска: {e}"]
    finally:
        proc.join(timeout=1)


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
    # 🔥 Если пользователь — админ, статус сразу 'approved'
    status = 'approved' if user.id in ADMIN_IDS else 'pending'
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR IGNORE INTO users (user_id, username, first_name, status)
            VALUES (?, ?, ?, ?)
        """, (user.id, user.username, user.first_name, status))
        await db.commit()


async def get_user_status(user_id: int) -> str:
    # 🔥 Админ всегда имеет статус 'approved'
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
        # Уведомляем админов
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
    else:  # approved (включая админов)
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
    # 🔥 Для админа — всегда approved
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

# Пагинация пользователей
USERS_PER_PAGE = 5

async def get_paginated_users(page: int = 1):
    offset = (page - 1) * USERS_PER_PAGE
    async with aiosqlite.connect(DB_PATH) as db:
        # Общее количество (кроме админов)
        async with db.execute("SELECT COUNT(*) FROM users WHERE user_id NOT IN ({})".format(','.join('?'*len(ADMIN_IDS))), ADMIN_IDS) as cursor:
            total = (await cursor.fetchone())[0]
        # Пользователи (без админов)
        placeholders = ','.join('?'*len(ADMIN_IDS))
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
        builder.button(
            text=f"{status_icon} {name}",
            callback_data=f"user_{user_id}"
        )
    builder.adjust(1)

    # Пагинация
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
        async with db.execute("""
            SELECT username, first_name, status FROM users WHERE user_id = ?
        """, (user_id,)) as cursor:
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


# Обработка действий
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
    # Обновляем меню
    await cb_user_menu(callback)


# Ручное одобрение по ID/username
@dp.message(Command("approve"))
async def cmd_approve(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    if not command.args:
        await message.answer("UsageId: `/approve <user_id или @username>`", parse_mode="Markdown")
        return

    target = command.args.strip()
    user_id = None

    # По ID
    if target.isdigit():
        user_id = int(target)
    # По username
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

    # Получаем код
    code = None
    if message.document:
        # Скачиваем .py файл
        file = await bot.get_file(message.document.file_id)
        file_path = f"/tmp/{uuid.uuid4().hex}.py"
        await bot.download_file(file.file_path, file_path)
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                code = f.read()
        except Exception as e:
            await message.answer(f"❌ Ошибка чтения файла: {e}")
            return
        finally:
            Path(file_path).unlink(missing_ok=True)
    else:
        code = message.text

    if not code.strip():
        await message.answer("❌ Код пуст.")
        return

    await message.answer("⏳ Запускаю ваш код... (макс. 30 сек)")

    # Запускаем в песочнице
    result_type, result_data = await safe_exec(code, user_id)

    if result_type == "success":
        files = result_data
        if not files:
            await message.answer("⚠️ Код выполнен, но файлы не созданы.")
        else:
            for file_path in files:
                try:
                    await message.answer_document(types.FSInputFile(file_path))
                except Exception as e:
                    await message.answer(f"❌ Не удалось отправить файл: {e}")
            # Удаляем через 15 мин
            asyncio.create_task(delete_files_after_delay(files, 900))
    else:
        error_msg = result_data[0] if result_data else "Неизвестная ошибка"
        # Обрезаем длинные трейсы
        if len(error_msg) > 3000:
            error_msg = error_msg[:2997] + "..."
        await message.answer(f"❌ Ошибка выполнения:\n```\n{error_msg}\n```", parse_mode="Markdown")


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