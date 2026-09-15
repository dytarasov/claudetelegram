"""Чтение логов с конца файла.

Тонкость одна, зато важная: логи растут до мегабайтов, а нужны почти всегда
последние несколько десятков строк. Читать файл целиком ради этого — верный
способ съесть память на ровном месте, поэтому берём последний кусок с конца и
разбираем только его. Если строк в куске не хватило, отступаем дальше назад —
но не бесконечно: за пределом просто честно отдаём, что нашли.
"""
from __future__ import annotations

import logging
from pathlib import Path

from ...settings import Settings

log = logging.getLogger(__name__)

# Сколько байт с конца читаем за один заход и до какого предела расширяемся.
CHUNK = 256 * 1024
MAX_READ = 4 * 1024 * 1024


class FileLogReader:
    """Реализация порта LogReader поверх файлов в run/."""

    def __init__(self, settings: Settings) -> None:
        self._files: dict[str, Path] = {
            "events": settings.run_dir / "events.log",
            "app": settings.run_dir / "app.log",
            "guard": settings.run_dir / "guard.log",
        }

    def streams(self) -> list[str]:
        return [name for name, path in self._files.items() if path.exists()]

    def path(self, stream: str) -> Path | None:
        path = self._files.get(stream)
        return path if path and path.exists() else None

    def size(self, stream: str) -> int:
        path = self.path(stream)
        return path.stat().st_size if path else 0

    def tail(self, stream: str, limit: int, needle: str | None = None) -> list[str]:
        path = self.path(stream)
        if not path:
            return []
        needle = (needle or "").strip().lower()
        want = max(1, limit)
        read = CHUNK
        while True:
            lines = self._read_tail(path, read)
            if needle:
                lines = [line for line in lines if needle in line.lower()]
            # Прочитали весь файл или набрали достаточно — отдаём.
            if len(lines) >= want or read >= MAX_READ or read >= path.stat().st_size:
                return lines[-want:]
            read *= 4

    @staticmethod
    def _read_tail(path: Path, size: int) -> list[str]:
        try:
            with path.open("rb") as fh:
                fh.seek(0, 2)
                start = max(0, fh.tell() - size)
                fh.seek(start)
                data = fh.read()
            text = data.decode("utf-8", "replace")
            if start:  # первая строка почти наверняка обрезана посередине
                text = text.split("\n", 1)[-1]
            return [line for line in text.splitlines() if line.strip()]
        except OSError:
            log.warning("не смог прочитать %s", path, exc_info=True)
            return []
