"""Обычный текст — самый частый случай: всё, что не команда, уходит в сессию.

Роутер намеренно последний в цепочке: он ловит вообще любой текст, включая
несуществующие команды вроде /фигня — пусть с ними разбирается claude, а не
шаблонный ответ «неизвестная команда».

Здесь же склейка входящих. Клиент Telegram режет длинное сообщение на части, и
они прилетают отдельными апдейтами; без склейки на каждый кусок был бы свой ход
и свой ответ. Поэтому копим куски в буфер по чату и запускаем ход только после
паузы coalesce_delay_s без новых сообщений — тогда всё уходит одной репликой.
"""
from __future__ import annotations

import asyncio

from aiogram import F, Router
from aiogram.types import Message
from dishka.integrations.aiogram import FromDishka

from ...infrastructure.telegram.sender import MessageSender
from ...services.conversation import ConversationService
from ...settings import Settings
from .common import enqueue

router = Router(name="text")

# Буферы кусков и таймеры дозакрытия — по чату. Живут в модуле: это состояние
# ровно на время склейки, между перезапусками ему смысла нет.
_BUFFERS: dict[int, list[Message]] = {}
_TIMERS: dict[int, asyncio.Task] = {}


async def _flush(chat_id: int, conversation: ConversationService,
                 sender: MessageSender) -> None:
    """Собрать накопленные куски в один ход."""
    msgs = _BUFFERS.pop(chat_id, [])
    _TIMERS.pop(chat_id, None)
    if not msgs:
        return
    text = "\n".join((m.text or "") for m in msgs).strip()
    if not text:
        return
    # Отвечаем на первый кусок — с него человек начал мысль.
    await enqueue(msgs[0], conversation, sender, text)


async def _schedule(chat_id: int, delay: float, conversation: ConversationService,
                    sender: MessageSender) -> None:
    try:
        await asyncio.sleep(delay)
    except asyncio.CancelledError:
        return  # пришёл ещё кусок — этот таймер отменили, флашнет следующий
    await _flush(chat_id, conversation, sender)


async def feed(message: Message, conversation: ConversationService,
               sender: MessageSender, delay: float) -> None:
    """Положить кусок в буфер и перезавести таймер дозакрытия."""
    chat_id = message.chat.id
    _BUFFERS.setdefault(chat_id, []).append(message)
    old = _TIMERS.get(chat_id)
    if old is not None and not old.done():
        old.cancel()
    _TIMERS[chat_id] = asyncio.create_task(_schedule(chat_id, delay, conversation, sender))


@router.message(F.text)
async def on_text(message: Message, conversation: FromDishka[ConversationService],
                  sender: FromDishka[MessageSender],
                  settings: FromDishka[Settings]) -> None:
    await feed(message, conversation, sender, settings.coalesce_delay_s)
