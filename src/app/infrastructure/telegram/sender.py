"""Отправка сообщений — единственное место, где мы дёргаем Telegram API.

Здесь же вся защита от его капризов: слишком частые запросы (RetryAfter) и
отказ разобрать разметку (BadRequest). Реализует порт Notifier, поэтому сервисы
могут писать в чат, ничего не зная про aiogram.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import re

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (BufferedInputFile, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, InputRichMessage, Message)

from .formatting import TG_HARD_LIMIT, esc

log = logging.getLogger(__name__)


class MessageSender:
    def __init__(self, bot: Bot) -> None:
        self._bot = bot

    @property
    def bot(self) -> Bot:
        return self._bot

    async def send(self, chat_id: int, html: str, **kw) -> Message | None:
        """Отправить с разметкой, а при её отказе — тем же текстом без тегов.

        Потерять сообщение из-за сломанного тега хуже, чем показать его голым:
        человек всё равно должен увидеть, что произошло.
        """
        try:
            return await self._bot.send_message(chat_id, html, **kw)
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 1)
            return await self.send(chat_id, html, **kw)
        except TelegramBadRequest as exc:
            log.warning("отправка с разметкой не прошла (%s), шлю без неё", exc)
            plain = re.sub(r"<[^>]+>", "", html)[:TG_HARD_LIMIT]
            with contextlib.suppress(TelegramBadRequest):
                return await self._bot.send_message(chat_id, plain, parse_mode=None, **kw)
        return None

    async def edit(self, chat_id: int, message_id: int, html: str,
                   reply_markup: InlineKeyboardMarkup | None = None) -> bool:
        """Правка сообщения. reply_markup передаём и при повторных правках:
        Telegram при правке текста без разметки СНИМАЕТ клавиатуру, поэтому
        живая кнопка (например «Прервать») должна переприкладываться каждый раз,
        а финальная правка без неё её же и убирает."""
        try:
            await self._bot.edit_message_text(html, chat_id=chat_id, message_id=message_id,
                                              reply_markup=reply_markup)
            return True
        except TelegramBadRequest as exc:
            if "message is not modified" not in str(exc):
                log.debug("правка сообщения не прошла: %s", exc)
            return False

    async def draft(self, chat_id: int, draft_id: int, html: str) -> bool:
        """Нативный стрим партиала (Bot API 9.5, sendMessageDraft).

        Драфт эфемерный — живёт 30 секунд и не является сообщением; правки с
        тем же draft_id Telegram анимирует сам, без фликера и без лимитов на
        edit. В конце хода обязателен обычный send с полным текстом, иначе в
        чате ничего не останется — это делает finish() во вьюхе.

        Возвращает False, если метод не прошёл (старый сервер, кривой тег): по
        нему вьюха откатывается на правки. Терять из-за этого ход нельзя,
        поэтому на кривой разметке шлём драфт без тегов.
        """
        try:
            await self._bot.send_message_draft(
                chat_id=chat_id, draft_id=draft_id, text=html[:TG_HARD_LIMIT])
            return True
        except TelegramRetryAfter as exc:
            # Драфты почти не лимитируются, но если прилетело — не спим в потоке.
            log.debug("draft rate-limited: retry_after=%s", exc.retry_after)
            return True
        except TelegramBadRequest as exc:
            plain = re.sub(r"<[^>]+>", "", html)[:TG_HARD_LIMIT]
            try:
                await self._bot.send_message_draft(
                    chat_id=chat_id, draft_id=draft_id, text=plain, parse_mode=None)
                return True
            except TelegramBadRequest:
                log.debug("sendMessageDraft не поддержан/отклонён: %s", exc)
                return False
        except Exception as exc:  # noqa: BLE001 — метода может не быть на сервере
            log.debug("sendMessageDraft недоступен: %s", exc)
            return False

    async def draft_rich(self, chat_id: int, draft_id: int, html: str) -> bool:
        """Стрим партиала как rich-сообщения (Bot API 9.x, sendRichMessageDraft).

        В отличие от draft(), который кладёт черновик в строку ввода (и мак
        перерисовывает его целиком → мигание), rich-драфт рисуется пузырём
        прямо в ленте — расчёт на то, что клиент анимирует это плавно, без
        фликера. Механика та же: эфемерно, тот же draft_id анимируется,
        финал обязан уйти обычным сообщением (это делает finish()).

        False — метод не прошёл (старый сервер, кривой тег): вьюха уходит на
        правки. На кривой разметке пробуем без тегов, чтобы не терять ход.
        """
        try:
            await self._bot.send_rich_message_draft(
                chat_id=chat_id, draft_id=draft_id,
                rich_message=InputRichMessage(html=html[:TG_HARD_LIMIT]))
            return True
        except TelegramRetryAfter as exc:
            log.debug("rich draft rate-limited: retry_after=%s", exc.retry_after)
            return True
        except TelegramBadRequest as exc:
            plain = re.sub(r"<[^>]+>", "", html)[:TG_HARD_LIMIT]
            try:
                await self._bot.send_rich_message_draft(
                    chat_id=chat_id, draft_id=draft_id,
                    rich_message=InputRichMessage(html=plain, skip_entity_detection=True))
                return True
            except TelegramBadRequest:
                log.debug("sendRichMessageDraft отклонён: %s", exc)
                return False
        except Exception as exc:  # noqa: BLE001 — метода может не быть на сервере
            log.debug("sendRichMessageDraft недоступен: %s", exc)
            return False

    async def send_error(self, chat_id: int, title: str, detail: str) -> None:
        """Сообщение о сбое: заголовок жирным, подробности моноширинно.

        Экранирование живёт здесь, а не у вызывающего: сервис не обязан знать,
        что текст поедет в HTML-разметке.
        """
        await self.send(chat_id, f"{title}\n<code>{esc(detail)}</code>")

    async def typing(self, chat_id: int) -> None:
        with contextlib.suppress(Exception):
            await self._bot.send_chat_action(chat_id, "typing")

    async def alive(self) -> bool:
        """Отвечает ли Telegram нам лично (а не «есть ли интернет»)."""
        try:
            await self._bot.get_me()
            return True
        except Exception:  # noqa: BLE001
            return False

    async def send_actions(self, chat_id: int, html: str,
                           actions: list[tuple[str, str]]) -> Message | None:
        """Сообщение с кнопками под ним.

        Нужно всему, что бот присылает сам: у человека должен быть способ
        закрыть напоминание одним касанием, не набирая команду. Кнопки идут по
        две в ряд — так они читаемы и на телефоне, и не занимают пол-экрана.
        """
        def make(action) -> InlineKeyboardButton:
            label, data, *rest = action
            return InlineKeyboardButton(text=label, callback_data=data,
                                        style=rest[0] if rest else "default")

        rows = [[make(a) for a in actions[i:i + 2]] for i in range(0, len(actions), 2)]
        return await self.send(chat_id, html,
                               reply_markup=InlineKeyboardMarkup(inline_keyboard=rows))

    async def send_text_file(self, chat_id: int, text: str, filename: str) -> None:
        await self._bot.send_document(
            chat_id, BufferedInputFile(text.encode(), filename=filename)
        )

    async def send_file(self, chat_id: int, path) -> None:
        await self._bot.send_document(chat_id, FSInputFile(path))
