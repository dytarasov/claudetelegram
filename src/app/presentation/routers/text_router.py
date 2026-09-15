"""Обычный текст — самый частый случай: всё, что не команда, уходит в сессию.

Роутер намеренно последний в цепочке: он ловит вообще любой текст, включая
несуществующие команды вроде /фигня — пусть с ними разбирается claude, а не
шаблонный ответ «неизвестная команда».
"""
from __future__ import annotations

from aiogram import F, Router
from aiogram.types import Message
from dishka.integrations.aiogram import FromDishka

from ...infrastructure.telegram.sender import MessageSender
from ...services.conversation import ConversationService
from .common import enqueue

router = Router(name="text")


@router.message(F.text)
async def on_text(message: Message, conversation: FromDishka[ConversationService],
                  sender: FromDishka[MessageSender]) -> None:
    await enqueue(message, conversation, sender, message.text)
