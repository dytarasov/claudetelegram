"""Мелочи, общие для роутеров.

Роутер обязан оставаться тонким: собрать DTO, позвать сервис, показать ответ.
Всё, что повторяется в нескольких роутерах, живёт здесь.
"""
from __future__ import annotations

from aiogram.types import Message

from ...domain.dto import TurnRequest
from ...domain.enums import TurnSource
from ...infrastructure.telegram import ui
from ...infrastructure.telegram.sender import MessageSender
from ...services.conversation import ConversationService


async def enqueue(message: Message, conversation: ConversationService,
                  sender: MessageSender, text: str, note: str | None = None,
                  source: TurnSource = TurnSource.TEXT) -> None:
    """Поставить реплику в очередь и, если очередь не пуста, честно сказать об этом."""
    request = TurnRequest(
        chat_id=message.chat.id,
        user_id=message.from_user.id if message.from_user else None,
        text=text,
        reply_to=message.message_id,
        note=note,
        source=source,
    )
    waiting = await conversation.enqueue(request)
    if conversation.busy and waiting:
        # Кнопка рядом с уведомлением: если наставил лишнего в очередь, пока идёт
        # долгий ход, — снять всё ожидающее одним касанием. Активный ход при этом
        # не трогается (для него своя кнопка «Прервать» на живом сообщении).
        await sender.send(
            message.chat.id, f"<i>принято, в очереди: {waiting}</i>",
            reply_to_message_id=message.message_id,
            reply_markup=ui.keyboard([ui.button("Отмена очереди", "ui:queue_clear", ui.DANGER)]),
        )
