"""Точка запуска для systemd.

Юнит claude-tg.service указывает на этот файл исторически, и менять его путь
нельзя: unit-файл лежит в /etc/systemd/system, а откат сторожа — это git reset
внутри проекта. Если бы ExecStart указывал на файл, который в старой версии
называется иначе, откат оставил бы систему в нерабочем состоянии.

Поэтому здесь остаётся тонкая прослойка, а всё приложение живёт в src/app/.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.main import main  # noqa: E402

if __name__ == "__main__":
    main()
