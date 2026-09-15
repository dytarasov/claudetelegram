"""Стриминг ответа: режимы edit | draft | rich с откатом на правки.

Проверяется поведение вьюхи, а не Telegram: что партиал уходит нужным методом
(обычный draft или rich-draft), что живое сообщение при этом не плодится, и что
отказ метода на старом сервере не роняет ход, а бесшовно переключает на правки.
Плюс главное правило драфта: он эфемерный, поэтому финал обязан уйти обычным
сообщением.
"""
from __future__ import annotations

import pytest

from app.infrastructure.telegram.views import TurnView
from app.settings import Settings


def _settings(**kw) -> Settings:
    return Settings(_env_file=None, telegram_bot_token="t", allowed_user_ids="1",
                    database_dsn="d", **kw)


class FakeBot:
    def __init__(self):
        self.sent: list[str] = []
        self.drafts: list[tuple[int, str]] = []
        self.rich_drafts: list[tuple[int, str]] = []
        self.draft_should_fail = False

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)
        class _M:  # aiogram Message достаточно замокать до message_id
            message_id = 42
        return _M()

    async def send_message_draft(self, chat_id, draft_id, text, **kw):
        if self.draft_should_fail:
            raise RuntimeError("метода нет на сервере")
        self.drafts.append((draft_id, text))

    async def send_rich_message_draft(self, chat_id, draft_id, rich_message, **kw):
        if self.draft_should_fail:
            raise RuntimeError("метода нет на сервере")
        self.rich_drafts.append((draft_id, rich_message.html))


class FakeSender:
    """Обёртка над FakeBot с настоящей логикой draft()/edit()/send()."""

    def __init__(self, bot: FakeBot):
        self.bot = bot
        self.edits: list[str] = []
        self.last_markup = None

    async def draft(self, chat_id, draft_id, html) -> bool:
        try:
            await self.bot.send_message_draft(chat_id=chat_id, draft_id=draft_id, text=html)
            return True
        except Exception:
            return False

    async def draft_rich(self, chat_id, draft_id, html) -> bool:
        from aiogram.types import InputRichMessage
        try:
            await self.bot.send_rich_message_draft(
                chat_id=chat_id, draft_id=draft_id,
                rich_message=InputRichMessage(html=html))
            return True
        except Exception:
            return False

    async def edit(self, chat_id, message_id, html, reply_markup=None) -> bool:
        self.edits.append(html)
        self.last_markup = reply_markup
        return True

    async def send(self, chat_id, html, **kw):
        return await self.bot.send_message(chat_id, html, **kw)

    async def send_text_file(self, chat_id, text, filename):
        pass


def _view(sender, **skw) -> TurnView:
    return TurnView(chat_id=1, sender=sender, settings=_settings(**skw),
                    context_usage=lambda: (0, 200000), reply_to=None)


async def test_partial_goes_out_as_draft_not_as_a_message():
    bot = FakeBot(); sender = FakeSender(bot)
    view = _view(sender, stream_mode="draft")
    view.live_text = "начал отвечать"
    await view.refresh(force=True)

    assert bot.drafts, "партиал должен был уйти драфтом"
    assert bot.sent == [], "живое сообщение плодиться не должно — только драфт"
    assert view.msg is None
    assert view.draft_id != 0


async def test_draft_id_is_stable_across_updates():
    """Один draft_id весь ход — иначе Telegram не анимирует, а перерисовывает."""
    bot = FakeBot(); sender = FakeSender(bot)
    view = _view(sender, stream_mode="draft")
    for i in range(3):
        view.live_text = f"кусок {i}"
        await view.refresh(force=True)
    ids = {d[0] for d in bot.drafts}
    assert len(ids) == 1, f"draft_id прыгал: {ids}"


async def test_fallback_to_edits_when_draft_unsupported():
    """Старый сервер без метода — ход не падает, идём правками."""
    bot = FakeBot(); bot.draft_should_fail = True
    sender = FakeSender(bot)
    view = _view(sender, stream_mode="draft")
    view.live_text = "первый партиал"
    await view.refresh(force=True)

    assert view._mode == "edit", "после отказа драфта режим обязан упасть в edit"
    assert bot.sent, "должно было уйти обычным сообщением-правкой"
    assert view.msg is not None


async def test_draft_disabled_by_flag_uses_edits_directly():
    bot = FakeBot(); sender = FakeSender(bot)
    view = _view(sender, stream_mode="edit")
    view.live_text = "текст"
    await view.refresh(force=True)
    assert bot.drafts == []
    assert bot.sent, "при выключенном драфте сразу правки"


async def test_final_message_is_persisted_after_draft_stream():
    """Драфт эфемерный: без финального send в чате не останется ничего."""
    bot = FakeBot(); sender = FakeSender(bot)
    view = _view(sender, stream_mode="draft")
    view.segments.append(type(view.segments[0]) if view.segments else None)  # noqa
    view.segments.clear()
    from app.infrastructure.telegram.views import Segment
    view.segments.append(Segment("text", text="итоговый ответ"))
    await view.finish({"total_cost_usd": 0.01})

    assert any("итоговый ответ" in m for m in bot.sent), "финал не персистнулся"


async def test_rich_mode_streams_via_rich_draft():
    """Режим rich шлёт партиал через sendRichMessageDraft, не через обычный draft."""
    bot = FakeBot(); sender = FakeSender(bot)
    view = _view(sender, stream_mode="rich")
    view.live_text = "партиал"
    await view.refresh(force=True)

    assert bot.rich_drafts, "партиал должен был уйти rich-драфтом"
    assert bot.drafts == [], "обычный draft в rich-режиме не используется"
    assert bot.sent == [], "живое сообщение плодиться не должно"


async def test_rich_mode_falls_back_to_edits_when_unsupported():
    """Старый сервер без rich-метода — ход не падает, идём правками."""
    bot = FakeBot(); bot.draft_should_fail = True
    sender = FakeSender(bot)
    view = _view(sender, stream_mode="rich")
    view.live_text = "партиал"
    await view.refresh(force=True)

    assert view._mode == "edit"
    assert bot.sent, "должно было уйти обычным сообщением-правкой"


async def test_live_message_carries_cancel_button():
    """На живом сообщении хода висит кнопка «Прервать» (колбэк ui:stop)."""
    bot = FakeBot(); sender = FakeSender(bot)
    view = _view(sender, stream_mode="edit")
    view.live_text = "первый кусок"
    await view.refresh(force=True)   # создаёт живое сообщение
    view.live_text = "второй кусок"
    await view.refresh(force=True)   # правка — сюда прилетает клавиатура

    markup = sender.last_markup
    assert markup is not None, "на живом сообщении должна быть кнопка отмены"
    data = [b.callback_data for row in markup.inline_keyboard for b in row]
    assert "ui:stop" in data
