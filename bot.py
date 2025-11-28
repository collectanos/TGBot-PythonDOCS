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
            # Разрешаем os.path, но не os
            if name == 'os':
                import types, os as real_os
                fake_os = types.SimpleNamespace()
                fake_os.path = real_os.path
                return fake_os
            base_name = name.split('.')[0]
            if base_name not in ALLOWED_MODULES:
                raise ImportError(f"❌ Запрещён импорт: {name}")
            return original_import(name, *args, **kwargs)

        builtins.__import__ = safe_import

        import io as _io
        import os as _os
        safe_temp = Path(temp_subdir)
        safe_temp.mkdir(parents=True, exist_ok=True)

        # ——— Патчим save() для docx ———
        try:
            from docx import Document
            orig_save = Document.save

            def patched_save(self, filename):
                filename = _os.path.basename(str(filename))
                if not filename.lower().endswith(('.docx', '.pdf', '.pptx', '.png', '.jpg', '.jpeg')):
                    raise ValueError("❌ Разрешены только: .docx, .pptx, .pdf, .png, .jpg")
                full_path = safe_temp / filename
                return orig_save(self, str(full_path))
            Document.save = patched_save
        except Exception as e:
            pass  # Если docx не используется — игнорируем

        # ——— Патчим save() для pptx ———
        try:
            from pptx import Presentation
            orig_save = Presentation.save

            def patched_save(self, filename):
                filename = _os.path.basename(str(filename))
                if not filename.lower().endswith(('.pptx', '.pdf')):
                    raise ValueError("❌ Разрешены только: .pptx, .pdf")
                full_path = safe_temp / filename
                return orig_save(self, str(full_path))
            Presentation.save = patched_save
        except Exception as e:
            pass

        # ——— Патчим Canvas для reportlab ———
        try:
            from reportlab.pdfgen import canvas
            orig_init = canvas.Canvas.__init__

            def patched_init(self, filename, *args, **kwargs):
                filename = _os.path.basename(str(filename))
                if not filename.lower().endswith('.pdf'):
                    raise ValueError("❌ Разрешены только: .pdf")
                full_path = safe_temp / filename
                return orig_init(self, str(full_path), *args, **kwargs)
            canvas.Canvas.__init__ = patched_init
        except Exception as e:
            pass

        # ——— Глобальные переменные для exec ———
        sandbox_globals = {
            '__builtins__': __builtins__,
            '__name__': '__main__',
            'BytesIO': _io.BytesIO,
            'StringIO': _io.StringIO,
        }

        # Добавляем разрешённые встроенные модули
        for mod_name in ['random', 'datetime', 're', 'json', 'math', 'textwrap', 'base64']:
            sandbox_globals[mod_name] = __import__(mod_name)

        # Выполняем код
        exec(code, sandbox_globals)

        # Собираем сгенерированные файлы
        generated_files = [
            str(f) for f in safe_temp.iterdir()
            if f.is_file() and f.suffix.lower() in ['.docx', '.pptx', '.pdf', '.png', '.jpg', '.jpeg']
        ]

        result_pipe.send(("success", generated_files))

    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)}\n\n{traceback.format_exc(limit=2)}"
        result_pipe.send(("error", [error_msg]))


async def safe_exec(code: str, user_id: int) -> Tuple[str, List[str]]:
    temp_subdir = TEMP_DIR / f"{user_id}_{uuid.uuid4().hex}"
    parent_conn, child_conn = multiprocessing.Pipe()
    
    proc = multiprocessing.Process(
        target=_run_code_in_sandbox,
        args=(code, str(temp_subdir), child_conn),
        daemon=True
    )
    proc.start()

    try:
        if parent_conn.poll(30):  # Таймаут 30 секунд
            return parent_conn.recv()
        proc.terminate()
        proc.join(1)
        if proc.is_alive():
            proc.kill()
        return "error", ["⚠️ Превышено время выполнения (30 секунд)"]
    except Exception as e:
        return "error", [f"❌ Критическая ошибка: {str(e)}"]
    finally:
        proc.join(timeout=1)


# ==============================
# 🗑 AUTO-DELETE
# ==============================

async def delete_files_after_delay(file_paths: List[str], delay: int = 900):
    await asyncio.sleep(delay)
    for path in file_paths:
        try:
            Path(path).unlink(missing_ok=True)
        except Exception as e:
            print(f"❌ Ошибка удаления {path}: {e}")

# ==============================
# 🤖 BOT SETUP
# ==============================

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher(storage=MemoryStorage())

# ==============================
# 🧑 USER UTILS
# ==============================

async def ensure_user_registered(user: types.User):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR IGNORE INTO users (user_id, username, first_name)
            VALUES (?, ?, ?)
        """, (user.id, user.username, user.first_name))
        await db.commit()

async def get_user_status(user_id: int) -> str:
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
        await message.answer("❌ Вы заблокированы в боте.")
        return
    elif status == "pending":
        text = (
            "👋 Привет! Я — бот для генерации документов по вашему коду.\n\n"
            "✅ Поддерживаемые форматы:\n"
            " • .docx (через python-docx)\n"
            " • .pptx (через python-pptx)\n"
            " • .pdf  (через reportlab)\n\n"
            "🖼 Можно загружать изображения из интернета через requests + PIL.\n\n"
            "⏳ Ваш аккаунт ожидает одобрения администратором. Пожалуйста, ожидайте."
        )
        await message.answer(text)
        
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
            except Exception as e:
                print(f"❌ Не удалось уведомить админа {admin_id}: {e}")
    else:  # approved
        text = (
            "✅ Добро пожаловать!\n\n"
            "Отправьте Python-код для генерации документа:\n"
            " • Текстом в сообщении\n"
            " • Или прикрепите файл с расширением `.py`\n\n"
            "ℹ️ Подробнее — команда /info"
        )
        await message.answer(text)

@dp.message(Command("info"))
async def cmd_info(message: types.Message):
    text = (
        "📄 *Бот генерирует документы по вашему коду*\n\n"
        "✅ *Поддерживаемые форматы:*\n"
        " • `.docx` — через `python-docx`\n"
        " • `.pptx` — через `python-pptx`\n"
        " • `.pdf`  — через `reportlab`\n\n"
        "🖼 *Работа с изображениями:*\n"
        " • Загрузка по URL: `requests.get(url).content`\n"
        " • Обработка: `PIL.Image.open(BytesIO(content))`\n\n"
        "🔧 *Разрешённые библиотеки:*\n"
        "`random`, `datetime`, `re`, `json`, `math`, `textwrap`, `base64`, `io`, `os.path`,\n"
        "`docx`, `pptx`, `reportlab`, `PIL`, `requests`\n\n"
        "❌ *Запрещено:* `os`, `sys`, `subprocess`, `eval`, `exec`, `socket`, и др.\n\n"
        "ℹ️ Все файлы автоматически удаляются через 15 минут."
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
        await message.answer("❌ Пользователь не найден в базе.")
        return
    
    status, username, first_name = row
    status_text = {
        "approved": "✅ одобрен",
        "banned": "❌ заблокирован",
        "pending": "⏳ ожидает подтверждения"
    }.get(status, status)
    
    profile_text = (
        f"👤 *Имя:* {first_name or '—'}\n"
        f"🆔 *ID:* `{message.from_user.id}`\n"
        f"📇 *Username:* @{username or '—'}\n"
        f"🛡 *Статус:* {status_text}"
    )
    await message.answer(profile_text, parse_mode="Markdown")

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
        " • 🚫 Заблокировать / 🔓 Разблокировать\n"
        " • Изменить количество жетонов (если будет добавлено)"
    )
    await message.answer(help_text, parse_mode="Markdown")

# ==============================
# 👑 ADMIN PANEL
# ==============================

USERS_PER_PAGE = 5

async def get_paginated_users(page: int):
    offset = (page - 1) * USERS_PER_PAGE
    async with aiosqlite.connect(DB_PATH) as db:
        # Общее количество пользователей
        async with db.execute("SELECT COUNT(*) FROM users") as cursor:
            total = (await cursor.fetchone())[0]
        
        # Список пользователей для страницы
        async with db.execute("""
            SELECT user_id, username, first_name, status 
            FROM users 
            ORDER BY created_at DESC 
            LIMIT ? OFFSET ?
        """, (USERS_PER_PAGE, offset)) as cursor:
            users = await cursor.fetchall()
    
    return users, total

def build_players_keyboard(users: List[tuple], page: int, total: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    
    # Кнопки пользователей
    for user_id, username, first_name, status in users:
        name = f"{first_name or ''} @{username or '—'}".strip()
        if not name or name == "@—":
            name = f"ID {user_id}"
        if len(name) > 25:
            name = name[:22] + "..."
        
        status_icon = {
            "approved": "✅",
            "banned": "❌",
            "pending": "⏳"
        }.get(status, "❓")
        
        builder.button(
            text=f"{status_icon} {name}",
            callback_data=f"user_{user_id}"
        )
    builder.adjust(1)
    
    # Пагинация
    total_pages = (total + USERS_PER_PAGE - 1) // USERS_PER_PAGE
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"players_{page-1}"))
    nav_buttons.append(InlineKeyboardButton(text=f"Стр. {page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton(text="Вперёд ➡️", callback_data=f"players_{page+1}"))
    
    if nav_buttons:
        builder.row(*nav_buttons)
    
    return builder.as_markup()

@dp.message(Command("players"))
async def cmd_players(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    page = 1
    if command.args and command.args.isdigit():
        page = int(command.args)
    
    users, total = await get_paginated_users(page)
    if not users:
        await message.answer("📭 Пользователи не найдены.")
        return
    
    keyboard = build_players_keyboard(users, page, total)
    await message.answer(f"👥 Список пользователей (страница {page}):", reply_markup=keyboard)

@dp.callback_query(lambda c: c.data.startswith("players_"))
async def cb_players_pagination(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    try:
        page = int(callback.data.split("_")[1])
    except (IndexError, ValueError):
        await callback.answer("❌ Неверный формат страницы")
        return
    
    users, total = await get_paginated_users(page)
    if not users:
        await callback.answer("❌ Пользователи не найдены на этой странице")
        return
    
    keyboard = build_players_keyboard(users, page, total)
    await callback.message.edit_text(f"👥 Список пользователей (страница {page}):", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("user_"))
async def cb_user_details(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    try:
        user_id = int(callback.data.split("_")[1])
    except (IndexError, ValueError):
        await callback.answer("❌ Неверный ID пользователя")
        return
    
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT username, first_name, status FROM users WHERE user_id = ?
        """, (user_id,)) as cursor:
            row = await cursor.fetchone()
    
    if not row:
        await callback.answer("❌ Пользователь не найден в базе", show_alert=True)
        return
    
    username, first_name, status = row
    name = f"{first_name or ''} @{username or '—'}".strip() or f"ID {user_id}"
    
    # Клавиатура действий
    builder = InlineKeyboardBuilder()
    
    if status == "pending":
        builder.button(text="✅ Одобрить", callback_data=f"approve_{user_id}")
    elif status == "approved":
        builder.button(text="🔄 Сбросить статус", callback_data=f"reset_{user_id}")
    
    if status != "banned":
        builder.button(text="🚫 Заблокировать", callback_data=f"ban_{user_id}")
    else:
        builder.button(text="🔓 Разблокировать", callback_data=f"unban_{user_id}")
    
    builder.button(text="⬅️ Назад к списку", callback_data="back_players")
    builder.adjust(1)
    
    status_text = {
        "approved": "✅ одобрен",
        "banned": "❌ заблокирован",
        "pending": "⏳ ожидает подтверждения"
    }.get(status, status)
    
    await callback.message.edit_text(
        f"👤 *{name}*\n"
        f"🆔 ID: `{user_id}`\n"
        f"🛡 Статус: {status_text}",
        parse_mode="Markdown",
        reply_markup=builder.as_markup()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "back_players")
async def cb_back_to_players(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    users, total = await get_paginated_users(1)
    if not users:
        await callback.message.edit_text("📭 Пользователи не найдены.")
        return
    
    keyboard = build_players_keyboard(users, 1, total)
    await callback.message.edit_text("👥 Список пользователей (страница 1):", reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.data.split("_")[0] in ["approve", "ban", "unban", "reset"])
async def cb_admin_action(callback: types.CallbackQuery):
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    action, user_id_str = callback.data.split("_", 1)
    try:
        user_id = int(user_id_str)
    except ValueError:
        await callback.answer("❌ Неверный ID пользователя", show_alert=True)
        return
    
    async with aiosqlite.connect(DB_PATH) as db:
        if action == "approve":
            await db.execute("""
                UPDATE users 
                SET status = 'approved', approved_by = ?, approved_at = ?
                WHERE user_id = ? AND status = 'pending'
            """, (callback.from_user.id, datetime.now().isoformat(), user_id))
        elif action == "ban":
            await db.execute("UPDATE users SET status = 'banned' WHERE user_id = ?", (user_id,))
        elif action == "unban":
            await db.execute("UPDATE users SET status = 'pending' WHERE user_id = ?", (user_id,))
        elif action == "reset":
            await db.execute("UPDATE users SET status = 'pending' WHERE user_id = ?", (user_id,))
        
        await db.commit()
    
    await callback.answer(f"✅ Действие '{action}' выполнено", show_alert=True)
    await cb_user_details(callback)

@dp.message(Command("approve"))
async def cmd_approve(message: types.Message, command: CommandObject):
    if message.from_user.id not in ADMIN_IDS:
        return
    
    if not command.args:
        await message.answer("UsageId: `/approve <user_id или @username>`", parse_mode="Markdown")
        return
    
    target = command.args.strip()
    user_id = None
    
    # Поиск по ID
    if target.isdigit():
        user_id = int(target)
    # Поиск по username
    elif target.startswith("@"):
        username = target[1:]
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT user_id FROM users WHERE username = ?", (username,)) as cursor:
                row = await cursor.fetchone()
                if row:
                    user_id = row[0]
    
    if not user_id:
        await message.answer("❌ Пользователь не найден. Проверьте ID или username.")
        return
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE users 
            SET status = 'approved', approved_by = ?, approved_at = ?
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

# ==============================
# 📥 CODE HANDLING
# ==============================

@dp.message()
async def handle_code(message: types.Message):
    # Пропускаем команды
    if message.text and message.text.startswith('/'):
        return
    
    # Проверяем тип сообщения
    has_text = bool(message.text)
    has_file = bool(message.document and message.document.mime_type == "text/x-python")
    
    if not (has_text or has_file):
        return
    
    user_id = message.from_user.id
    status = await get_user_status(user_id)
    
    if status != "approved":
        text = "❌ Вы заблокированы." if status == "banned" else "⏳ Ваш аккаунт ожидает подтверждения администратором."
        await message.answer(text)
        return
    
    # Получаем код
    code = None
    if has_file:
        try:
            file = await bot.get_file(message.document.file_id)
            file_path = f"/tmp/{uuid.uuid4().hex}.py"
            await bot.download_file(file.file_path, file_path)
            
            with open(file_path, "r", encoding="utf-8") as f:
                code = f.read()
            
            Path(file_path).unlink(missing_ok=True)
        except Exception as e:
            await message.answer(f"❌ Ошибка при чтении файла: {str(e)}")
            return
    else:
        code = message.text
    
    if not code or not code.strip():
        await message.answer("❌ Присланный код пуст.")
        return
    
    await message.answer("⏳ Выполняю ваш код... (максимум 30 секунд)")
    
    # Запускаем в песочнице
    result_type, result_data = await safe_exec(code, user_id)
    
    if result_type == "success":
        file_paths = result_data
        if not file_paths:
            await message.answer("⚠️ Код выполнен успешно, но файлы не созданы.")
            return
        
        for file_path in file_paths:
            try:
                await message.answer_document(types.FSInputFile(file_path))
            except Exception as e:
                await message.answer(f"❌ Ошибка отправки файла: {str(e)}")
        
        # Запускаем удаление через 15 минут
        asyncio.create_task(delete_files_after_delay(file_paths))
    
    else:  # error
        error_msg = result_data[0] if result_data else "Неизвестная ошибка"
        if len(error_msg) > 3500:
            error_msg = error_msg[:3500] + "..."
        
        await message.answer(
            f"❌ Ошибка при выполнении кода:\n```\n{error_msg}\n```",
            parse_mode="Markdown"
        )

# ==============================
# 🚀 MAIN
# ==============================

async def main():
    await init_db()
    print("✅ Бот успешно запущен!")
    print(f"🔑 ADMIN_IDS: {ADMIN_IDS}")
    print("🛑 Для остановки нажмите Ctrl+C")
    
    try:
        await dp.start_polling(bot)
    except TelegramConflictError:
        print("❌ Ошибка: обнаружен другой запущенный экземпляр бота с этим токеном.")
        print("💡 Решение: остановите все другие процессы бота и перезапустите.")
    except Exception as e:
        print(f"❌ Критическая ошибка: {e}")
        traceback.print_exc()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n🛑 Бот остановлен вручную.")
    except Exception as e:
        print(f"❌ Фатальная ошибка: {e}")
        traceback.print_exc()