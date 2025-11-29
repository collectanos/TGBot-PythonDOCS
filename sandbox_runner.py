#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Изолированный исполнитель кода.
Запускается как отдельный процесс: `python sandbox_runner.py <temp_dir> <code_file>`
Выводит JSON: {"status": "success", "files": [...]} или {"status": "error", "message": "..."}
"""

import sys
import os
import json
import traceback
from pathlib import Path

# Сохраняем ОРИГИНАЛЬНЫЙ __import__ до подмены
_original_import = __import__

# Разрешённые модули
ALLOWED_MODULES = {
    'random', 'datetime', 're', 'json', 'math', 'textwrap', 'base64', 'io',
    'os.path',
    'docx', 'pptx', 'reportlab', 'PIL', 'requests',
}

def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    # Разрешаем 'os' → только os.path
    if name == 'os':
        import types
        import os as real_os
        fake_os = types.SimpleNamespace()
        fake_os.path = real_os.path
        return fake_os
    
    # Проверяем базовое имя модуля
    base_name = name.split('.')[0]
    if base_name not in ALLOWED_MODULES:
        raise ImportError(f"❌ Запрещён импорт: {name}")
    
    # Используем ОРИГИНАЛЬНЫЙ __import__, а не рекурсивный
    return _original_import(name, globals, locals, fromlist, level)

def main():
    if len(sys.argv) != 3:
        print(json.dumps({"status": "error", "message": "UsageId: sandbox_runner.py <temp_dir> <code_file>"}))
        return

    temp_dir = Path(sys.argv[1])
    code_file = Path(sys.argv[2])

    temp_dir.mkdir(parents=True, exist_ok=True)

    # Подменяем импорт
    import builtins
    builtins.__import__ = safe_import

    # Патчим save() методы
    try:
        from docx import Document
        orig_save = Document.save
        def patched_save(self, filename):
            filename = os.path.basename(str(filename))
            if not filename.lower().endswith(('.docx', '.pdf', '.pptx', '.png', '.jpg', '.jpeg')):
                raise ValueError("❌ Только .docx/.pptx/.pdf/.png/.jpg")
            return orig_save(self, str(temp_dir / filename))
        Document.save = patched_save
    except Exception:
        pass

    try:
        from pptx import Presentation
        orig_save = Presentation.save
        def patched_save(self, filename):
            filename = os.path.basename(str(filename))
            if not filename.lower().endswith(('.pptx', '.pdf')):
                raise ValueError("❌ Только .pptx/.pdf")
            return orig_save(self, str(temp_dir / filename))
        Presentation.save = patched_save
    except Exception:
        pass

    try:
        from reportlab.pdfgen import canvas
        orig_init = canvas.Canvas.__init__
        def patched_init(self, filename, *args, **kwargs):
            filename = os.path.basename(str(filename))
            if not filename.lower().endswith('.pdf'):
                raise ValueError("❌ Только .pdf")
            return orig_init(self, str(temp_dir / filename), *args, **kwargs)
        canvas.Canvas.__init__ = patched_init
    except Exception:
        pass

    # Подготавливаем глобальные переменные
    g = {
        '__builtins__': __builtins__,
        '__name__': '__main__',
        'BytesIO': __import__('io').BytesIO,
        'StringIO': __import__('io').StringIO,
    }
    
    # Добавляем разрешённые встроенные модули
    for mod_name in ['random', 'datetime', 're', 'json', 'math', 'textwrap', 'base64']:
        g[mod_name] = __import__(mod_name)

    try:
        with open(code_file, 'r', encoding='utf-8') as f:
            code = f.read()
        exec(code, g)

        files = [str(f) for f in temp_dir.iterdir() if f.is_file()]
        print(json.dumps({"status": "success", "files": files}))
    except Exception as e:
        msg = f"{type(e).__name__}: {e}\n\n{traceback.format_exc(limit=2)}"
        print(json.dumps({"status": "error", "message": msg}))


if __name__ == "__main__":
    main()