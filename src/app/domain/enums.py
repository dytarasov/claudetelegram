"""Перечисления домена.

Держим их отдельным файлом, чтобы на них могли ссылаться и модели, и DTO, и
репозитории, не таща друг друга по кругу. Значения — строки: они как есть
ложатся в Postgres и читаются глазами в psql без расшифровки.
"""
from __future__ import annotations

from enum import StrEnum


class DeployPhase(StrEnum):
    """Куда доехала выкатка.

    Единственный источник правды о фазе — таблица deployments; файл
    run/update.json остаётся дублем для сторожа, которому нельзя зависеть от
    живой БД (см. services/selfupdate.py).
    """

    PENDING = "pending"          # запись создана, сторож ещё не перезапускал
    APPLYING = "applying"        # сервис перезапущен, ждём пульса
    SOAKING = "soaking"          # пульс есть, идёт выдержка
    OK = "ok"                    # выжил, метка stable переехала
    ROLLING_BACK = "rolling_back"
    ROLLED_BACK = "rolled_back"  # вернулись на прошлую версию
    FAILED = "failed"            # не поднялось даже после отката — нужен человек


class TurnStatus(StrEnum):
    """Чем закончился ход разговора."""

    OK = "ok"
    INTERRUPTED = "interrupted"  # /stop
    FAILED = "failed"            # исключение внутри хода
    SESSION_DEAD = "session_dead"  # процесс claude умер


class TriggerKind(StrEnum):
    """Кто инициировал выкатку или откат — для журнала и разбора полётов."""

    ASSISTANT = "assistant"  # Claude сам себя допилил
    USER = "user"            # человек нажал /rollback
    GUARD = "guard"          # сторож откатил по таймауту


class GoalStatus(StrEnum):
    """Состояние цели. Разделены «пауза» и «стоп»: первую человек снимет, вторая — конец."""

    ACTIVE = "active"
    PAUSED = "paused"
    DONE = "done"
    FAILED = "failed"     # исчерпан бюджет или бот признал, что застрял
    STOPPED = "stopped"   # снята человеком


class TurnSource(StrEnum):
    """Откуда пришла реплика. Влияет на то, как она подаётся модели.

    Голосовая расшифровка — не то же самое, что напечатанный текст: в ней могут
    быть перевраны термины и имена. Модель должна об этом знать, иначе она
    принимает «клаудикод» за чистую монету вместо того, чтобы понять смысл или
    переспросить.
    """

    TEXT = "text"
    VOICE = "voice"
    FILE = "file"
    AGENT = "agent"   # ход, который бот начал сам, идя к поставленной цели


class ReminderKind(StrEnum):
    """Зачем эта запись существует — от этого зависит, как настойчиво пинать."""

    REMINDER = "reminder"      # просто напомнить в момент времени
    COMMITMENT = "commitment"  # обязательство с дедлайном: пинаем всерьёз
    FOLLOWUP = "followup"      # «мы это не доделали» — бот заводит сам
    DIGEST = "digest"          # регулярная сводка


class ReminderStatus(StrEnum):
    OPEN = "open"
    SNOOZED = "snoozed"
    DONE = "done"
    DROPPED = "dropped"
