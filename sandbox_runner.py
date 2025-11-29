#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Изолированный исполнитель кода.
Запускается как отдельный процесс: `python sandbox_runner.py <temp_dir> <code_file>`
"""

import sys
import os
import json as json_module
from pathlib import Path

# Сохраняем оригинальный __import__ ДО подмены
_original_import = __import__

# ⚠️ Только реально опасные модули запрещены
FORBIDDEN_MODULES = {
    'subprocess', 'socket', 'threading', 'multiprocessing',
    'inspect', 'pickle', 'shutil', 'ctypes', 'code', 'compile',
    'exec', 'eval', '__import__', 'runpy', 'importlib.util',
}

# 🔒 Безопасный sys
class SafeSys:
    def __init__(self, real_sys):
        self._real_sys = real_sys
        self.argv = [__file__]
        self.path = real_sys.path.copy()
        self.modules = real_sys.modules
        self.version = real_sys.version
        self.platform = real_sys.platform

    def __getattr__(self, name):
        if name in ('exit', '_getframe', 'stdin', 'stdout', 'stderr', 'settrace', 'setprofile'):
            raise RuntimeError(f"❌ sys.{name} запрещён")
        return getattr(self._real_sys, name)

def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
    base = name.split('.')[0]
    if base in FORBIDDEN_MODULES:
        raise ImportError(f"❌ Запрещён: {name}")
    if name == 'sys':
        import sys as real_sys
        return SafeSys(real_sys)
    if name == 'os':
        import types, os as real_os
        fake_os = types.SimpleNamespace()
        fake_os.path = real_os.path
        return fake_os
    return _original_import(name, globals, locals, fromlist, level)

def main():
    if len(sys.argv) != 3:
        print(json_module.dumps({"status": "error", "message": "Нужно 2 аргумента: temp_dir code_file"}))
        return

    temp_dir = Path(sys.argv[1])
    code_file = Path(sys.argv[2])
    temp_dir.mkdir(parents=True, exist_ok=True)

    # Подмена импорта
    import builtins
    builtins.__import__ = safe_import

    # Патчим save()
    try:
        from docx import Document
        orig = Document.save
        def patched(self, fn):
            fn = os.path.basename(str(fn))
            if not fn.lower().endswith(('.docx', '.pdf', '.pptx', '.png', '.jpg', '.jpeg')):
                raise ValueError("❌ Только .docx/.pptx/.pdf/.png/.jpg")
            return orig(self, str(temp_dir / fn))
        Document.save = patched
    except: pass

    try:
        from pptx import Presentation
        orig = Presentation.save
        def patched(self, fn):
            fn = os.path.basename(str(fn))
            if not fn.lower().endswith(('.pptx', '.pdf')):
                raise ValueError("❌ Только .pptx/.pdf")
            return orig(self, str(temp_dir / fn))
        Presentation.save = patched
    except: pass

    try:
        from reportlab.pdfgen import canvas
        orig = canvas.Canvas.__init__
        def patched(self, fn, *a, **k):
            fn = os.path.basename(str(fn))
            if not fn.lower().endswith('.pdf'):
                raise ValueError("❌ Только .pdf")
            return orig(self, str(temp_dir / fn), *a, **k)
        canvas.Canvas.__init__ = patched
    except: pass

    # Подготавливаем глобальные переменные — ЗАРАНЕЕ ИМПОРТИРУЕМ ВСЁ
    g = {
        '__builtins__': __builtins__,
        '__name__': '__main__',
        'json': json_module,
    }

    # Импортируем по одному — без exec
    try: g['random'] = __import__('random')
    except: pass
    try: g['datetime'] = __import__('datetime')
    except: pass
    try: g['re'] = __import__('re')
    except: pass
    try: g['math'] = __import__('math')
    except: pass
    try: g['textwrap'] = __import__('textwrap')
    except: pass
    try: g['base64'] = __import__('base64')
    except: pass
    try: g['io'] = __import__('io')
    except: pass
    try: g['BytesIO'] = __import__('io').BytesIO
    except: pass
    try: g['StringIO'] = __import__('io').StringIO
    except: pass

    try:
        with open(code_file, 'r', encoding='utf-8') as f:
            code = f.read()
        exec(code, g)

        files = [str(f) for f in temp_dir.iterdir() if f.is_file()]
        print(json_module.dumps({"status": "success", "files": files}))
    except Exception as e:
        # ❗ НЕ ИСПОЛЬЗУЕМ traceback — он вызывает рекурсию
        msg = f"{type(e).__name__}: {e}"
        print(json_module.dumps({"status": "error", "message": msg}))


if __name__ == "__main__":
    main()