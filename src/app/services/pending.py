"""Ожидание ответа на кнопку: как получить текст там, где кнопки не хватает.

Задача. Половина команд требует произвольного текста: заметка, цель, поиск по
памяти, напоминание, путь к папке. Кнопка текст собрать не может, и обычный
выход — заставить человека набирать /note …, то есть вернуться к командам.

Решение. Кнопка запоминает, чего мы ждём от этого чата, и присылает сообщение с
принудительным ответом (ForceReply). Следующая реплика человека уходит не в
сессию Claude, а в это ожидание. Получается диалог из двух касаний вместо
заученной команды.

Почему в памяти, а не в базе. Ожидание живёт минуты и осмысленно только внутри
текущего экрана. Пережившее перезапуск ожидание хуже отсутствующего: человек
давно забыл, о чём его спрашивали, и его очередная реплика молча пропала бы в
чужом обработчике вместо разговора.

Срок жизни — тот же довод. Нажал «Найти», отвлёкся на час, вернулся и написал
обычное сообщение: оно обязано уйти в сессию, а не в поиск.
"""
from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

TTL_SECONDS = 300


class PendingInput:
    """Что мы ждём от чата: действие и когда его запросили."""

    def __init__(self, ttl: float = TTL_SECONDS) -> None:
        self._ttl = ttl
        self._waiting: dict[int, tuple[str, float]] = {}

    def expect(self, chat_id: int, action: str) -> None:
        self._waiting[chat_id] = (action, time.monotonic())
        log.debug("жду ввод «%s» от чата %s", action, chat_id)

    def take(self, chat_id: int) -> str | None:
        """Забрать ожидание. Второй раз подряд не сработает — это одноразово.

        Одноразовость важна: иначе одно нажатие «Найти» превратило бы в поиск
        всё, что человек напишет дальше.
        """
        item = self._waiting.pop(chat_id, None)
        if item is None:
            return None
        action, asked_at = item
        if time.monotonic() - asked_at > self._ttl:
            log.debug("ожидание «%s» протухло", action)
            return None
        return action

    def cancel(self, chat_id: int) -> bool:
        return self._waiting.pop(chat_id, None) is not None

    @property
    def waiting_count(self) -> int:
        return len(self._waiting)
