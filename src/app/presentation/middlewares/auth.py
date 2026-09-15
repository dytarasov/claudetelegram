"""Пропускать только своих.

Бот работает на сервере с полными правами: любой чужой апдейт — это чужие руки
на машине. Поэтому проверка стоит внешней мидлварью, до всех роутеров и до DI,
и отказ логируется вместе с id, чтобы владельцу было что внести в белый список.
"""
from __future__ import annotations

import contextlib
import logging

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject

from ...services import events

log = logging.getLogger(__name__)


class AuthMiddleware(BaseMiddleware):
    def __init__(self, allowed: set[int]) -> None:
        self._allowed = allowed

    async def __call__(self, handler, event: TelegramObject, data: dict):
        user = data.get("event_from_user")
        if user is None:
            return None
        if user.id not in self._allowed:
            events.access_denied(user.id, user.username)
            message = getattr(event, "message", None) or getattr(event, "edited_message", None)
            if message is not None:
                with contextlib.suppress(Exception):
                    await message.answer(
                        f"Доступа нет. Твой Telegram ID: <code>{user.id}</code>"
                    )
            return None
        return await handler(event, data)
