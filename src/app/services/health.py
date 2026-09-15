"""Пульс и сводка о живости.

Пульс — это файл run/heartbeat.json, который процесс переписывает раз в
несколько секунд. По нему сторож снаружи понимает, ожил ли бот после выкатки.

Почему файл, а не таблица в БД: сторож обязан работать в самой плохой ситуации —
когда бот не поднялся вовсе или Postgres лёг. Файл читается четырьмя строчками
на python3 из системного интерпретатора, без venv и без зависимостей.

Пульс намеренно строже, чем «процесс запустился»: в нём три флага — ready
(дошли до конца инициализации), telegram_ok (Telegram отвечает) и claude_alive
(процесс claude жив). Версия, которая стартует, но не может ответить в чат,
здоровой не считается и будет откачена.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time

from ..domain.dto import HealthView
from ..domain.models import Heartbeat
from ..domain.ports import AssistantProcess, Notifier, StorageHealth, VersionControl
from ..settings import Settings

from . import events

log = logging.getLogger(__name__)

# Как часто перепроверять доступность Telegram и БД: каждый удар пульса дёргать
# внешние системы незачем, а раз в минуту — в самый раз.
DEEP_CHECK_EVERY_S = 60.0


class HealthService:
    def __init__(self, settings: Settings, process: AssistantProcess,
                 notifier: Notifier, storage: StorageHealth, git: VersionControl) -> None:
        self._s = settings
        self._process = process
        self._notifier = notifier
        self._storage = storage
        self._git = git
        self._started = time.time()
        self._telegram_ok = False
        self._db_ok = False
        self._last_deep = 0.0
        self._commit: str | None = None

    async def _deep_check(self) -> None:
        """Раз в минуту убеждаемся, что внешний мир на месте."""
        now = time.time()
        if now - self._last_deep < DEEP_CHECK_EVERY_S and self._telegram_ok:
            return
        self._last_deep = now
        # Событие пишем только на переходах: иначе лента раз в минуту твердила бы
        # «всё хорошо» и потеряла бы всякий смысл.
        was_telegram, was_db = self._telegram_ok, self._db_ok
        self._telegram_ok = await self._notifier.alive()
        self._db_ok = await self._storage.healthy()
        if self._telegram_ok != was_telegram:
            events.health_changed("telegram", self._telegram_ok)
        if self._db_ok != was_db:
            events.health_changed("postgres", self._db_ok)

    def _current_commit(self) -> str | None:
        if self._commit is None:
            try:
                self._commit = self._git.head()
            except Exception:  # noqa: BLE001
                self._commit = ""
        return self._commit or None

    def beat(self, *, ready: bool = True) -> None:
        """Записать пульс. Файл переписывается атомарно, через .tmp + replace."""
        hb = Heartbeat(
            pid=os.getpid(), ts=time.time(), ready=ready,
            commit=self._current_commit(),
            session_id=self._process.status().get("session_id"),
            telegram_ok=self._telegram_ok,
            claude_alive=self._process.alive,
        )
        self._s.run_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._s.heartbeat_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({
            "pid": hb.pid, "ts": hb.ts, "ready": hb.ready, "commit": hb.commit,
            "session_id": hb.session_id, "telegram_ok": hb.telegram_ok,
            "claude_alive": hb.claude_alive,
            "healthy": hb.fully_healthy,
        }, ensure_ascii=False))
        tmp.replace(self._s.heartbeat_file)

    async def run_loop(self, on_beat=None) -> None:
        """Вечный цикл пульса. on_beat — что ещё сделать на каждом ударе."""
        while True:
            try:
                await self._deep_check()
                self.beat()
                if on_beat is not None:
                    await on_beat()
            except Exception:  # noqa: BLE001 — пульс не имеет права умереть
                log.exception("сбой в цикле пульса")
            await asyncio.sleep(self._s.heartbeat_interval_s)

    def snapshot(self) -> HealthView:
        beat_age = None
        try:
            data = json.loads(self._s.heartbeat_file.read_text())
            beat_age = time.time() - float(data.get("ts", 0))
        except (OSError, json.JSONDecodeError, ValueError):
            pass
        return HealthView(
            alive=self._process.alive,
            pid=os.getpid(),
            commit=self._current_commit(),
            uptime_s=time.time() - self._started,
            telegram_ok=self._telegram_ok,
            claude_alive=self._process.alive,
            db_ok=self._db_ok,
            last_beat_age_s=beat_age,
        )
