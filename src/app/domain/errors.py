"""Ошибки домена.

Свои типы нужны, чтобы presentation мог отвечать человеку по-разному на «ты
попросил ерунду» и «у меня всё сломалось», не разбирая текст исключения.
"""
from __future__ import annotations


class AppError(Exception):
    """Корень всех наших ошибок — по нему ловим в мидлвари."""


class UserError(AppError):
    """Виноват ввод: не та версия, не тот путь. Показываем человеку как есть."""


class SessionDead(AppError):
    """Процесс claude умер посреди хода. Лечится перезапуском процесса."""


class DeployBlocked(AppError):
    """Выкатку нельзя начинать: preflight не прошёл или другая уже едет."""


class InfrastructureError(AppError):
    """Внешняя система (git, systemd, Postgres) ответила не так, как обещала."""
