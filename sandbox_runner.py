#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Изолированный исполнитель кода.
Запускается как отдельный процесс: `python sandbox_runner.py <temp_dir> <code_file>`
Выводит JSON: {"status": "success", "files": [...]} или {"status": "error", "message": "..."}
"""

import sys
import os
import json as json_lib  # ← импортируем заранее, чтобы не зависеть от sandbox
import traceback
from pathlib import Path

# Сохраняем оригинальный __import__ до подмены
_original_import = __import__

# ⚠️ ЗАПРЕЩЁННЫЕ модули (только реально опасные)
FORBIDDEN_MODULES = {
    'subprocess', 'socket', 'threading', 'multiprocessing',
    'inspect', 'pickle', 'shutil', 'ctypes', 'code', 'compile',
    'exec', 'eval', '__import__', 'runpy', 'importlib.util',
}

# 🔒 Безопасная обёртка для sys
class SafeSys:
    def __init__(self, real_sys):
        self._real_sys = real_sys
        # Разрешаем только безопасные атрибуты
        self.argv = [__file__]
        self.path = real_sys.path.copy()
        self.modules = real_sys.modules
        self.version = real_sys.version
        self.platform = real_sys.platform
        self.byteorder = real_sys.byteorder
        self.executable = real_sys.executable

    def __getattr__(self, name):
        if name in ('exit', '_getframe', 'stdin', 'stdout', 'stderr', 'settrace', 'setprofile'):
            raise AttributeError(f"❌ Доступ к sys.{name} запрещён в целях безопасности")
        return getattr(self._real_sys, name)

def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    base_name = name.split('.')[0]
    
    # Запрещаем опасные модули
    if base_name in FORBIDDEN_MODULES:
        raise ImportError(f"❌ Запрещён опасный модуль: {name}")
    
    # Подменяем sys → безопасная обёртка
    if name == 'sys':
        import sys as real_sys
        return SafeSys(real_sys)
    
    # Подменяем os → только os.path
    if name == 'os':
        import types
        import os as real_os
        fake_os = types.SimpleNamespace()
        fake_os.path = real_os.path
        return fake_os

    # Всё остальное — как есть
    return _original_import(name, globals, locals, fromlist, level)

def main():
    if len(sys.argv) != 3:
        print(json_lib.dumps({"status": "error", "message": "UsageId: sandbox_runner.py <temp_dir> <code_file>"}))
        return

    temp_dir = Path(sys.argv[1])
    code_file = Path(sys.argv[2])
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Подменяем импорт
    import builtins
    builtins.__import__ = safe_import

    # Патчим save() для docx
    try:
        from docx import Document
        orig_save = Document.save
        def patched_save(self, filename):
            filename = os.path.basename(str(filename))
            if not filename.lower().endswith(('.docx', '.pdf', '.pptx', '.png', '.jpg', '.jpeg')):
                raise ValueError("❌ Только .docx/.pptx/.pdf/.png/.jpg")
            return orig_save(self, str(temp_dir / filename))
        Document.save = patched_save
    except Exception as e:
        pass

    # Патчим save() для pptx
    try:
        from pptx import Presentation
        orig_save = Presentation.save
        def patched_save(self, filename):
            filename = os.path.basename(str(filename))
            if not filename.lower().endswith(('.pptx', '.pdf')):
                raise ValueError("❌ Только .pptx/.pdf")
            return orig_save(self, str(temp_dir / filename))
        Presentation.save = patched_save
    except Exception as e:
        pass

    # Патчим Canvas для reportlab
    try:
        from reportlab.pdfgen import canvas
        orig_init = canvas.Canvas.__init__
        def patched_init(self, filename, *args, **kwargs):
            filename = os.path.basename(str(filename))
            if not filename.lower().endswith('.pdf'):
                raise ValueError("❌ Только .pdf")
            return orig_init(self, str(temp_dir / filename), *args, **kwargs)
        canvas.Canvas.__init__ = patched_init
    except Exception as e:
        pass

    # Глобальные переменные для exec
    g = {
        '__builtins__': __builtins__,
        '__name__': '__main__',
        'BytesIO': __import__('io').BytesIO,
        'StringIO': __import__('io').StringIO,
    }

    # Добавляем безопасные модули
    for mod in ['random', 'datetime', 're', 'math', 'textwrap', 'base64']:
        try:
            g[mod] = __import__(mod)
        except Exception:
            pass

    # Вручную добавляем json (через заранее импортированный json_lib)
    g['json'] = json_lib

    try:
        with open(code_file, 'r', encoding='utf-8') as f:
            code = f.read()
        exec(code, g)

        files = [str(f) for f in temp_dir.iterdir() if f.is_file()]
        print(json_lib.dumps({"status": "success", "files": files}))
    except Exception as e:
        msg = f"{type(e).__name__}: {e}\n\n{traceback.format_exc(limit=2)}"
        print(json_lib.dumps({"status": "error", "message": msg}))


if __name__ == "__main__":
    main()