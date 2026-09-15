"""Мелкое состояние между перезапусками: id сессии, рабочая директория, модель.

Раньше это был state.json, и он остаётся — но уже как запасной аэродром.
Порядок такой: пишем и туда, и туда; читаем сначала из БД, а если она молчит —
из файла. Причина простая: без state.json бот, поднявшийся при лежащей базе,
потерял бы контекст разговора, а это худшее, что может случиться при откате.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .base import BaseRepository

log = logging.getLogger(__name__)


class KeyValueStore(BaseRepository):
    """KeyValueRepository с файловым дублем."""

    def __init__(self, db, state_file: Path) -> None:
        super().__init__(db)
        self._file = state_file

    # ---- файл ----
    def _file_read(self) -> dict[str, str]:
        try:
            data = json.loads(self._file.read_text())
            return {k: str(v) for k, v in data.items() if v is not None}
        except (OSError, json.JSONDecodeError, AttributeError):
            return {}

    def _file_write(self, data: dict[str, str]) -> None:
        try:
            tmp = self._file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
            tmp.replace(self._file)
        except OSError:
            log.warning("не смог обновить %s", self._file)

    # ---- порт ----
    async def get(self, key: str) -> str | None:
        try:
            async with self._db.connection() as conn:
                value = await conn.fetchval("SELECT value FROM kv WHERE key=$1", key)
            if value is not None:
                return value
        except Exception:  # noqa: BLE001 — падать из-за БД тут нельзя
            log.warning("kv.get(%s): база недоступна, читаю файл", key)
        return self._file_read().get(key)

    async def set(self, key: str, value: str) -> None:
        snapshot = self._file_read()
        snapshot[key] = value
        self._file_write(snapshot)
        try:
            async with self._db.connection() as conn:
                await conn.execute(
                    """INSERT INTO kv(key, value) VALUES($1,$2)
                       ON CONFLICT (key) DO UPDATE SET value=$2, updated_at=now()""",
                    key, value,
                )
        except Exception:  # noqa: BLE001
            log.warning("kv.set(%s): база недоступна, осталось только в файле", key)

    async def all(self) -> dict[str, str]:
        merged = self._file_read()
        try:
            async with self._db.connection() as conn:
                rows = await conn.fetch("SELECT key, value FROM kv")
            merged.update({r["key"]: r["value"] for r in rows})
        except Exception:  # noqa: BLE001
            log.warning("kv.all(): база недоступна, отдаю файл")
        return merged
