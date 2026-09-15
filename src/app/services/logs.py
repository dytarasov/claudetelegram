"""Показ собственных логов: последние строки и выгрузка файлом.

Смысл этого сервиса — чтобы не приходилось лезть по SSH ради вопроса «а что
вообще сейчас происходило». Два режима:

  • лента событий (по умолчанию) — короткие человеческие строки;
  • подробный лог — со всеми техническими деталями, для разбора аварий.

Выгрузка файлом сжимает лог, если он крупный: Telegram не примет больше 50 МБ,
да и качать десятки мегабайт текста в телефон незачем.
"""
from __future__ import annotations

import gzip
import shutil
import tempfile
from pathlib import Path

from ..domain.errors import UserError
from ..domain.ports import LogReader

# Крупнее этого — отдаём сжатым. Текстовые логи жмутся примерно в десять раз.
GZIP_OVER = 2 * 1024 * 1024
TELEGRAM_LIMIT = 45 * 1024 * 1024

ALIASES = {
    "события": "events", "лента": "events", "events": "events",
    "подробно": "app", "всё": "app", "app": "app", "детально": "app",
    "сторож": "guard", "guard": "guard",
}


class LogService:
    def __init__(self, reader: LogReader) -> None:
        self._reader = reader

    def resolve(self, name: str | None) -> str:
        stream = ALIASES.get((name or "events").strip().lower())
        if stream is None:
            raise UserError(f"не знаю такого лога: {name}. Есть: события, подробно, сторож")
        return stream

    def tail(self, stream: str, limit: int = 30, needle: str | None = None) -> list[str]:
        return self._reader.tail(stream, limit, needle)

    def problems(self, limit: int = 30) -> list[str]:
        """Только тревожное из подробного лога — быстрый ответ на «что сломалось»."""
        lines = self._reader.tail("app", limit * 6)
        bad = [ln for ln in lines if " WARNING " in ln or " ERROR " in ln or " CRITICAL " in ln]
        return bad[-limit:]

    def sizes(self) -> dict[str, int]:
        return {name: self._reader.size(name) for name in self._reader.streams()}

    def export(self, stream: str) -> tuple[Path, bool]:
        """Файл для отправки. Возвращает (путь, сжат ли).

        Копия делается всегда: лог пишется прямо сейчас, и отдавать его
        меняющимся под руками — верный способ получить обрезанный файл.
        """
        source = self._reader.path(stream)
        if source is None:
            raise UserError(f"лог «{stream}» пока пуст")
        size = source.stat().st_size
        if size > TELEGRAM_LIMIT:
            raise UserError(f"лог слишком большой ({size / 1024 / 1024:.0f} МБ)")

        tmp = Path(tempfile.mkdtemp(prefix="logs-"))
        if size > GZIP_OVER:
            target = tmp / f"{stream}.log.gz"
            with source.open("rb") as src, gzip.open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
            return target, True
        target = tmp / f"{stream}.log"
        shutil.copyfile(source, target)
        return target, False
